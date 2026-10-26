"""当前上课的时刻边界、群隔离、排序与命令/工具/PNG集成。"""

from io import BytesIO
from datetime import datetime

from PIL import Image, ImageDraw

import base64
import json

import pytest

import timetable_test_plugin as entry
from timetable_test_plugin import group_current as current_module
from timetable_test_plugin.group_current import format_group_current, group_current
from timetable_test_plugin.presentation import render_group_current
from timetable_test_plugin.calendar_engine import stamp

from test_group_images import group as group
from test_query import natural_tool as natural_tool
from test_timetable import dt, ics, lessons


def register(store, uid="100", group_id="123", stream="group-stream", platform="qq", zone="Asia/Shanghai"):
    owner = json.dumps([platform, group_id, uid], separators=(",", ":"))
    store.member(owner, stream, uid, "旧名" + uid, zone)
    return owner


def collect(store, ids=("100", "200"), now=None, page=1):
    return group_current(store, "qq", "123", "group-stream", [{"user_id": uid, "name": "群友" + uid} for uid in ids],
                         "Asia/Shanghai", now or dt(clock="09:00"), page)


def test_current_end_sort_differs_from_start_sort_and_excludes_not_active(store):
    first, second = register(store), register(store, "200")
    store.import_ics(first, ics(summary="后下课", end="20261026T100000"), "合并")
    store.import_ics(second, ics(uid="early", summary="先下课", start="20261026T085000", end="20261026T092000"), "合并")
    store.import_ics(first, ics(uid="future", summary="还没上课", start="20261026T100000", end="20261026T110000"), "合并")
    store.import_ics(second, ics(uid="past", summary="已下课", start="20261026T070000", end="20261026T090000"), "合并")
    result = collect(store)
    assert [x["subject"] for x in result["lessons"]] == ["先下课", "后下课"]
    assert result["members_in_class"] == 2 and result["sort_by"] == "end_ascending"
    data = json.loads(result["content"].split("\n", 1)[1].split("\n回复要求：", 1)[0])
    assert data["lessons"][0]["member_name"] == "群友200"


@pytest.mark.parametrize("clock,expected", [("07:59", 0), ("08:00", 1), ("09:39", 1), ("09:40", 0)])
def test_exact_current_class_boundaries(store, clock, expected):
    store.import_ics(register(store), ics(), "合并")
    assert collect(store, now=dt(clock=clock))["total"] == expected


def test_overnight_and_long_events_are_not_lost_to_day_or_24_hour_cutoff(store):
    owner = register(store)
    store.import_ics(owner, ics(uid="overnight", start="20261025T230000", end="20261026T010000"), "合并")
    store.import_ics(owner, ics(uid="long", start="20261023T080000", end="20261026T100000", summary="持续实践课程"), "合并")
    midnight = collect(store, now=dt(clock="00:30"))
    assert midnight["total"] == 2
    morning = collect(store)
    assert morning["total"] == 1 and morning["lessons"][0]["subject"] == "持续实践课程"


def test_rescheduled_into_current_window_uses_new_end_time_and_room(store):
    owner = register(store)
    store.import_ics(owner, ics(start="20261027T080000", end="20261027T094000"), "合并")
    origin = lessons(store, owner, "2026-10-27")[0]
    store.change(owner, origin, {"start": stamp(dt(clock="08:30")), "end": stamp(dt(clock="09:30")), "room": "新教室"})
    result = collect(store)
    assert result["total"] == 1 and result["lessons"][0]["classroom"] == "新教室"
    assert result["lessons"][0]["end"].startswith("2026-10-26T09:30")
    store.change(owner, origin, {"start": stamp(dt("2026-10-28")), "end": stamp(dt("2026-10-28", "09:40"))})
    assert collect(store)["total"] == 0


def test_cancel_deleted_course_and_reminders_off_have_distinct_meanings(store):
    owner = register(store)
    store.import_ics(owner, ics(), "合并")
    store.setting(owner, "enabled", 0)
    assert collect(store)["total"] == 1
    lesson = lessons(store, owner)[0]
    store.change(owner, lesson, {"cancelled": True})
    assert collect(store)["total"] == 0
    store.restore(owner, lesson.course_id)
    store.patch(owner, lesson.course_id, {"deleted": True})
    assert collect(store)["total"] == 0


def test_only_live_same_group_and_stream_members_with_tables_are_counted(store):
    for uid, gid, stream, platform in [("100", "123", "group-stream", "qq"),
                                       ("200", "999", "group-stream", "qq"),
                                       ("300", "123", "another-stream", "qq"),
                                       ("400", "123", "group-stream", "telegram"),
                                       ("500", "123", "group-stream", "qq")]:
        store.import_ics(register(store, uid, gid, stream, platform), ics(uid=uid), "合并")
    register(store, "600")  # 只有成员记录，没有课程。
    result = collect(store, ids=("100", "200", "300", "400", "600"))
    assert [x["user_id"] for x in result["lessons"]] == ["100"]
    assert result["members_with_timetable"] == 1 and result["group_member_count"] == 5


