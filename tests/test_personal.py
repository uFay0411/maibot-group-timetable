"""私聊、真实来源、合并与跨群提醒；全部接口模拟，不向QQ发消息。"""

from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image, ImageDraw

import asyncio
import base64
import json

import pytest

from timetable_test_plugin.calendar_engine import stamp
from timetable_test_plugin.group_access import require_admin
from timetable_test_plugin.group_current import group_current, format_group_current
from timetable_test_plugin.presentation import render_group_current
from timetable_test_plugin.store import Store

from test_group_images import group as group
from test_query import natural_tool as natural_tool
from test_timetable import dt, ics, lessons


@pytest.fixture
def personal(group):
    p, _, actor, bob, members, source = group
    p.store.setting(bob, "enabled", 0)
    second = json.dumps(["qq", "456", "100"], separators=(",", ":"))
    private = json.dumps(["qq", "", "100"], separators=(",", ":"))
    p.store.member(second, "second-stream", "100", "Alice", "Asia/Shanghai")
    p.store.import_ics(second, ics(uid="different-calendar-id"), "合并")
    p.store.member(private, "private-stream", "100", "Alice", "Asia/Shanghai")
    source["session_id"] = "private-stream"
    source["message_info"]["group_info"] = None
    source["raw_message"] = [{"type": "text", "data": "明天有什么课"}]
    context = {"platform": "qq", "user_id": "100", "stream_id": "private-stream"}
    settings = {"friends": ["100", "200"], "temporary": None, "gone": False, "sent": []}
    operations = (
        "account.get_friend_list",
        "group.get_group_info_ex",
        "message.send_private_msg",
        "file.get_private_file_url",
    )
    p.ctx.api.list.return_value += [
        dict(
            name="adapter.snowluma." + op,
            full_name="maibot-team.snowluma-adapter.adapter.snowluma." + op,
            plugin_id="maibot-team.snowluma-adapter",
            version="1",
            public=True,
            enabled=True,
        )
        for op in operations
    ]

    async def api(name, **kw):
        if name.endswith("account.get_friend_list"):
            return [{"user_id": int(uid)} for uid in settings["friends"]]
        if name.endswith("group.get_group_member_info"):
            gid, uid = str(kw["group_id"]), str(kw["user_id"])
            if settings["gone"] or (gid == "456" and uid != "100"):
                return None
            return {
                "group_id": int(gid),
                "user_id": int(uid),
                "nickname": "Alice" if uid == "100" else "Bob",
                "role": "owner",
            }
        if name.endswith("group.get_group_info_ex"):
            value = {"group_id": kw["params"]["group_id"]}
            if settings["temporary"] is not None:
                value["allow_member_temporary_session"] = settings["temporary"]
            return {"retcode": 0, "data": value}
        if name.endswith("message.send_private_msg"):
            settings["sent"].append(kw["params"])
            return {"retcode": 0, "data": {"message_id": 12345}}
        raise AssertionError(name)

    p.ctx.api.call.side_effect = api
    route = dict(platform="qq", account_id="bot-999", scope="qq-main")
    p.ctx.chat = SimpleNamespace(
        get_all_streams=AsyncMock(
            return_value=[
                dict(route, session_id="group-stream", group_id="123", chat_type="group"),
                dict(route, session_id="second-stream", group_id="456", chat_type="group"),
                dict(route, session_id="private-stream", user_id="100", chat_type="private"),
            ]
        ),
        open_session=AsyncMock(
            return_value=dict(
                route,
                success=True,
                stream_id="private-stream",
                session_id="private-stream",
                user_id="100",
                chat_type="private",
            )
        ),
    )
    p.ctx.maisaka.context = SimpleNamespace(append=AsyncMock(return_value={"success": True}))
    return p, context, actor, second, private, bob, source, settings


async def test_private_query_merges_different_uids_and_does_not_modify_sources(personal):
    p, ctx, actor, second, private, _, _, _ = personal
    result = await p.query_timetable(message_id="question-123", period="2026-10-26", **ctx)
    assert result["success"] and result["merged"] and result["total"] == 1
    assert len(result["lessons"][0]["sources"]) == 2
    assert not p.store.course_details(private)
    assert len(p.store.course_details(actor)) == len(p.store.course_details(second)) == 1
    assert not p.ctx.send.hybrid.called


