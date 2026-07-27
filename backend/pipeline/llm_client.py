"""Ollama 流式客户端：逐 token 异步产出文本。"""
import json
from typing import AsyncIterator, List, Dict

import httpx

from config import OLLAMA_URL, OLLAMA_MODEL


async def stream_chat(
    messages: List[Dict[str, str]],
    model: str = OLLAMA_MODEL,
    url: str = OLLAMA_URL,
) -> AsyncIterator[str]:
    """向 Ollama /api/chat 发起流式请求，yield 每个增量 token 文本。"""
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": 0.7},
    }
    timeout = httpx.Timeout(120.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{url}/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("done"):
                    break
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
