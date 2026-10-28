"""从真实引用消息取回 ICS，禁止把引用文本误当作当前用户的指令。"""

import re


def safe_api_error(value) -> str:
    """保留 API 拒绝原因，去除链接和鉴权信息，避免再次只记录错误字典的 keys。"""
    text = str(value or "未提供错误原因")
    text = re.sub(r"https?://[^\s\"'<>]+", "[链接已省略]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bBearer\s+\S+", "Bearer [已隐藏]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(token|access_token|api_key|password|secret)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
                  r"\1=[已隐藏]", text, flags=re.IGNORECASE)
    return " ".join(text.split())[:600]


SNOWLUMA_PLUGIN = "maibot-team.snowluma-adapter"


async def call_snowluma_api(ctx, operation: str, **arguments):
    """从同一个统一 QQ 连接器发现已注册名称；两种前缀属于同一连接器、同一客户端。"""
    aliases = [f"adapter.snowluma.{operation}", f"adapter.napcat.{operation}"]
    try:
        entries = await ctx.api.list(plugin_id=SNOWLUMA_PLUGIN)
    except Exception as exc:
        raise ValueError(f"无法读取 QQ 连接器接口，请允许 api.list：{safe_api_error(exc)}") from exc
    if isinstance(entries, dict) and entries.get("success") is False:
        raise ValueError(f"无法读取 QQ 连接器接口：{safe_api_error(entries.get('error'))}")
    if not isinstance(entries, list):
        raise ValueError("QQ 连接器未返回有效接口列表，请检查 api.list 权限与连接器状态")
    candidates = [e for e in entries if isinstance(e, dict) and e.get("plugin_id") == SNOWLUMA_PLUGIN
                  and e.get("name") in aliases and str(e.get("version")) == "1" and e.get("public") is True
                  and e.get("enabled") is True]
    if not candidates:
        raise ValueError(f"当前 QQ 连接器未公开或未启用 {operation} 接口，请检查连接器版本与启用状态")
    candidates.sort(key=lambda entry: aliases.index(entry["name"]))
    entry = candidates[0]
    full_name = f"{SNOWLUMA_PLUGIN}.{entry['name']}"
    if entry.get("full_name") != full_name:
        raise ValueError("QQ 连接器接口名称与提供方不一致，停止调用")
    try:
        result = await ctx.api.call(full_name, version="1", **arguments)
    except Exception as exc:
        error = safe_api_error(exc)
        ctx.logger.warning("QQ连接器API调用失败: api=%s error=%s", full_name, error)
        raise ValueError(f"QQ 连接器 {operation} 调用失败：{error}") from exc
    if isinstance(result, dict) and result.get("success") is False:
        error = safe_api_error(result.get("error"))
        ctx.logger.warning("QQ连接器API调用失败: api=%s error=%s", full_name, error)
        raise ValueError(f"QQ 连接器 {operation} 调用失败：{error}")
    return result


def protocol_id(value) -> str:
    """整数与十进制字符串无损统一；负消息 ID 合法，禁止浮点与布尔 ID。"""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    text = str(value).strip()
    try:
        return str(int(text)) if text else ""
    except ValueError:
        return ""


def snowluma_detail(response) -> dict:
    """兼容公开 API 已解包的详情和 OneBot 标准响应包，不把失败包当成详情。"""
    if not isinstance(response, dict):
        raise ValueError("SnowLuma 未找到这条引用消息，请重新上传 ICS 后再引用")
    detail = response
    for _ in range(3):
        if detail.get("success") is False:
            raise ValueError(f"SnowLuma 引用消息查询失败：{safe_api_error(detail.get('error'))}")
        if "retcode" in detail:
            if detail["retcode"] != 0 or detail.get("status") in {"failed", "error"}:
                raise ValueError(f"SnowLuma 引用消息查询失败（retcode={detail['retcode']}）："
                                 f"{safe_api_error(detail.get('wording') or detail.get('message'))}")
            nested = detail.get("data")
        elif "success" in detail and "result" in detail:
            nested = detail["result"]
        else:
            return detail
        if not isinstance(nested, dict):
            raise ValueError("SnowLuma 引用消息查询未返回消息详情")
        detail = nested
    raise ValueError("SnowLuma 消息返回结构层级异常，请查看插件日志")


def snowluma_summary(response) -> dict:
    """只记录字段与归属 ID，不记录消息正文、文件内容、下载链接或 token。"""
    try:
        detail = snowluma_detail(response)
    except ValueError:
        summary = {"response_type": type(response).__name__,
                   "keys": sorted(response) if isinstance(response, dict) else []}
        if isinstance(response, dict):
            if response.get("success") is False:
                summary.update(success=False, error=safe_api_error(response.get("error")))
            if "retcode" in response:
                summary.update(retcode=response["retcode"],
                               error=safe_api_error(response.get("wording") or response.get("message")))
        return summary
    sender = detail.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    return {"keys": sorted(detail), "message_id": protocol_id(detail.get("message_id")),
            "real_id": protocol_id(detail.get("real_id")), "group_id": protocol_id(detail.get("group_id")),
            "sender_id": protocol_id(sender.get("user_id") or detail.get("user_id")),
            "message_type": detail.get("message_type") if isinstance(detail.get("message_type"), str)
            and detail["message_type"] in {"group", "private"} else "",
            "segments_type": type(detail.get("message")).__name__}

