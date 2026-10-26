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
    assert set(info["metadata"]["parameters_raw"]) == {
        "message_id", "period", "days", "subject", "time_of_day", "course_ids", "catalog_offset",
        "target_member", "output_format", "selection_token"}
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


@pytest.mark.parametrize("start,end,is_early", [("075900", "090000", False), ("080000", "094000", True),
                                               ("083000", "100000", True), ("085900", "103000", True),
                                               ("090000", "104000", False)])
def test_early_class_start_time_boundaries(store, start, end, is_early):
    store.import_ics("alice", ics(start="20261026T" + start, end="20261026T" + end), "合并")
    result = query_schedule(store, "alice", "明天", 1, "", "早八", dt("2026-10-25"))
    assert result["has_early_class"] is is_early
    assert result["total"] == int(is_early)
    assert result["daily_summary"][0]["has_early_class"] is is_early
    if is_early:
        assert result["lessons"][0]["start"][11:19] == f"{start[:2]}:{start[2:4]}:{start[4:]}"


def test_week_early_class_summary_includes_days_without_early_classes(store):
    store.import_ics("alice", ics(), "合并")
    store.import_ics("alice", ics(uid="wednesday", start="20261028T083000", end="20261028T101000"), "合并")
    store.import_ics("alice", ics(uid="tuesday", start="20261027T090000", end="20261027T104000"), "合并")
    result = query_schedule(store, "alice", "下周", 1, "", "早八", dt("2026-10-25"))
    summary = result["daily_summary"]
    assert len(summary) == 7
    assert [(x["date"], x["weekday"]) for x in summary if x["has_early_class"]] == [
        ("2026-10-26", "周一"), ("2026-10-28", "周三")]
    assert not summary[1]["has_early_class"]  # 周二有九点的课，仍属于没有早八。
    assert sum(not x["has_early_class"] for x in summary) == 5


def test_early_class_query_applies_member_timezone(store):
    store.import_ics("alice", ics(tz="", start="20261026T000000Z", end="20261026T014000Z"), "合并")
    result = query_schedule(store, "alice", "明天", 1, "", "早八", dt("2026-10-25"))
    assert result["has_early_class"] is True
    assert result["lessons"][0]["start"].endswith("08:00:00+08:00")


def test_next_early_class_searches_beyond_one_week_and_skips_cancelled_lesson(store):
    store.import_ics("alice", ics("RRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE;TZID=Asia/Shanghai:20261102T080000\r\n"), "合并")
    first = store.lessons("alice", dt(clock="00:00"), dt("2026-10-27", "00:00"))[0]
    store.change("alice", first, {"cancelled": True})
    result = query_schedule(store, "alice", "下一次早八", 1, "", "全天", dt("2026-10-25"))
    assert result["time_of_day"] == "早八" and result["has_early_class"] is True
    assert result["lessons"][0]["start"].startswith("2026-11-09T08:00")


def test_early_class_reschedule_removes_original_day_and_keeps_new_day(store):
    store.import_ics("alice", ics(), "合并")
    lesson = store.lessons("alice", dt(clock="00:00"), dt("2026-10-27", "00:00"))[0]
    store.change("alice", lesson, {"start": dt("2026-10-27", "08:30").isoformat(),
                                    "end": dt("2026-10-27", "10:10").isoformat()})
    result = query_schedule(store, "alice", "下周", 1, "", "早八", dt("2026-10-25"))
    assert not result["daily_summary"][0]["has_early_class"]
    assert result["daily_summary"][1]["has_early_class"]
    assert result["lessons"][0]["start"].startswith("2026-10-27T08:30")


def test_next_early_class_does_not_return_started_or_outside_window_classes(store):
    store.import_ics("alice", ics(), "合并")
    store.import_ics("alice", ics(uid="far-away", start="20261201T080000", end="20261201T094000"), "合并")
    result = query_schedule(store, "alice", "下次早八", 1, "", "早八", dt(clock="10:00"))
    assert result["total"] == 0 and result["has_early_class"] is False
    assert "31天" in result["content"]


async def test_natural_early_class_tool_uses_question_author(natural_tool):
    p, context, _ = natural_tool
    context.pop("user_id")
    p.ctx.message.get_by_id.return_value = question_message()
    result = await p.query_timetable(message_id="question-123", period="明天", time_of_day="早八", **context)
    assert result["success"] and result["has_early_class"] is True
    assert result["lessons"][0]["subject"] == "高等数学"
    assert not p.ctx.send.text.called


def test_early_class_tool_schema_and_description():
    from timetable_test_plugin import TimetablePlugin
    info = next(x for x in TimetablePlugin().get_components() if x["name"] == "query_timetable")["metadata"]
    assert "早八" in info["parameters_raw"]["time_of_day"]["enum"]
    assert "下一次早八" in info["description"]


def visible_query_data(result):
    # MaiBot 优先使用 content；只检查模型实际收到的字段，不能依赖旁路 lessons 掩盖问题。
    header, body = result["content"].split("\n", 1)
    assert "实际执行结果" in header
    data, _ = json.JSONDecoder().raw_decode(body)
    return data


async def test_model_visible_tool_content_contains_actual_lessons(natural_tool):
    p, context, _ = natural_tool
    result = await p.query_timetable(period="明天", **context)
    data = visible_query_data(result)
    assert data["success"] and data["total"] == 1
    assert data["timezone"] == "Asia/Shanghai"
    assert data["lessons"][0]["subject"] == "高等数学"
    assert data["lessons"][0]["classroom"] == "教一101"
    assert data["lessons"][0]["start"] == "2026-10-26T08:00:00+08:00"
    assert data["lessons"][0]["end"] == "2026-10-26T09:40:00+08:00"
    assert "content" not in data  # 不递归嵌入自己。


def test_model_visible_early_class_content_contains_daily_availability(store):
    store.import_ics("alice", ics(), "合并")
    result = query_schedule(store, "alice", "下周", 1, "", "早八", dt("2026-10-25"))
    data = visible_query_data(result)
    assert data["has_early_class"]
    assert data["daily_summary"][0]["date"] == "2026-10-26"
    assert data["daily_summary"][0]["has_early_class"] is True
    assert data["daily_summary"][1]["has_early_class"] is False
    assert data["lessons"][0]["classroom"] == "教一101"


def test_model_visible_empty_query_is_successful_empty_data(store):
    result = query_schedule(store, "alice", "明天", 1, "", "全天", dt("2026-10-25"))
    data = visible_query_data(result)
    assert data["success"] is True and data["total"] == 0 and data["lessons"] == []
    assert data["range_start"] == "2026-10-26T00:00:00+08:00"
    assert "没有符合条件" in result["content"]


def test_model_visible_next_early_query_contains_honest_search_window(store):
    result = query_schedule(store, "alice", "下一次早八", 1, "", "早八", dt("2026-10-25"))
    data = visible_query_data(result)
    assert data["has_early_class"] is False and data["total"] == 0
    assert "31天" in result["content"] and "以后都没有" in result["content"]


def test_course_text_remains_json_data_in_visible_content(store):
    title = '英语 "A班"\n周一'
    store.import_ics("alice", ics(summary=title.replace("\n", "\\n")), "合并")
    result = query_schedule(store, "alice", "明天", 1, "", "全天", dt("2026-10-25"))
    data = visible_query_data(result)
    assert data["lessons"][0]["subject"] == title
