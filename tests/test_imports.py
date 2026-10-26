from unittest.mock import AsyncMock

import base64
import re

import pytest
import timetable_test_plugin as plugin_module

from timetable_test_plugin import TimetablePlugin, identity
from timetable_test_plugin.imports import (
    call_snowluma_api, ics_files, own_text, quoted_message_id, safe_api_error, snowluma_source, snowluma_summary,
)

from test_timetable import dt, ics


def source_message():
    return {"message_id": "456", "platform": "qq", "session_id": "group-stream", "message_info": {
        "group_info": {"group_id": "123"}, "user_info": {"user_id": "100", "user_cardname": "Alice"}},
        "raw_message": [{"type": "file", "data": {"name": "math.ics",
                         "base64": base64.b64encode(ics()).decode()}}]}


def reply_command(text="/课表 解析"):
    message = source_message()
    message["message_id"] = "789"
    message["raw_message"] = [{"type": "reply", "data": {"target_message_id": "456"}},
                              {"type": "text", "data": text}]
    return message


def snowluma_detail():
    return {"message_id": 456, "group_id": 123, "sender": {"user_id": 100},
            "message": [{"type": "file", "data": {"name": "math.ics", "file_id": "file-uuid", "busid": 102}}]}


def upload_notice():
    message = source_message()
    message["message_id"] = "qq-notice-synthetic"
    message["is_notify"] = True
    message["raw_message"] = [{"type": "text", "data": "[事件-群文件上传] Alice 上传了文件 math.ics"}]
    message["message_info"]["additional_config"] = {
        "napcat_notice_type": "group_upload",
        "napcat_notice_payload": {"notice_type": "group_upload", "group_id": 123, "user_id": 100,
                                  "file": {"id": "file-uuid", "name": "math.ics", "busid": 102}}}
    return message


async def test_quote_import_with_host_reply_prefix(plugin):
    source, command = source_message(), reply_command()
    plugin.ctx.message.get_by_id.return_value = source
    pattern = next(x for x in TimetablePlugin().get_components() if x["name"] == "timetable")["metadata"]["command_pattern"]
    processed = "[回复了 Alice 的消息: [文件] math.ics]\n/课表 解析"
    assert not re.search(pattern, processed)
    normalized = await plugin.prepare_timetable_command(message=command)
    assert re.search(pattern, normalized['modified_kwargs']['message']['processed_plain_text'])
    await plugin.command(message=command, text=processed, stream_id="group-stream")
    plugin.ctx.message.get_by_id.assert_awaited_once_with("456", stream_id="group-stream", include_binary_data=True)
    owner = identity(command)[0]
    assert plugin.store.lessons(owner, dt(clock="00:00"), dt("2026-10-27", "00:00"))[0].title == "高等数学"
    assert "从引用文件" in plugin.ctx.send.text.call_args.args[0]
    assert not plugin.ctx.api.call.called


async def test_quote_replace_and_save_for_later_reparse(plugin):
    command = reply_command("/课表 解析 替换")
    owner, _ = await plugin.register_member(command)
    plugin.store.import_ics(owner, ics(uid="old", summary="旧课程"), "合并")
    plugin.ctx.message.get_by_id.return_value = source_message()
    await plugin.command(message=command, text="/课表 解析 替换")
    result = plugin.store.lessons(owner, dt(clock="00:00"), dt("2026-10-27", "00:00"))
    assert [x.title for x in result] == ["高等数学"]
    assert plugin.store.import_latest(owner, "合并") == (1, 0)


@pytest.mark.parametrize("field,value", [("user", "200"), ("group", "999"), ("stream", "another"),
                                       ("message_id", "999")])
async def test_quote_validates_source_identity(plugin, field, value):
    source = source_message()
    if field == "user":
        source["message_info"]["user_info"]["user_id"] = value
    elif field == "group":
        source["message_info"]["group_info"]["group_id"] = value
    elif field == "stream":
        source["session_id"] = value
    else:
        source["message_id"] = value
    plugin.ctx.message.get_by_id.return_value = source
    command = reply_command()
    await plugin.command(message=command, text="/课表 解析")
    assert not plugin.store.course_list(identity(command)[0])
    assert not plugin.ctx.api.call.called


async def test_bad_quote_never_imports_last_cached_file(plugin):
    command = reply_command()
    owner, _ = await plugin.register_member(command)
    plugin.store.upload(owner, "cached.ics", ics())
    plugin.ctx.api.call.return_value = None
    await plugin.command(message=command, text="/课表 解析")
    assert not plugin.store.course_list(owner)
    assert "未找到" in plugin.ctx.send.text.call_args.args[0]


async def test_quote_text_never_executes_destructive_command(plugin):
    command = reply_command("你好")
    command["raw_message"][0]["data"]["target_message_content"] = "/课表 清空 确认"
    result = await plugin.command(message=command, text="[回复了某人的消息: /课表 清空 确认]")
    assert result == (False, None, False)
    assert not plugin.ctx.send.text.called and not plugin.ctx.message.get_by_id.called
    assert own_text(command) == "你好"