def own_text(message: dict, fallback: str = "") -> str:
    if "raw_message" not in message:
        return fallback.strip()
    return "".join(s.get("data", "") for s in message["raw_message"]
                   if isinstance(s, dict) and s.get("type") == "text" and isinstance(s.get("data"), str)).strip()


def quoted_message_id(message: dict) -> str:
    ids = set()
    has_reply = False
    for segment in message.get("raw_message", []):
        if not isinstance(segment, dict) or segment.get("type") != "reply":
            continue
        has_reply = True
        data = segment.get("data")
        value = data.get("target_message_id", "") if isinstance(data, dict) else data
        if value:
            ids.add(str(value))
    if not ids and message.get("reply_to"):
        ids.add(str(message["reply_to"]))
    if has_reply and not ids:
        raise ValueError("引用消息缺少消息编号，请重新引用自己上传的 ICS 文件")
    if len(ids) > 1:
        raise ValueError("请只引用一条 ICS 文件消息")
    return next(iter(ids), "")


def ics_files(message: dict) -> list[dict]:
    files = [s["data"] for s in message.get("raw_message", []) if isinstance(s, dict)
            and s.get("type") == "file" and isinstance(s.get("data"), dict)
            and str(s["data"].get("name", "")).lower().endswith(".ics")]
    if files:
        return files
    info = message.get("message_info", {})
    config = info.get("additional_config") or {}
    # 统一 QQ 连接器即使连接 SnowLuma，结构化通知仍使用 napcat_notice_* 字段。
    payload = config.get("napcat_notice_payload")
    if config.get("napcat_notice_type") != "group_upload" or not isinstance(payload, dict):
        return []
    group = info.get("group_info") or {}
    user = info.get("user_info") or {}
    if str(payload.get("group_id", "")) != str(group.get("group_id", "")) or \
            str(payload.get("user_id", "")) != str(user.get("user_id", "")):
        raise ValueError("群文件通知的上传者或群号与消息归属不一致")
    file = payload.get("file")
    if not isinstance(file, dict):
        return []
    file = normalized_file(file)
    return [file] if file["name"].lower().endswith(".ics") else []


def normalized_file(file: dict) -> dict:
    return {**file, "name": str(file.get("name") or file.get("file_name") or file.get("file") or ""),
            "file_id": str(file.get("file_id") or file.get("id") or "")}


def snowluma_source(detail: dict, current: dict, source_id: str) -> dict:
    """仅接受真实群消息详情，不从引用摘要或文件名猜测归属。"""
    detail = snowluma_detail(detail)
    info = current["message_info"]
    group_id = str((info.get("group_info") or {}).get("group_id", ""))
    user_id = str((info.get("user_info") or {}).get("user_id", ""))
    sender = detail.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    # SnowLuma get_msg 在存储记录缺少 message_id 时，仅补 real_id=请求编号。
    # 有明确 message_id 时仍以该字段为准，不能用 real_id 绕过一个真实的编号冲突。
    returned_id = protocol_id(detail.get("message_id"))
    if detail.get("message_id") is None or detail.get("message_id") == "":
        returned_id = protocol_id(detail.get("real_id"))
    if returned_id in {"", "0"}:
        raise ValueError("SnowLuma 返回的引用消息缺少有效 message_id/real_id，请查看插件日志")
    if returned_id != protocol_id(source_id):
        raise ValueError("SnowLuma 返回的消息编号与引用目标不一致，请查看插件日志中的引用校验信息")
    if protocol_id(detail.get("group_id")) in {"", "0"}:
        raise ValueError("SnowLuma 返回的引用消息缺少群号，不能确认文件归属，请查看插件日志")
    if protocol_id(detail["group_id"]) != protocol_id(group_id):
        raise ValueError("SnowLuma 返回的群号与当前群不一致，请查看插件日志中的引用校验信息")
    if detail.get("message_type") is not None and detail["message_type"] != "group":
        raise ValueError("被引用文件不是群消息，未修改课表")
    if protocol_id(sender.get("user_id") or detail.get("user_id")) != protocol_id(user_id):
        raise ValueError("请引用你自己在本群上传的 ICS 文件")
    segments = detail.get("message")
    if not isinstance(segments, list):
        raise ValueError("SnowLuma 未返回文件消息段；引用文本摘要无法替代文件内容")
    files = [{"type": "file", "data": normalized_file(s["data"])} for s in segments
             if isinstance(s, dict) and s.get("type") == "file" and isinstance(s.get("data"), dict)]
    return {"message_id": source_id, "platform": current["platform"], "session_id": current["session_id"],
            "message_info": {"group_info": info["group_info"], "user_info": info["user_info"]},
            "raw_message": files}
