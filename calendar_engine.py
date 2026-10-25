"""保留原始重复规则，按查询窗口展开；不把无限重复课表截断为固定学期。"""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

from icalendar import Calendar

import recurring_ical_events

UTC = timezone.utc
MAX_BYTES = 2 * 1024 * 1024


def aware(value: datetime, zone: str) -> datetime:
    return value.replace(tzinfo=ZoneInfo(zone)) if value.tzinfo is None else value


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class Lesson:
    course_id: str
    original: str
    title: str
    room: str
    start: datetime
    end: datetime

    @property
    def token(self) -> str:
        return f"{self.course_id}@{self.start:%Y%m%dT%H%M}"


def parse_calendar(raw: bytes, zone: str) -> Tuple[Dict[str, str], int]:
    """整个文件验证成功后才能入库；全天事项跳过，错误课程拒绝整次导入。"""
    if len(raw) > MAX_BYTES:
        raise ValueError("ICS 文件不能超过 2 MiB")
    ZoneInfo(zone)
    try:
        calendar = Calendar.from_ical(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("无法解析 ICS，请上传 UTF-8 编码的 iCalendar 文件") from exc
    if calendar.name != "VCALENDAR":
        raise ValueError("文件不是 VCALENDAR")
    groups = {}
    skipped = 0
    for event in calendar.walk("VEVENT"):
        if event.errors:
            raise ValueError(f"课程字段无效：{event.errors}")
        uid = str(event.get("UID", "")).strip()
        if not uid:
            raise ValueError("课程缺少 UID")
        if str(event.get("STATUS", "")).upper() != "CANCELLED":
            start = event.decoded("DTSTART", None)
            if isinstance(start, date) and not isinstance(start, datetime):
                skipped += 1
                continue
            if not isinstance(start, datetime) or not str(event.get("SUMMARY", "")).strip():
                raise ValueError("课程缺少有效的开始时间或科目名称")
            # 浮动时间按成员时区解释；显式 UTC / TZID 保留。
            if start.tzinfo is None and event["DTSTART"].params.get("TZID"):
                raise ValueError(f"无法识别时区：{event['DTSTART'].params['TZID']}")
            for field in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                value = event.decoded(field, None)
                if isinstance(value, datetime) and value.tzinfo is None:
                    event[field].dt = aware(value, zone)
                    event[field].params["TZID"] = zone
            end = event.decoded("DTEND", None)
            duration = event.decoded("DURATION", None)
            if end is None and duration is None:
                raise ValueError(f"{event['SUMMARY']} 缺少结束时间或 DURATION")
            if end is not None and aware(end, zone) <= aware(start, zone):
                raise ValueError("下课时间必须晚于上课时间")
            if duration is not None and duration <= timedelta(0):
                raise ValueError("课程时长必须大于零")
        groups.setdefault(uid, []).append(event)
    if not groups:
        raise ValueError("文件中没有带具体时间的课程（全天事项不作为课程）")
    if sum(map(len, groups.values())) > 5000:
        raise ValueError("一个文件最多包含 5000 个课程事件")
    result = {}
    for uid, events in groups.items():
        # 常见取消实例只带 UID / RECURRENCE-ID，展开库仍要求 DTSTART。
        # 补回原始时刻只供计算，STATUS 保持 CANCELLED，最终永不输出。
        for event in events:
            if str(event.get("STATUS", "")).upper() == "CANCELLED" and "DTSTART" not in event:
                original = event.decoded("RECURRENCE-ID", None)
                if isinstance(original, datetime):
                    event.add("DTSTART", aware(original, zone))
                    event.add("DTEND", aware(original, zone) + timedelta(minutes=1))
        item = Calendar()
        item.add("VERSION", "2.0")
        item.add("PRODID", "-//Wu//MaiBot Timetable//CN")
        for tz in calendar.walk("VTIMEZONE"):
            item.add_component(tz)
        for event in events:
            item.add_component(event)
        # 与导入顺序无关，重新上传同 UID 不会不断创建副本。
        result[sha256(uid.encode()).hexdigest()[:12]] = item.to_ical().decode()
    return result, skipped


def expand(course_id: str, ics: str, lower: datetime, upper: datetime, zone: str) -> List[Lesson]:
    calendar = Calendar.from_ical(ics)
    if all(str(e.get("STATUS", "")).upper() == "CANCELLED" for e in calendar.walk("VEVENT")):
        return []
    events = recurring_ical_events.of(calendar).between(lower, upper)
    lessons = []
    for event in events:
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start = aware(event.decoded("DTSTART"), zone).astimezone(ZoneInfo(zone))
        end = aware(event.decoded("DTEND"), zone).astimezone(ZoneInfo(zone))
        if not lower <= start < upper:
            continue
        original = aware(event.decoded("RECURRENCE-ID", event.decoded("DTSTART")), zone)
        lessons.append(Lesson(course_id, stamp(original), str(event.get("SUMMARY", "")),
                              str(event.get("LOCATION", "")), start, end))
    if len(lessons) > 2000:
        raise ValueError("查询窗口内课程过多，请检查 ICS 重复规则")
    return lessons


def apply_change(lesson: Lesson, change: dict, zone: str) -> Lesson:
    return replace(lesson, title=change.get("title", lesson.title), room=change.get("room", lesson.room),
                   start=datetime.fromisoformat(change["start"]).astimezone(ZoneInfo(zone))
                   if "start" in change else lesson.start,
                   end=datetime.fromisoformat(change["end"]).astimezone(ZoneInfo(zone))
                   if "end" in change else lesson.end)
