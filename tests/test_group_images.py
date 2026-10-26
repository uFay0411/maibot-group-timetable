"""管理员代管、同群查询与真实 PNG 输出；适配器和发送仅模拟接口。"""

from io import BytesIO

from PIL import Image

import base64
import json

import pytest

from timetable_test_plugin import presentation
from timetable_test_plugin.group_access import command_text
from timetable_test_plugin.presentation import render_timetable
from timetable_test_plugin.query import query_schedule

from test_query import natural_tool as natural_tool, question_message
from test_timetable import dt, ics, lessons
from test_wakeup import share as share


@pytest.fixture
def group(natural_tool):
    p, context, actor = natural_tool
    bob = json.dumps(["qq", "123", "200"], separators=(",", ":"))
    p.store.member(bob, "group-stream", "200", "小明", "Asia/Shanghai")
    # 与管理员拥有同一 ICS UID，特意验证仅靠 course_id 无法区分成员的情况。
    p.store.import_ics(bob, ics(summary="大学英语", room="教二202"), "合并")
    source = question_message()
    p.ctx.message.get_by_id.return_value = source
    members = {
        "100": {"group_id": 123, "user_id": 100, "card": "管理员", "nickname": "Alice", "role": "admin"},
        "200": {"group_id": 123, "user_id": 200, "card": "小明", "nickname": "Bob", "role": "member"},
    }

    async def api(name, **kwargs):
        assert kwargs["group_id"] == 123 and kwargs["no_cache"] is True
        if name.endswith("group.get_group_member_info"):
            return members.get(str(kwargs["user_id"]))
        if name.endswith("group.get_group_member_list"):
            return list(members.values())
        raise AssertionError(name)

    p.ctx.api.call.side_effect = api
    p.ctx.config.get.return_value=["qq:100"]
    return p, context, actor, bob, members, source


@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_group_admin_add_change_cancel_delete_and_restore(group, role):
    p, context, actor, bob, members, _ = group
    members["100"]["role"] = role
    args = dict(message_id="question-123", target_member="200", **context)
    added = await p.manage_timetable(action="加课", date="2026-10-26", start_time="14:00", end_time="15:40",
                                    subject="大学物理", classroom="实验楼302", **args)
    assert added["success"] and added["admin_operation"] and added["member"]["user_id"] == "200"
    token = added["after"][0]["lesson_id"]
    moved = await p.manage_timetable(action="调课", lesson_id=token, date="2026-10-27", start_time="10:00", **args)
    assert moved["success"] and moved["after"]["start"].startswith("2026-10-27T10:00")
    cancelled = await p.manage_timetable(action="停课", lesson_id=moved["after"]["lesson_id"], **args)
    assert cancelled["success"] and not lessons(p.store, bob, "2026-10-27")
    cid = added["course_id"]
    changed = await p.manage_timetable(action="改课", course_id=cid, subject="大学物理实验", classroom="实验楼501", **args)
    assert changed["success"]
    deleted = await p.manage_timetable(action="删课", course_id=cid, **args)
    assert deleted["success"] and deleted["after"]["deleted"]
    restored = await p.manage_timetable(action="恢复", course_id=cid, **args)
    assert restored["success"] and not restored["after"]["deleted"]
    assert [x.title for x in lessons(p.store, actor)] == ["高等数学"]


@pytest.mark.parametrize("role", ["member", None, "administrator", "admin", "owner"])
async def test_cannot_spoof_admin_role_or_mutate_others(group, role):
    p, context, _, bob, members, source = group
    members["100"]["role"] = role
    p.ctx.config.get.return_value=[]
    source["message_info"]["user_info"]["role"] = "owner"
    token = lessons(p.store, bob)[0].token
    result = await p.manage_timetable(action="停课", lesson_id=token, target_member="200", message_id="question-123",
                                    **context, is_admin=True, role="admin")
    assert not result["success"] and len(lessons(p.store, bob)) == 1


