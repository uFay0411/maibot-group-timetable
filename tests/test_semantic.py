"""验证候选 -> LLM选择编号 -> 真实课次的工具契约，不模拟语义规则冒充模型。"""

import json

import pytest

from timetable_test_plugin import TimetablePlugin
from timetable_test_plugin import semantic
from timetable_test_plugin.matching import match_subjects
from timetable_test_plugin.semantic import semantic_query

from test_query import natural_tool as natural_tool, question_message
from test_timetable import dt, ics, lessons


def visible(result):
    return json.loads(result["content"].split("\n", 1)[1].split("\n回复要求：", 1)[0])


@pytest.fixture
def semantic_tool(natural_tool):
    p, context, owner = natural_tool
    p.store.import_ics(owner, ics(uid="ethics", summary="思想道德与法治", room="教二202"), "合并")
    p.ctx.message.get_by_id.return_value = question_message()
    return p, context, owner


async def test_sizheng_returns_real_catalog_for_llm_not_alias_search_failure(semantic_tool):
    p, context, _ = semantic_tool
    assert match_subjects("思政", ["思想道德与法治"]) == []  # 没有新增硬编码别名。
    result = await p.query_timetable(subject="思政", message_id="question-123", **context)
    data = visible(result)
    assert data["requires_course_selection"] and data["resolution_status"] == "needs_llm_selection"
    assert {x["subject"] for x in data["courses"]} == {"思想道德与法治", "高等数学"}
    assert "lessons" not in data and "total" not in data
    assert "再调用 query_timetable" in result["content"]
    assert not p.ctx.send.text.called and not p.ctx.send.hybrid.called


async def test_llm_selected_id_queries_sizheng_without_reapplying_keyword_rules(semantic_tool):
    p, context, _ = semantic_tool
    first = await p.query_timetable(subject="思政", message_id="question-123", **context)
    # 这里只模拟 Planner 传入读到的编号，不声称已测试外部 LLM 的语义判断。
    selected = next(x["course_id"] for x in first["courses"] if x["subject"] == "思想道德与法治")
    result = await p.query_timetable(subject="思政", course_ids=[selected], message_id="question-123", **context)
    data = visible(result)
    assert not data["requires_course_selection"] and data["total"] == 1
    assert data["lessons"][0]["subject"] == "思想道德与法治"
    assert data["lessons"][0]["classroom"] == "教二202"
    assert data["subject_resolution"]["query"] == "思政"
    assert data["subject_resolution"]["method"] == "maibot_llm_course_selection"


async def test_catalog_is_entire_own_timetable_even_when_course_not_on_requested_day(semantic_tool):
    p, context, owner = semantic_tool
    p.store.import_ics(owner, ics(uid="later", summary="马克思主义基本原理", start="20261103T140000", end="20261103T154000"), "合并")
    result = await p.query_timetable(subject="思政", period="明天", message_id="question-123", **context)
    assert "马克思主义基本原理" in {x["subject"] for x in result["courses"]}
    assert "先询问用户" in result["content"]
    selected = next(x["course_id"] for x in result["courses"] if x["subject"] == "马克思主义基本原理")
    tomorrow = await p.query_timetable(subject="思政", period="明天", course_ids=[selected], message_id="question-123", **context)
    assert tomorrow["total"] == 0
    assert tomorrow["subject_resolution"]["selected_subjects"] == ["马克思主义基本原理"]


async def test_same_subject_representative_includes_all_wakeup_arrangements(semantic_tool):
    p, context, owner = semantic_tool
    p.store.import_ics(owner, ics(uid="ethics-evening", summary="思想道德与法治", start="20261026T180000", end="20261026T194000"), "合并")
    catalog = await p.query_timetable(subject="思政", message_id="question-123", **context)
    course = next(x for x in catalog["courses"] if x["subject"] == "思想道德与法治")
    assert course["arrangement_count"] == 2
    result = await p.query_timetable(subject="思政", course_ids=[course["course_id"]], message_id="question-123", **context)
    assert result["total"] == 2 and result["subject_resolution"]["included_arrangements"] == 2
    afternoon = await p.query_timetable(subject="思政", course_ids=[course["course_id"]], time_of_day="晚上",
                                      message_id="question-123", **context)
    assert afternoon["total"] == 1 and afternoon["lessons"][0]["start"][11:16] == "18:00"


