"""使用与 Host 相同的 package spec 加载插件，测试不启动机器人或模型。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import importlib.util
import logging
import sys

import pytest

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("timetable_test_plugin", root / "plugin.py",
                                            submodule_search_locations=[str(root)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


@pytest.fixture
def store(tmp_path):
    store_type = importlib.import_module("timetable_test_plugin.store").Store
    instance = store_type(tmp_path / "timetable.sqlite3")
    instance.member("alice", "group-stream", "100", "Alice", "Asia/Shanghai")
    instance.member("bob", "group-stream", "200", "Bob", "Asia/Shanghai")
    return instance


@pytest.fixture
def plugin(store):
    p = module.TimetablePlugin()
    p.set_plugin_config(p.build_default_config())
    p.store = store
    p._ctx = SimpleNamespace(logger=logging.getLogger("test"),
                             config=SimpleNamespace(get=AsyncMock(return_value=[])),
                             maisaka=SimpleNamespace(reply=SimpleNamespace(generate=AsyncMock(return_value={
                                 "success": True, "text": "高等数学要上课啦，08:00 在教一101，准备出发！"}))),
                             message=SimpleNamespace(get_by_id=AsyncMock(return_value=None)),
                             api=SimpleNamespace(call=AsyncMock(return_value=None), list=AsyncMock(return_value=[
                                 {"name": "adapter.snowluma." + operation,
                                  "full_name": "maibot-team.snowluma-adapter.adapter.snowluma." + operation,
                                  "plugin_id": "maibot-team.snowluma-adapter", "version": "1",
                                  "public": True, "enabled": True}
                                 for operation in ("message.get_msg", "file.get_group_file_url",
                                                   "group.get_group_member_info", "group.get_group_member_list")
                             ])),
                             send=SimpleNamespace(hybrid=AsyncMock(return_value={"sent": True, "message_id": "m1"}),
                                                  text=AsyncMock()))
    return p
