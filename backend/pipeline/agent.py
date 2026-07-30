"""工具调用编排（Agent 循环）。

两阶段策略，兼顾「工具调用」与「现有流式口型链路」：

阶段一（探测/工具循环，非流式）：
    用 chat_with_tools 带上工具清单请求 LLM。
    - 若 LLM 返回 tool_calls：执行工具 → 把结果作为 role=tool 消息回喂 → 再探测；
      如此循环，最多 TOOL_MAX_ROUNDS 轮，防止无限调用。
    - 若 LLM 直接给出答案（无 tool_calls）：进入阶段二。

阶段二（最终答案，流式）：
    - 没用过工具：直接把探测阶段拿到的完整答案分块产出（省一次调用）。
    - 用过工具：再调用 stream_chat 基于工具结果流式生成最终答案（口型/表情实时）。

对外暴露 async 生成器 agent_stream(...)，产出 token 文本，
可直接接到现有 text_router.route() 做分句/抽动作。
"""
from __future__ import annotations

import json
from typing import AsyncIterator, Callable, Dict, List, Optional, Awaitable

from config import ENABLE_TOOLS, TOOL_MAX_ROUNDS, TOOL_MAX_PERMISSION
from pipeline.llm_client import stream_chat, chat_with_tools
from tools import REGISTRY, RESOURCES
from tools.base import ToolContext, Permission


def _perm_from_str(s: str) -> Permission:
    try:
        return Permission(s)
    except ValueError:
        return Permission.READ


def _assistant_toolcall_msg(calls: List[Dict]) -> Dict:
    """把归一化的 tool_calls 还原成 OpenAI 格式的 assistant 消息（供回喂）。"""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)},
            }
            for c in calls
        ],
    }


async def agent_stream(
    messages: List[Dict],
    *,
    session_id: str = "",
    conn_id: str = "",
    cancel=None,
    emit_status: Optional[Callable[[str], Awaitable[None]]] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> AsyncIterator[str]:
    """带工具调用的对话生成器，逐段产出最终答案文本。"""
    # 未启用工具：退回纯流式，行为与之前完全一致。
    if not ENABLE_TOOLS:
        async for tok in stream_chat(messages):
            yield tok
        return

    max_perm = _perm_from_str(TOOL_MAX_PERMISSION)
    tools = REGISTRY.schemas(max_permission=max_perm)
    if not tools:
        async for tok in stream_chat(messages):
            yield tok
        return

    ctx = ToolContext(session_id=session_id, conn_id=conn_id, cancel=cancel,
                      emit_status=emit_status, resources=RESOURCES)

    used_tools = False
    working = list(messages)  # 复制一份，工具消息只作用于本轮推理，不污染长期历史

    # ---- 阶段一：工具循环 ----
    for round_i in range(TOOL_MAX_ROUNDS):
        if cancel is not None and cancel.is_set():
            return
        probe = await chat_with_tools(working, tools)
        calls = probe.get("tool_calls") or []
        if not calls:
            # LLM 直接作答：没用工具就把这段答案直接产出（省一次调用）
            if not used_tools:
                content = probe.get("content") or ""
                if content:
                    yield content
                    return
                # 极少数情况下 content 为空，退回流式
                async for tok in stream_chat(working):
                    yield tok
                return
            break  # 用过工具 → 跳出去做阶段二流式总结

        # 执行本轮所有工具调用（可并行，这里顺序执行便于日志）
        used_tools = True
        working.append(_assistant_toolcall_msg(calls))
        for c in calls:
            result = await REGISTRY.execute(c["name"], c["arguments"], ctx, max_permission=max_perm)
            working.append({
                "role": "tool",
                "tool_call_id": c["id"],
                "content": result.content,
            })

    # ---- 阶段二：基于工具结果流式生成最终答案 ----
    if cancel is not None and cancel.is_set():
        return
    async for tok in stream_chat(working):
        yield tok