def test_reply_id_validation():
    message = reply_command()
    assert quoted_message_id(message) == "456"
    message["raw_message"][0]["data"] = "456"
    assert quoted_message_id(message) == "456"
    message["raw_message"][0]["data"] = {}
    with pytest.raises(ValueError, match="编号"):
        quoted_message_id(message)


async def test_snowluma_group_upload_notice_auto_import(plugin, monkeypatch):
    plugin.ctx.api.call.return_value = {"retcode": 0, "data": {"url": "https://files.example.edu/math.ics"}}
    reader = AsyncMock(return_value=ics())
    monkeypatch.setattr(plugin_module, "read_upload", reader)
    message = upload_notice()
    await plugin.receive_ics(message)
    plugin.ctx.api.call.assert_awaited_once_with("maibot-team.snowluma-adapter.adapter.snowluma.file.get_group_file_url", version="1",
                                                params={"group_id": 123, "file_id": "file-uuid", "busid": 102})
    assert reader.call_args.args[0]["url"] == "https://files.example.edu/math.ics"
    assert plugin.store.course_list(identity(message)[0])


@pytest.mark.parametrize("host_has_text", [False, True])
async def test_snowluma_quoted_file_recovered_from_client(plugin, monkeypatch, host_has_text):
    if host_has_text:
        source = source_message()
        source["raw_message"] = [{"type": "text", "data": "[文件] math.ics"}]
        plugin.ctx.message.get_by_id.return_value = source
    plugin.ctx.api.call.side_effect = [snowluma_detail(), {"retcode": 0, "data": {"url": "https://example.edu/math.ics"}}]
    monkeypatch.setattr(plugin_module, "read_upload", AsyncMock(return_value=ics()))
    command = reply_command()
    await plugin.command(message=command, text="/课表 解析")
    assert plugin.ctx.api.call.call_args_list[0].args[0] == "maibot-team.snowluma-adapter.adapter.snowluma.message.get_msg"
    assert plugin.ctx.api.call.call_args_list[0].kwargs["message_id"] == "456"
    assert len(plugin.store.course_list(identity(command)[0])) == 1


async def test_snowluma_recovered_foreign_message_not_imported(plugin, monkeypatch):
    detail = snowluma_detail()
    detail["sender"]["user_id"] = 200
    plugin.ctx.api.call.return_value = detail
    reader = AsyncMock(return_value=ics())
    monkeypatch.setattr(plugin_module, "read_upload", reader)
    command = reply_command()
    await plugin.command(message=command, text="/课表 解析")
    assert not reader.called
    assert "你自己" in plugin.ctx.send.text.call_args.args[0]


def test_snowluma_notice_sender_must_match_outer_message():
    message = upload_notice()
    message["message_info"]["additional_config"]["napcat_notice_payload"]["user_id"] = 999
    with pytest.raises(ValueError, match="归属不一致"):
        ics_files(message)


async def test_invalid_quoted_ics_preserves_courses_and_last_upload(plugin):
    command = reply_command("/课表 解析 替换")
    owner, _ = await plugin.register_member(command)
    plugin.store.import_ics(owner, ics(), "合并", upload_name="good.ics")
    source = source_message()
    source["raw_message"][0]["data"]["base64"] = base64.b64encode(b"invalid calendar").decode()
    plugin.ctx.message.get_by_id.return_value = source
    await plugin.command(message=command, text="/课表 解析 替换")
    assert len(plugin.store.course_list(owner)) == 1
    assert plugin.store.import_latest(owner, "合并") == (1, 0)


async def test_permission_failure_does_not_query_other_source(plugin):
    plugin.ctx.message.get_by_id.return_value = {"success": False, "error": "permission denied"}
    await plugin.command(message=reply_command(), text="/课表 解析")
    assert not plugin.ctx.api.call.called
    assert "message.get_by_id" in plugin.ctx.send.text.call_args.args[0]


async def test_snowluma_api_rejection_preserves_existing_courses(plugin, monkeypatch):
    message = upload_notice()
    owner, _ = await plugin.register_member(message)
    plugin.store.import_ics(owner, ics(), "合并")
    plugin.ctx.api.call.return_value = {"retcode": 100, "data": {}}
    reader = AsyncMock(return_value=ics())
    monkeypatch.setattr(plugin_module, "read_upload", reader)
    await plugin.receive_ics(message)
    assert not reader.called
    assert len(plugin.store.course_list(owner)) == 1


@pytest.mark.parametrize("wrapped", [False, True])
async def test_get_msg_real_id_without_message_id_imports(plugin, monkeypatch, wrapped):
    # SnowLuma 的真实 get_msg 测试覆盖存储记录无 message_id、仅补 real_id 的返回方式。
    detail = snowluma_detail()
    detail["real_id"] = detail.pop("message_id")
    response = {"status": "ok", "retcode": 0, "data": detail} if wrapped else detail
    plugin.ctx.api.call.side_effect = [response, {"retcode": 0, "data": {"url": "https://example.edu/math.ics"}}]
    monkeypatch.setattr(plugin_module, "read_upload", AsyncMock(return_value=ics()))
    command = reply_command()
    await plugin.command(message=command, text="/课表 解析")
    assert plugin.store.course_list(identity(command)[0])


