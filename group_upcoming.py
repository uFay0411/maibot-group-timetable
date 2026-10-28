"""盘点本群每位成员在指定范围内的下一节课，按开课时刻排序。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import math

from .query import date_window, model_visible_content

PAGE_SIZE = 50


def group_upcoming(
    store,
    platform: str,
    group_id: str,
    stream: str,
    members: list[dict],
    timezone: str,
    now: datetime,
    period="未来7天",
    days=7,
    page=1,
) -> dict:
    if type(page) is not int or page < 1:
        raise ValueError("页码必须为1起的整数")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("查询需要带时区的当前时刻")
    zone = ZoneInfo(timezone)
    lower, upper, _ = date_window(period, days, now.astimezone(zone))
    lower = max(lower, now)
    if upper <= lower:
        raise ValueError("接下来课程的查询范围必须包含未来时间")
    live = {x["user_id"]: x for x in members}
    rows, covered, without, with_next = [], 0, [], set()
    for member in store.group_members(platform, group_id, stream):
        uid = member["user"]
        if uid not in live or not store.course_details(member["owner"]):
            continue
        covered += 1
        lessons = [x for x in store.lessons(member["owner"], lower, upper) if x.start > now]
        if not lessons:
            without.append({"user_id": uid, "member_name": live[uid]["name"]})
            continue
        earliest = min(x.start for x in lessons)
        next_lessons = [x for x in lessons if x.start == earliest]
        with_next.add(uid)
        for lesson in next_lessons:
            rows.append(
                {
                    "user_id": uid,
                    "member_name": live[uid]["name"],
                    "subject": lesson.title,
                    "classroom": lesson.room or None,
                    "start": lesson.start.astimezone(zone).isoformat(),
                    "end": lesson.end.astimezone(zone).isoformat(),
                    "course_id": lesson.course_id,
                    "lesson_id": lesson.token,
                    "conflicting_lessons": len(next_lessons) > 1,
                }
            )
    rows.sort(
        key=lambda r: (
            datetime.fromisoformat(r["start"]).timestamp(),
            datetime.fromisoformat(r["end"]).timestamp(),
            r["user_id"],
            r["course_id"],
        )
    )
    total = len(rows)
    without.sort(key=lambda r: r["user_id"])
    total_pages = max(1, math.ceil(max(total, len(without)) / PAGE_SIZE))
    if page > total_pages:
        raise ValueError(f"当前只有{total_pages}页，请重新查询")
    result = {
        "success": True,
        "query_type": "group_upcoming",
        "group_id": group_id,
        "queried_at": now.astimezone(zone).isoformat(),
        "timezone": timezone,
        "range_start": lower.astimezone(zone).isoformat(),
        "range_end": upper.astimezone(zone).isoformat(),
        "sort_by": "start_ascending",
        "group_member_count": len(live),
        "members_with_timetable": covered,
        "members_with_next": len(with_next),
        "members_without_upcoming_count": len(without),
        "members_without_upcoming": without[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
        "total": total,
        "lessons": rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
        "page": page,
        "page_size": PAGE_SIZE,
        "total_pages": total_pages,
        "has_more": page < total_pages,
        "next_page": page + 1 if page < total_pages else None,
        "content": "这些是本群已建立课表的现有成员在指定未来范围内各自的下一节课，按开始时间从早到晚。"
        "正在上的课不算下一节；范围内无后续课不代表以后都无课，未导入课表者无法判断。"
        "同一成员同一开始时间的冲突课程全部保留。保持顺序，通过reply自然盘点，用户要求图片时工具实际发送。",
    }
    result["content"] = model_visible_content(result)
    return result


def format_group_upcoming(result: dict) -> str:
    lines = [
        f"群友接下来课程 · {result['timezone']} · 按上课时间排序",
        f"范围：{result['range_start']} 至 {result['range_end']}（不含）",
        f"已建立课表{result['members_with_timetable']}/{result['group_member_count']}人；{result['members_with_next']}人有后续课程。",
    ]
    if result.get("roster_verified") is False:
        lines[2] = f"本群已建立课表{result['members_with_timetable']}人；{result['members_with_next']}人有后续课程。"
        lines.append(result["roster_note"])
    for row in result["lessons"]:
        start, end = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
        ending = end.strftime("%H:%M" if end.date() == start.date() else "%m-%d %H:%M")
        lines.append(
            f"{start:%m-%d %H:%M}–{ending} ｜{row['member_name']}({row['user_id']}) · {row['subject']} ｜{row['classroom'] or '教室未填写'}"
            + (" [时间冲突]" if row["conflicting_lessons"] else "")
        )
    lines.append(
        f"{result['members_without_upcoming_count']}名已建课表的群友在此范围内没有后续课程；未建课表者无法判断。"
    )
    if result["has_more"]:
        lines.append(f"第{result['page']}/{result['total_pages']}页，请继续查询第{result['next_page']}页。")
    return "\n".join(lines)
