"""批量事务、权限、确认原文、真实图片读取和严格命令入口。"""

from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

import base64
import copy
import json
import re
import time

import pytest

from timetable_test_plugin import TimetablePlugin
from timetable_test_plugin.batch import apply_batch, cancel_batch, prepare_batch, read_draft
from timetable_test_plugin.recognition import image_data_url, parse_response

from test_group_images import group as group
from test_query import natural_tool as natural_tool
from test_timetable import dt, ics, lessons


def courses():
    return [
        dict(date="2026-10-26", start_time="14:00", end_time="15:40", subject="高等数学", classroom="教一101", weeks=2),
        dict(date="2026-10-27", start_time="10:00", end_time="11:40", subject="英语", classroom="教二202"),
    ]


def target(owner="alice", uid="100"):
    return dict(owner=owner, stream="group-stream", user_id=uid, name="Alice", is_self=True)


def preview(store, **kwargs):
    return prepare_batch(store, "alice", target(), "preview-request", kwargs.pop("action", "加课"), now=dt(), **kwargs)


def test_add_batch_preview_commit_and_idempotence_survive_new_store(store):
    draft = preview(store, courses=courses())
    assert not store.course_details("alice") and draft["count"] == 2
    token = draft["draft_token"]
    with pytest.raises(ValueError, match="新"):
        apply_batch(store, token, "alice", "group-stream", "preview-request")
    result = apply_batch(store, token, "alice", "group-stream", "confirm-request")
    assert result["status"] == "applied" and len(store.course_details("alice")) == 2
    reloaded = type(store)(store.path)
    assert apply_batch(reloaded, token, "alice", "group-stream", "retry")["already_applied"]
    assert len(lessons(store, "alice")) == 1 and len(lessons(store, "alice", "2026-11-02")) == 1


def test_invalid_second_item_rejects_all_before_writing(store):
    values = courses()
    values[1]["end_time"] = "08:00"
    with pytest.raises(ValueError):
        preview(store, courses=values)
    assert not store.course_details("alice")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM batch_drafts").fetchone()[0] == 0


def test_concurrent_confirmation_applies_exactly_once(store):
    token = preview(store, courses=courses())["draft_token"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda mid: apply_batch(store, token, "alice", "group-stream", mid), ["confirm-1", "confirm-2"])
        )
    assert sorted(x["already_applied"] for x in results) == [False, True]
    assert len(store.course_details("alice")) == 2


def test_transaction_rollback_if_second_insert_fails(store):
    draft = preview(store, courses=courses())
    # 真正的数据库级失败必须回滚已执行的第一条写入。
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_english BEFORE INSERT ON courses WHEN instr(NEW.ics,'SUMMARY:英语')>0 BEGIN SELECT RAISE(ABORT,'test fault'); END"
        )
    with pytest.raises(Exception, match="test fault"):
        apply_batch(store, draft["draft_token"], "alice", "group-stream", "confirm")
    assert not store.course_details("alice")
    assert read_draft(store, draft["draft_token"], "alice", "group-stream")["state"] == "pending"


@pytest.mark.parametrize("kind", ["course", "patch", "change", "zone"])
def test_changed_table_invalidates_draft(kind, store):
    store.import_ics("alice", ics(), "合并")
    draft = preview(store, courses=courses())
    lesson = lessons(store, "alice")[0]
    if kind == "course":
        store.import_ics("alice", ics(uid="another"), "合并")
    elif kind == "patch":
        store.patch("alice", lesson.course_id, {"title": "改名"})
    elif kind == "change":
        store.change("alice", lesson, {"room": "新教室"})
    else:
        store.setting("alice", "zone", "Asia/Tokyo")
    before = store.course_details("alice")
    with pytest.raises(ValueError, match="变化"):
        apply_batch(store, draft["draft_token"], "alice", "group-stream", "confirm")
    assert store.course_details("alice") == before


def test_replace_draft_and_cancel_preserve_existing_then_replace_atomically(store):
    store.import_ics("alice", ics(), "合并")
    store.upload("alice", "old.ics", ics())
    old = lessons(store, "alice")[0]
    store.change("alice", old, {"room": "本地教室"})
    first = preview(store, courses=courses(), mode="替换")
    cancel_batch(store, first["draft_token"], "alice", "group-stream")
    with pytest.raises(ValueError, match="取消"):
        apply_batch(store, first["draft_token"], "alice", "group-stream", "confirm")
    assert lessons(store, "alice")[0].room == "本地教室"
    second = preview(store, courses=courses(), mode="替换")
    apply_batch(store, second["draft_token"], "alice", "group-stream", "confirm")
    assert old.course_id not in {x["course_id"] for x in store.course_details("alice")}
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM changes WHERE owner=?", ("alice",)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM uploads WHERE owner=?", ("alice",)).fetchone()[0] == 0