def test_get_msg_sdk_and_onebot_wrappers_and_negative_ids():
    detail = snowluma_detail()
    detail["message_id"] = -2147483648
    response = {"success": True, "result": {"retcode": 0, "status": "ok", "data": detail}}
    current = reply_command()
    source = snowluma_source(response, current, "-2147483648")
    assert source["message_id"] == "-2147483648" and len(ics_files(source)) == 1
    detail["group_id"] = " 000123 "
    assert snowluma_source(detail, current, "-2147483648")["message_info"]["group_info"]["group_id"] == "123"


def test_conflicting_message_id_cannot_be_overridden_by_real_id():
    detail = snowluma_detail()
    detail["message_id"] = 999
    detail["real_id"] = 456
    with pytest.raises(ValueError, match="消息编号.*不一致"):
        snowluma_source(detail, reply_command(), "456")


@pytest.mark.parametrize("group,expected", [(999, "群号.*不一致"), (None, "缺少群号"), (0, "缺少群号")])
def test_group_checks_remain_strict(group, expected):
    detail = snowluma_detail()
    detail["group_id"] = group
    with pytest.raises(ValueError, match=expected):
        snowluma_source(detail, reply_command(), "456")


def test_failed_response_is_not_unwrapped_as_success():
    response = {"retcode": 100, "status": "failed", "data": snowluma_detail()}
    with pytest.raises(ValueError, match="查询失败"):
        snowluma_source(response, reply_command(), "456")


async def test_diagnostics_exclude_content_download_url_and_credentials(plugin, caplog):
    detail = snowluma_detail()
    detail["message_id"] = 999
    detail["message"][0]["data"].update(url="https://example.edu/download?token=secret", base64="private-file-bytes")
    detail["raw_message"] = "private-message-content"
    plugin.ctx.api.call.return_value = detail
    await plugin.command(message=reply_command(), text="/课表 解析")
    assert "引用校验失败" in caplog.text
    assert "message_id" in caplog.text and "999" in caplog.text
    assert all(x not in caplog.text for x in ("secret", "private-file-bytes", "private-message-content", "download?"))
    assert "real_id" in snowluma_summary(detail)


async def test_actual_host_error_shape_preserves_reason(plugin, caplog):
    # 用户实际日志：response 只有 error / success，不能再丢弃 error。
    error = "未找到 API 提供方插件: adapter.snowluma.message"
    plugin.ctx.api.call.return_value = {"success": False, "error": error}
    await plugin.command(message=reply_command(), text="/课表 解析")
    assert error in plugin.ctx.send.text.call_args.args[0]
    assert error in caplog.text
    summary = snowluma_summary({"success": False, "error": error})
    assert summary["error"] == error and summary["success"] is False
    assert not plugin.store.course_list(identity(reply_command())[0])


async def test_discover_registered_canonical_alias_in_same_connector(plugin):
    entries = plugin.ctx.api.list.return_value
    for entry in entries:
        entry["name"] = entry["name"].replace("adapter.snowluma.", "adapter.napcat.")
        entry["full_name"] = entry["full_name"].replace("adapter.snowluma.", "adapter.napcat.")
    plugin.ctx.api.call.return_value = snowluma_detail()
    result = await call_snowluma_api(plugin.ctx, "message.get_msg", message_id="456")
    assert result["message_id"] == 456
    plugin.ctx.api.list.assert_awaited_once_with(plugin_id="maibot-team.snowluma-adapter")
    plugin.ctx.api.call.assert_awaited_once_with(
        "maibot-team.snowluma-adapter.adapter.napcat.message.get_msg", version="1", message_id="456")


@pytest.mark.parametrize("change", [{"enabled": False}, {"public": False}, {"plugin_id": "unrelated.plugin"},
                                   {"version": "2"}])
async def test_discovery_rejects_wrong_provider_or_unavailable_api(plugin, change):
    for entry in plugin.ctx.api.list.return_value:
        entry.update(change)
    with pytest.raises(ValueError, match="未公开或未启用"):
        await call_snowluma_api(plugin.ctx, "message.get_msg", message_id="456")
    assert not plugin.ctx.api.call.called


async def test_discovery_permission_failure_not_hidden(plugin):
    plugin.ctx.api.list.return_value = {"success": False, "error": "api.list permission denied"}
    with pytest.raises(ValueError, match="api.list permission denied"):
        await call_snowluma_api(plugin.ctx, "message.get_msg", message_id="456")
    assert not plugin.ctx.api.call.called


def test_api_error_redacts_credentials_and_links():
    error = "连接失败 https://example.edu/path?token=secret token=secret2 Authorization: Bearer secret3"
    safe = safe_api_error(error)
    assert "连接失败" in safe
    assert all(value not in safe for value in ("secret", "secret2", "secret3", "https://"))
