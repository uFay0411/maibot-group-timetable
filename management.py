"""自然语言维护工具：仅接受查询得到的真实编号，复用课表存储与修改规则。"""

from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

import json
import re

from .calendar_engine import Lesson, parse_calendar, stamp
from .query import date_window, model_visible_content
from .store import Store


def lesson_data(lesson: Lesson) -> dict[str, Any]:
    return {"lesson_id": lesson.token, "course_id": lesson.course_id, "subject": lesson.title,
            "classroom": lesson.room or None, "start": lesson.start.isoformat(), "end": lesson.end.isoformat()}


def add_course(store: Store, owner: str, start: datetime, end: datetime, subject: str, classroom: str,
               weeks: int, uid: str = "") -> str:
    if type(weeks) is not int or not 1 <= weeks <= 104:
        raise ValueError("周数应为 1~104 的整数")
    calendar, event = Calendar(), Event()
    calendar.add("VERSION", "2.0")
    calendar.add("PRODID", "-//Wu//MaiBot Timetable//CN")
    event.add("UID", uid or f"manual-{uuid4().hex}")
    for field, value in (("DTSTART", start), ("DTEND", end), ("SUMMARY", subject), ("LOCATION", classroom)):
        event.add(field, value)
    if weeks > 1:
        event.add("RRULE", {"FREQ": "WEEKLY", "COUNT": weeks})
    calendar.add_component(event)
    raw = calendar.to_ical()
    cid = next(iter(parse_calendar(raw, store.get_member(owner)["zone"])[0]))
    store.import_ics(owner, raw, "合并")
    return cid


def clock_value(value: str):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("时间必须为 HH:MM，例如 08:00；无法确定时间时请先询问用户")
    return datetime.strptime(value, "%H:%M").time()


def target_day(value: str, now: datetime):
    lower, upper, _ = date_window(value, 1, now)
    if upper - lower != timedelta(days=1):
        raise ValueError("维护课表必须指定单个日期，不能使用本周、下一节等范围")
    return lower.date()


