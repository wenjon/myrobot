"""LLM 流式客户端：支持火山引擎 Ark（OpenAI 兼容）与本地 Ollama。

由 config.LLM_PROVIDER 选择供应商：
- "ark"    → https://.../chat/completions（OpenAI 兼容 SSE，Bearer 鉴权）
- "ollama" → 本地 /api/chat（NDJSON 流）

对外统一暴露：
- stream_chat(messages) -> 逐 token 异步产出文本（增量 content）
- chat_once(messages)   -> 一次性返回完整文本（用于摘要等内部任务）
"""
import json
from typing import AsyncIterator, List, Dict

import httpx

from config import (
    LLM_PROVIDER,
    OLLAMA_URL,
    OLLAMA_MODEL,
    ARK_BASE_URL,
    ARK_API_KEY,
    ARK_MODEL,
)

_TIMEOUT = httpx.Timeout(180.0, connect=10.0)


# =====================================================================
# 火山引擎 Ark（OpenAI 兼容）
# =====================================================================
def _ark_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {ARK_API_KEY}", "Content-Type": "application/json"}


async def _ark_stream(messages: List[Dict[str, str]], temperature: float) -> AsyncIterator[str]:
    payload = {
        "model": ARK_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    url = f"{ARK_BASE_URL}/chat/completions"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", url, headers=_ark_headers(), json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                # 只取正式回答 content，忽略 reasoning_content（思维链）
                delta = choices[0].get("delta", {})
                chunk = delta.get("content") or ""
                if chunk:
                    yield chunk


async def _ark_once(messages: List[Dict[str, str]], temperature: float) -> str:
    payload = {
        "model": ARK_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }
    url = f"{ARK_BASE_URL}/chat/completions"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=_ark_headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"].get("content", "")


# =====================================================================
# 本地 Ollama
# =====================================================================
async def _ollama_stream(messages: List[Dict[str, str]], temperature: float) -> AsyncIterator[str]:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature},
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as resp:
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


async def _ollama_once(messages: List[Dict[str, str]], temperature: float) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("content", "")


# =====================================================================
# 统一入口
# =====================================================================
async def stream_chat(messages: List[Dict[str, str]], temperature: float = 0.7) -> AsyncIterator[str]:
    if LLM_PROVIDER == "ark":
        async for tok in _ark_stream(messages, temperature):
            yield tok
    else:
        async for tok in _ollama_stream(messages, temperature):
            yield tok


async def chat_once(messages: List[Dict[str, str]], temperature: float = 0.3) -> str:
    if LLM_PROVIDER == "ark":
        return await _ark_once(messages, temperature)
    return await _ollama_once(messages, temperature)
