from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import base64
import json

import pytest

from timetable_test_plugin import TimetablePlugin, identity
from timetable_test_plugin.calendar_engine import MAX_BYTES, expand, parse_calendar, stamp
from timetable_test_plugin.files import read_upload
from timetable_test_plugin.store import Store

ZONE = "Asia/Shanghai"


def dt(day="2026-10-26", clock="08:00"):
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=ZoneInfo(ZONE))


def ics(extra="", summary="高等数学", room="教一101", uid="math", start="20261026T080000",
        end="20261026T094000", tz=";TZID=Asia/Shanghai"):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//tests//CN\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTART{tz}:{start}\r\nDTEND{tz}:{end}\r\n"
            f"SUMMARY:{summary}\r\nLOCATION:{room}\r\n{extra}END:VEVENT\r\nEND:VCALENDAR\r\n").encode()


def lessons(store, owner="alice", day="2026-10-26", days=1):
    lower = dt(day, "00:00")
    return store.lessons(owner, lower, lower + timedelta(days=days))


def test_recurrence_exdate_rdate_and_folded_utf8():
    raw = ics("RRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE;TZID=Asia/Shanghai:20261102T080000\r\n"
              "RDATE;TZID=Asia/Shanghai:20261104T080000\r\n", summary="高等\r\n 数学")
    courses, skipped = parse_calendar(raw, ZONE)
    expanded = expand(next(iter(courses)), next(iter(courses.values())), dt("2026-10-26", "00:00"),
                      dt("2026-11-10", "00:00"), ZONE)
    assert skipped == 0
    assert [x.start.strftime("%m-%d") for x in expanded] == ["10-26", "11-04", "11-09"]
    assert all(x.title == "高等数学" for x in expanded)


def test_utc_and_floating_are_correct():
    for raw in (ics(start="20261026T000000Z", end="20261026T014000Z", tz=""), ics(tz="")):
        courses, _ = parse_calendar(raw, ZONE)
        found = expand("cid", next(iter(courses.values())), dt(clock="00:00"), dt("2026-10-27", "00:00"), ZONE)
        assert found[0].start == dt()


def test_recurrence_id_reschedule_and_cancellation():
    raw = ics("RRULE:FREQ=WEEKLY;COUNT=3\r\n")
    override = ("BEGIN:VEVENT\r\nUID:math\r\nRECURRENCE-ID;TZID=Asia/Shanghai:20261102T080000\r\n"
                "DTSTART;TZID=Asia/Shanghai:20261103T100000\r\nDTEND;TZID=Asia/Shanghai:20261103T114000\r\n"
                "SUMMARY:高等数学\r\nLOCATION:教二202\r\nEND:VEVENT\r\n"
                "BEGIN:VEVENT\r\nUID:math\r\nRECURRENCE-ID;TZID=Asia/Shanghai:20261109T080000\r\n"
                "STATUS:CANCELLED\r\nEND:VEVENT\r\n")
    raw = raw.replace(b"END:VCALENDAR", override.encode() + b"END:VCALENDAR")
    courses, _ = parse_calendar(raw, ZONE)
    found = expand("cid", next(iter(courses.values())), dt(clock="00:00"), dt("2026-11-11", "00:00"), ZONE)
    assert [x.start.strftime("%m-%d %H:%M") for x in found] == ["10-26 08:00", "11-03 10:00"]
    assert found[1].room == "教二202"
    assert found[1].original == stamp(dt("2026-11-02"))


def test_member_group_platform_isolation(store):
    store.import_ics("alice", ics(), "合并")
    store.import_ics("bob", ics(summary="大学英语", room="教三303"), "合并")
    assert lessons(store)[0].title == "高等数学"
    assert lessons(store, "bob")[0].room == "教三303"
    cid = lessons(store)[0].course_id
    store.patch("bob", cid, {"deleted": True})
    assert len(lessons(store)) == 1
    assert not lessons(store, "bob")
    message = {"platform": "qq", "session_id": "real-stream", "message_info": {
        "group_info": {"group_id": "123"}, "user_info": {"user_id": "100"}}}
    first = identity(message)[0]
    message["message_info"]["group_info"]["group_id"] = "456"
    assert identity(message)[0] != first
    message["message_info"]["group_info"]["group_id"] = "123"
    message["platform"] = "telegram"
    assert identity(message)[0] != first