def manage_schedule(store: Store, owner: str, action: str, *, message_id: str, lesson_id: str = "",
                    course_id: str = "", date: str = "", start_time: str = "", end_time: str = "",
                    subject: str = "", classroom: str = "", weeks: int = 1,
                    now: datetime | None = None) -> dict[str, Any]:
    fields = {"action": action, "message_id": message_id, "lesson_id": lesson_id, "course_id": course_id,
              "date": date, "start_time": start_time, "end_time": end_time, "subject": subject, "classroom": classroom}
    if any(not isinstance(value, str) or len(value) > 512 for value in fields.values()):
        raise ValueError("维护参数必须是长度不超过512的文本")
    if any(value and not value.strip() for value in fields.values()):
        raise ValueError("维护字段不能只包含空白字符")
    if not message_id.strip():
        raise ValueError("修改课表必须提供本次用户请求的 message_id")
    if type(weeks) is not int or not 1 <= weeks <= 104:
        raise ValueError("周数应为 1~104 的整数")
    # 拒绝错误组合，避免模型以为修改了实际被忽略的字段。
    allowed = {"加课": {"date", "start_time", "end_time", "subject", "classroom"},
               "调课": {"lesson_id", "date", "start_time", "end_time", "classroom"},
               "停课": {"lesson_id"}, "改课": {"course_id", "subject", "classroom"},
               "删课": {"course_id"}, "恢复": {"course_id"}, "课程列表": {"subject"}}
    if action not in allowed:
        raise ValueError("操作请使用 加课、调课、停课、改课、删课、恢复、课程列表")
    if any(value and key not in allowed[action] for key, value in fields.items() if key not in {"action", "message_id"}):
        raise ValueError(f"{action}包含不适用的字段，请按工具说明传参，不会忽略这些字段执行")
    if action != "加课" and weeks != 1:
        raise ValueError("只有加课支持 weeks；批量调课或停课请先明确每个课次")
    zone = ZoneInfo(store.get_member(owner)["zone"])
    now = now.astimezone(zone) if now is not None else datetime.now(zone)
    result = {"success": True, "action": action, "timezone": str(zone)}
    if action == "课程列表":
        courses = store.course_details(owner)
        if subject:
            courses = [x for x in courses if subject.casefold() in x["subject"].casefold()]
        result.update(courses=courses[:100], total=len(courses), truncated=len(courses) > 100,
                      content="这是当前成员的课程列表，包含停用课程。只按用户明确要求维护，不能猜测课程编号。")
    elif action == "加课":
        if not all((date, start_time, end_time, subject.strip())):
            raise ValueError("加课需要日期、开始时间、结束时间和科目；缺少信息请先询问用户")
        day = target_day(date, now)
        start = datetime.combine(day, clock_value(start_time), zone)
        end = datetime.combine(day, clock_value(end_time), zone)
        if end <= start:
            raise ValueError("下课时间必须晚于上课时间")
        # 同一消息、相同加课参数重试时使用同一 UID，避免模型重复调用加出两门相同课程。
        request = json.dumps([owner, message_id, start.isoformat(), end.isoformat(), subject, classroom, weeks],
                             ensure_ascii=False, separators=(",", ":"))
        uid = "manual-tool-" + sha256(request.encode()).hexdigest()
        existing = {x["course_id"] for x in store.course_details(owner)}
        cid = add_course(store, owner, start, end, subject.strip(), classroom.strip(), weeks, uid)
        after = [lesson_data(x) for x in store.lessons(owner, start, start + timedelta(seconds=1)) if x.course_id == cid]
        result.update(scope="whole_course", after=after, course_id=cid, weeks=weeks, reused_existing_course=cid in existing,
                      content="加课请求已保存；after 为首次课的当前真实明细，weeks 为每周次数。"
                      "reused_existing_course 为真时是同一请求重试，未重复加课；若 after 为空，首次课已另行调整或停用。"
                      "请通过 reply 自然说明实际结果。")
    elif action in {"调课", "停课"}:
        if not lesson_id:
            raise ValueError("请先调用 query_timetable 查到用户指定的课次，再填写真实 lesson_id；不明确时先询问")
        lesson = store.select(owner, lesson_id)
        result.update(scope="single_lesson", before=lesson_data(lesson))
        if action == "停课":
            store.change(owner, lesson, {"cancelled": True})
            result.update(after=None, content="仅这个课次已停课，其他周不变。请通过 reply 说明实际日期和科目。")
        else:
            if not any((date, start_time, end_time, classroom)):
                raise ValueError("调课需要新的日期、时间或教室；缺少信息请先询问用户")
            day = target_day(date, now) if date else lesson.start.date()
            start = datetime.combine(day, clock_value(start_time), zone) if start_time else lesson.start.replace(
                year=day.year, month=day.month, day=day.day)
            end = (datetime.combine(day, clock_value(end_time), zone) if end_time
                   else start + (lesson.end - lesson.start))
            if end <= start:
                raise ValueError("下课时间必须晚于上课时间")
            data = {"start": stamp(start), "end": stamp(end)}
            if classroom:
                data["room"] = classroom.strip()
            store.change(owner, lesson, data)
            after = next(x for x in store.lessons(owner, start, start + timedelta(seconds=1))
                         if x.course_id == lesson.course_id and x.original == lesson.original)
            result.update(after=lesson_data(after), content="仅这个课次已调整，其他周不变。未指定结束时间时保留原时长；"
                          "请通过 reply 按 before 和 after 自然说明变化，后续修改使用 after.lesson_id。")
    else:
        if not course_id:
            raise ValueError("请先查询课表或课程列表取得真实 course_id；只有用户明确要求整门课时才执行")
        before = next((x for x in store.course_details(owner) if x["course_id"] == course_id), None)
        if before is None:
            raise ValueError("找不到你在本群的课程编号")
        if action == "改课":
            patch = {}
            if subject.strip():
                patch["title"] = subject.strip()
            if classroom.strip():
                patch["room"] = classroom.strip()
            if not patch:
                raise ValueError("改课需要新的科目名称或教室")
            store.patch(owner, course_id, patch)
        elif action == "删课":
            store.patch(owner, course_id, {"deleted": True})
        else:
            store.restore(owner, course_id)
        after = next(x for x in store.course_details(owner) if x["course_id"] == course_id)
        result.update(scope="whole_course", before=before, after=after,
                      content="已按用户要求维护整门课。改课只改默认字段，已有单次调课仍优先；删课为停用，可恢复；"
                      "恢复会撤销整门课的全部本地修改和停课。请通过 reply 如实说明作用范围。")
    result["content"] = model_visible_content(result)
    return result
