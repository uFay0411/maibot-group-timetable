from io import BytesIO

from PIL import Image, ImageDraw

import base64

import pytest

from timetable_test_plugin.group_upcoming import group_upcoming
from timetable_test_plugin.calendar_engine import stamp
from timetable_test_plugin.presentation import render_group_current

from test_group_current import active_group as active_group, register
from test_group_images import group as group
from test_query import natural_tool as natural_tool
from test_timetable import dt, ics, lessons


def collect(store, **kwargs):
    return group_upcoming(
        store,
        "qq",
        "123",
        "group-stream",
        [{"user_id": "100", "name": "Alice"}, {"user_id": "200", "name": "Bob"}],
        "Asia/Shanghai",
        dt(clock="09:00"),
        **kwargs,
    )


def test_next_per_member_excludes_current_and_sorts_start_time(store):
    alice, bob = register(store), register(store, "200")
    store.import_ics(alice, ics(uid="current", summary="正在上课"), "合并")
    store.import_ics(alice, ics(uid="late", summary="下午物理", start="20261026T140000", end="20261026T154000"), "合并")
    store.import_ics(bob, ics(uid="early", summary="上午英语", start="20261026T100000", end="20261026T114000"), "合并")
    store.import_ics(bob, ics(uid="later", summary="后续课程", start="20261027T100000", end="20261027T114000"), "合并")
    result = collect(store)
    assert [x["subject"] for x in result["lessons"]] == ["上午英语", "下午物理"]
    assert result["members_with_next"] == 2 and result["sort_by"] == "start_ascending"
    assert result["members_with_timetable"] == 2


def test_next_reschedule_cancel_scope_and_reminders_off(store):
    alice, bob = register(store), register(store, "200")
    store.import_ics(alice, ics(uid="alice", start="20261027T080000", end="20261027T094000"), "合并")
    store.import_ics(bob, ics(uid="bob", start="20261026T100000", end="20261026T114000"), "合并")
    store.setting(alice, "enabled", 0)
    original = lessons(store, alice, "2026-10-27")[0]
    store.change(alice, original, {"start": stamp(dt(clock="09:30")), "end": stamp(dt(clock="10:30"))})
    store.change(bob, lessons(store, bob)[0], {"cancelled": True})
    # 后一周在未来7天窗口外，Bob应列为范围内无后续课。
    result = collect(store, period="今天")
    assert [x["user_id"] for x in result["lessons"]] == ["100"]
    assert result["members_without_upcoming_count"] == 1
    store.import_ics(register(store, "300", "999"), ics(uid="other"), "合并")
    assert collect(store, period="今天")["members_with_timetable"] == 2


def test_start_boundary_conflicts_and_member_timezones(store):
    alice = register(store, zone="Asia/Tokyo")
    for uid in ["one", "two"]:
        store.import_ics(alice, ics(uid=uid, tz="", start="20261026T100100", end="20261026T114000"), "合并")
    result = collect(store, period="今天")
    assert result["total"] == 2 and all(x["conflicting_lessons"] for x in result["lessons"])
    assert result["lessons"][0]["start"].startswith("2026-10-26T09:01")


def test_exactly_started_class_is_current_and_not_upcoming(store):
    owner = register(store)
    store.import_ics(owner, ics(start="20261026T090000", end="20261026T104000"), "合并")
    assert collect(store)["total"] == 0


async def test_natural_default_image_and_command_text(active_group):
    p, context, _, _, _, source = active_group
    result = await p.query_group_upcoming_courses(message_id="question-123", **context)
    assert result["success"] and result["query_type"] == "group_upcoming" and result["image_sent"]
    data = base64.b64decode(p.ctx.send.hybrid.call_args.args[0][0]["content"])
    with Image.open(BytesIO(data)) as image:
        assert image.width == 1080
    source["raw_message"] = [{"type": "text", "data": "/课表 群友接下来 明天 文字"}]
    await p.command(message=source)
    assert "接下来课程" in p.ctx.send.text.call_args.args[0]


def test_upcoming_image_draws_start_order_and_empty_coverage(store, monkeypatch):
    owner = register(store)
    store.import_ics(
        owner, ics(uid="first", summary="先上英语", start="20261026T100000", end="20261026T114000"), "合并"
    )
    bob = register(store, "200")
    store.import_ics(bob, ics(uid="second", summary="后上物理", start="20261026T140000", end="20261026T154000"), "合并")
    seen = []
    original = ImageDraw.ImageDraw.text

    def draw(self, xy, text, *args, **kwargs):
        if text in {"先上英语", "后上物理"}:
            seen.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", draw)
    pages = render_group_current(collect(store))
    assert seen == ["先上英语", "后上物理"]
    with Image.open(BytesIO(pages[0])) as image:
        assert image.height <= 2100


@pytest.mark.parametrize("page,days", [(0, 7), (True, 7), (1, 32), (1, 0)])
def test_invalid_parameters(store, page, days):
    with pytest.raises(ValueError):
        collect(store, page=page, days=days)
