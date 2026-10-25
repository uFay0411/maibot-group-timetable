"""读取 Host 已归一化的文件消息，限制下载体积和目标地址。"""

from urllib.parse import urlsplit

import asyncio
import base64
import binascii
import ipaddress
import socket

import httpx

from .calendar_engine import MAX_BYTES


async def read_upload(payload: dict, allowed_hosts: list[str]) -> bytes:
    if not str(payload.get("name", "")).lower().endswith(".ics"):
        raise ValueError("请上传 .ics 文件")
    encoded = payload.get("base64", "")
    if encoded:
        if len(encoded) > (MAX_BYTES + 2) // 3 * 4:
            raise ValueError("ICS 文件不能超过 2 MiB")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("文件 Base64 无效") from exc
    else:
        url = str(payload.get("url", ""))
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("适配器未提供可下载的 HTTP(S) 文件链接或 Base64；请检查文件消息转发配置")
        addresses = await asyncio.to_thread(socket.getaddrinfo, parts.hostname, parts.port or
                                            (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        if parts.hostname not in allowed_hosts and any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("文件链接指向内网；如为你的适配器，请在 allowed_file_hosts 中明确配置主机名")
        # 禁止重定向到另一目标；不使用宿主环境代理，避免链接隐式流向代理。
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                if response.is_redirect:
                    raise ValueError("文件下载链接发生重定向，请让适配器提供最终链接")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_BYTES:
                        raise ValueError("ICS 文件不能超过 2 MiB")
                raw = bytes(chunks)
    if len(raw) > MAX_BYTES:
        raise ValueError("ICS 文件不能超过 2 MiB")
    return raw