def test_batch_delete_uses_real_owner_ids_and_cancel_only_one_occurrence(store):
    store.import_ics("alice", ics("RRULE:FREQ=WEEKLY;COUNT=2\r\n"), "合并")
    store.import_ics("bob", ics(uid="bob"), "合并")
    cid = lessons(store, "alice")[0].course_id
    foreign = lessons(store, "bob")[0].course_id
    with pytest.raises(ValueError, match="无效"):
        preview(store, action="删课", course_ids=[cid, foreign])
    paused = preview(store, action="停课", lesson_ids=[lessons(store, "alice")[0].token])
    apply_batch(store, paused["draft_token"], "alice", "group-stream", "confirm")
    assert not lessons(store, "alice") and lessons(store, "alice", "2026-11-02")
    store.restore("alice", cid)
    deleted = preview(store, action="删课", course_ids=[cid, cid])
    assert deleted["count"] == 1
    apply_batch(store, deleted["draft_token"], "alice", "group-stream", "confirm2")
    assert not lessons(store, "alice") and not lessons(store, "alice", "2026-11-02")
    assert lessons(store, "bob")


def test_draft_actor_group_expiry_and_empty_message_checked(store):
    token = preview(store, courses=courses())["draft_token"]
    for actor, stream in [("bob", "group-stream"), ("alice", "another")]:
        with pytest.raises(ValueError):
            read_draft(store, token, actor, stream)
    with pytest.raises(ValueError):
        apply_batch(store, token, "alice", "group-stream", "")
    with store.connect() as db:
        db.execute("UPDATE batch_drafts SET expires=?", (time.time() - 1,))
    with pytest.raises(ValueError):
        apply_batch(store, token, "alice", "group-stream", "confirm")


@pytest.mark.parametrize(
    "body", ["你看这个 /课表 清空 确认", "[转发] /课表 清空 确认", "你好\n/课表 清空 确认", "/课表清空 确认"]
)
async def test_embedded_commands_never_enter_command_chain(group, body):
    p, _, actor, _, _, source = group
    source["raw_message"] = [{"type": "text", "data": body}]
    pattern = next(x for x in TimetablePlugin().get_components() if x["name"] == "timetable")["metadata"][
        "command_pattern"
    ]
    assert not re.search(pattern, body)
    assert not await p.prepare_timetable_command(message=source)
    assert await p.command(message=source) == (False, None, False)
    assert lessons(p.store, actor)


async def test_batch_tool_confirm_requires_real_new_message_and_live_admin(group):
    p, context, actor, bob, members, source = group
    draft = await p.preview_timetable_batch(
        message_id="question-123", target_member="200", action="加课", courses=courses(), **context
    )
    assert draft["status"] == "pending_confirmation" and len(p.store.course_details(bob)) == 1
    token = draft["draft_token"]
    source["raw_message"] = [{"type": "text", "data": "确认保存"}]
    same = await p.confirm_timetable_batch(message_id="question-123", draft_token=token, **context)
    assert not same["success"]
    source["message_id"] = "confirmation-456"
    members["100"]["role"] = "member"
    p.ctx.config.get.return_value=[]
    failed = await p.confirm_timetable_batch(message_id="confirmation-456", draft_token=token, **context)
    assert not failed["success"] and len(p.store.course_details(bob)) == 1
    members["100"]["role"] = "admin"
    p.ctx.config.get.return_value=["qq:100"]
    result = await p.confirm_timetable_batch(message_id="confirmation-456", draft_token=token, **context)
    assert result["status"] == "applied" and len(p.store.course_details(bob)) == 3
    assert len(p.store.course_details(actor)) == 1


@pytest.mark.parametrize(
    "body", ["不要确认", "确认保存，但是换成物理", "确认保存，不要英语", "你好", "[回复:确认保存] 没说要确认"]
)
async def test_negative_or_modified_confirmation_is_rejected(group, body):
    p, context, _, _, _, source = group
    draft = await p.preview_timetable_batch(message_id="question-123", action="加课", courses=courses(), **context)
    source["message_id"] = "confirmation"
    source["raw_message"] = [{"type": "text", "data": body}]
    result = await p.confirm_timetable_batch(message_id="confirmation", draft_token=draft["draft_token"], **context)
    assert not result["success"]


