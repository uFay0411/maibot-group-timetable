"""自然语言维护工具的真实存储、成员归属及模型可见结果回归。"""

from datetime import datetime

import json

import pytest

from timetable_test_plugin import TimetablePlugin
from timetable_test_plugin import management as management_module
from timetable_test_plugin.management import manage_schedule

from test_query import natural_tool as natural_tool, question_message
from test_timetable import dt, ics, lessons


@pytest.fixture
def maintenance(natural_tool, monkeypatch):
    p, context, owner = natural_tool

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return dt("2026-10-25", "20:00").astimezone(tz)

    monkeypatch.setattr(management_module, "datetime", FixedDateTime)
    p.ctx.message.get_by_id.return_value = question_message()
    return p, context, owner


def visible(result):
    return json.loads(result["content"].split("\n", 1)[1].split("\n回复要求：", 1)[0])


async def test_natural_move_returns_actual_details_and_preserves_other_weeks(maintenance):
    p, context, owner = maintenance
    p.store.import_ics(owner, ics("RRULE:FREQ=WEEKLY;COUNT=3\r\n"), "合并")
    query = await p.query_timetable(period="明天", **context)
    result = await p.manage_timetable(action="调课", message_id="question-123", lesson_id=query["lessons"][0]["lesson_id"],
                                    date="后天", start_time="10:00", classroom="教二202", **context)
    assert result["success"] and result["scope"] == "single_lesson"
    assert lessons(p.store, owner) == []
    moved = lessons(p.store, owner, "2026-10-27")[0]
    assert moved.start == dt("2026-10-27", "10:00") and moved.end == dt("2026-10-27", "11:40")
    assert moved.room == "教二202"
    assert lessons(p.store, owner, "2026-11-02")[0].start == dt("2026-11-02")
    assert visible(result)["after"]["classroom"] == "教二202"
    # 工具不额外发送固定话术，由 Planner 根据实际结果调用 reply。
    assert not p.ctx.send.text.called and not p.ctx.send.hybrid.called


async def test_room_only_change_then_cancel_uses_latest_token(maintenance):
    p, context, owner = maintenance
    original = lessons(p.store, owner)[0]
    result = await p.manage_timetable(action="调课", message_id="question-123", lesson_id=original.token,
                                    classroom="实验楼302", **context)
    assert result["success"] and result["after"]["start"] == original.start.isoformat()
    assert result["after"]["end"] == original.end.isoformat()
    cancelled = await p.manage_timetable(action="停课", message_id="question-123", lesson_id=result["after"]["lesson_id"], **context)
    assert cancelled["success"] and visible(cancelled)["after"] is None
    assert lessons(p.store, owner) == []
    query = await p.query_timetable(period="明天", time_of_day="早八", **context)
    assert query["total"] == 0 and not query["has_early_class"]


async def test_natural_add_is_retry_safe_and_supports_weekly_recurrence(maintenance):
    p, context, owner = maintenance
    args = dict(action="加课", message_id="question-123", date="后天", start_time="14:00", end_time="15:40",
                subject="大学英语", classroom="教三303", weeks=3)
    first = await p.manage_timetable(**args, **context)
    retry = await p.manage_timetable(**args, **context)
    assert first["success"] and retry["course_id"] == first["course_id"]
    assert not first["reused_existing_course"] and retry["reused_existing_course"]
    assert len(lessons(p.store, owner, "2026-10-27", 22)) == 3
    assert visible(first)["after"][0]["subject"] == "大学英语"


async def test_new_member_can_add_without_importing_ics(maintenance):
    p, context, _ = maintenance
    context.pop("user_id")
    p.ctx.message.get_by_id.return_value = question_message(user_id="300")
    result = await p.manage_timetable(action="加课", message_id="question-123", date="明天", start_time="14:00",
                                    end_time="15:00", subject="体育", **context)
    assert result["success"] and result["after"][0]["classroom"] is None


