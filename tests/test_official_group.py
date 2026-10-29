"""官方QQ OpenID与已建课表汇总；不伪称拿到了官方群完整成员列表。"""

from datetime import datetime
from io import BytesIO

from PIL import Image, ImageDraw

import base64
import json

import pytest
import timetable_test_plugin as entry

from timetable_test_plugin.group_scope import query_roster
from test_timetable import dt, ics


@pytest.fixture
def official(plugin, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return dt(clock="09:00").astimezone(tz)
    monkeypatch.setattr(entry, "datetime", FixedDatetime)
    group, user, bob = "A" * 32, "B" * 32, "C" * 32
    stream = "official-stream"
    for uid, name, room, end in [(user, "Alice", "教一101", "20261026T094000"),
                                  (bob, "Bob", "教二202", "20261026T092000")]:
        owner = json.dumps(["qq", group, uid], separators=(",", ":"))
        plugin.store.member(owner, stream, uid, name, "Asia/Shanghai")
        plugin.store.import_ics(owner, ics(room=room, end=end), "合并")
    # 未建课表、其他群或另一聊天流的数据均不能进入汇总。
    for gid, uid, sid, subject in [(group, "D"*32, stream, ""), ("E"*32, "F"*32, stream, "其他群秘密课"),
                                   (group, "1"*32, "other-stream", "其他流秘密课")]:
        owner = json.dumps(["qq", gid, uid], separators=(",", ":"))
        plugin.store.member(owner, sid, uid, uid, "Asia/Shanghai")
        if subject:
            plugin.store.import_ics(owner, ics(summary=subject), "合并")
    source = {"platform": "qq", "session_id": stream, "message_id": "official-question",
              "message_info": {"group_info": {"group_id": group},
                  "user_info": {"user_id": user, "user_nickname": "Alice"},
                  "additional_config": {"qq_official_group_openid": group}},
              "raw_message": [{"type": "text", "data": "/课表 群内当前"}]}
    plugin.ctx.message.get_by_id.return_value = source
    plugin.ctx.api.list.side_effect = AssertionError("Official groups must not call SnowLuma roster")
    context = {"stream_id": stream, "platform": "qq", "group_id": group, "user_id": user}
    return plugin, source, context


async def test_official_current_tool_supports_openids_and_sorted_data(official):
    p, source, context = official
    result = await p.query_group_current_courses(message_id="official-question", output_format="text", **context)
    assert result["success"] and [x["member_name"] for x in result["lessons"]] == ["Bob", "Alice"]
    assert result["members_with_timetable"] == 2 and result["group_member_count"] is None
    assert result["roster_source"] == "stored_timetables" and not result["roster_verified"]
    assert "无法核实离群状态" in result["content"] and "秘密课" not in result["content"]
    assert not p.ctx.api.list.called and not p.ctx.api.call.called


async def test_official_command_text_never_requests_numeric_qq(official):
    p, source, _ = official
    source["raw_message"][0]["data"] += " 文字"
    await p.command(message=source)
    text = p.ctx.send.text.call_args.args[0]
    assert "Bob" in text and "Alice" in text and "QQ官方" in text
    assert "有效成员QQ号" not in text and "/None" not in text


async def test_official_current_png_marks_unverified_roster(official, monkeypatch):
    p, source, _ = official
    source["raw_message"][0]["data"] += " 图片"
    drawn = []
    original = ImageDraw.ImageDraw.text
    def recording(draw, xy, text, *args, **kwargs):
        drawn.append(text)
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", recording)
    await p.command(message=source)
    raw = base64.b64decode(p.ctx.send.hybrid.call_args.args[0][0]["content"])
    with Image.open(BytesIO(raw)) as image:
        assert image.format == "PNG"
    assert any("未核实离群状态" in text for text in drawn)
    assert not any("/None" in text for text in drawn)


async def test_official_upcoming_text_uses_same_scope(official):
    p, source, context = official
    owner = json.dumps(["qq", context["group_id"], context["user_id"]], separators=(",", ":"))
    p.store.import_ics(owner, ics(uid="later", summary="大学英语", start="20261026T140000", end="20261026T154000"), "合并")
    source["raw_message"][0]["data"] = "/课表 群友接下来 文字"
    await p.command(message=source)
    text = p.ctx.send.text.call_args.args[0]
    assert "大学英语" in text and "无法核实离群状态" in text and "/None" not in text
    result = await p.query_group_upcoming_courses(message_id="official-question", output_format="text", **context)
    assert result["success"] and result["members_with_timetable"] == 2


async def test_historical_openids_work_without_adapter_metadata(official):
    p, source, context = official
    source["message_info"].pop("additional_config")
    result = await p.query_group_current_courses(message_id="official-question", output_format="text", **context)
    assert result["success"] and result["roster_verified"] is False


async def test_numeric_unified_official_ids_use_metadata_not_snowluma(official):
    p, source, _ = official
    source["message_info"]["group_info"]["group_id"] = "123"
    source["message_info"]["user_info"]["user_id"] = "100"
    actor = json.dumps(["qq", "123", "100"], separators=(",", ":"))
    p.store.member(actor, source["session_id"], "100", "Alice", "Asia/Shanghai")
    p.store.import_ics(actor, ics(), "合并")
    result = await p.query_group_current_courses(message_id="official-question", stream_id=source["session_id"])
    assert result["success"] and result["total"] == 1 and not result["roster_verified"]


async def test_roster_rejects_mismatched_message_identity(official):
    p, source, context = official
    actor = json.dumps(["qq", context["group_id"], context["user_id"]], separators=(",", ":"))
    source["message_info"]["group_info"]["group_id"] = "E"*32
    with pytest.raises(ValueError, match="不一致"):
        await query_roster(p.ctx, p.store, actor, context["stream_id"], source)
