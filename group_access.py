"""同群成员解析与实时管理员校验；不接受模型提供的权限或群号。"""

import json
import re

from .imports import call_snowluma_api, own_text, protocol_id


class MemberChoiceRequired(ValueError):
    def __init__(self, candidates: list[dict]):
        self.candidates = candidates
        labels = "、".join(f"{x['name']}({x['user_id']})" for x in candidates[:10])
        super().__init__(f"有多名同群成员符合，请明确 @ 成员或填写QQ号：{labels}")


def unpack(response):
    for _ in range(3):
        if not isinstance(response, dict):
            return response
        if response.get("success") is False or ("retcode" in response and response["retcode"] != 0):
            raise ValueError("群成员查询失败，未执行课表操作")
        if "retcode" in response:
            response = response.get("data")
        elif "success" in response and "result" in response:
            response = response["result"]
        else:
            return response
    raise ValueError("群成员接口响应层级异常")


def qq_id(value) -> str:
    text = protocol_id(value)
    if not re.fullmatch(r"[1-9]\d{0,19}", text):
        raise ValueError("请使用有效成员QQ号或群名片")
    return text


def owner_parts(owner: str) -> tuple[str, str, str]:
    platform, group, user = json.loads(owner)
    if platform != "qq":
        raise ValueError("群友查询和管理员代改目前支持 SnowLuma QQ 群")
    return platform, qq_id(group), qq_id(user)


def checked_member(value, group: str, user: str | None = None) -> dict:
    if not isinstance(value, dict) or protocol_id(value.get("group_id")) != group:
        raise ValueError("无法确认目标属于当前群，未执行课表操作")
    uid = qq_id(value.get("user_id"))
    if user is not None and uid != user:
        raise ValueError("群成员接口返回的身份与目标不一致")
    return {"user_id": uid, "name": str(value.get("card") or value.get("nickname") or uid),
            "nickname": str(value.get("nickname") or ""), "role": value.get("role")}


async def member_info(ctx, group: str, user: str) -> dict:
    result = await call_snowluma_api(ctx, "group.get_group_member_info", group_id=int(group), user_id=int(user), no_cache=True)
    return checked_member(unpack(result), group, user)


async def require_admin(ctx, actor_owner: str):
    _, group, actor = owner_parts(actor_owner)
    info = await member_info(ctx, group, actor)
    if info["role"] not in {"owner", "admin"}:
        raise ValueError("只有本群群主或管理员可以增删改其他成员课表；未修改任何课程")


async def list_group_members(ctx, actor_owner: str) -> list[dict]:
    _, group, actor = owner_parts(actor_owner)
    rows = unpack(await call_snowluma_api(ctx, "group.get_group_member_list", group_id=int(group), no_cache=True))
    if not isinstance(rows, list) or len(rows) > 20000:
        raise ValueError("无法取得当前群完整成员列表，不能汇总群内课表")
    members = {}
    for row in rows:
        item = checked_member(row, group)
        members[item["user_id"]] = item
    if actor not in members:
        raise ValueError("无法确认提问者仍属于当前群，不能查询群内课表")
    return list(members.values())


async def resolve_member(ctx, actor_owner: str, stream: str, target: str) -> dict:
    if not isinstance(target, str) or len(target) > 128:
        raise ValueError("目标成员应为 @成员、QQ号或群名片，最多128字")
    platform, group, actor = owner_parts(actor_owner)
    target = target.strip().removeprefix("@").strip()
    if not target or target in {"我", "自己", "本人", actor}:
        return {"owner": actor_owner, "stream": stream, "user_id": actor, "name": actor, "is_self": True}
    if re.fullmatch(r"[1-9]\d{0,19}", target):
        info = await member_info(ctx, group, target)
    else:
        result = unpack(await call_snowluma_api(ctx, "group.get_group_member_list", group_id=int(group), no_cache=True))
        if not isinstance(result, list) or len(result) > 20000:
            raise ValueError("无法取得当前群成员列表，请直接 @成员或填写QQ号")
        candidates = []
        for row in result:
            info = checked_member(row, group)
            candidates.append(info)
        key = target.casefold()
        exact = [x for x in candidates if key in {x["name"].casefold(), x["nickname"].casefold()}]
        matches = exact or ([x for x in candidates if key in x["name"].casefold() or key in x["nickname"].casefold()]
                            if len(key) >= 2 else [])
        matches = list({x["user_id"]: x for x in matches}.values())
        if not matches:
            raise ValueError("当前群没有找到这名成员，请明确 @成员或填写QQ号")
        if len(matches) > 1:
            raise MemberChoiceRequired([{"user_id": x["user_id"], "name": x["name"]} for x in matches])
        info = await member_info(ctx, group, matches[0]["user_id"])
    uid = info["user_id"]
    return {"owner": json.dumps([platform, group, uid], ensure_ascii=False, separators=(",", ":")),
            "stream": stream, "user_id": uid, "name": info["name"], "is_self": uid == actor}


def command_text(message: dict, fallback: str = "") -> str:
    """保留指令正文内的真实 @；跳过指令前 @机器人及引用摘要。"""
    if "raw_message" not in message:
        return own_text(message, fallback)
    chunks, started = [], False
    for segment in message["raw_message"]:
        if not isinstance(segment, dict):
            continue
        value = segment.get("data")
        if segment.get("type") == "text" and isinstance(value, str):
            if not started:
                if not value.lstrip().startswith("/课表"):
                    if value.strip():
                        return own_text(message, fallback)
                    continue
                started = True
            chunks.append(value)
        elif started and segment.get("type") == "at":
            uid = value.get("target_user_id") if isinstance(value, dict) else value
            if uid is None or str(uid) == "all":
                raise ValueError("请指定一名具体群成员，不能 @全体成员")
            chunks.append(" " + qq_id(uid) + " ")
    return "".join(chunks).strip() if started else own_text(message, fallback)