def test_mixed_member_timezones_sort_by_real_end_and_display_one_timezone(store):
    tokyo = register(store, "100", zone="Asia/Tokyo")
    shanghai = register(store, "200")
    store.import_ics(tokyo, ics(tz="", start="20261026T090000", end="20261026T104000"), "合并")
    store.import_ics(shanghai, ics(uid="sh", end="20261026T093000"), "合并")
    result = collect(store)
    assert [x["user_id"] for x in result["lessons"]] == ["200", "100"]
    assert [x["end"][11:16] for x in result["lessons"]] == ["09:30", "09:40"]
    assert all(x["end"].endswith("+08:00") for x in result["lessons"])


def test_conflicting_lessons_are_all_kept_and_pages_remain_end_sorted(store, monkeypatch):
    owner = register(store)
    store.import_ics(owner, ics(uid="one", summary="课程一", end="20261026T091000"), "合并")
    store.import_ics(owner, ics(uid="two", summary="课程二", end="20261026T092000"), "合并")
    monkeypatch.setattr(current_module, "PAGE_SIZE", 1)
    first, second = collect(store), collect(store, page=2)
    assert first["total"] == 2 and first["members_in_class"] == 1 and first["next_page"] == 2
    assert first["lessons"][0]["subject"] == "课程一" and second["lessons"][0]["subject"] == "课程二"
    assert first["lessons"][0]["conflicting_lessons"] and not second["has_more"]
    with pytest.raises(ValueError):
        collect(store, page=3)


@pytest.mark.parametrize("page", [0, -1, True, "1"])
def test_invalid_pages_rejected(store, page):
    with pytest.raises(ValueError):
        collect(store, page=page)


@pytest.fixture
def active_group(group, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return dt(clock="09:00").astimezone(tz)
    monkeypatch.setattr(entry, "datetime", FixedDatetime)
    return group


async def test_natural_group_tool_uses_one_roster_call_and_real_sorted_data(active_group):
    p, context, actor, bob, members, _ = active_group
    members["100"]["role"] = "member"
    lesson = lessons(p.store, bob)[0]
    p.store.change(bob, lesson, {"end": stamp(dt(clock="09:20"))})
    result = await p.query_group_current_courses(message_id="question-123", **context)
    assert result["success"] and [x["user_id"] for x in result["lessons"]] == ["200", "100"]
    assert p.ctx.api.call.await_count == 1 and not p.ctx.send.text.called and not p.ctx.send.hybrid.called
    assert lessons(p.store, actor)[0].title == "高等数学"


async def test_current_command_text_and_png_are_supported(active_group):
    p, _, _, _, _, source = active_group
    source["raw_message"] = [{"type": "text", "data": "/课表 群内当前"}]
    await p.command(message=source)
    assert "下课" in p.ctx.send.text.call_args.args[0] and "大学英语" in p.ctx.send.text.call_args.args[0]
    source["raw_message"] = [{"type": "text", "data": "/课表 群内 当前 图片"}]
    await p.command(message=source)
    data = base64.b64decode(p.ctx.send.hybrid.call_args.args[0][0]["content"])
    with Image.open(BytesIO(data)) as image:
        assert image.format == "PNG" and image.width == 1080


async def test_actor_must_still_belong_to_group_and_list_api_must_work(active_group):
    p, context, _, _, members, _ = active_group
    members.pop("100")
    failed = await p.query_group_current_courses(message_id="question-123", **context)
    assert not failed["success"] and "lessons" not in failed
    p.ctx.api.list.return_value = []
    failed = await p.query_group_current_courses(message_id="question-123", **context)
    assert not failed["success"]


async def test_tool_requires_question_identity_and_respects_enabled(active_group):
    p, context, _, _, _, _ = active_group
    assert not (await p.query_group_current_courses(**context))["success"]
    p.config.plugin.enabled = False
    result = await p.query_group_current_courses(message_id="question-123", **context)
    assert not result["success"] and not p.ctx.api.call.called


def test_group_image_preserves_end_order_in_actual_draw_calls(store, monkeypatch):
    first, second = register(store), register(store, "200")
    store.import_ics(first, ics(summary="后下课", end="20261026T100000"), "合并")
    store.import_ics(second, ics(uid="early", summary="先下课", start="20261026T085000", end="20261026T092000"), "合并")
    result, drawn = collect(store), []
    original = ImageDraw.ImageDraw.text
    def recording(draw, xy, text, *args, **kwargs):
        if text in {"先下课", "后下课"}:
            drawn.append((text, xy[1]))
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", recording)
    pages = render_group_current(result)
    assert [x[0] for x in drawn] == ["先下课", "后下课"] and drawn[0][1] < drawn[1][1]
    with Image.open(BytesIO(pages[0])) as image:
        assert image.width == 1080 and image.height <= 2100


def test_empty_summary_does_not_claim_unregistered_members_are_free(store):
    result = collect(store)
    assert result["total"] == 0 and result["members_with_timetable"] == 0
    assert "已建立课表" in format_group_current(result)
