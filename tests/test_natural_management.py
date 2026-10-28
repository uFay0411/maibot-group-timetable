"""自然语言维护的语义候选、设置、真实身份与管理员代管回归。"""

import json

import pytest

from test_group_images import group as group
from test_management import maintenance as maintenance, visible
from test_query import natural_tool as natural_tool, question_message
from test_timetable import ics, lessons


async def test_semantic_stop_uses_real_subject_then_single_lesson(maintenance):
    p, context, owner = maintenance
    p.store.import_ics(owner, ics(uid="ethics", summary="思想道德与法治"), "合并")
    before = p.store.course_details(owner)
    candidates = await p.manage_timetable(action="停课", match_subject="思政", period="明天",
                                          message_id="question-123", **context)
    assert candidates["success"] and candidates["requires_target_selection"]
    assert not candidates["write_performed"] and p.store.course_details(owner) == before
    course = next(x for x in candidates["courses"] if x["subject"] == "思想道德与法治")
    selected = await p.query_timetable(message_id="question-123", selection_token=candidates["selection_token"],
                                      course_ids=[course["course_id"]], **context)
    assert selected["total"] == 1
    changed = await p.manage_timetable(action="停课", message_id="question-123",
        lesson_id=selected["lessons"][0]["lesson_id"], selection_token=selected["selection_token"], **context)
    assert changed["success"] and changed["before"]["subject"] == "思想道德与法治"
    assert [x.title for x in lessons(p.store, owner)] == ["高等数学"]
    assert not p.ctx.send.text.called and not p.ctx.send.hybrid.called


async def test_ambiguous_candidates_never_choose_or_mutate(maintenance):
    p, context, owner = maintenance
    p.store.import_ics(owner, ics(uid="ethics", summary="思想道德与法治"), "合并")
    p.store.import_ics(owner, ics(uid="marx", summary="马克思主义基本原理"), "合并")
    before = p.store.course_details(owner)
    result = await p.manage_timetable(action="删课", match_subject="思政", message_id="question-123", **context)
    assert {x["subject"] for x in result["courses"]} >= {"思想道德与法治", "马克思主义基本原理"}
    assert result["requires_target_selection"] and not result["write_performed"]
    assert p.store.course_details(owner) == before and len(lessons(p.store, owner)) == 3
    assert visible(result)["write_performed"] is False


async def test_restore_catalog_includes_disabled_and_rename_distinguishes_old_name(maintenance):
    p, context, owner = maintenance
    original = lessons(p.store, owner)[0]
    p.store.patch(owner, original.course_id, {"deleted": True})
    result = await p.manage_timetable(action="恢复", match_subject="高数", message_id="question-123", **context)
    assert result["courses"][0]["deleted"]
    restored = await p.manage_timetable(action="恢复", message_id="question-123",
        course_id=result["courses"][0]["course_id"], selection_token=result["selection_token"], **context)
    assert restored["success"] and not restored["after"]["deleted"]
    result = await p.manage_timetable(action="改课", match_subject="高数", subject="数学分析",
                                     message_id="question-123", **context)
    assert lessons(p.store, owner)[0].title == "高等数学"
    renamed = await p.manage_timetable(action="改课", subject="数学分析", message_id="question-123",
        course_id=result["courses"][0]["course_id"], selection_token=result["selection_token"], **context)
    assert renamed["success"] and lessons(p.store, owner)[0].title == "数学分析"


async def test_move_keeps_original_date_separate_from_destination(maintenance):
    p, context, owner = maintenance
    result = await p.manage_timetable(action="调课", match_subject="高数", period="明天", date="后天",
                                     start_time="10:00", classroom="教二202", message_id="question-123", **context)
    selected = await p.query_timetable(message_id="question-123", selection_token=result["selection_token"],
                                      course_ids=[result["courses"][0]["course_id"]], **context)
    assert selected["lessons"][0]["start"].startswith("2026-10-26")
    changed = await p.manage_timetable(action="调课", lesson_id=selected["lessons"][0]["lesson_id"],
        date="后天", start_time="10:00", classroom="教二202", message_id="question-123",
        selection_token=selected["selection_token"], **context)
    assert changed["success"] and changed["after"]["start"].startswith("2026-10-27T10:00")
    assert changed["after"]["end"].startswith("2026-10-27T11:40")


async def test_reminder_switch_preserves_courses_and_status_is_readonly(maintenance):
    p, context, owner = maintenance
    original = p.store.course_details(owner)
    for value in [False, True]:
        result = await p.manage_timetable(action="提醒", reminder_enabled=value, message_id="question-123", **context)
        assert result["success"] and result["after"]["reminder_enabled"] is value
        status = await p.manage_timetable(action="状态", message_id="question-123", **context)
        assert status["success"] and not status["write_performed"]
        assert visible(status)["after"]["reminder_enabled"] is value
        assert p.store.course_details(owner) == original
    assert p.store.get_member("alice")["enabled"] == 1


