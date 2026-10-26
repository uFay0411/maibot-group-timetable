"""虚构 WakeUp 课表与模拟官方网关；不包含真实用户的口令、课表或响应。"""

from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import base64
import copy
import json

import httpx
import pytest

import timetable_test_plugin as plugin_module
from timetable_test_plugin import TimetablePlugin, identity
from timetable_test_plugin import wakeup
from timetable_test_plugin import _wakeup_protocol as protocol
from timetable_test_plugin.calendar_engine import parse_calendar

from test_query import question_message
from test_timetable import dt, ics, lessons

CODE = "a" * 32


@pytest.fixture
def share():
    return [{"id": 1},
            [{"node": 1, "timeTable": 1, "startTime": "08:00", "endTime": "08:45"},
             {"node": 2, "timeTable": 1, "startTime": "08:55", "endTime": "09:40"}],
            {"id": 1, "tid": "synthetic-table", "timeTable": 1, "startDate": "2026-10-26"},
            [{"id": 1, "courseName": "测试数学"}],
            [{"id": 1, "day": 1, "startWeek": 1, "endWeek": 4, "type": 0, "startNode": 1, "step": 2,
              "ownTime": False, "room": "测试教室101", "teacher": "测试教师"}]]


@pytest.mark.parametrize("text", [CODE, CODE.upper(), f"分享口令为「{CODE}」", f"https://i.wakeup.fun/share_schedule/get?key={CODE}",
                                  f"这是来自「WakeUp课程表」的课表分享\n分享口令为「{CODE}」"])
def test_extract_plain_key_complete_text_and_link(text):
    assert wakeup.share_key(text) == CODE


@pytest.mark.parametrize("text", ["", "abc", "g" + CODE, CODE + "z", CODE[:-1], CODE + "b", CODE + " " + "b" * 32, None])
def test_invalid_ambiguous_or_partial_key_rejected(text):
    with pytest.raises(ValueError):
        wakeup.share_key(text)


def import_share(store, share):
    store.import_ics("alice", wakeup.to_ics(share, "Asia/Shanghai"), "合并")
    return lessons(store, "alice", "2026-10-26", 31)


def test_regular_schedule_uses_actual_node_times(store, share):
    rows = import_share(store, share)
    assert len(rows) == 4
    assert rows[0].start == dt() and rows[0].end == dt(clock="09:40")
    assert rows[0].title == "测试数学" and rows[0].room == "测试教室101"


