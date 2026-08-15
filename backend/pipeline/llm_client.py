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
    LLAMACPP_URL,
    LLAMACPP_MODEL,
    LLAMACPP_API_KEY,
    ENABLE_THINKING,
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
# 本地 llama.cpp server（OpenAI 兼容 /v1/chat/completions）
# =====================================================================
# 适用：llama.cpp 起的本地 server（典型如 Qwen3.6-35B-A3B MTP @ 127.0.0.1:8080）。
# 与 Ark 走同一套 OpenAI 兼容协议，但默认不需要 API key，模型 id 由 LLAMACPP_MODEL 指定。
# 注意事项：
#   - 服务端会把推理过程放在 reasoning_content，正式回答才在 content；
#     与 Ark 一致，只取 content，跳过思维链。
#   - 启动时通常给到 reasoning budget（默认 1024），所以正式回答可能来得稍慢；
#     这不影响 TTS 流式分句，前端照样能边收边说。
def _llamacpp_headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if LLAMACPP_API_KEY:
        h["Authorization"] = f"Bearer {LLAMACPP_API_KEY}"
    return h


async def _llamacpp_stream(messages: List[Dict[str, str]], temperature: float) -> AsyncIterator[str]:
    payload = {
        "model": LLAMACPP_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    if not ENABLE_THINKING:
        # Qwen3 在 jinja chat template 下默认走深度思考，max_tokens 内往往装不下；
        # 语音对话要秒级响应，关掉。系统 prompt 已经约束了回答风格，不必再叠思考。
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    url = f"{LLAMACPP_URL}/chat/completions"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", url, headers=_llamacpp_headers(), json=payload) as resp:
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
                delta = choices[0].get("delta", {}) or {}
                chunk = delta.get("content") or ""
                if chunk:
                    yield chunk


async def _llamacpp_once(messages: List[Dict[str, str]], temperature: float) -> str:
    payload = {
        "model": LLAMACPP_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }
    if not ENABLE_THINKING:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    url = f"{LLAMACPP_URL}/chat/completions"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=_llamacpp_headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"].get("content") or "").strip()


async def _llamacpp_with_tools(messages, tools, temperature):
    payload = {
        "model": LLAMACPP_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "tools": tools,
        "tool_choice": "auto",
    }
    if not ENABLE_THINKING:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    url = f"{LLAMACPP_URL}/chat/completions"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=_llamacpp_headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
    msg = data["choices"][0]["message"]
    return _normalize_toolcalls(msg)


# =====================================================================
# 统一入口
# =====================================================================
async def stream_chat(messages: List[Dict[str, str]], temperature: float = 0.7) -> AsyncIterator[str]:
    if LLM_PROVIDER == "ark":
        async for tok in _ark_stream(messages, temperature):
            yield tok
    elif LLM_PROVIDER == "llamacpp":
        async for tok in _llamacpp_stream(messages, temperature):
            yield tok
    else:
        async for tok in _ollama_stream(messages, temperature):
            yield tok


# =====================================================================
# 工具调用：非流式请求，返回 {content, tool_calls}
# 第一阶段用它探测「LLM 是否想调用工具」；无工具则可回退流式直接答。
# =====================================================================
async def chat_with_tools(messages: List[Dict], tools: List[Dict],
                          temperature: float = 0.3) -> Dict:
    """带 tools 的非流式对话。

    返回: {"content": str, "tool_calls": [{"id","name","arguments"(dict)}...]}
    tool_calls 为空表示 LLM 选择直接回答（不调用工具）。
    """
    if LLM_PROVIDER == "ark":
        return await _ark_with_tools(messages, tools, temperature)
    if LLM_PROVIDER == "llamacpp":
        return await _llamacpp_with_tools(messages, tools, temperature)
    return await _ollama_with_tools(messages, tools, temperature)



async def chat_once(messages: List[Dict[str, str]], temperature: float = 0.3) -> str:
    if LLM_PROVIDER == "ark":
        return await _ark_once(messages, temperature)
    if LLM_PROVIDER == "llamacpp":
        return await _llamacpp_once(messages, temperature)
    return await _ollama_once(messages, temperature)


# ---- Ark: 带 tools 的非流式实现 ----
async def _ark_with_tools(messages, tools, temperature):
    payload = {
        "model": ARK_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "tools": tools,
        "tool_choice": "auto",
    }
    url = f"{ARK_BASE_URL}/chat/completions"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=_ark_headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
    msg = data["choices"][0]["message"]
    return _normalize_toolcalls(msg)


# ---- Ollama: 带 tools 的非流式实现 ----
async def _ollama_with_tools(messages, tools, temperature):
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "tools": tools,
        "options": {"temperature": temperature},
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
    msg = data.get("message", {})
    return _normalize_toolcalls(msg)


def _normalize_toolcalls(msg: Dict) -> Dict:
    """把 Ark(OpenAI) 与 Ollama 两种 message 结构统一成
    {content, tool_calls:[{id,name,arguments(dict)}]}。"""
    content = msg.get("content") or ""
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function", {}) or {}
        name = fn.get("name", "")
        raw_args = fn.get("arguments", {})
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        else:
            args = raw_args or {}
        calls.append({"id": tc.get("id") or f"call_{i}", "name": name, "arguments": args})
    return {"content": content, "tool_calls": calls}
