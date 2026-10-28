"""群汇总的身份与覆盖范围：SnowLuma实查成员，官方QQ只统计本群已存课表。"""

import asyncio
import json
import re

from .group_access import list_group_members, owner_parts
from .query import model_visible_content


async def query_roster(ctx, store, actor: str, stream: str, source: dict | None = None):
    platform, group, user = json.loads(actor)
    if platform != "qq" or not group or not user:
        raise ValueError("请在QQ当前群聊中查询群课表")
    additional = {}
    if source is not None:
        info = source.get("message_info", {})
        if (source.get("platform") != platform or source.get("session_id") != stream
                or str((info.get("group_info") or {}).get("group_id", "")) != group
                or str((info.get("user_info") or {}).get("user_id", "")) != user):
            raise ValueError("群汇总的原消息身份与当前群不一致")
        additional = info.get("additional_config") or {}
    official = (isinstance(additional, dict) and bool(re.fullmatch(
        r"[0-9a-fA-F]{32}", str(additional.get("qq_official_group_openid", "")))))
    # 历史消息可能不包含additional_config；仍按原始OpenID处理，绝不转成QQ号。
    official = official or bool(re.fullmatch(r"[0-9a-fA-F]{32}", group)
                               and re.fullmatch(r"[0-9a-fA-F]{32}", user))
    if official:
        def registered():
            return [{"user_id": row["user"], "name": row["name"]}
                    for row in store.group_members(platform, group, stream) if store.course_details(row["owner"])]
        return platform, group, await asyncio.to_thread(registered), False
    platform, group, _ = owner_parts(actor)
    return platform, group, await list_group_members(ctx, actor), True


def annotate_roster(result: dict, verified: bool) -> dict:
    result.update(roster_verified=verified, roster_source="adapter_member_list" if verified else "stored_timetables")
    if not verified:
        result["group_member_count"] = None
        note = "QQ官方接口未提供完整群成员列表；仅汇总本群已建立课表的成员，无法核实离群状态。"
        result["roster_note"] = note
        result["content"] = (result["content"].split("\n回复要求：", 1)[-1]
                             .replace("现有成员", "已建立课表的成员") + " " + note)
    result["content"] = model_visible_content(result)
    return result