@pytest.mark.parametrize("week_type,first_week,expected", [(1, 1, [1, 3]), (2, 1, [2, 4]), (1, 2, [3]), (2, 2, [2, 4])])
def test_odd_even_week_parity_is_absolute_semester_week(store, share, week_type, first_week, expected):
    share[4][0].update(type=week_type, startWeek=first_week)
    rows = import_share(store, share)
    assert [1 + (x.start.date() - dt().date()).days // 7 for x in rows] == expected


def test_custom_times_override_node_time_table(store, share):
    share[4][0].update(ownTime=True, startTime="14:15", endTime="16:05")
    rows = import_share(store, share)
    assert rows[0].start == dt(clock="14:15") and rows[0].end == dt(clock="16:05")


def test_unpadded_start_date_and_week_one_monday(store, share):
    share[2]["startDate"] = "2026-9-2"  # 周三属于从8月31日起的第一周。
    raw = wakeup.to_ics(share, "Asia/Shanghai")
    store.import_ics("alice", raw, "合并")
    assert lessons(store, "alice", "2026-08-31")[0].start == dt("2026-08-31")


def test_non_selected_time_tables_do_not_change_course_times(store, share):
    share[1].append({"node": 1, "timeTable": 2, "startTime": "12:00", "endTime": "12:45"})
    assert import_share(store, share)[0].start == dt()


def test_reshare_preserves_identity_and_local_adjustments(store, share):
    original = import_share(store, share)[0]
    store.change("alice", original, {"room": "本地调课教室"})
    changed = copy.deepcopy(share)
    changed[3][0]["courseName"] = "更新后的科目"
    changed[4][0].update(room="新的默认教室", endWeek=5)
    raw = wakeup.to_ics(changed, "Asia/Shanghai")
    store.import_ics("alice", raw, "合并")
    rows = lessons(store, "alice", "2026-10-26", 35)
    assert len(store.course_details("alice")) == 1 and len(rows) == 5
    assert rows[0].room == "本地调课教室" and rows[0].title == "更新后的科目"
    assert rows[1].room == "新的默认教室"


@pytest.mark.parametrize("mutate", [
    lambda x: x[2].pop("startDate"),
    lambda x: x[1].pop(),
    lambda x: x[3].clear(),
    lambda x: x[4][0].update(type=3),
    lambda x: x[4][0].update(day=8),
    lambda x: x[4][0].update(startWeek=True),
    lambda x: x[4][0].update(endWeek=105),
    lambda x: x[4][0].update(ownTime=True, startTime="15:00", endTime="14:00"),
    lambda x: x[4][0].update(ownTime=True, startTime="bad", endTime="15:00"),
    lambda x: x[4].append({**x[4][0], "room": "无法区分的另一间教室"}),
])
def test_bad_schedule_rejected_entirely(share, mutate):
    mutate(share)
    with pytest.raises(ValueError):
        wakeup.to_ics(share, "Asia/Shanghai")


def test_protocol_matches_frozen_original_upstream_vectors():
    assert protocol.native_des_encrypt("WakeUp protocol test", protocol.SIGN_A_KEY).hex() == (
        "8113976c79d7779ae72e94eb6f8ec5381af14080787503ae")
    key = protocol.native_get_key("450", "testtoken0")
    assert protocol.rc4_crypt(b"key=" + CODE.encode(), key).hex() == (
        "cc5351b03c000012c9b189b394dfd5259c6d56fff90c08aa2f4b681b0d051d22e714db91")


async def test_complete_v2_http_exchange_decrypts_share(monkeypatch, share):
    token = "testtoken0"
    paths = []

    def handler(request):
        assert request.url.host == "api.wakeup.fun"
        paths.append(request.url.path)
        form = parse_qs(request.content.decode())
        if request.url.path == "/pluto/app/antispam":
            plain = protocol.native_des_decrypt(protocol.native_hex_decode(form["data"][0]), protocol.SIGN_A_KEY).decode()
            rand = plain.split("##")[1]
            sign_b = protocol.native_hex_encode(protocol.native_des_encrypt(rand + "##" + token, rand[:5] + "#G4"))
            return httpx.Response(200, json={"errNo": 0, "data": {"data": sign_b}})
        assert request.url.path == "/share_schedule/getv2" and request.content.startswith(b"&data=")
        key = protocol.native_get_key(form["vc"][0], token)
        assert protocol.rc4_crypt(base64.b64decode(form["data"][0]), key) == ("key=" + CODE).encode()
        assert int(form["_t_"][0]) < 10**11  # 官方协议使用秒，不是毫秒。
        plain = json.dumps({"shareData": "\n".join(json.dumps(x, ensure_ascii=False) for x in share)}).encode()
        encrypted = base64.b64encode(protocol.rc4_crypt(plain, key)).decode()
        return httpx.Response(200, json={"errNo": 0, "data": {"data": encrypted}})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(wakeup.httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))
    assert await wakeup.fetch_schedule(CODE) == share
    assert paths == ["/pluto/app/antispam", "/share_schedule/getv2"]


@pytest.mark.parametrize("status,body", [(403, b"private response"), (302, b"redirect"), (200, b"not json"), (200, b"[]")])
async def test_http_errors_are_explicit_and_do_not_expose_response(status, body):
    transport = httpx.MockTransport(lambda _: httpx.Response(status, content=body))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError) as error:
            await wakeup.post_json(client, "/share_schedule/getv2", "private request", {})
    assert "private" not in str(error.value)


async def test_http_response_size_is_bounded(monkeypatch):
    monkeypatch.setattr(wakeup, "MAX_BYTES", 100)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 201))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="过大"):
            await wakeup.post_json(client, "/share_schedule/getv2", "", {})


@pytest.mark.parametrize("payload", [{"errNo": 1, "data": "sensitive"}, {"errNo": 0, "data": "bad-base64"},
                                     {"errNo": 0}, {"errNo": False}])
def test_expired_invalid_or_corrupted_response_does_not_leak_data(payload):
    with pytest.raises(ValueError) as error:
        wakeup.decode_response(payload, "test-key")
    assert "sensitive" not in str(error.value) and "bad-base64" not in str(error.value)


@pytest.fixture
def importer(plugin, share, monkeypatch):
    fetch = AsyncMock(return_value=share)
    monkeypatch.setattr(plugin_module, "fetch_wakeup_schedule", fetch)
    source = question_message()
    source["raw_message"] = [{"type": "text", "data": "/课表 wakeup 分享口令为「" + CODE + "」\n完整文案有单引号'"}]
    return plugin, source, fetch