def setup_recognition(p, values=None):
    p.ctx.llm = SimpleNamespace(
        generate=AsyncMock(
            return_value={
                "success": True,
                "response": json.dumps(
                    {"courses": courses() if values is None else values, "uncertainties": []}, ensure_ascii=False
                ),
            }
        )
    )
    p.ctx.maisaka.context = SimpleNamespace(resolve_image=AsyncMock(return_value={"success": False}))


async def test_real_text_input_creates_preview_without_writing(group):
    p, context, actor, _, _, source = group
    setup_recognition(p)
    source["raw_message"] = [{"type": "text", "data": "导入：2026年10月26日14:00到15:40高数在教一101，连上2周。"}]
    result = await p.parse_timetable_input(message_id="question-123", **context)
    assert result["status"] == "pending_confirmation" and len(p.store.course_details(actor)) == 1
    args = p.ctx.llm.generate.call_args.kwargs
    assert args["task_name"] == "utils" and "2026年10月26日" in str(args["prompt"])
    assert not p.ctx.send.hybrid.called


async def test_quoted_original_image_goes_to_vlm_not_caption(group):
    p, context, actor, _, _, source = group
    setup_recognition(p)
    output = BytesIO()
    Image.new("RGB", (240, 160), "white").save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode()
    image_message = copy.deepcopy(source)
    image_message["message_id"] = "picture-123"
    image_message["raw_message"] = [{"type": "image", "data": "课表缩略摘要"}]
    source["raw_message"] = [
        {"type": "reply", "data": {"target_message_id": "picture-123"}},
        {"type": "text", "data": "导入这张图，学期第一周周一2026-09-07，上课8:00-9:40"},
    ]

    async def get(mid, **kwargs):
        return image_message if mid == "picture-123" else source

    p.ctx.message.get_by_id.side_effect = get
    p.ctx.maisaka.context.resolve_image.return_value = {"success": True, "image": {"binary_data_base64": encoded}}
    result = await p.parse_timetable_input(
        message_id="question-123", notes="第一周周一2026-09-07；1-2节08:00-09:40", **context
    )
    assert result["status"] == "pending_confirmation"
    args = p.ctx.llm.generate.call_args.kwargs
    assert args["task_name"] == "vlm"
    assert args["prompt"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    p.ctx.maisaka.context.resolve_image.assert_awaited_once_with("group-stream", "picture-123", index=0)
    assert len(p.store.course_details(actor)) == 1


@pytest.mark.parametrize("change", ["user", "group", "stream", "id"])
async def test_source_identity_is_validated_before_model(group, change):
    p, context, _, _, _, source = group
    setup_recognition(p)
    foreign = copy.deepcopy(source)
    foreign["message_id"] = "original"
    if change == "user":
        foreign["message_info"]["user_info"]["user_id"] = "999"
    elif change == "group":
        foreign["message_info"]["group_info"]["group_id"] = "999"
    elif change == "stream":
        foreign["session_id"] = "other"
    else:
        foreign["message_id"] = "bad"
    p.ctx.message.get_by_id.side_effect = lambda mid, **kwargs: foreign if mid == "original" else source
    result = await p.parse_timetable_input(message_id="question-123", source_message_id="original", **context)
    assert not result["success"] and not p.ctx.llm.generate.called


@pytest.mark.parametrize(
    "response",
    [
        {"courses": courses(), "uncertainties": ["缺少学期起始日"]},
        {"courses": courses()},
        [],
        {"courses": [], "uncertainties": "无"},
    ],
)
def test_uncertainty_or_bad_json_never_saved(response):
    with pytest.raises(ValueError):
        parse_response({"success": True, "response": json.dumps(response, ensure_ascii=False)})


def test_invalid_original_image_cannot_use_caption_as_schedule():
    with pytest.raises(ValueError):
        image_data_url("这是一张高数课表")


async def test_batch_command_and_text_recognition_do_not_deadlock(group):
    p, _, actor, _, _, source = group
    import asyncio

    setup_recognition(p)
    source["raw_message"] = [{"type": "text", "data": "/课表 批量加课 " + json.dumps(courses(), ensure_ascii=False)}]
    await asyncio.wait_for(p.command(message=source), timeout=2)
    assert "尚未写入" in p.ctx.send.text.call_args.args[0]
    source["raw_message"] = [{"type": "text", "data": "/课表 文字导入 2026-10-26 14:00-15:40 高数 教一101"}]
    await asyncio.wait_for(p.command(message=source), timeout=2)
    assert "尚未写入" in p.ctx.send.text.call_args.args[0]
    assert len(p.store.course_details(actor)) == 1