async def test_admin_role_is_refreshed_for_each_operation(group):
    p, context, _, bob, members, _ = group
    token = lessons(p.store, bob)[0].token
    args = dict(target_member="200", message_id="question-123", **context)
    first = await p.manage_timetable(action="调课", lesson_id=token, classroom="新教室", **args)
    assert first["success"]
    members["100"]["role"] = "member"
    p.ctx.config.get.return_value=[]
    second = await p.manage_timetable(action="停课", lesson_id=first["after"]["lesson_id"], **args)
    assert not second["success"] and lessons(p.store, bob)[0].room == "新教室"


async def test_non_admin_can_query_group_friend_by_card(group):
    p, context, _, _, members, _ = group
    members["100"]["role"] = "member"
    p.ctx.config.get.return_value=[]
    result = await p.query_timetable(target_member="小明", message_id="question-123", **context)
    assert result["success"] and result["member"]["name"] == "小明"
    assert result["lessons"][0]["subject"] == "大学英语"
    assert result["lessons"][0]["classroom"] == "教二202"


async def test_duplicate_names_request_explicit_member_before_any_write(group):
    p, context, _, bob, members, _ = group
    members["300"] = {"group_id": 123, "user_id": 300, "card": "小明", "role": "member"}
    result = await p.manage_timetable(action="停课", lesson_id=lessons(p.store, bob)[0].token,
                                    target_member="小明", message_id="question-123", **context)
    assert not result["success"] and len(result["member_candidates"]) == 2
    assert len(lessons(p.store, bob)) == 1


async def test_target_response_must_match_current_group(group):
    p, context, _, _, members, _ = group
    members["200"]["group_id"] = 999
    result = await p.query_timetable(target_member="200", message_id="question-123", **context)
    assert not result["success"] and "lessons" not in result


async def test_semantic_token_binds_member_even_with_identical_course_ids(group):
    p, context, actor, bob, _, _ = group
    assert lessons(p.store, actor)[0].course_id == lessons(p.store, bob)[0].course_id
    first = await p.query_timetable(subject="大英", target_member="200", message_id="question-123", **context)
    selected = first["courses"][0]["course_id"]
    # 模型第二次漏传 target_member 时仍绑定小明，不会误查到自己的同 UID 高数。
    second = await p.query_timetable(course_ids=[selected], selection_token=first["selection_token"],
                                     message_id="question-123", **context)
    assert second["member"]["user_id"] == "200" and second["lessons"][0]["subject"] == "大学英语"
    wrong = await p.query_timetable(course_ids=[selected], selection_token=first["selection_token"],
                                    target_member="100", message_id="question-123", **context)
    assert not wrong["success"]


async def test_selection_token_expires_and_cannot_cross_questions(group):
    p, context, _, _, _, source = group
    first = await p.query_timetable(subject="大英", target_member="200", message_id="question-123", **context)
    token = first["selection_token"]
    source["message_id"] = "new-question"
    changed = await p.query_timetable(course_ids=[first["courses"][0]["course_id"]], selection_token=token,
                                      message_id="new-question", **context)
    assert not changed["success"]
    source["message_id"] = "question-123"
    p.selection_contexts[token]["expires"] = 0
    expired = await p.query_timetable(course_ids=[first["courses"][0]["course_id"]], selection_token=token,
                                      message_id="question-123", **context)
    assert not expired["success"]


def mentioned_command(text, rest):
    source = question_message()
    source["raw_message"] = [{"type": "at", "data": {"target_user_id": "999"}},
                             {"type": "text", "data": text},
                             {"type": "at", "data": {"target_user_id": "200"}},
                             {"type": "text", "data": rest}]
    return source


async def test_admin_command_accepts_real_at_segments(group):
    p, _, actor, bob, _, _ = group
    source = mentioned_command("/课表 管理 ", " 加课 2026-10-27 14:00 15:40 大学物理 实验楼302")
    assert command_text(source).startswith("/课表 管理  200")
    await p.command(message=source)
    assert lessons(p.store, bob, "2026-10-27")[0].title == "大学物理"
    assert not lessons(p.store, actor, "2026-10-27")