async def test_whole_course_change_delete_list_and_restore(maintenance):
    p, context, owner = maintenance
    original = lessons(p.store, owner)[0]
    await p.manage_timetable(action="调课", message_id="question-123", lesson_id=original.token, classroom="特例教室", **context)
    common = dict(message_id="question-123", course_id=original.course_id, **context)
    changed = await p.manage_timetable(action="改课", subject="数学分析", classroom="教四404", **common)
    assert changed["success"] and changed["scope"] == "whole_course"
    assert lessons(p.store, owner)[0].room == "特例教室"
    assert lessons(p.store, owner)[0].title == "数学分析"
    deleted = await p.manage_timetable(action="删课", **common)
    assert deleted["success"] and deleted["after"]["deleted"]
    assert not lessons(p.store, owner)
    listing = await p.manage_timetable(action="课程列表", message_id="question-123", subject="数学分析", **context)
    assert visible(listing)["courses"][0]["course_id"] == original.course_id
    assert visible(listing)["courses"][0]["deleted"]
    restored = await p.manage_timetable(action="恢复", **common)
    assert restored["success"] and not restored["after"]["deleted"]
    assert lessons(p.store, owner)[0].room == "教一101" and lessons(p.store, owner)[0].title == "高等数学"


@pytest.mark.parametrize("change", [{"group": "999"}, {"stream": "another-stream"}, {"msg_id": "wrong"}, {"user_id": "200"}])
async def test_maintenance_requires_real_question_in_current_group(maintenance, change):
    p, context, owner = maintenance
    token = lessons(p.store, owner)[0].token
    p.ctx.message.get_by_id.return_value = question_message(**change)
    result = await p.manage_timetable(action="停课", message_id="question-123", lesson_id=token, **context)
    assert not result["success"] and len(lessons(p.store, owner)) == 1


async def test_foreign_lesson_and_course_ids_cannot_modify_requester(maintenance):
    p, context, owner = maintenance
    other = json.dumps(["qq", "123", "200"], separators=(",", ":"))
    p.store.member(other, "group-stream", "200", "Bob", "Asia/Shanghai")
    p.store.import_ics(other, ics(uid="bob-physics", summary="大学物理"), "合并")
    foreign = lessons(p.store, other)[0]
    for args in (dict(action="停课", lesson_id=foreign.token), dict(action="删课", course_id=foreign.course_id)):
        result = await p.manage_timetable(message_id="question-123", **args, **context)
        assert not result["success"]
    assert len(lessons(p.store, other)) == len(lessons(p.store, owner)) == 1


async def test_message_id_required_even_if_host_has_user_context(maintenance):
    p, context, owner = maintenance
    result = await p.manage_timetable(action="停课", lesson_id=lessons(p.store, owner)[0].token, **context)
    assert not result["success"] and "message_id" in result["error"]
    assert len(lessons(p.store, owner)) == 1


@pytest.mark.parametrize("args", [
    dict(action="加课", date="明天", start_time="14:00", subject="英语"),
    dict(action="加课", date="下周", start_time="14:00", end_time="15:00", subject="英语"),
    dict(action="加课", date="明天", start_time="25:00", end_time="26:00", subject="英语"),
    dict(action="加课", date="明天", start_time="14:00", end_time="13:00", subject="英语"),
    dict(action="加课", date="明天", start_time="14:00", end_time="15:00", subject="英语", weeks=True),
    dict(action="停课"), dict(action="调课", lesson_id="invented"), dict(action="改课"),
    dict(action="停课", lesson_id="unused", subject="高数"), dict(action="删课", course_id="unused", weeks=2),
    dict(action="调课", lesson_id="unused", classroom="   "),
])
async def test_invalid_or_incomplete_requests_do_not_mutate(maintenance, args):
    p, context, owner = maintenance
    before = p.store.course_details(owner)
    result = await p.manage_timetable(message_id="question-123", **args, **context)
    assert not result["success"] and p.store.course_details(owner) == before
    assert len(lessons(p.store, owner)) == 1


def test_relative_maintenance_date_uses_member_timezone(store):
    result = manage_schedule(store, "alice", "加课", message_id="q", date="明天", start_time="08:00", end_time="09:00",
                             subject="英语", now=datetime.fromisoformat("2026-10-25T18:30:00+00:00"))
    assert result["after"][0]["start"] == "2026-10-27T08:00:00+08:00"


async def test_disabled_plugin_cannot_mutate(maintenance):
    p, context, owner = maintenance
    p.config.plugin.enabled = False
    result = await p.manage_timetable(action="停课", message_id="question-123", lesson_id=lessons(p.store, owner)[0].token, **context)
    assert not result["success"] and len(lessons(p.store, owner)) == 1


def test_maintenance_schema_exposes_no_caller_chosen_identity():
    info = next(x for x in TimetablePlugin().get_components() if x["name"] == "manage_timetable")["metadata"]
    assert info["visibility"] == "visible"
    params = info["parameters_raw"]
    assert params["message_id"]["required"] and params["action"]["required"]
    assert not {"owner", "user_id", "group_id", "stream_id"}.intersection(params)
