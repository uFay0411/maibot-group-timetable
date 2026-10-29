"""重现官方带图命令无聊天上下文时的取图失败，并验证帮助/版本真实PNG。"""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

import base64
import copy
import json

import pytest

from timetable_test_plugin import image_cache as cache_module
from timetable_test_plugin.image_cache import InboundImages
from timetable_test_plugin.info_cards import info_request, version_text
from timetable_test_plugin.presentation import render_info

from test_batch_input import setup_recognition
from test_group_images import group as group
from test_query import natural_tool as natural_tool


def image_message(source):
    message = copy.deepcopy(source)
    output = BytesIO()
    Image.new("RGB", (320, 180), "white").save(output, format="PNG")
    message["raw_message"] = [{"type": "image", "data": "[图片]", "hash": "actual-image-hash",
        "binary_data_base64": base64.b64encode(output.getvalue()).decode()},
        {"type": "text", "data": "/课表 图片导入 首周2026-09-07，周一1-2节08:00-09:40，第1至8周"}]
    return message


async def test_current_official_image_command_works_without_maisaka_history(group):
    p, _, actor, _, _, source = group
    setup_recognition(p)
    full = image_message(source)
    full["message_info"]["additional_config"] = {"qq_official_group_openid": "A"*32}
    await p.capture_timetable_images(message=full)
    command = copy.deepcopy(full)
    command["raw_message"][0].pop("binary_data_base64")  # Host命令序列化的实际行为。
    p.ctx.message.get_by_id.return_value = None  # 命令尚未落库。
    await p.command(message=command)
    assert p.ctx.llm.generate.await_count == 1
    content = p.ctx.llm.generate.call_args.kwargs
    assert content["task_name"] == "vlm" and "data:image/jpeg;base64," in str(content["prompt"])
    assert not p.ctx.maisaka.context.resolve_image.called
    assert "确认" in p.ctx.send.text.call_args.args[0]
    assert len(p.store.course_details(actor)) == 1  # 只创建待确认草稿。


async def test_existing_image_can_be_read_from_message_api_before_context(group):
    p, _, actor, _, _, source = group
    setup_recognition(p)
    full = image_message(source)
    command = copy.deepcopy(full)
    command["raw_message"][0].pop("binary_data_base64")
    p.ctx.message.get_by_id.return_value = full
    await p.command(message=command)
    p.ctx.message.get_by_id.assert_awaited_once_with("question-123", stream_id="group-stream", include_binary_data=True)
    assert p.ctx.llm.generate.called and not p.ctx.maisaka.context.resolve_image.called


async def test_original_message_identity_checked_before_using_reloaded_image(group):
    p, _, actor, _, _, source = group
    setup_recognition(p)
    full = image_message(source)
    command = copy.deepcopy(full)
    command["raw_message"][0].pop("binary_data_base64")
    full["message_info"]["user_info"]["user_id"] = "999"
    p.ctx.message.get_by_id.return_value = full
    await p.command(message=command)
    assert not p.ctx.llm.generate.called and "不一致" in p.ctx.send.text.call_args.args[0]


async def test_quoted_image_cache_is_used_only_for_valid_original(group):
    p, context, actor, _, _, source = group
    setup_recognition(p)
    original = image_message(source)
    original["message_id"] = "original-picture"
    await p.capture_timetable_images(message=original)
    original["raw_message"][0].pop("binary_data_base64")
    source["raw_message"] = [{"type": "reply", "data": {"target_message_id": "original-picture"}},
                              {"type": "text", "data": "导入这张课表"}]
    p.ctx.message.get_by_id.side_effect = lambda mid, **kw: original if mid == "original-picture" else source
    result = await p.parse_timetable_input(message_id="question-123", **context)
    assert result["success"] and result["status"] == "pending_confirmation"
    assert p.ctx.llm.generate.called and not p.ctx.maisaka.context.resolve_image.called


def test_image_cache_expiry_identity_hash_and_byte_budget(group, monkeypatch):
    _, _, _, _, _, source = group
    now = [0]
    monkeypatch.setattr(cache_module, "monotonic", lambda: now[0])
    cache = InboundImages()
    full = image_message(source)
    cache.remember(full)
    thin = copy.deepcopy(full)
    thin["raw_message"][0].pop("binary_data_base64")
    assert cache.restore(thin)["raw_message"][0].get("binary_data_base64")
    wrong = copy.deepcopy(thin)
    wrong["message_info"]["group_info"]["group_id"] = "other-group"
    assert not cache.restore(wrong)["raw_message"][0].get("binary_data_base64")
    wrong = copy.deepcopy(thin)
    wrong["raw_message"][0]["hash"] = "other-image"
    assert not cache.restore(wrong)["raw_message"][0].get("binary_data_base64")
    now[0] = cache.TTL+1
    assert not cache.restore(thin)["raw_message"][0].get("binary_data_base64") and not cache.entries
    cache.MAX_BYTES = 10
    cache.remember(full)
    assert not cache.entries


@pytest.mark.parametrize("body", ["/课表", "/课表 帮助", "/课表 版本", "/课表 更新日志"])
async def test_help_and_version_default_to_actual_png(group, body):
    p, _, _, _, _, source = group
    source["raw_message"] = [{"type": "text", "data": body}]
    await p.command(message=source)
    assert p.ctx.send.hybrid.called and not p.ctx.send.text.called
    raw = base64.b64decode(p.ctx.send.hybrid.call_args.args[0][0]["content"])
    with Image.open(BytesIO(raw)) as image:
        assert image.format == "PNG" and image.width == 1080 and image.height <= 2100


async def test_version_text_and_selected_changelog_are_copyable(group):
    p, _, _, _, _, source = group
    source["raw_message"] = [{"type": "text", "data": "/课表 版本 文字"}]
    await p.command(message=source)
    text = p.ctx.send.text.call_args.args[0]
    root = Path(cache_module.__file__).parent
    version = json.loads((root/"_manifest.json").read_text(encoding="utf-8"))["version"]
    assert "当前版本："+version in text and "SDK：2.16.0～2.16.99" in text
    assert "# 1.10.4" in version_text("1.10.4")
    with pytest.raises(ValueError, match="没有这个版本"):
        version_text("9.99.9")
    assert not p.ctx.send.hybrid.called


def test_card_pagination_keeps_last_lines_and_default_help_is_one_image(monkeypatch):
    title, text, picture = info_request("", "full-help")
    assert picture and len(render_info(title, text)) == 1
    drawn = []
    draw_text = ImageDraw.ImageDraw.text
    def recording(draw, xy, text, *args, **kwargs):
        drawn.append(text)
        return draw_text(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", recording)
    pages = render_info("更新日志", "\n".join(f"line-{i}" for i in range(120)))
    assert len(pages) == 3 and "line-119" in drawn


async def test_failed_info_image_is_reported_without_success_claim(group):
    p, _, _, _, _, source = group
    source["raw_message"] = [{"type": "text", "data": "/课表 版本"}]
    p.ctx.send.hybrid.return_value = {"sent": False}
    await p.command(message=source)
    assert "发送未成功" in p.ctx.send.text.call_args.args[0]