async def test_timezone_validation_keeps_absolute_course_times(maintenance):
    p, context, owner = maintenance
    before = lessons(p.store, owner)[0].start.timestamp()
    invalid = await p.manage_timetable(action="时区", timezone="not/a_zone", message_id="question-123", **context)
    assert not invalid["success"] and p.store.get_member(owner)["zone"] == "Asia/Shanghai"
    changed = await p.manage_timetable(action="时区", timezone="Asia/Tokyo", message_id="question-123", **context)
    assert changed["success"] and changed["after"]["timezone"] == "Asia/Tokyo"
    assert lessons(p.store, owner)[0].start.timestamp() == before


@pytest.mark.parametrize("params", [dict(action="提醒"), dict(action="提醒", reminder_enabled=1),
    dict(action="状态", reminder_enabled=False), dict(action="状态", timezone="UTC"),
    dict(action="时区"), dict(action="停课", match_subject="   "),
    dict(action="加课", match_subject="高数"), dict(action="删课", match_subject="高数", catalog_offset=-1)])
async def test_incomplete_or_wrong_fields_never_mutate(maintenance, params):
    p, context, owner = maintenance
    before = p.store.get_member(owner), p.store.course_details(owner)
    result = await p.manage_timetable(message_id="question-123", **params, **context)
    assert not result["success"]
    assert (p.store.get_member(owner), p.store.course_details(owner)) == before


async def test_management_candidate_cannot_be_reused_for_another_action_or_message(maintenance):
    p, context, owner = maintenance
    result = await p.manage_timetable(action="删课", match_subject="高数", message_id="question-123", **context)
    cid = result["courses"][0]["course_id"]
    wrong_action = await p.manage_timetable(action="改课", subject="其他", message_id="question-123",
        course_id=cid, selection_token=result["selection_token"], **context)
    assert not wrong_action["success"]
    p.ctx.message.get_by_id.return_value = question_message(msg_id="new-question")
    wrong_message = await p.manage_timetable(action="删课", message_id="new-question", course_id=cid,
        selection_token=result["selection_token"], **context)
    assert not wrong_message["success"] and len(lessons(p.store, owner)) == 1


async def test_admin_candidate_token_preserves_target_and_permission_is_rechecked(group):
    p, context, actor, bob, members, source = group
    result = await p.manage_timetable(action="删课", match_subject="大英", target_member="200",
                                     message_id="question-123", **context)
    assert result["success"] and result["member"]["user_id"] == "200"
    cid = result["courses"][0]["course_id"]
    p.ctx.config.get.return_value = []
    # QQ owner/admin身份不等于MaiBot管理员，实际写入前撤销权限即拒绝。
    members["100"]["role"] = "owner"
    refused = await p.manage_timetable(action="删课", course_id=cid, selection_token=result["selection_token"],
                                      message_id="question-123", **context)
    assert not refused["success"] and not p.store.course_details(bob)[0]["deleted"]
    p.ctx.config.get.return_value = ["qq:100"]
    applied = await p.manage_timetable(action="删课", course_id=cid, selection_token=result["selection_token"],
                                      message_id="question-123", **context)
    assert applied["success"] and p.store.course_details(bob)[0]["deleted"]
    assert not p.store.course_details(actor)[0]["deleted"]


async def test_group_admin_only_cannot_change_member_reminder(group):
    p, context, actor, bob, members, source = group
    p.ctx.config.get.return_value = []
    denied = await p.manage_timetable(action="提醒", reminder_enabled=False, target_member="200",
                                     message_id="question-123", **context)
    assert not denied["success"] and p.store.get_member(bob)["enabled"] == 1


async def test_private_message_never_creates_private_management(maintenance):
    p, context, owner = maintenance
    message = question_message()
    message["message_info"]["group_info"] = None
    p.ctx.message.get_by_id.return_value = message
    result = await p.manage_timetable(action="提醒", reminder_enabled=False, message_id="question-123", **context)
    assert not result["success"] and p.store.get_member(owner)["enabled"] == 1


async def test_whole_course_candidates_paginate_without_keyword_filter(maintenance):
    p, context, owner = maintenance
    from unittest.mock import patch
    courses = [{"course_id": str(i), "subject": f"真实课程{i}", "deleted": False} for i in range(105)]
    with patch.object(p.store, "course_details", return_value=courses):
        first = await p.manage_timetable(action="删课", match_subject="思政", message_id="question-123", **context)
        last = await p.manage_timetable(action="删课", match_subject="思政", catalog_offset=first["next_catalog_offset"],
                                        message_id="question-123", **context)
    assert len(first["courses"]) == 100 and first["has_more"]
    assert len(last["courses"]) == 5 and not last["has_more"]
    assert json.loads(first["content"].split("\n")[1])["write_performed"] is False
