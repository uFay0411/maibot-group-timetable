from datetime import datetime

import json

import pytest

from timetable_test_plugin import query as query_module
from timetable_test_plugin.query import date_window, query_schedule

from test_timetable import dt, ics


def test_relative_dates_use_member_timezone():
    # UTC 的 10 月 25 日晚间已经是上海 10 月 26 日，明天应查询 27 日。
    now = datetime.fromisoformat("2026-10-25T18:30:00+00:00").astimezone(dt().tzinfo)
    lower, upper, _ = date_window("明天", 1, now)
    assert lower == dt("2026-10-27", "00:00")
    assert upper == dt("2026-10-28", "00:00")
    lower, upper, _ = date_window("下周", 1, dt("2026-10-25", "20:00"))
    assert lower == dt("2026-10-26", "00:00") and upper == dt("2026-11-02", "00:00")
    lower, upper, _ = date_window("下周一", 1, dt("2026-10-25", "20:00"))
    assert lower == dt("2026-10-26", "00:00") and upper == dt("2026-10-27", "00:00")


def test_filters_and_current_adjustments(store):
    store.import_ics("alice", ics(), "合并")
    store.import_ics("alice", ics(summary="大学英语", room="教三303", uid="english",
                                  start="20261026T140000", end="20261026T154000"), "合并")
    result = query_schedule(store, "alice", "明天", 1, "英语", "下午", dt("2026-10-25"))
    assert result["total"] == 1 and result["lessons"][0]["classroom"] == "教三303"
    assert result["lessons"][0]["start"].endswith("14:00:00+08:00")
    first = store.lessons("alice", dt(clock="00:00"), dt("2026-10-27", "00:00"))[0]
    store.change("alice", first, {"cancelled": True})
    result = query_schedule(store, "alice", "明天", 1, "", "上午", dt("2026-10-25"))
    assert result["total"] == 0 and result["lessons"] == []


def test_next_class_excludes_started_and_includes_conflicts(store):
    store.import_ics("alice", ics(), "合并")
    for uid, subject in (("english", "大学英语"), ("physics", "大学物理")):
        store.import_ics("alice", ics(uid=uid, summary=subject,
                                      start="20261026T140000", end="20261026T154000"), "合并")
    result = query_schedule(store, "alice", "下一节", 1, "", "全天", dt(clock="10:00"))
    assert {x["subject"] for x in result["lessons"]} == {"大学英语", "大学物理"}


@pytest.mark.parametrize("period,days", [("随便", 1), ("本周乱一", 1), ("2026-10-26", 32),
                                       ("明天", True), (None, 1)])
def test_invalid_dates_rejected(period, days):
    with pytest.raises(ValueError):
        date_window(period, days, dt())


