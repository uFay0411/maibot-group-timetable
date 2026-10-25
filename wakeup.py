"""WakeUp 分享口令取课与五段分享数据转 ICS；网络目标固定，不记录口令。"""

from datetime import datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

import asyncio
import base64
import json
import re

import httpx

from . import _wakeup_protocol as protocol
from .calendar_engine import MAX_BYTES

API_HOST = "https://api.wakeup.fun"
# 从 WakeUpDecoder 固定版本附带的 6.1.70 APK 提取的公开兼容参数；不含用户凭证。
APP = {"package": "com.suda.yzune.wakeupschedule", "version_name": "6.1.70", "version_code": 450,
       "channel": "100271a", "public_token": "1_XPXQH3c5HRPtFHkSwi3sCCURmT25QfxM",
       "signature_chars_md5": "318c6d4f74655d4f032fb0466bcfdfbc"}


def share_key(text: str) -> str:
    if not isinstance(text, str) or len(text) > 10000:
        raise ValueError("请粘贴 WakeUp 分享口令或完整分享文案（最多10000字）")
    keys = set(re.findall(r"(?<![A-Za-z0-9])([a-fA-F0-9]{32})(?![A-Za-z0-9])", text))
    keys = {x.lower() for x in keys}
    if len(keys) != 1:
        raise ValueError("请只提供一条完整的32位 WakeUp 分享口令，也可以粘贴完整分享文案")
    return keys.pop()


def common_params():
    android_id = "0000000000000000"
    cuid = protocol.cuid_from_android_id(android_id)
    adid = protocol.adid_from_android_id(android_id)
    params = [("area", ""), ("screensize", "1080x2400"), ("cuid", cuid), ("os", "android"), ("city", ""),
              ("abis", "arm64-v8a"), ("channel", APP["channel"]), ("appBit", "64"), ("vc", str(APP["version_code"])),
              ("deviceId", ""), ("token", APP["public_token"]), ("adid", adid), ("province", ""),
              ("pkgName", APP["package"]), ("appId", "wakeup"), ("download_type", "1"),
              ("vcname", APP["version_name"]), ("sdk", "35"), ("device", "Pixel 7"), ("brand", "google"),
              ("operatorid", "")]
    return params, cuid, adid


def antispam_request(cuid: str, params: list):
    rand = protocol.generate_rand10()
    plain = f"{protocol.MAGIC}##{rand}##{APP['signature_chars_md5']}##{cuid}"
    sign = protocol.native_hex_encode(protocol.native_des_encrypt(plain, protocol.SIGN_A_KEY))
    return protocol.build_antispam_request(sign, params)["body"], rand


async def post_json(client: httpx.AsyncClient, path: str, body: str, headers: dict) -> dict:
    try:
        async with client.stream("POST", API_HOST + path, content=body.encode(), headers=headers) as response:
            if response.status_code != 200:
                raise ValueError(f"WakeUp 接口返回 HTTP {response.status_code}，未修改课表")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BYTES * 2:
                    raise ValueError("WakeUp 响应过大，未修改课表")
    except httpx.HTTPError as exc:
        # 不转发包含请求正文、签名和口令的底层异常。
        raise ValueError(f"WakeUp 连接失败（{type(exc).__name__}），请稍后重试，未修改课表") from None
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError):
        raise ValueError("WakeUp 未返回有效 JSON，请稍后重试或使用 ICS，未修改课表") from None
    if not isinstance(payload, dict):
        raise ValueError("WakeUp 响应格式异常，未修改课表")
    return payload


def read_token(payload: dict, rand: str) -> str:
    sign = protocol.extract_sign_b(payload)
    if not sign or len(sign) > 4096 or len(sign) % 32 or not re.fullmatch(r"[0-9a-fA-F]+", sign):
        raise ValueError("WakeUp 接口验证失败，请稍后重试或使用 ICS，未修改课表")
    try:
        token, _ = protocol.token_from_sign_b(sign, rand)
    except (ValueError, IndexError):
        raise ValueError("WakeUp 验证响应不匹配，未修改课表") from None
    return token


def decode_response(payload: dict, key: str) -> list:
    if type(payload.get("errNo")) is not int or payload["errNo"] != 0:
        raise ValueError("WakeUp 拒绝分享请求，口令可能已过期；请重新生成口令，未修改课表")
    encrypted = payload.get("data")
    if isinstance(encrypted, dict):
        encrypted = encrypted.get("data")
    if not isinstance(encrypted, str) or not encrypted:
        raise ValueError("WakeUp 未返回分享数据，口令可能已失效，未修改课表")
    try:
        plain = protocol.rc4_crypt(base64.b64decode(encrypted, validate=True), key)
        decoded = json.loads(plain)
        text = decoded.get("shareData") if isinstance(decoded, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ValueError
        return share_parts(text)
    except (ValueError, UnicodeError, IndexError):
        raise ValueError("WakeUp 分享数据无法解码或已失效，请重新分享，未修改课表") from None


def share_parts(text: str) -> list:
    if len(text.encode()) > MAX_BYTES:
        raise ValueError("WakeUp 分享数据过大")
    parts = [json.loads(line) for line in text.splitlines() if line.strip()]
    if len(parts) != 5 or not all(isinstance(parts[i], dict if i in {0, 2} else list) for i in range(5)):
        raise ValueError("WakeUp 分享数据应为五段完整课表")
    if any(len(parts[i]) > 5000 for i in (1, 3, 4)):
        raise ValueError("WakeUp 课程或时间表过多")
    return parts


async def fetch_schedule(text: str) -> list:
    code = share_key(text)
    params, cuid, adid = common_params()
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "User-Agent": "okhttp/4.12.0",
               "Accept-Encoding": "gzip, deflate", "na__zyb_source__": "wakeup", "zyb-cuid": cuid, "zyb-adid": adid}
    body, rand = await asyncio.to_thread(antispam_request, cuid, params)
    async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10), follow_redirects=False) as client:
        anti = await post_json(client, "/pluto/app/antispam", body, headers)
        token = await asyncio.to_thread(read_token, anti, rand)
        request = await asyncio.to_thread(protocol.build_share_request, code, token, APP["version_code"], params)
        response = await post_json(client, "/share_schedule/getv2", request["body"], headers)
    return await asyncio.to_thread(decode_response, response, request["rc4_key"])


