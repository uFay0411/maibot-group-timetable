"""今日看板状态、时间边界、分页和真实绘制内容。"""

from io import BytesIO

from PIL import Image, ImageDraw

from timetable_test_plugin.presentation import render_group_current
from test_group_current import register, collect
from test_group_current import active_group as active_group
from test_group_images import group as group
from test_query import natural_tool as natural_tool
from test_timetable import dt, ics


def test_board_includes_next_finished_free_but_not_unregistered(store):
    for uid, start, end, title in [
        ('100','20261026T080000','20261026T094000','进行中的课'),
        ('200','20261026T100000','20261026T114000','即将上的课'),
        ('300','20261026T070000','20261026T080000','上完的课'),
        ('400','20261027T080000','20261027T094000','明天的课')]:
        store.import_ics(register(store, uid), ics(uid=uid, summary=title, start=start, end=end), '合并')
    register(store, '500')
    result = collect(store, ids=('100','200','300','400','500'))
    assert [x['status'] for x in result['lessons']] == ['in_class','next','finished','free']
    assert result['members_with_timetable'] == 4
    assert result['lessons'][0]['remaining_minutes'] == 40
    assert result['lessons'][1]['starts_in_minutes'] == 60
    assert result['lessons'][2]['completed_minutes'] == 60
    assert result['lessons'][2]['subject'] is None
    assert result['members_free_today'] == 1
    assert '明天的课' not in result['content']


def test_next_class_earliest_ties_all_retained_and_start_sorted(store):
    owner = register(store)
    for uid, start, end in [('later','20261026T110000','20261026T120000'),
                            ('one','20261026T100000','20261026T114000'),
                            ('two','20261026T100000','20261026T110000')]:
        store.import_ics(owner, ics(uid=uid, summary=uid, start=start, end=end), '合并')
    store.import_ics(register(store,'200'), ics(uid='bob', start='20261026T093000', end='20261026T094000'), '合并')
    result = collect(store)
    assert [x['user_id'] for x in result['lessons']] == ['200','100','100']
    assert result['members_with_next'] == 2 and result['members_in_class'] == 0
    assert all(x['conflicting_lessons'] for x in result['lessons'][1:])
    assert 'later' not in result['content']


def test_finished_duration_unions_conflicts_and_clips_overnight(store):
    owner = register(store)
    for uid, start, end in [('night','20261025T230000','20261026T010000'),
                            ('a','20261026T070000','20261026T083000'),
                            ('b','20261026T080000','20261026T090000')]:
        store.import_ics(owner, ics(uid=uid, start=start, end=end), '合并')
    row = collect(store)['lessons'][0]
    assert row['status'] == 'finished' and row['completed_minutes'] == 180
    assert row['completed_lessons'] == 3


def test_midnight_excludes_tomorrow_next_and_rounds_countdown_up(store):
    owner = register(store)
    store.import_ics(owner, ics(start='20261027T000000', end='20261027T010000'), '合并')
    assert collect(store, now=dt(clock='23:59:30'))['lessons'][0]['status'] == 'free'
    store.import_ics(owner, ics(uid='last', start='20261026T235950', end='20261027T001000'), '合并')
    row = collect(store, now=dt(clock='23:59:30'))['lessons'][0]
    assert row['status'] == 'next' and row['starts_in_minutes'] == 1
    assert collect(store, now=dt('2026-10-27', '00:00'))['members_in_class'] == 1


def test_board_draws_status_room_countdown_and_paginates(store, monkeypatch):
    ids = tuple(str(100+i) for i in range(11))
    for uid in ids:
        store.import_ics(register(store,uid), ics(uid=uid, room='教一101'), '合并')
    result = collect(store, ids=ids)
    drawn=[]
    original=ImageDraw.ImageDraw.text
    def recording(draw, xy, text, *args, **kwargs):
        drawn.append(text)
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',recording)
    pages=render_group_current(result)
    assert len(pages)==2
    assert drawn.count('进行中')==11
    assert any('剩余 40分钟' in x and '教一101' in x for x in drawn)
    assert any('图片 2/2' in x for x in drawn)
    for raw in pages:
        with Image.open(BytesIO(raw)) as image:
            assert image.width==1080 and image.height<=2200


async def test_command_defaults_to_image_and_rejects_conflicting_format(active_group):
    p, _, _, _, _, source=active_group
    source['raw_message']=[{'type':'text','data':'/课表 群内当前'}]
    await p.command(message=source)
    assert p.ctx.send.hybrid.await_count==1 and not p.ctx.send.text.called
    source['raw_message'][0]['data']='/课表 群内当前 图片 文字'
    await p.command(message=source)
    assert p.ctx.send.hybrid.await_count==1
    assert '用法' in p.ctx.send.text.call_args.args[0]
