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


@tool(
    name="web_search",
    description=(
        "联网搜索实时信息。当问题涉及最新新闻、实时数据、你不知道或不确定的事实、"
        "特定网站内容时使用。返回若干条网页标题、摘要与链接。"
    ),
    category=ToolCategory.WEB,
    permission=Permission.READ,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词或问题"},
            "top_k": {"type": "integer", "description": "返回结果条数（默认 5，最多 10）"},
        },
        "required": ["query"],
    },
    timeout_s=20.0,
)
async def web_search(query: str, top_k: int = 5, ctx: ToolContext = None):
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
        "search_depth": "basic",
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

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        content = (r.get("content") or "").strip().replace("\n", " ")
        url = r.get("url", "")
        lines.append(f"{i}. {title}\n   摘要: {content[:300]}\n   链接: {url}")
    answer = data.get("answer")
    head = f"[搜索摘要] {answer}\n\n" if answer else ""
    return ToolResult.success(head + "搜索结果：\n" + "\n".join(lines),
                              data={"results": results})