def number(value, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"WakeUp {name}无效，应为{lower}~{upper}的整数")
    return value


def identifier(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip() or len(str(value)) > 128:
        raise ValueError("WakeUp 课程或课表编号无效")
    return str(value)


def clock(value) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", value):
        raise ValueError("WakeUp 缺少有效作息时间，不能猜测上课时间")
    return time(*[int(x) for x in value.split(":")])


def to_ics(parts: list, zone: str) -> bytes:
    """有限周数使用 RDATE 保留准确单双周；所有字段验证完后才允许持久化。"""
    if not isinstance(parts, list) or len(parts) != 5:
        raise ValueError("WakeUp 分享数据不完整")
    header, slots, settings, definitions, arrangements = parts
    if not all(isinstance(x, dict) for x in (header, settings)) or not all(
            isinstance(x, list) and len(x) <= 5000 and all(isinstance(row, dict) for row in x)
            for x in (slots, definitions, arrangements)):
        raise ValueError("WakeUp 分享数据字段格式无效")
    try:
        first_day = datetime.strptime(settings["startDate"], "%Y-%m-%d").date()
    except (KeyError, TypeError, ValueError):
        raise ValueError("WakeUp 缺少有效开学日期，不能推测课程周数") from None
    monday = first_day - timedelta(days=first_day.weekday())
    tz = ZoneInfo(zone)
    table_id = identifier(settings.get("tid") or settings.get("id"))
    time_table = settings.get("timeTable", header.get("id"))
    nodes = {}
    for row in slots:
        if row.get("timeTable", time_table) != time_table:
            continue
        node = number(row.get("node"), "节次", 1, 100)
        if node in nodes:
            raise ValueError("WakeUp 同一时间表存在重复节次")
        nodes[node] = row
    courses = {}
    for row in definitions:
        cid = identifier(row.get("id"))
        if cid in courses or not isinstance(row.get("courseName"), str) or not row["courseName"].strip():
            raise ValueError("WakeUp 课程定义重复或缺少科目名称")
        courses[cid] = row["courseName"].strip()
    calendar = Calendar()
    calendar.add("VERSION", "2.0")
    calendar.add("PRODID", "-//Wu//WakeUp Timetable Import//CN")
    seen = {}
    for row in arrangements:
        cid = identifier(row.get("id"))
        if cid not in courses:
            raise ValueError("WakeUp 课程安排引用了不存在的科目")
        day = number(row.get("day"), "星期", 1, 7)
        begin_week = number(row.get("startWeek"), "开始周", 1, 104)
        end_week = number(row.get("endWeek"), "结束周", begin_week, 104)
        week_type = number(row.get("type", 0), "单双周类型", 0, 2)
        own = row.get("ownTime", False)
        if type(own) is not bool:
            raise ValueError("WakeUp 自定义时间标记无效")
        start_node = number(row.get("startNode"), "开始节次", 1, 100)
        step = number(row.get("step"), "连续节数", 1, 100)
        if own:
            begin, end = clock(row.get("startTime")), clock(row.get("endTime"))
        else:
            end_node = start_node + step - 1
            if any(node not in nodes for node in range(start_node, end_node + 1)):
                raise ValueError("WakeUp 时间表缺少课程对应的节次，未导入任何课程")
            begin, end = clock(nodes[start_node].get("startTime")), clock(nodes[end_node].get("endTime"))
        if end <= begin:
            raise ValueError("WakeUp 下课时间必须晚于上课时间")
        room, teacher = row.get("room", ""), row.get("teacher", "")
        if not isinstance(room, str) or not isinstance(teacher, str):
            raise ValueError("WakeUp 教室或教师字段无效")
        weeks = [week for week in range(begin_week, end_week + 1)
                 if week_type == 0 or (week_type == 1 and week % 2 == 1) or (week_type == 2 and week % 2 == 0)]
        if not weeks:
            continue
        # 不使用分享口令作为 UID；重复分享同一安排能更新，并保留现有本地修改。
        key = json.dumps([table_id, cid, day, start_node, step, begin_week, week_type,
                          begin.isoformat() if own else ""], separators=(",", ":"))
        starts = [datetime.combine(monday + timedelta(weeks=week - 1, days=day - 1), begin, tz) for week in weeks]
        event = Event()
        event.add("UID", "wakeup-" + sha256(key.encode()).hexdigest())
        event.add("SUMMARY", courses[cid])
        event.add("DTSTART", starts[0])
        event.add("DTEND", datetime.combine(starts[0].date(), end, tz))
        if len(starts) > 1:
            event.add("RDATE", starts[1:])
        event.add("LOCATION", room)
        if teacher:
            event.add("DESCRIPTION", "教师：" + teacher)
        encoded = event.to_ical()
        if key in seen:
            if seen[key] != encoded:
                raise ValueError("WakeUp 存在无法区分的重复课程安排，请检查原课表")
            continue
        seen[key] = encoded
        calendar.add_component(event)
    if not seen:
        raise ValueError("WakeUp 分享中没有有效的课程安排，未修改课表")
    raw = calendar.to_ical()
    if len(raw) > MAX_BYTES:
        raise ValueError("转换后的课表超过2 MiB，未修改课表")
    return raw