async def test_command_imports_full_multiline_text_for_sender_only(importer):
    p, source, fetch = importer
    assert await p.command(message=source) == (True, None, True)
    owner = identity(source)[0]
    assert len(lessons(p.store, owner, "2026-10-26", 31)) == 4
    assert not lessons(p.store, "bob")
    fetch.assert_awaited_once()
    assert CODE not in p.ctx.send.text.call_args.args[0]
    assert p.store.import_latest(owner, "合并")[0] == 1


async def test_natural_import_reads_original_request_and_returns_model_visible_result(importer):
    p, source, fetch = importer
    p.ctx.message.get_by_id.return_value = source
    result = await p.import_wakeup_timetable(message_id="question-123", chat_id="group-stream")
    assert result["success"] and result["imported_arrangements"] == 1
    data = json.loads(result["content"].split("\n", 1)[1].split("\n回复要求：", 1)[0])
    assert data["source"] == "WakeUp" and data["mode"] == "合并"
    assert CODE not in result["content"] and not p.ctx.send.text.called
    assert CODE in fetch.call_args.args[0]


@pytest.mark.parametrize("failure", [ValueError("WakeUp 口令已过期"), TimeoutError()])
async def test_failed_replace_preserves_all_old_courses_and_upload(importer, failure):
    p, source, fetch = importer
    owner, _ = await p.register_member(source)
    p.store.import_ics(owner, ics(), "合并", upload_name="original.ics")
    first = lessons(p.store, owner)[0]
    p.store.change(owner, first, {"room": "保留本地修改"})
    fetch.side_effect = failure
    source["raw_message"] = [{"type": "text", "data": "/课表 wakeup 替换 " + CODE}]
    await p.command(message=source)
    assert lessons(p.store, owner)[0].room == "保留本地修改"
    assert "WakeUp" in p.ctx.send.text.call_args.args[0]
    assert p.store.import_latest(owner, "合并")[0] == 1
    assert lessons(p.store, owner)[0].title == "高等数学"


async def test_invalid_conversion_does_not_replace_old_schedule(importer, share):
    p, source, _ = importer
    owner, _ = await p.register_member(source)
    p.store.import_ics(owner, ics(), "合并")
    share[1].clear()
    p.ctx.message.get_by_id.return_value = source
    result = await p.import_wakeup_timetable(message_id="question-123", mode="替换", chat_id="group-stream")
    assert not result["success"] and lessons(p.store, owner)[0].title == "高等数学"


async def test_natural_import_rejects_foreign_request_before_fetch(importer):
    p, source, fetch = importer
    source["message_info"]["group_info"]["group_id"] = "999"
    p.ctx.message.get_by_id.return_value = source
    result = await p.import_wakeup_timetable(message_id="question-123", chat_id="group-stream", group_id="123")
    assert not result["success"] and not fetch.called


@pytest.mark.parametrize("prefix,suffix", [("替换 ", ""), ("", " 替换")])
async def test_explicit_replace_clears_old_courses_and_local_changes(importer, prefix, suffix):
    p, source, _ = importer
    owner, _ = await p.register_member(source)
    p.store.import_ics(owner, ics(), "合并")
    first = lessons(p.store, owner)[0]
    p.store.change(owner, first, {"cancelled": True})
    source["raw_message"] = [{"type": "text", "data": "/课表 wakeup " + prefix + CODE + suffix}]
    await p.command(message=source)
    assert len(p.store.course_details(owner)) == 1
    assert lessons(p.store, owner)[0].title == "测试数学"
    assert "替换导入" in p.ctx.send.text.call_args.args[0]


async def test_default_merge_keeps_existing_schedule(importer):
    p, source, _ = importer
    owner, _ = await p.register_member(source)
    p.store.import_ics(owner, ics(), "合并")
    first = lessons(p.store, owner)[0]
    p.store.change(owner, first, {"room": "保留本地教室"})
    await p.command(message=source)
    assert len(p.store.course_details(owner)) == 2
    assert any(x.room == "保留本地教室" for x in lessons(p.store, owner))


def test_natural_import_schema_never_accepts_model_supplied_token_or_identity():
    info = next(x for x in TimetablePlugin().get_components() if x["name"] == "import_wakeup_timetable")["metadata"]
    assert set(info["parameters_raw"]) == {"message_id", "mode", "target_member"}
    assert info["parameters_raw"]["message_id"]["required"]


def test_converted_calendar_has_no_share_code_and_can_be_read_by_ics_parser(share):
    raw = wakeup.to_ics(share, "Asia/Shanghai")
    assert CODE.encode() not in raw
    courses, _ = parse_calendar(raw, "Asia/Shanghai")
    assert len(courses) == 1
