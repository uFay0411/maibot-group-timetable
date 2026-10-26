"""批量变更草稿：全部验证后预览，确认时在同一 SQLite 事务内提交。"""

from datetime import datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

import json
import re
import secrets
import time

from .calendar_engine import parse_calendar
from .management import clock_value, lesson_data, target_day
from .query import model_visible_content

MAX_ITEMS = 500
DRAFT_TTL = 1800


def fingerprint(db, owner: str) -> str:
    data = [
        [tuple(row) for row in db.execute(sql, (owner,))]
        for sql in (
            "SELECT zone,stream,user FROM members WHERE owner=?",
            "SELECT id,ics,patch FROM courses WHERE owner=? ORDER BY id",
            "SELECT id,original,data FROM changes WHERE owner=? ORDER BY id,original",
        )
    ]
    return sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def normalized_courses(courses: list, zone: str, now: datetime) -> list[dict]:
    if not isinstance(courses, list) or not 1 <= len(courses) <= MAX_ITEMS:
        raise ValueError(f"批量加课需要1~{MAX_ITEMS}条课程安排")
    result, seen = [], set()
    for item in courses:
        if not isinstance(item, dict) or set(item) - {
            "date",
            "start_time",
            "end_time",
            "subject",
            "classroom",
            "weeks",
        }:
            raise ValueError("课程字段只支持date/start_time/end_time/subject/classroom/weeks")
        value = {key: item.get(key, "") for key in ("date", "start_time", "end_time", "subject", "classroom")}
        if any(not isinstance(v, str) or len(v) > 512 for v in value.values()):
            raise ValueError("课程字段必须为不超过512字的文本")
        if not all(value[key].strip() for key in ("date", "start_time", "end_time", "subject")):
            raise ValueError("缺少日期、上下课时间或科目；请先补充，不能猜测节次时间")
        weeks = item.get("weeks", 1)
        if type(weeks) is not int or not 1 <= weeks <= 104:
            raise ValueError("周数必须为1~104整数")
        day = target_day(value["date"], now)
        start = datetime.combine(day, clock_value(value["start_time"]), ZoneInfo(zone))
        end = datetime.combine(day, clock_value(value["end_time"]), ZoneInfo(zone))
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间；跨午夜课程请改用ICS明确日期")
        value.update(
            date=day.isoformat(), subject=value["subject"].strip(), classroom=value["classroom"].strip(), weeks=weeks
        )
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def courses_ics(courses: list[dict], zone: str, request_key: str) -> bytes:
    calendar = Calendar()
    calendar.add("VERSION", "2.0")
    calendar.add("PRODID", "-//Wu//MaiBot Timetable Batch//CN")
    for value in courses:
        event = Event()
        uid = sha256((request_key + json.dumps(value, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()
        event.add("UID", "batch-" + uid)
        for field, key in (("SUMMARY", "subject"), ("LOCATION", "classroom")):
            event.add(field, value[key])
        for field, key in (("DTSTART", "start_time"), ("DTEND", "end_time")):
            event.add(field, datetime.fromisoformat(value["date"] + "T" + value[key]).replace(tzinfo=ZoneInfo(zone)))
        if value["weeks"] > 1:
            event.add("RRULE", {"FREQ": "WEEKLY", "COUNT": value["weeks"]})
        calendar.add_component(event)
    return calendar.to_ical()


def prepare_batch(
    store,
    actor: str,
    target: dict,
    message_id: str,
    action: str,
    *,
    courses=None,
    course_ids=None,
    lesson_ids=None,
    mode="合并",
    now=None,
) -> dict:
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 512:
        raise ValueError("批量操作必须提供真实请求消息编号")
    if action not in {"加课", "删课", "停课"} or mode not in {"合并", "替换"}:
        raise ValueError("批量操作支持加课、删课、停课；导入模式为合并或替换")
    if action != "加课" and mode != "合并":
        raise ValueError("替换模式仅适用于加课或文字图片导入")
    if (action != "加课" and courses) or (action != "删课" and course_ids) or (action != "停课" and lesson_ids):
        raise ValueError("批量操作包含不适用字段，未生成草稿")
    owner, stream = target["owner"], target["stream"]
    zone = store.get_member(owner)["zone"]
    now = now.astimezone(ZoneInfo(zone)) if now else datetime.now(ZoneInfo(zone))
    payload = {"action": action, "mode": mode, "timezone": zone}
    # 在读取候选前保存版本；期间发生的并发改动会使草稿在确认时失效。
    with store.connect() as db:
        baseline = fingerprint(db, owner)
    if action == "加课":
        rows = normalized_courses(courses, zone, now)
        raw = courses_ics(rows, zone, owner + message_id)
        parsed, _ = parse_calendar(raw, zone)
        payload.update(courses=rows, ics=parsed)
    else:
        ids = course_ids if action == "删课" else lesson_ids
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= MAX_ITEMS
            or any(not isinstance(x, str) or not x or len(x) > 512 for x in ids)
        ):
            raise ValueError(f"需要1~{MAX_ITEMS}个查询得到的真实编号")
        ids = list(dict.fromkeys(ids))
        if action == "删课":
            catalog = {x["course_id"]: x for x in store.course_details(owner)}
            if any(cid not in catalog for cid in ids):
                raise ValueError("包含无效课程编号；请先查询目标成员课表，整批未写入")
            rows = [catalog[cid] for cid in ids]
            payload["course_ids"] = ids
        else:
            lessons = [store.select(owner, lid) for lid in ids]
            rows = [lesson_data(x) for x in lessons]
            payload["changes"] = [
                {"course_id": x.course_id, "original": x.original, "source_start": x.start.isoformat()} for x in lessons
            ]
    token = secrets.token_urlsafe(18)
    public = {
        "success": True,
        "status": "pending_confirmation",
        "draft_token": token,
        "action": action,
        "mode": mode,
        "target_member": {"user_id": target["user_id"], "name": target["name"]},
        "timezone": zone,
        "items": rows,
        "count": len(rows),
        "expires_in_seconds": DRAFT_TTL,
        "content": "仅生成预览，尚未增删课程。请展示完整待处理条目与目标成员。"
        "替换模式会移除目标成员旧课程和调课。要求用户在新消息中明确确认后，"
        "再调用confirm_timetable_batch并回传draft_token；不能在当前请求自行确认。",
    }
    payload["preview"] = public
    with store.connect() as db:
        db.execute("DELETE FROM batch_drafts WHERE expires<?", (time.time(),))
        if db.execute("SELECT count(*) FROM batch_drafts WHERE actor=?", (actor,)).fetchone()[0] >= 20:
            raise ValueError("待处理草稿过多，请先确认/取消已有草稿或等待过期")
        db.execute(
            "INSERT INTO batch_drafts(token,actor,owner,stream,message_id,expires,baseline,payload,state) VALUES(?,?,?,?,?,?,?,?, 'pending')",
            (
                token,
                actor,
                owner,
                stream,
                message_id,
                time.time() + DRAFT_TTL,
                baseline,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    public["content"] = model_visible_content(public)
    return public


def read_draft(store, token: str, actor: str, stream: str) -> dict:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", token):
        raise ValueError("请提供预览返回的完整草稿编号")
    with store.connect() as db:
        row = db.execute("SELECT * FROM batch_drafts WHERE token=?", (token,)).fetchone()
    if row is None or row["actor"] != actor or row["stream"] != stream or row["expires"] < time.time():
        raise ValueError("草稿已过期、不存在或不属于当前提问者和群；请重新预览")
    return dict(row)


def apply_batch(store, token: str, actor: str, stream: str, message_id: str) -> dict:
    if not isinstance(message_id, str) or not message_id.strip():
        raise ValueError("确认操作必须提供新的真实消息编号")
    draft = read_draft(store, token, actor, stream)
    if message_id == draft["message_id"]:
        raise ValueError("必须由用户在新的消息中明确确认，不能在生成预览的同一请求执行")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        draft = dict(db.execute("SELECT * FROM batch_drafts WHERE token=?", (token,)).fetchone())
        if draft["expires"] < time.time():
            raise ValueError("草稿已过期，请重新预览")
        if draft["state"] == "applied":
            result = json.loads(draft["result"])
            result["already_applied"] = True
            result["content"] = model_visible_content(result)
            return result
        if draft["state"] != "pending":
            raise ValueError("草稿已取消，未修改课表")
        owner = draft["owner"]
        if fingerprint(db, owner) != draft["baseline"]:
            raise ValueError("预览后课表或时区已变化，请重新生成预览；整批未写入")
        payload = json.loads(draft["payload"])
        action = payload["action"]
        if action == "加课":
            if payload["mode"] == "替换":
                db.execute("DELETE FROM courses WHERE owner=?", (owner,))
                db.execute("DELETE FROM changes WHERE owner=?", (owner,))
                db.execute("DELETE FROM uploads WHERE owner=?", (owner,))
            for cid, ics in payload["ics"].items():
                db.execute(
                    "INSERT INTO courses(owner,id,ics) VALUES(?,?,?) ON CONFLICT(owner,id) DO NOTHING",
                    (owner, cid, ics),
                )
        elif action == "删课":
            for cid in payload["course_ids"]:
                old = json.loads(
                    db.execute("SELECT patch FROM courses WHERE owner=? AND id=?", (owner, cid)).fetchone()[0]
                )
                old["deleted"] = True
                db.execute(
                    "UPDATE courses SET patch=? WHERE owner=? AND id=?",
                    (json.dumps(old, ensure_ascii=False), owner, cid),
                )
        else:
            for item in payload["changes"]:
                args = (owner, item["course_id"], item["original"])
                row = db.execute("SELECT data FROM changes WHERE owner=? AND id=? AND original=?", args).fetchone()
                data = json.loads(row[0]) if row else {}
                data.setdefault("source_start", item["source_start"])
                data["cancelled"] = True
                db.execute(
                    "INSERT OR REPLACE INTO changes VALUES(?,?,?,?)", (*args, json.dumps(data, ensure_ascii=False))
                )
        result = {
            **payload["preview"],
            "status": "applied",
            "already_applied": False,
            "content": "用户已确认，全部条目在一个事务内完成。删课是整门课程停用，可用恢复指令撤销；停课仅影响列出的课次。请按实际目标与条目自然回复。",
        }
        result["content"] = model_visible_content(result)
        db.execute(
            "UPDATE batch_drafts SET state='applied',result=? WHERE token=?",
            (json.dumps(result, ensure_ascii=False), token),
        )
        return result


def cancel_batch(store, token: str, actor: str, stream: str):
    read_draft(store, token, actor, stream)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = db.execute("SELECT state FROM batch_drafts WHERE token=?", (token,)).fetchone()[0]
        if state == "applied":
            raise ValueError("草稿已经执行，不能取消；请通过课表维护撤销相应课程")
        db.execute("UPDATE batch_drafts SET state='cancelled' WHERE token=? AND state='pending'", (token,))
    return {"success": True, "status": "cancelled", "content": "已取消草稿，未修改课表。"}


def format_preview(result: dict) -> str:
    lines = [
        f"待确认 · {result['target_member']['name']} · {result['action']} {result['count']}条 · {result['mode']} · {result['timezone']}"
    ]
    if result["mode"] == "替换":
        lines.append("注意：确认后将替换旧课程与本地调课。")
    for index, item in enumerate(result["items"], 1):
        if "date" in item:
            detail = f"{item['date']} {item['start_time']}–{item['end_time']} · 每周1次共{item['weeks']}次"
        elif "start" in item:
            detail = item["start"] + " · " + item["lesson_id"]
        else:
            detail = "整门课（所有周） · " + item["course_id"]
        lines.append(f"{index}. {item['subject']} · {item.get('classroom') or '教室未填写'} · {detail}")
    lines += ["尚未写入，30分钟内有效。", f"/课表 确认 {result['draft_token']}", f"/课表 取消 {result['draft_token']}"]
    return "\n".join(lines)