async def test_selected_courses_use_latest_cancel_and_early_class_data(semantic_tool):
    p, context, owner = semantic_tool
    first = await p.query_timetable(subject="思政", message_id="question-123", **context)
    selected = next(x["course_id"] for x in first["courses"] if x["subject"] == "思想道德与法治")
    lesson = next(x for x in lessons(p.store, owner) if x.course_id == selected)
    p.store.change(owner, lesson, {"cancelled": True})
    result = await p.query_timetable(subject="思政", course_ids=[selected], time_of_day="早八", message_id="question-123", **context)
    assert result["total"] == 0 and not result["has_early_class"]
    assert not result["daily_summary"][0]["has_early_class"]


@pytest.mark.parametrize("ids", ["invented", [None], [True], [""], ["x" * 129], ["invented"], ["x"] * 101])
async def test_invalid_or_invented_course_ids_are_rejected(semantic_tool, ids):
    p, context, _ = semantic_tool
    result = await p.query_timetable(subject="思政", course_ids=ids, message_id="question-123", **context)
    assert not result["success"] and "lessons" not in result


async def test_ids_from_another_members_table_are_not_accepted(semantic_tool):
    p, context, _ = semantic_tool
    other = json.dumps(["qq", "123", "200"], separators=(",", ":"))
    p.store.member(other, "group-stream", "200", "Bob", "Asia/Shanghai")
    p.store.import_ics(other, ics(uid="bob-marx", summary="马克思主义基本原理"), "合并")
    foreign = lessons(p.store, other)[0].course_id
    first = await p.query_timetable(subject="思政", message_id="question-123", **context)
    assert foreign not in {x["course_id"] for x in first["courses"]}
    result = await p.query_timetable(subject="思政", course_ids=[foreign], message_id="question-123", **context)
    assert not result["success"]


async def test_empty_selection_requests_clarification_not_fabricated_no_class(semantic_tool):
    p, context, _ = semantic_tool
    result = await p.query_timetable(subject="思政", course_ids=[], message_id="question-123", **context)
    assert result["requires_course_selection"] and result["resolution_status"] == "needs_clarification"
    assert "total" not in visible(result)


async def test_no_subject_query_keeps_single_call_and_accepts_empty_optional_ids(semantic_tool):
    p, context, _ = semantic_tool
    for ids in (None, []):
        result = await p.query_timetable(course_ids=ids, message_id="question-123", **context)
        assert result["total"] == 2 and "courses" not in result


def test_catalog_pagination_never_silently_hides_candidates(store, monkeypatch):
    store.import_ics("alice", ics(summary="课程甲", uid="a"), "合并")
    store.import_ics("alice", ics(summary="课程乙", uid="b"), "合并")
    monkeypatch.setattr(semantic, "CATALOG_PAGE_SIZE", 1)
    first = semantic_query(store, "alice", "明天", 1, "某门课", "全天", now=dt("2026-10-25"))
    assert first["has_more"] and first["next_catalog_offset"] == 1 and first["total_subjects"] == 2
    second = semantic_query(store, "alice", "明天", 1, "某门课", "全天", catalog_offset=1, now=dt("2026-10-25"))
    assert not second["has_more"] and second["next_catalog_offset"] is None
    assert first["courses"][0]["subject"] != second["courses"][0]["subject"]


def test_empty_catalog_is_explicit_and_not_a_semantic_search_failure(store):
    result = semantic_query(store, "alice", "明天", 1, "思政", "全天", now=dt("2026-10-25"))
    assert result["total"] == 0 and result["subject_resolution"]["method"] == "empty_catalog"
    assert not result["requires_course_selection"]


def test_tool_contract_exposes_real_id_selection_and_no_new_model_settings():
    info = next(x for x in TimetablePlugin().get_components() if x["name"] == "query_timetable")["metadata"]
    assert info["parameters_raw"]["course_ids"]["type"] == "array"
    assert "LLM" in info["description"] and "requires_course_selection" in info["description"]
    assert "思政" in info["description"]
