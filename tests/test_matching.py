"""课程简称、模糊匹配及其查询/维护边界；全部课表为合成数据。"""

import json

import pytest

from timetable_test_plugin import TimetablePlugin
from timetable_test_plugin.matching import match_subjects
from timetable_test_plugin.query import query_schedule

from test_query import natural_tool as natural_tool, question_message
from test_timetable import dt, ics, lessons


@pytest.mark.parametrize("query,title,kind", [
    ("高数", "高等数学A（上）", "alias"), ("大英", "大学英语Ⅱ", "alias"),
    ("线代", "线性代数", "alias"), ("大物", "大学物理B", "alias"),
    ("概统", "概率论与数理统计", "alias"), ("概统", "概率与数理统计", "alias"),
    ("计网", "计算机网络", "alias"), ("计组", "计算机组成原理", "alias"),
    ("马原", "马克思主义基本原理概论", "alias"), ("思修", "思想道德与法治", "alias"),
    ("高数A", "高等数学（A）", "alias"), ("大学英语", "大英", "alias"),
    ("OS", "操作系统", "alias"), ("操作系统", "OS 实验", "alias"),
    ("英语", "大学英语", "substring"), ("高等数学 A 上", "高等数学（Ａ）-上", "exact"),
    ("college english", "College-English", "exact"), ("C++", "C++程序设计", "substring"),
    ("数构", "数据结构", "abbreviation"), ("机学习", "机器学习", "abbreviation"),
    ("线性代素", "线性代数", "typo"), ("大学英雨", "大学英语（A）", "typo"),
    ("calcllus", "Calculus", "typo"),
])
def test_supported_matching(query, title, kind):
    assert match_subjects(query, [title]) == [{"subject": title, "match_type": kind}]


@pytest.mark.parametrize("query,titles", [
    ("高数", ["高等数论", "大学英语"]),
    ("大英", ["大学物理", "线性代数"]),
    ("数计", ["计算数学"]),  # 缩写不能打乱字符顺序。
    ("英语", ["大学物理"]), ("C++", ["C语言程序设计", "C#程序设计"]),
    ("OS", ["Philosophy"]), ("大学樱雨", ["大学英语"]),  # 两字误写不自动容错。
])
def test_unrelated_names_are_not_invented_as_matches(query, titles):
    assert match_subjects(query, titles) == []


def test_strong_matches_exclude_similar_but_different_names():
    assert match_subjects("大学物理", ["大学物理", "大学地理", "大学物理A"]) == [
        {"subject": "大学物理", "match_type": "exact"}, {"subject": "大学物理A", "match_type": "substring"}]
    assert {x["subject"] for x in match_subjects("高数", ["高数辅导", "高等数学A"])} == {"高数辅导", "高等数学A"}


@pytest.mark.parametrize("query", ["！！！", "+", "#", "-", None, "数" * 101])
def test_invalid_query_is_not_treated_as_match_everything(query):
    with pytest.raises(ValueError):
        match_subjects(query, ["测试数学"])


def test_query_returns_real_names_and_all_ambiguous_matches_in_model_content(store):
    store.import_ics("alice", ics(summary="高等数学A"), "合并")
    store.import_ics("alice", ics(summary="高等数学B", uid="math-b", room="教二202"), "合并")
    result = query_schedule(store, "alice", "明天", 1, "高数", "全天", dt("2026-10-25"))
    assert result["total"] == 2
    visible = json.loads(result["content"].split("\n", 1)[1].split("\n回复要求：", 1)[0])
    assert visible["subject_matching"]["query"] == "高数"
    assert visible["subject_matching"]["ambiguous"]
    assert {x["subject"] for x in visible["lessons"]} == {"高等数学A", "高等数学B"}
    assert all(x["match_type"] == "alias" for x in visible["subject_matching"]["matched_subjects"])


def test_fuzzy_candidates_are_limited_by_date_time_and_current_adjustments(store):
    store.import_ics("alice", ics(summary="大学物理", start="20261026T140000", end="20261026T154000"), "合并")
    store.import_ics("alice", ics(summary="大学地理", uid="geography"), "合并")
    result = query_schedule(store, "alice", "明天", 1, "大学物里", "上午", dt("2026-10-25"))
    assert result["total"] == 0
    result = query_schedule(store, "alice", "明天", 1, "大学物里", "下午", dt("2026-10-25"))
    assert result["lessons"][0]["subject"] == "大学物理"
    assert result["subject_matching"]["matched_subjects"][0]["match_type"] == "typo"
    p = lessons(store, "alice")[1]
    store.change("alice", p, {"cancelled": True})
    result = query_schedule(store, "alice", "明天", 1, "大物", "全天", dt("2026-10-25"))
    assert result["total"] == 0


def test_early_class_and_future_31_day_search_keep_course_filter(store):
    store.import_ics("alice", ics(summary="线性代数", start="20261109T083000", end="20261109T101000"), "合并")
    result = query_schedule(store, "alice", "未来31天", 1, "线代", "早八", dt("2026-10-25"))
    assert result["total"] == 1 and result["has_early_class"]
    assert result["lessons"][0]["start"].startswith("2026-11-09T08:30")
    assert len(result["daily_summary"]) == 31


async def test_natural_tool_reads_own_timetable_using_user_original_abbreviation(natural_tool):
    p, context, owner = natural_tool
    p.ctx.message.get_by_id.return_value = question_message()
    result = await p.query_timetable(message_id="question-123", subject="高数", period="明天", **context)
    assert result["success"] and result["lessons"][0]["subject"] == "高等数学"
    other = json.dumps(["qq", "123", "200"], separators=(",", ":"))
    p.store.member(other, "group-stream", "200", "Bob", "Asia/Shanghai")
    p.store.import_ics(other, ics(summary="高等数学B", uid="bob-math"), "合并")
    again = await p.query_timetable(message_id="question-123", subject="高数", **context)
    assert again["total"] == 1 and not again["subject_matching"]["ambiguous"]
    assert len(p.store.course_details(owner)) == 1


async def test_course_list_fuzzy_search_does_not_modify_any_course(natural_tool):
    p, context, owner = natural_tool
    original = p.store.course_details(owner)
    p.ctx.message.get_by_id.return_value = question_message(msg_id="q")
    result = await p.manage_timetable(action="课程列表", subject="高数", message_id="q", **context)
    assert result["success"] and result["courses"][0]["subject"] == "高等数学"
    assert p.store.course_details(owner) == original
    assert "高等数学" in p.execute(owner, ["课程", "高数"])


def test_search_command_shares_fuzzy_rules_and_keeps_real_lesson_ids(plugin, monkeypatch):
    import timetable_test_plugin as entry

    class FixedDateTime(entry.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt("2026-10-25", "20:00").astimezone(tz)

    monkeypatch.setattr(entry, "datetime", FixedDateTime)
    plugin.store.import_ics("alice", ics(), "合并")
    result = plugin.execute("alice", ["搜索", "高数"])
    assert "高等数学" in result and "教一101" in result
    assert lessons(plugin.store)[0].token in result
    assert "没有匹配" in plugin.execute("alice", ["搜索", "线代", "明天"])
    assert "高等数学" in plugin.execute("alice", ["搜索", "高数", "2026-10-26", "7"])
    with pytest.raises(ValueError):
        plugin.execute("alice", ["搜索", ""])


def test_tool_describes_preserving_user_keyword_and_disambiguating():
    info = next(x for x in TimetablePlugin().get_components() if x["name"] == "query_timetable")["metadata"]
    assert "模糊" in info["description"]
    assert "subject" in info["parameters_raw"]