def test_atomic_import_merge_replace_and_restore(store):
    store.import_ics("alice", ics(), "合并")
    first = lessons(store)[0]
    store.change("alice", first, {"cancelled": True})
    store.import_ics("alice", ics(), "合并")
    assert not lessons(store)
    store.restore("alice", first.course_id)
    assert len(lessons(store)) == 1
    with pytest.raises(ValueError):
        store.import_ics("alice", b"not an ics", "替换")
    assert len(lessons(store)) == 1
    store.import_ics("alice", ics(summary="英语", uid="english"), "合并")
    assert len(lessons(store)) == 2
    store.import_ics("alice", ics(summary="英语", uid="english"), "替换")
    assert [x.title for x in lessons(store)] == ["英语"]


def test_move_across_query_window_and_second_move(store):
    store.import_ics("alice", ics("RRULE:FREQ=WEEKLY;COUNT=3\r\n"), "合并")
    first = lessons(store)[0]
    store.change("alice", first, {"start": stamp(dt("2026-11-20", "10:00")),
                                  "end": stamp(dt("2026-11-20", "11:40")), "room": "教二202"})
    assert not lessons(store)
    assert len(lessons(store, day="2026-11-02")) == 1
    moved = lessons(store, day="2026-11-20")[0]
    assert moved.original == first.original and moved.room == "教二202"
    selected = store.select("alice", moved.token)
    store.change("alice", selected, {"start": stamp(dt("2026-11-21", "10:00")),
                                     "end": stamp(dt("2026-11-21", "11:40"))})
    assert not lessons(store, day="2026-11-20")
    assert lessons(store, day="2026-11-21")[0].room == "教二202"


def test_local_move_of_ics_recurrence_override(store):
    raw = ics("RRULE:FREQ=WEEKLY;COUNT=2\r\n")
    override = ("BEGIN:VEVENT\r\nUID:math\r\nRECURRENCE-ID;TZID=Asia/Shanghai:20261102T080000\r\n"
                "DTSTART;TZID=Asia/Shanghai:20261103T100000\r\nDTEND;TZID=Asia/Shanghai:20261103T114000\r\n"
                "SUMMARY:高等数学\r\nLOCATION:教二202\r\nEND:VEVENT\r\n")
    store.import_ics("alice", raw.replace(b"END:VCALENDAR", override.encode() + b"END:VCALENDAR"), "合并")
    changed = lessons(store, day="2026-11-03")[0]
    store.change("alice", changed, {"start": stamp(dt("2026-12-01", "10:00")),
                                     "end": stamp(dt("2026-12-01", "11:40"))})
    assert not lessons(store, day="2026-11-03")
    assert lessons(store, day="2026-12-01")[0].room == "教二202"


def test_invalid_and_all_day_files(store):
    with pytest.raises(ValueError):
        parse_calendar(b"x" * (MAX_BYTES + 1), ZONE)
    all_day = ics().replace(b"DTSTART;TZID=Asia/Shanghai:20261026T080000", b"DTSTART;VALUE=DATE:20261026")
    with pytest.raises(ValueError, match="没有带具体时间"):
        parse_calendar(all_day, ZONE)
    for raw in (ics(end="20261026T070000"), ics("RRULE:FREQ=MINUTELY\r\n")):
        with pytest.raises(ValueError):
            store.import_ics("alice", raw, "合并")


async def test_exact_20_minutes_and_persistent_dedup(plugin):
    plugin.store.import_ics("alice", ics(), "合并")
    await plugin.tick(dt(clock="07:39:59"))
    assert not plugin.ctx.send.hybrid.called
    await plugin.tick(dt(clock="07:40"))
    plugin.ctx.maisaka.reply.generate.assert_awaited_once()
    call = plugin.ctx.send.hybrid.call_args
    assert call.args[0][0] == {"type": "at", "data": {"target_user_id": "100"}}
    assert call.args[0][1]["data"].startswith("高等数学")
    assert call.args[1] == "group-stream"
    plugin.store = Store(plugin.store.path)  # 模拟重启后打开同一数据库。
    await plugin.tick(dt(clock="07:40:15"))
    assert plugin.ctx.send.hybrid.await_count == 1
    assert plugin.store.delivery_status("alice") == {"sent": 1}


