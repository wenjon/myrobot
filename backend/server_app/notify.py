# -*- coding: utf-8 -*-
"""server_app.notify：服务端主动推送给前端的辅助消息。

目前只发一种：「interrupted」——告诉前端这是“自动打断”，
让它做自然收尾（渐弱 + “我在听”倾听表情）。
与“手动点击打断按钮”区分：手动打断是用户主动要求立即安静，
不需要渐弱/倾听表情。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import WebSocket


async def notify_interrupted(ws: WebSocket, reason: str = "") -> None:
    """下发 interrupted 消息，异常时静默（连接可能已断开）。"""
    try:
        await ws.send_text(json.dumps(
            {"type": "interrupted", "reason": reason},
            ensure_ascii=False,
        ))
    except Exception:
        # 连接已断等情况：静默吞掉，避免打断主流程
        pass


async def send_json(ws: WebSocket, payload: Any) -> None:
    """通用：把 dict/list 序列化后下发，异常静默。"""
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass
