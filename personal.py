"""同一用户的多群课表实时只读合并视图。"""

from dataclasses import replace
from datetime import datetime
from typing import Any, List
from hashlib import sha256

import asyncio
import json
import unicodedata

from .calendar_engine import Lesson, stamp
from .store import Store
from .group_access import member_info


def parts(owner: str) -> List[str]:
    value = json.loads(owner)
    if not isinstance(value, list) or len(value) != 3 or any(not isinstance(x, str) for x in value):
        raise ValueError("无效课表归属")
    return value


def normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def lesson_key(platform: str, user: str, lesson: Lesson) -> str:
    # 同科目、同上下课时刻视为一节；教室差异合并并明确提示冲突，不能导致多次提醒。
    return sha256(
        json.dumps(
            [platform, user, normalize(lesson.title), stamp(lesson.start), stamp(lesson.end)], ensure_ascii=False
        ).encode()
    ).hexdigest()


async def accessible_members(ctx: Any, store: Store, platform: str, user: str) -> List[dict]:
    result = []
    for member in await asyncio.to_thread(store.user_members, platform, user):
        _, group, _ = parts(member["owner"])
        if group:
            try:
                await member_info(ctx, group, user)
            except ValueError:
                continue
        result.append(member)
    return result


class MergedView:
    """只读实时视图，不复制或截断原来的重复规则，不改动源群课表。"""

    def __init__(self, store: Store, owner: str, members: List[dict], zone: str):
        self.store, self.owner, self.members, self.zone = store, owner, members, zone
        self.sources = {}
        self.skipped = 0

    def get_member(self, owner: str) -> dict:
        if owner != self.owner:
            raise ValueError("合并视图不属于当前用户")
        return {"owner": owner, "zone": self.zone}

    def course_details(self, owner: str) -> List[dict]:
        self.get_member(owner)
        result = []
        for member in self.members:
            for course in self.store.course_details(member["owner"]):
                cid = sha256((member["owner"] + "|" + course["course_id"]).encode()).hexdigest()[:24]
                result.append({**course, "course_id": cid})
        return result

    def lessons(self, owner: str, lower: datetime, upper: datetime, *, overlapping: bool = False) -> List[Lesson]:
        platform, _, user = parts(owner)
        self.get_member(owner)
        grouped = {}
        for member in self.members:
            for lesson in self.store.lessons(member["owner"], lower, upper, overlapping=overlapping):
                cid = sha256((member["owner"] + "|" + lesson.course_id).encode()).hexdigest()[:24]
                key = lesson_key(platform, user, lesson)
                grouped.setdefault(key, []).append((replace(lesson, course_id=cid), member, lesson))
        rows = []
        for items in grouped.values():
            representative = items[0][0]
            rooms = sorted({x[0].room for x in items if x[0].room})
            merged = replace(representative, room=" / ".join(rooms) + ("（来源教室冲突）" if len(rooms) > 1 else ""))
            self.sources[merged.token] = [
                {
                    "group_id": parts(member["owner"])[1] or None,
                    "course_id": original.course_id,
                    "lesson_id": original.token,
                    "classroom": original.room,
                    "source": parts(member["owner"])[1] or "个人",
                }
                for _, member, original in items
            ]
            rows.append(merged)
        return sorted(rows, key=lambda x: (x.start, x.course_id))

    def annotate(self, result: dict) -> dict:
        result["merged"] = True
        result["source_count"] = len(self.members)
        result["unverified_source_count"] = self.skipped
        if self.skipped:
            result["content"] += f" 有{self.skipped}个来源无法验证，未参与；不能推断这些来源没有课程。"
        for row in result.get("lessons", []):
            row["sources"] = self.sources.get(row["lesson_id"], [])
        result["content"] += (
            " 这是当前用户的多群及个人课表实时合并视图，同一课次已去重。教室冲突如实显示。维护须先切换来源，再查询该来源真实编号；合并编号不能用于修改源课程。"
        )
        return result