async def test_disabled_stale_and_catchup(plugin):
    plugin.store.import_ics("alice", ics(), "合并")
    plugin.store.setting("alice", "enabled", 0)
    await plugin.tick(dt(clock="07:40"))
    assert not plugin.ctx.send.hybrid.called
    plugin.store.setting("alice", "enabled", 1)
    await plugin.tick(dt(clock="07:44"))
    assert not plugin.ctx.send.hybrid.called
    await plugin.tick(dt(clock="07:42"))
    assert plugin.ctx.send.hybrid.await_count == 1


async def test_missing_facts_retries_without_template(plugin):
    plugin.store.import_ics("alice", ics(), "合并")
    plugin.ctx.maisaka.reply.generate.return_value = {"success": True, "text": "该上课啦"}
    await plugin.tick(dt(clock="07:40"))
    assert not plugin.ctx.send.hybrid.called
    assert plugin.store.delivery_status("alice") == {}
    plugin.ctx.maisaka.reply.generate.return_value = {"success": True, "text": "08:00 高等数学 教一101 快去！"}
    await plugin.tick(dt(clock="07:41"))
    assert plugin.ctx.send.hybrid.await_count == 1


async def test_edit_during_generation_cancels_stale_reminder(plugin):
    plugin.store.import_ics("alice", ics(), "合并")
    first = lessons(plugin.store)[0]

    async def generate(*args, **kwargs):
        plugin.store.change("alice", first, {"cancelled": True})
        return {"success": True, "text": "08:00 高等数学 教一101 快去！"}
    plugin.ctx.maisaka.reply.generate.side_effect = generate
    await plugin.tick(dt(clock="07:40"))
    assert not plugin.ctx.send.hybrid.called
    assert plugin.store.delivery_status("alice") == {}


async def test_send_unknown_not_replayed(plugin):
    plugin.store.import_ics("alice", ics(), "合并")
    plugin.ctx.send.hybrid.side_effect = TimeoutError("response lost")
    await plugin.tick(dt(clock="07:40"))
    await plugin.tick(dt(clock="07:41"))
    assert plugin.ctx.send.hybrid.await_count == 1
    assert plugin.store.delivery_status("alice") == {"pending": 1}


async def test_restart_during_generation_is_recoverable(plugin):
    plugin.store.import_ics("alice", ics(), "合并")
    assert plugin.store.claim("interrupted", "alice", dt(clock="07:40").timestamp())
    assert plugin.store.delivery_status("alice") == {"generating": 1}
    plugin.store = Store(plugin.store.path)
    assert plugin.store.delivery_status("alice") == {}
    await plugin.tick(dt(clock="07:41"))
    assert plugin.ctx.send.hybrid.await_count == 1


async def test_real_sdk_context_rpc_payloads(plugin):
    from maibot_sdk.context import PluginContext
    calls = []

    async def rpc(method, plugin_id, payload, **kwargs):
        calls.append((method, {"plugin_id": plugin_id, "payload": payload, **kwargs}))
        capability = payload["capability"]
        if capability == "maisaka.reply.generate":
            return {"success": True, "text": "高等数学 08:00 教一101 该出发啦"}
        return {"success": True, "sent": True, "message_id": "sdk-message"}

    plugin._set_context(PluginContext("wu.group-timetable", rpc_call=rpc))
    plugin.store.import_ics("alice", ics(), "合并")
    await plugin.tick(dt(clock="07:40"))
    assert [x[1]["payload"]["capability"] for x in calls] == ["maisaka.reply.generate", "send.hybrid"]
    assert plugin.store.delivery_status("alice") == {"sent": 1}


async def test_send_rejected_retries(plugin):
    plugin.store.import_ics("alice", ics(), "合并")
    plugin.ctx.send.hybrid.return_value = {"sent": False}
    await plugin.tick(dt(clock="07:40"))
    assert plugin.store.delivery_status("alice") == {}
    plugin.ctx.send.hybrid.return_value = {"sent": True, "message_id": "m2"}
    await plugin.tick(dt(clock="07:41"))
    assert plugin.store.delivery_status("alice") == {"sent": 1}