async def test_private_source_selection_updates_group_but_replies_only_private(personal):
    p, ctx, actor, _, _, _, source, _ = personal
    selected = await p.personal_timetable(message_id="question-123", action="切换来源", source_group="123", **ctx)
    assert selected["success"]
    result = await p.manage_timetable(
        message_id="question-123",
        action="加课",
        date="2026-10-27",
        start_time="14:00",
        end_time="15:40",
        subject="大学物理",
        classroom="实验楼101",
        **ctx,
    )
    assert result["success"] and lessons(p.store, actor, "2026-10-27")[0].title == "大学物理"
    source["raw_message"] = [{"type": "text", "data": "/课表 图片 2026-10-27"}]
    await p.command(message=source)
    assert p.ctx.send.hybrid.call_args.args[1] == "private-stream"
    assert p.store.get_member(actor)["stream"] == "group-stream"


async def test_private_personal_add_and_import_ics(personal):
    p, ctx, _, _, private, _, source, _ = personal
    result = await p.manage_timetable(
        message_id="question-123",
        action="加课",
        date="2026-10-27",
        start_time="10:00",
        end_time="11:40",
        subject="英语",
        classroom="教二202",
        **ctx,
    )
    assert result["success"] and lessons(p.store, private, "2026-10-27")
    source["raw_message"] = [
        {"type": "file", "data": {"name": "private.ics", "base64": base64.b64encode(ics(uid="private")).decode()}}
    ]
    await p.receive_ics(message=source)
    assert lessons(p.store, private)
    assert p.ctx.send.text.call_args.args[1] == "private-stream"


async def test_private_batch_keeps_selected_group_and_new_confirmation(personal):
    p, ctx, actor, _, _, _, source, _ = personal
    assert (await p.personal_timetable(message_id="question-123", action="切换来源", source_group="123", **ctx))[
        "success"
    ]
    draft = await p.preview_timetable_batch(
        message_id="question-123",
        action="加课",
        courses=[
            {
                "date": "2026-10-27",
                "start_time": "10:00",
                "end_time": "11:40",
                "subject": "线性代数",
                "classroom": "教二301",
            }
        ],
        **ctx,
    )
    assert draft["success"] and not lessons(p.store, actor, "2026-10-27")
    source["message_id"] = "confirm-private"
    source["raw_message"] = [{"type": "text", "data": "确认保存"}]
    done = await p.confirm_timetable_batch(message_id="confirm-private", draft_token=draft["draft_token"], **ctx)
    assert done["success"] and lessons(p.store, actor, "2026-10-27")[0].title == "线性代数"


async def test_private_selection_token_cannot_follow_changed_source(personal):
    p, ctx, _, _, _, _, _, _ = personal
    result = await p.query_timetable(message_id="question-123", subject="高数", period="2026-10-26", **ctx)
    assert result["requires_course_selection"]
    assert (await p.personal_timetable(message_id="question-123", action="切换来源", source_group="123", **ctx))[
        "success"
    ]
    stale = await p.query_timetable(
        message_id="question-123",
        selection_token=result["selection_token"],
        course_ids=[result["courses"][0]["course_id"]],
        **ctx,
    )
    assert not stale["success"] and "来源" in stale["content"]


async def test_maibot_admin_in_private_can_select_other_member_group(personal):
    p, ctx, actor, _, _, bob, _, _ = personal
    p.ctx.config.get.return_value = []
    denied = await p.personal_timetable(
        message_id="question-123", action="切换来源", source_group="123", target_user="200", **ctx
    )
    assert not denied["success"]
    p.ctx.config.get.return_value = [" QQ:100 "]
    assert (
        await p.personal_timetable(
            message_id="question-123", action="切换来源", source_group="123", target_user="200", **ctx
        )
    )["success"]
    cid = lessons(p.store, bob)[0].course_id
    assert (await p.manage_timetable(message_id="question-123", action="删课", course_id=cid, **ctx))["success"]
    assert not lessons(p.store, bob) and lessons(p.store, actor)
    p.ctx.config.get.return_value = []
    assert not (await p.query_timetable(message_id="question-123", period="2026-10-26", **ctx))["success"]