@pytest.fixture
def natural_tool(plugin, monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = dt("2026-10-25", "20:00")
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
    monkeypatch.setattr(query_module, "datetime", FixedDateTime)
    context = {"platform": "qq", "group_id": "123", "user_id": "100", "stream_id": "group-stream"}
    owner = json.dumps(["qq", "123", "100"], ensure_ascii=False, separators=(",", ":"))
    plugin.store.member(owner, "group-stream", "100", "Alice", "Asia/Shanghai")
    plugin.store.import_ics(owner, ics(), "合并")
    return plugin, context, owner


async def test_tool_queries_requester_without_sending_twice(natural_tool):
    p, context, _ = natural_tool
    result = await p.query_timetable(period="明天", **context)
    assert result["success"] and result["total"] == 1
    assert result["lessons"][0]["subject"] == "高等数学"
    assert result["lessons"][0]["classroom"] == "教一101"
    assert result["range_start"].startswith("2026-10-26")
    # 返回给 Planner 继续 reply；工具本身不另发固定格式消息。
    assert not p.ctx.send.text.called and not p.ctx.send.hybrid.called


async def test_tool_isolates_members_groups_and_streams(natural_tool):
    p, context, _ = natural_tool
    for changed in ({"user_id": "200"}, {"group_id": "456"}, {"platform": "telegram"},
                    {"stream_id": "another-stream"}, {"group_id": ""}):
        result = await p.query_timetable(period="明天", **{**context, **changed})
        assert result["success"] is False and "lessons" not in result


async def test_tool_disabled_and_invalid_filters(natural_tool):
    p, context, _ = natural_tool
    result = await p.query_timetable(time_of_day="凌晨", **context)
    assert result["success"] is False
    p.config.plugin.enabled = False
    result = await p.query_timetable(**context)
    assert result["success"] is False and "关闭" in result["content"]


def test_query_tool_schema_exposes_dates_without_identity():
    from timetable_test_plugin import TimetablePlugin
    tools = [x for x in TimetablePlugin().get_components() if x["name"] == "query_timetable"]
    assert len(tools) == 1
    info = tools[0]
    assert info["metadata"]["visibility"] == "visible"
    assert set(info["metadata"]["parameters_raw"]) == {"message_id", "period", "days", "subject", "time_of_day"}
    assert info["metadata"]["parameters_raw"]["message_id"]["required"] is True


def question_message(user_id="100", msg_id="question-123", stream="group-stream", group="123"):
    return {"message_id": msg_id, "platform": "qq", "session_id": stream,
            "message_info": {"group_info": {"group_id": group},
                             "user_info": {"user_id": user_id, "user_cardname": "提问者"}},
            "raw_message": [{"type": "text", "data": "明天有什么课"}]}


async def test_group_stream_without_user_id_resolves_question_author(natural_tool):
    p, context, _ = natural_tool
    del context["user_id"]
    p.ctx.message.get_by_id.return_value = question_message()
    result = await p.query_timetable(message_id="question-123", period="明天", **context)
    assert result["success"] and result["lessons"][0]["subject"] == "高等数学"
    p.ctx.message.get_by_id.assert_awaited_once_with("question-123", stream_id="group-stream", include_binary_data=False)


async def test_question_resolves_platform_and_group_from_real_stream(natural_tool):
    p, context, _ = natural_tool
    p.ctx.message.get_by_id.return_value = question_message()
    result = await p.query_timetable(message_id="question-123", period="明天", chat_id="group-stream")
    assert result["success"] and result["total"] == 1


async def test_concurrent_askers_use_each_question_not_latest_speaker(natural_tool):
    p, context, _ = natural_tool
    other_owner = json.dumps(["qq", "123", "200"], ensure_ascii=False, separators=(",", ":"))
    p.store.member(other_owner, "group-stream", "200", "Bob", "Asia/Shanghai")
    p.store.import_ics(other_owner, ics(uid="english", summary="大学英语", room="教二202"), "合并")
    messages = {"question-A": question_message("100", "question-A"),
                "question-B": question_message("200", "question-B")}

    async def read_message(message_id, **kwargs):
        return messages[message_id]
    p.ctx.message.get_by_id.side_effect = read_message
    context.pop("user_id")
    result_b = await p.query_timetable(message_id="question-B", **context)
    result_a = await p.query_timetable(message_id="question-A", **context)
    assert result_a["lessons"][0]["subject"] == "高等数学"
    assert result_b["lessons"][0]["subject"] == "大学英语"


@pytest.mark.parametrize("change", [{"group": "999"}, {"stream": "another-stream"}, {"msg_id": "another-id"},
                                   {"user_id": "200"}])
async def test_question_must_match_current_group_and_host_identity(natural_tool, change):
    p, context, _ = natural_tool
    p.ctx.message.get_by_id.return_value = question_message(**change)
    result = await p.query_timetable(message_id="question-123", **context)
    assert result["success"] is False and "lessons" not in result


async def test_missing_question_identity_requests_message_id_not_group_retry(natural_tool):
    p, context, _ = natural_tool
    context.pop("user_id")
    result = await p.query_timetable(**context)
    assert result["success"] is False
    assert "message_id" in result["error"]
    assert "请在群聊中" not in result["content"]


async def test_unknown_question_does_not_read_someone_elses_recent_message(natural_tool):
    p, context, _ = natural_tool
    p.ctx.message.get_by_id.return_value = None
    result = await p.query_timetable(message_id="missing-message", **context)
    assert result["success"] is False and "没有找到" in result["content"]


async def test_question_lookup_permission_error_is_preserved(natural_tool):
    p, context, _ = natural_tool
    p.ctx.message.get_by_id.return_value = {"success": False, "error": "message.get_by_id permission denied"}
    result = await p.query_timetable(message_id="question-123", **context)
    assert result["success"] is False and "permission denied" in result["error"]