def test_commands_add_move_cancel_restore_clear(plugin):
    plugin.execute("alice", ["加课", "2026-10-26", "08:00", "09:40", "高等数学", "教一101", "3"])
    first = lessons(plugin.store)[0]
    assert len(lessons(plugin.store, days=22)) == 3
    plugin.execute("alice", ["调课", first.token, "2026-10-27", "10:00", "11:40", "教二202"])
    moved = lessons(plugin.store, day="2026-10-27")[0]
    plugin.execute("alice", ["停课", moved.token])
    assert not lessons(plugin.store, day="2026-10-27")
    plugin.execute("alice", ["恢复", first.course_id])
    assert lessons(plugin.store)[0].room == "教一101"
    plugin.execute("alice", ["改课", first.course_id, "线性代数", "教三303"])
    assert lessons(plugin.store)[0].title == "线性代数"
    plugin.execute("alice", ["删课", first.course_id])
    assert not lessons(plugin.store)
    plugin.execute("alice", ["清空", "确认"])
    assert not lessons(plugin.store)


async def test_file_base64_and_file_id_error():
    assert await read_upload({"name": "math.ics", "base64": base64.b64encode(ics()).decode()}, []) == ics()
    with pytest.raises(ValueError, match="未提供"):
        await read_upload({"name": "math.ics", "file_id": "123"}, [])
    with pytest.raises(ValueError, match="Base64"):
        await read_upload({"name": "math.ics", "base64": "!!!!"}, [])


async def test_http_file_download_and_private_host_restriction(monkeypatch):
    import httpx
    from timetable_test_plugin import files
    client = httpx.AsyncClient

    async def transport(request):
        assert str(request.url) == "https://files.example.edu/math.ics"
        return httpx.Response(200, content=ics())

    monkeypatch.setattr(files.socket, "getaddrinfo", lambda *args, **kwargs:
                        [(2, 1, 6, "", ("1.1.1.1", 443))])
    monkeypatch.setattr(files.httpx, "AsyncClient", lambda **kwargs:
                        client(transport=httpx.MockTransport(transport), **kwargs))
    assert await read_upload({"name": "math.ics", "url": "https://files.example.edu/math.ics"}, []) == ics()
    monkeypatch.setattr(files.socket, "getaddrinfo", lambda *args, **kwargs:
                        [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="内网"):
        await read_upload({"name": "math.ics", "url": "https://files.example.edu/math.ics"}, [])


async def test_hook_imports_the_actual_sender(plugin):
    message = {"platform": "qq", "session_id": "real-stream", "message_info": {
        "group_info": {"group_id": "123"}, "user_info": {"user_id": "100", "user_cardname": "Alice"}},
        "raw_message": [{"type": "file", "data": {"name": "math.ics",
                         "base64": base64.b64encode(ics()).decode()}}]}
    await plugin.receive_ics(message)
    owner = identity(message)[0]
    assert lessons(plugin.store, owner)[0].title == "高等数学"
    assert not lessons(plugin.store, "bob")


def test_sdk_components_and_manifest():
    from pathlib import Path
    p = TimetablePlugin()
    components = p.get_components()
    assert {(x["type"], x["name"]) for x in components} == {
        ("COMMAND", "timetable"), ("TOOL", "query_timetable"),
        ("TOOL", "manage_timetable"), ("HOOK_HANDLER", "receive_ics"),
        ("TOOL", "import_wakeup_timetable"),
        ("TOOL", "import_ics_timetable"),
        ("TOOL", "query_group_current_courses"),
    }
    manifest = json.loads((Path(__file__).parents[1] / "_manifest.json").read_text(encoding="utf-8"))
    assert "maisaka.reply.generate" in manifest["capabilities"]
    assert p.build_default_config()["timetable"]["default_timezone"] == ZONE


def test_default_config_repairs_versionless_install():
    from pathlib import Path
    import tomllib

    p = TimetablePlugin()
    with pytest.raises(ValueError, match="plugin.config_version"):
        p.normalize_plugin_config({"plugin": {"enabled": True}})
    template = Path(__file__).parents[1] / "config.default.toml"
    data = tomllib.loads(template.read_text(encoding="utf-8"))
    p.set_plugin_config(data)
    assert p.config.plugin.config_version == "1.0.0"
    assert p.config.plugin.enabled is True
    assert p.get_plugin_config_data() == p.build_default_config()