async def test_admin_configuration_is_platform_scoped_and_qq_role_irrelevant(personal):
    p, _, actor, _, _, _, _, _ = personal
    p.ctx.config.get.return_value = ["telegram:100", "qq:1000", "100"]
    with pytest.raises(ValueError):
        await require_admin(p.ctx, actor)
    p.ctx.config.get.return_value = ["qq:100"]
    await require_admin(p.ctx, actor)
    p.ctx.config.get.assert_awaited_with("plugin.permission", [])


async def test_private_merged_query_removes_left_group(personal):
    p, ctx, _, _, _, _, _, settings = personal
    settings["gone"] = True
    result = await p.query_timetable(message_id="question-123", period="2026-10-26", **ctx)
    assert result["success"] and result["total"] == 0


async def test_room_conflict_is_merged_and_never_silently_selected(personal):
    p, ctx, _, second, _, _, _, _ = personal
    p.store.patch(second, lessons(p.store, second)[0].course_id, {"room": "教三303"})
    result = await p.query_timetable(message_id="question-123", period="2026-10-26", **ctx)
    assert result["total"] == 1 and "来源教室冲突" in result["lessons"][0]["classroom"]


async def test_group_reminders_deduplicate_across_restart(personal):
    p, _, _, _, _, _, _, _ = personal
    await asyncio.gather(p.tick(dt(clock="07:40")), p.tick(dt(clock="07:40")))
    assert p.ctx.send.hybrid.await_count == 1
    assert p.ctx.send.hybrid.call_args.args[1] in {"group-stream", "second-stream"}
    p.ctx.chat.open_session.assert_not_awaited()
    p.store = Store(p.store.path)
    await p.tick(dt(clock="07:41"))
    assert p.ctx.send.hybrid.await_count == 1


async def test_old_delivery_from_one_group_prevents_new_global_reminder(personal):
    p, _, actor, _, _, _, _, _ = personal
    lesson = lessons(p.store, actor)[0]
    old = sha256(f"{actor}|{lesson.course_id}|{lesson.original}|{stamp(lesson.start)}".encode()).hexdigest()
    p.store.claim(old, actor, dt(clock="07:40").timestamp())
    p.store.sending([old], dt(clock="07:40").timestamp())
    await p.tick(dt(clock="07:41"))
    assert not p.ctx.send.hybrid.called


async def test_unknown_group_send_is_not_replayed_after_preference_changes(personal):
    p, _, actor, _, _, _, _, _ = personal
    p.ctx.send.hybrid.side_effect = TimeoutError("unknown result")
    await p.tick(dt(clock="07:40"))
    assert p.store.delivery_status(actor) == {"pending": 1}
    p.store.set_reminder_mode("qq", "100", "off")
    p.store.set_reminder_mode("qq", "100", "group")
    await p.tick(dt(clock="07:41"))
    assert p.ctx.send.hybrid.await_count == 1


async def test_rescheduled_same_occurrence_is_not_notified_twice(personal):
    p, _, actor, second, _, _, _, _ = personal
    await p.tick(dt(clock="07:40"))
    assert p.ctx.send.hybrid.await_count == 1
    for owner in [actor, second]:
        lesson = lessons(p.store, owner)[0]
        p.store.change(owner, lesson, {"start": stamp(dt(clock="08:10")), "end": stamp(dt(clock="09:50"))})
    await p.tick(dt(clock="07:50"))
    assert p.ctx.send.hybrid.await_count == 1


async def test_no_due_courses_do_not_poll_qq_permissions(personal):
    p, _, _, _, _, _, _, _ = personal
    p.ctx.api.call.reset_mock()
    await p.tick(dt(clock="07:00"))
    assert not p.ctx.api.call.called and not p.ctx.send.hybrid.called