async def test_group_query_command_is_read_only(group):
    p, _, _, bob, members, _ = group
    members["100"]["role"] = "member"
    p.ctx.config.get.return_value=[]
    await p.command(message=mentioned_command("/课表 群友 ", " 明天"))
    assert "小明" in p.ctx.send.text.call_args.args[0] and "大学英语" in p.ctx.send.text.call_args.args[0]
    await p.command(message=mentioned_command("/课表 群友 ", " 清空 确认"))
    assert len(lessons(p.store, bob)) == 1
    assert "只读" in p.ctx.send.text.call_args.args[0]


async def test_group_and_own_image_commands_send_actual_png(group):
    p, _, _, _, _, _ = group
    await p.command(message=mentioned_command("/课表 群友 ", " 明天 图片"))
    payload = p.ctx.send.hybrid.call_args.args[0][0]
    assert payload["type"] == "image"
    blob = base64.b64decode(payload["content"])
    with Image.open(BytesIO(blob)) as image:
        assert image.format == "PNG" and image.width == 1080 and image.height <= 2100
    own = question_message()
    own["raw_message"] = [{"type": "text", "data": "/课表 图片 2026-10-26 7"}]
    await p.command(message=own)
    assert p.ctx.send.hybrid.await_count >= 2


async def test_natural_image_waits_for_semantic_selection_and_deduplicates_retry(group):
    p, context, _, _, _, _ = group
    first = await p.query_timetable(subject="大英", target_member="200", output_format="image", message_id="question-123", **context)
    assert first["requires_course_selection"] and not p.ctx.send.hybrid.called
    args = dict(course_ids=[first["courses"][0]["course_id"]], selection_token=first["selection_token"],
                message_id="question-123", **context)
    second = await p.query_timetable(**args)
    assert second["image_sent"] and second["image_pages"] == 1 and second["member"]["user_id"] == "200"
    await p.query_timetable(**args)
    assert p.ctx.send.hybrid.await_count == 1


async def test_image_send_failure_does_not_claim_sent(group):
    p, context, _, _, _, _ = group
    p.ctx.send.hybrid.return_value = {"sent": False}
    result = await p.query_timetable(output_format="image", message_id="question-123", **context)
    assert not result["success"] and "发送未成功" in result["content"]
    assert not p.image_receipts


def test_long_chinese_schedule_paginates_without_growing_unbounded(store):
    store.import_ics("alice", ics(), "合并")
    result = query_schedule(store, "alice", "2026-10-26", 7, "", "全天", dt("2026-10-25"))
    row = result["lessons"][0]
    result["lessons"] = [{**row, "subject": f"测试课程{i}：思想道德与法治和中国特色社会主义理论体系概论", "classroom": "工程训练中心第一教学楼三层实验室"} for i in range(30)]
    result["total"] = 30
    pages = render_timetable(result, "测试同学")
    assert 2 <= len(pages) <= 8
    for page in pages:
        with Image.open(BytesIO(page)) as image:
            assert image.width == 1080 and 200 <= image.height <= 2100


