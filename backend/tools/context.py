"""工具框架 · 共享资源管理。

未来的数据库连接池、HTTP client、硬件串口句柄等，都在这里懒加载并复用，
服务关闭时统一释放。工具通过 ctx.resources 获取，不各自新建连接。

当前 demo 只提供一个共享的 httpx.AsyncClient（web_search 会用到）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class ResourceManager:
    """集中管理跨工具复用的资源。"""

    def __init__(self) -> None:
        self._http: Optional[Any] = None
        self._store: Dict[str, Any] = {}

    async def http_client(self):
        """懒加载一个共享的 httpx.AsyncClient。"""
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        return self._http

    # 通用 KV：未来放 DB pool / 串口句柄等
    def set(self, key: str, value: Any) -> None:
        self._store[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    async def aclose(self) -> None:
        """释放所有资源（服务关闭时调用）。"""
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        self._store.clear()


# 全局单例
RESOURCES = ResourceManager()
