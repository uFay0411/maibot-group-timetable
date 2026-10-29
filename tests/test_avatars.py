"""头像使用真实路由、安全下载和昵称标识；不猜测OpenID。"""

from io import BytesIO
from unittest.mock import AsyncMock

from PIL import Image

import pytest

from timetable_test_plugin import avatars as module
from timetable_test_plugin.avatars import Avatars, download_avatar, thumbnail


@pytest.mark.parametrize('url',['http://qlogo.cn/a','https://localhost/a','https://qlogo.cn.evil.test/a',
                                'https://127.0.0.1/a','https://user:pass@qlogo.cn/a','https://qlogo.cn:444/a'])
async def test_avatar_rejects_non_cdn_or_unsafe_url(url):
    with pytest.raises(ValueError):
        await download_avatar(url)


async def test_avatar_rejects_private_dns(monkeypatch):
    monkeypatch.setattr(module.socket,'getaddrinfo',lambda *a,**kw: [(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError, match='公网'):
        await download_avatar('https://qlogo.cn/a')


async def test_avatar_route_cache_and_unsupported_do_not_guess_ids(plugin, monkeypatch):
    cache=Avatars()
    source={'platform':'qq','account_id':'botA','scope':'connectionA'}
    result={'lessons':[{'user_id':'A'*32},{'user_id':'A'*32}]}
    assert await cache.collect(plugin.ctx,result,source)=={}
    plugin.ctx.chat.get_avatar.assert_awaited_once_with('qq','A'*32,account_id='botA',scope='connectionA')
    assert await cache.collect(plugin.ctx,result,source)=={}
    assert plugin.ctx.chat.get_avatar.await_count==1
    source['account_id']='botB'
    await cache.collect(plugin.ctx,result,source)
    assert plugin.ctx.chat.get_avatar.await_count==2
    cache.clear()
    assert not cache.cache


async def test_avatar_reads_and_caches_only_resized_image(plugin,monkeypatch):
    raw=BytesIO()
    Image.new('RGB',(300,180),'red').save(raw,'PNG')
    plugin.ctx.chat.get_avatar.return_value={'status':'available','url':'https://qlogo.cn/a'}
    downloader=AsyncMock(return_value=thumbnail(raw.getvalue()))
    monkeypatch.setattr(module,'download_avatar',downloader)
    cache=Avatars()
    result=await cache.collect(plugin.ctx,{'lessons':[{'user_id':'100'}]},{'platform':'qq'})
    with Image.open(BytesIO(result['100'])) as image:
        assert image.size==(132,132)
    await cache.collect(plugin.ctx,{'lessons':[{'user_id':'100'}]},{'platform':'qq'})
    assert downloader.await_count==1


async def test_avatar_timeout_does_not_abort_board(plugin):
    plugin.ctx.chat.get_avatar.side_effect=TimeoutError('private signed URL must not be logged')
    result=await Avatars().collect(plugin.ctx,{'lessons':[{'user_id':'100'}]},{'platform':'qq'})
    assert result=={}


def test_avatar_thumbnail_rejects_oversized_pixels():
    raw=BytesIO()
    Image.new('RGB',(2500,2000),'white').save(raw,'PNG')
    with pytest.raises(ValueError,match='像素'):
        thumbnail(raw.getvalue())
