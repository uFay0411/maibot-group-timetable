"""只从宿主公开头像接口读取当前路由；头像失败不影响真实课程看板。"""

from io import BytesIO
from time import monotonic
from urllib.parse import urlsplit

import asyncio
import ipaddress
import socket

import httpx
from PIL import Image, ImageOps

MAX_BYTES = 512 * 1024


def thumbnail(raw: bytes) -> bytes:
    with Image.open(BytesIO(raw)) as image:
        if image.width * image.height > 4_000_000:
            raise ValueError("头像像素过多")
        result = ImageOps.fit(image.convert("RGB"), (132, 132))
        out = BytesIO()
        result.save(out, "PNG")
        return out.getvalue()


async def download_avatar(url: str) -> bytes:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    # 只接受QQ头像CDN，不把平台OpenID猜成QQ号，也不获取任意用户输入URL。
    if (parts.scheme != "https" or parts.username or parts.password or parts.port not in {None, 443}
            or not any(host == domain or host.endswith("." + domain) for domain in ("qlogo.cn", "qpic.cn"))):
        raise ValueError("头像地址不是支持的QQ头像CDN")
    addresses = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("头像地址不是公网地址")
    async with httpx.AsyncClient(timeout=2, follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError("头像地址不允许重定向")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise ValueError("头像文件过大")
    return await asyncio.to_thread(thumbnail, bytes(raw))


class Avatars:
    def __init__(self):
        self.cache = {}

    def clear(self):
        self.cache.clear()

    async def collect(self, ctx, result: dict, source: dict) -> dict[str, bytes]:
        platform = source["platform"]
        account, scope = str(source.get("account_id") or ""), str(source.get("scope") or "")
        self.cache = {key: value for key, value in self.cache.items() if value[0] > monotonic()}
        images, limit = {}, asyncio.Semaphore(4)

        async def one(uid):
            key = (platform, account, scope, uid)
            if key in self.cache:
                if self.cache[key][1]:
                    images[uid] = self.cache[key][1]
                return
            async with limit:
                try:
                    info = await asyncio.wait_for(ctx.chat.get_avatar(platform, uid, account_id=account, scope=scope), 2.5)
                    raw = await download_avatar(info["url"]) if info.get("status") == "available" and info.get("url") else None
                except Exception as exc:
                    # 可选头像失败只记类型，避免把签名URL、身份或凭据写入日志。
                    ctx.logger.debug("课程看板头像不可用: %s", type(exc).__name__)
                    raw = None
                if len(self.cache) >= 128:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = (monotonic() + (600 if raw else 60), raw)
                if raw:
                    images[uid] = raw

        tasks = [asyncio.create_task(one(uid)) for uid in {row["user_id"] for row in result["lessons"]}]
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=6)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return images
