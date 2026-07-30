# -*- coding: utf-8 -*-
"""server_app.peers：把 WS 客户端地址格式化成便于阅读的字符串。

三种模式：
  - SHOW_REAL_IP=0  → 恒显示 127.0.0.1:端口（永远 IPv4）
  - IPv6 的 v4-映射地址 ::ffff:1.2.3.4 → 还原成纯 IPv4
  - 纯 IPv6 → 加方括号 […]，避免与端口冒号混淆

另外提供 peer_prefix() 作为日志的连接标识前缀。
"""
from __future__ import annotations

from fastapi import WebSocket

from config import SHOW_REAL_IP


def format_peer(ws: WebSocket) -> str:
    if not ws.client:
        return "?"
    host = ws.client.host
    port = ws.client.port
    if not SHOW_REAL_IP:
        return f"127.0.0.1:{port}"
    if host.startswith("::ffff:") and "." in host:
        host = host.split("::ffff:")[-1]
    elif ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}"


def peer_prefix(ws: WebSocket, conn_id: str) -> str:
    return f"#{conn_id} {format_peer(ws)}"
