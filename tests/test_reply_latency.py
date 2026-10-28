"""复现真实 Replyer 约 376 秒后才完成、原 90 秒 RPC 已放弃的问题。"""

from unittest.mock import AsyncMock

import pytest
import timetable_test_plugin as plugin_module

from maibot_sdk.context import PluginContext
from test_timetable import dt, ics


@pytest.mark.parametrize("elapsed,expected_sent", [(376, True), (1201, False)])
async def test_slow_host_reply_waits_and_rechecks_course_start(plugin, monkeypatch, elapsed, expected_sent):
    clock = [0.0]
    monkeypatch.setattr(plugin_module, "monotonic", lambda: clock[0])
    sent = AsyncMock(return_value={"sent": True, "message_id": "slow-reply"})

    async def rpc(method, plugin_id, payload, **kwargs):
        if payload["capability"] == "maisaka.reply.generate":
            # 宿主用 120s 表达选择、240s 回复首选模型，随后备用模型成功。
            if kwargs["timeout_ms"] < 376000:
                raise TimeoutError("RPC expired before Host Replyer completed")
            clock[0] += elapsed
            return {"success": True, "text": "高等数学 08:00 教一101，该出发了"}
        return await sent()

    plugin._set_context(PluginContext("wu.group-timetable", rpc_call=rpc))
    plugin.store.import_ics("alice", ics(), "合并")
    await plugin.tick(dt(clock="07:40"))
    await plugin.tick(dt(clock="07:40:15"))
    assert sent.await_count == int(expected_sent)
    assert plugin.store.delivery_status("alice") == ({"sent": 1} if expected_sent else {})