def test_image_requires_complete_query_and_bundled_font(store, monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        render_timetable({"requires_course_selection": True}, "测试")
    result = query_schedule(store, "alice", "明天", 1, "", "全天", dt("2026-10-25"))
    monkeypatch.setattr(presentation, "FONT_PATH", tmp_path / "missing.otf")
    with pytest.raises(ValueError, match="字体缺失"):
        render_timetable(result, "测试")


async def test_admin_natural_clear_is_explicit_and_scoped_to_target(group):
    p, context, actor, bob, _, _ = group
    args = dict(action="清空", target_member="200", message_id="question-123", **context)
    missing_confirmation = await p.manage_timetable(**args)
    assert not missing_confirmation["success"] and len(lessons(p.store, bob)) == 1
    cleared = await p.manage_timetable(confirm_clear=True, **args)
    assert cleared["success"] and cleared["removed_courses"] == 1
    assert not p.store.course_details(bob) and len(lessons(p.store, actor)) == 1


async def test_admin_natural_ics_import_and_non_admin_rejection(group):
    p, context, actor, bob, members, source = group
    source["raw_message"].append({"type": "file", "data": {"name": "synthetic.ics", "base64": base64.b64encode(
        ics(uid="new-physics", summary="大学物理")).decode()}})
    members["100"]["role"] = "member"
    p.ctx.config.get.return_value=[]
    refused = await p.import_ics_timetable(target_member="200", message_id="question-123", **context)
    assert not refused["success"] and len(p.store.course_details(bob)) == 1
    members["100"]["role"] = "admin"
    p.ctx.config.get.return_value=["qq:100"]
    imported = await p.import_ics_timetable(target_member="200", mode="替换", message_id="question-123", **context)
    assert imported["success"] and lessons(p.store, bob)[0].title == "大学物理"
    assert lessons(p.store, actor)[0].title == "高等数学"


async def test_admin_quoted_own_ics_can_replace_targets_table(group):
    p, _, actor, bob, _, source = group
    request = mentioned_command("/课表 管理 ", " 解析 替换")
    request["raw_message"].insert(0, {"type": "reply", "data": {"target_message_id": "file-1"}})
    upload = dict(source)
    upload["message_id"] = "file-1"
    upload["raw_message"] = [{"type": "file", "data": {"name": "synthetic.ics", "base64": base64.b64encode(
        ics(uid="linear", summary="线性代数")).decode()}}]
    p.ctx.message.get_by_id.return_value = upload
    await p.command(message=request)
    assert lessons(p.store, bob)[0].title == "线性代数"
    assert lessons(p.store, actor)[0].title == "高等数学"


async def test_admin_natural_wakeup_import_targets_the_verified_member(group, share, monkeypatch):
    import timetable_test_plugin as entry
    from unittest.mock import AsyncMock

    p, context, actor, bob, members, source = group
    source["raw_message"] = [{"type": "text", "data": "给小明导入：" + "a" * 32}]
    fetch = AsyncMock(return_value=share)
    monkeypatch.setattr(entry, "fetch_wakeup_schedule", fetch)
    members["100"]["role"] = "member"
    p.ctx.config.get.return_value=[]
    refused = await p.import_wakeup_timetable(target_member="200", message_id="question-123", **context)
    assert not refused["success"] and not fetch.called
    members["100"]["role"] = "admin"
    p.ctx.config.get.return_value=["qq:100"]
    imported = await p.import_wakeup_timetable(target_member="200", mode="替换", message_id="question-123", **context)
    assert imported["success"] and imported["member"]["user_id"] == "200"
    assert lessons(p.store, bob)[0].title == "测试数学" and lessons(p.store, actor)[0].title == "高等数学"


async def test_group_api_disabled_denies_management_without_fallback(group):
    p, context, _, bob, _, _ = group
    p.ctx.api.list.return_value = []
    result = await p.manage_timetable(action="清空", confirm_clear=True, target_member="200", message_id="question-123", **context)
    assert not result["success"] and len(lessons(p.store, bob)) == 1


async def test_manage_after_group_query_cannot_accidentally_change_self(group):
    p, context, actor, bob, _, _ = group
    queried = await p.query_timetable(target_member="200", message_id="question-123", **context)
    token = queried["lessons"][0]["lesson_id"]
    omitted = await p.manage_timetable(action="停课", lesson_id=token, message_id="question-123", **context)
    assert not omitted["success"] and len(lessons(p.store, actor)) == len(lessons(p.store, bob)) == 1
    bound = await p.manage_timetable(action="调课", lesson_id=token, classroom="绑定后的教室",
                                    selection_token=queried["selection_token"], message_id="question-123", **context)
    assert bound["success"] and bound["member"]["user_id"] == "200"
    assert lessons(p.store, bob)[0].room == "绑定后的教室" and lessons(p.store, actor)[0].room == "教一101"
