"""同一用户的多群课表合并视图与 QQ 私聊路由验证。"""

from dataclasses import replace
from datetime import datetime
from typing import Any, List
from hashlib import sha256

import asyncio
import json
import unicodedata

from .calendar_engine import Lesson, stamp
from .store import Store
from .group_access import member_info, unpack
from .imports import call_snowluma_api, protocol_id


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


def temporary_permission(info: dict, group: str) -> bool | None:
    if not isinstance(info, dict) or protocol_id(info.get("group_id")) != group:
        raise ValueError("群详情的群号与来源不一致，不能确认临时会话权限")
    # 只接受含义明确的布尔开关；不从群角色、好友关系或未知标志位猜测。
    found = []
    for field in ("allow_member_temporary_session", "allowMemberTemporarySession"):
        if field in info:
            value = info[field]
            if type(value) is bool:
                found.append(value)
            elif type(value) is int and value in (0, 1):
                found.append(bool(value))
            else:
                raise ValueError("QQ临时会话开关值无效，未允许发送")
    if not found:
        return None
    return all(found)


async def private_route(ctx: Any, platform: str, user: str, members: List[dict]) -> dict:
    if platform != "qq":
        raise ValueError("主动私聊提醒目前仅支持QQ")
    friends = unpack(await call_snowluma_api(ctx, "account.get_friend_list", no_cache=True))
    if not isinstance(friends, list):
        raise ValueError("无法确认机器人好友关系，未发送私聊")
    friend = any(isinstance(x, dict) and protocol_id(x.get("user_id")) == user for x in friends)
    source_group = ""
    if not friend:
        for member in members:
            _, gid, _ = parts(member["owner"])
            if not gid:
                continue
            try:
                await member_info(ctx, gid, user)
                info = unpack(
                    await call_snowluma_api(
                        ctx, "group.get_group_info_ex", params={"group_id": int(gid), "no_cache": True}
                    )
                )
                allowed = temporary_permission(info, gid)
            except ValueError:
                continue
            if allowed is True:
                source_group = gid
                break
        if not source_group:
            raise ValueError(
                "不是机器人好友，且QQ未允许或接口未返回群临时会话开关；未发送私聊。当前SnowLuma可能不暴露此开关，可先添加机器人为好友"
            )
    streams = await ctx.chat.get_all_streams(platform=platform)
    if not isinstance(streams, list):
        raise ValueError("无法读取真实聊天路由，未发送私聊")
    source_ids = {x["stream"] for x in members}
    routes = [
        x
        for x in streams
        if isinstance(x, dict)
        and x.get("platform") == platform
        and str(x.get("session_id") or x.get("stream_id") or "") in source_ids
    ]
    identities = {(str(x.get("account_id") or ""), str(x.get("scope") or "")) for x in routes}
    if len(identities) != 1:
        raise ValueError("课表来源缺少唯一机器人账号路由，未发送私聊")
    account, scope = next(iter(identities))
    opened = await ctx.chat.open_session(
        platform=platform, chat_type="private", user_id=user, account_id=account, scope=scope
    )
    if (
        not isinstance(opened, dict)
        or opened.get("success") is not True
        or opened.get("platform") != platform
        or opened.get("chat_type") != "private"
        or str(opened.get("user_id")) != user
    ):
        raise ValueError("未能建立目标用户的真实私聊会话")
    if str(opened.get("account_id") or "") != account or str(opened.get("scope") or "") != scope:
        raise ValueError("私聊账号路由与课表来源不一致")
    stream = str(opened.get("stream_id") or opened.get("session_id") or "")
    if not stream:
        raise ValueError("私聊会话缺少真实聊天流编号")
    return {"stream": stream, "temporary_group_id": source_group, "friend": friend}