def test_private_quoted_file_requires_same_sender_and_private_message():
    from timetable_test_plugin.imports import snowluma_source

    current = {
        "platform": "qq",
        "session_id": "private-stream",
        "message_info": {"user_info": {"user_id": "100"}, "group_info": None},
    }
    detail = {
        "message_id": 666,
        "message_type": "private",
        "sender": {"user_id": 100},
        "user_id": 100,
        "message": [{"type": "file", "data": {"name": "private.ics", "base64": base64.b64encode(ics()).decode()}}],
    }
    source = snowluma_source(detail, current, "666")
    assert source["message_info"]["group_info"] is None
    detail["sender"]["user_id"] = 200
    with pytest.raises(ValueError):
        snowluma_source(detail, current, "666")
    detail["sender"]["user_id"] = 100
    detail["message_type"] = "group"
    detail["group_id"] = 123
    with pytest.raises(ValueError):
        snowluma_source(detail, current, "666")


def test_group_current_fallback_next_labels_and_excludes_unregistered(store, monkeypatch):
    owners = []
    for uid in ["100", "200"]:
        owner = json.dumps(["qq", "123", uid], separators=(",", ":"))
        owners.append(owner)
        store.member(owner, "group-stream", uid, uid, "Asia/Shanghai")
    store.import_ics(owners[0], ics(), "合并")
    store.import_ics(owners[1], ics(uid="next", start="20261026T100000", end="20261026T114000"), "合并")
    result = group_current(
        store,
        "qq",
        "123",
        "group-stream",
        [
            {"user_id": "100", "name": "正在上课的同学"},
            {"user_id": "200", "name": "稍后上课的同学"},
            {"user_id": "300", "name": "不应显示的同学"},
        ],
        "Asia/Shanghai",
        dt(clock="09:00"),
        include_next=True,
    )
    assert [x["status"] for x in result["lessons"]] == ["上课中", "下一节课"]
    assert result["members_in_class"] == result["members_with_next"] == 1
    text = format_group_current(result)
    assert "不应显示" not in text and "[上课中]" in text and "[下一节课]" in text
    seen = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kw):
        seen.append(text)
        return original(self, xy, text, *args, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    pages = render_group_current(result)
    assert "上课中" in seen and "下一节课" in seen and not any("不应显示" in x for x in seen)
    with Image.open(BytesIO(pages[0])) as img:
        assert img.width == 1080


async def test_removed_private_reminder_cannot_be_enabled_by_tool_or_command(personal):
    p, ctx, _, _, _, _, source, _ = personal
    result = await p.personal_timetable(message_id="question-123", action="私聊提醒", reminder_mode="private", **ctx)
    assert not result["success"] and "已移除" in result["content"]
    with pytest.raises(ValueError):
        p.store.set_reminder_mode("qq", "100", "private")
    source["raw_message"] = [{"type": "text", "data": "/课表 私聊提醒 开"}]
    await p.command(message=source)
    assert "已移除" in p.ctx.send.text.call_args.args[0]
    assert not p.ctx.chat.open_session.called and not p.ctx.send.hybrid.called


async def test_legacy_private_preference_is_disabled_without_falling_back_to_group(personal):
    p, _, _, _, private, _, _, _ = personal
    with p.store.connect() as db:
        db.execute("INSERT OR REPLACE INTO personal_settings VALUES('qq','100','private')")
    p.store.import_ics(private, ics(uid="private-only"), "合并")
    assert p.store.reminder_mode("qq", "100") == "off"
    await p.tick(dt(clock="07:40"))
    assert not p.ctx.send.hybrid.called and not p.ctx.chat.open_session.called
    assert not p.ctx.maisaka.reply.generate.called
    p.store.set_reminder_mode("qq", "100", "group")
    await p.tick(dt(clock="07:41"))
    assert p.ctx.send.hybrid.await_count == 1
    assert p.ctx.send.hybrid.call_args.args[1] != "private-stream"


def test_manifest_and_tool_schema_no_longer_offer_private_reminders():
    from timetable_test_plugin import TimetablePlugin

    root = __import__("pathlib").Path(__file__).parents[1]
    manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    assert not {"chat.open_session", "chat.get_all_streams", "maisaka.context.append"} & set(manifest["capabilities"])
    tool = next(x for x in TimetablePlugin().get_components() if x["name"] == "personal_timetable")
    params = tool["metadata"]["parameters_raw"]
    assert "私聊提醒" not in params["action"]["enum"] and "private" not in params["reminder_mode"]["enum"]
