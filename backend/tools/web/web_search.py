"""联网搜索工具（Tavily）。

Tavily 是面向 LLM 的搜索 API，返回结构化、干净的结果摘要，几行即可接入。
申请 key: https://tavily.com 。key 通过 config.TAVILY_API_KEY 配置。
"""
from __future__ import annotations

from ..registry import tool
from ..base import ToolCategory, Permission, ToolResult, ToolContext

import sys
import os

# 允许直接 import 顶层 config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from config import TAVILY_API_KEY, TAVILY_URL  # noqa: E402


@tool(category=ToolCategory.WEB, permission=Permission.READ, timeout_s=20.0)
async def web_search(query: str, top_k: int = 5, ctx: ToolContext = None):
    """联网搜索实时信息。当问题涉及最新新闻、实时数据、你不知道或不确定的事实、特定网站内容时使用。返回若干条网页标题、摘要与链接。

    Args:
        query: 搜索关键词或问题
        top_k: 返回结果条数（默认 5，最多 10）
    """
    if not TAVILY_API_KEY:
        return ToolResult.failure("未配置 TAVILY_API_KEY，无法联网搜索")
    top_k = max(1, min(int(top_k or 5), 10))
    if ctx:
        await ctx.status(f"正在联网搜索：{query}")

    # 复用共享 http client（若有），否则临时建一个
    client = None
    temp = False
    if ctx and ctx.resources:
        client = await ctx.resources.http_client()
    if client is None:
        import httpx
        client = httpx.AsyncClient(timeout=20.0)
        temp = True

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": top_k,
        # advanced：返回更长、更相关的正文片段（比 basic 更全，稍慢）。
        "search_depth": "advanced",
    }
    try:
        resp = await client.post(TAVILY_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        return ToolResult.failure(f"搜索请求失败: {e}")
    finally:
        if temp:
            await client.aclose()

    results = data.get("results") or []
    if not results:
        return ToolResult.success("没有搜索到相关结果。")

    # 单条正文上限与总预算：放宽到能覆盖较长内容，同时限制总量避免撑爆上下文。
    PER_ITEM_MAX = 1500   # 每条结果保留的正文字符上限
    TOTAL_MAX = 6000      # 所有结果合计上限
    lines = []
    used = 0
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        content = (r.get("content") or "").strip().replace("\n", " ")
        url = r.get("url", "")
        snippet = content[:PER_ITEM_MAX]
        if used + len(snippet) > TOTAL_MAX:
            snippet = snippet[: max(0, TOTAL_MAX - used)]
        used += len(snippet)
        lines.append(f"{i}. {title}\n   内容: {snippet}\n   链接: {url}")
        if used >= TOTAL_MAX:
            break
    answer = data.get("answer")
    head = f"[搜索摘要] {answer}\n\n" if answer else ""
    return ToolResult.success(head + "搜索结果：\n" + "\n".join(lines),
                              data={"results": results})
