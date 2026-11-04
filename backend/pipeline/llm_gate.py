"""本地单卡推理的请求闸门（priority gate）。

为什么需要这一层
----------------
NPU / 单张显卡同一时刻只能跑一个生成任务。多个 WebSocket 同时发问时，
FLM、Ollama、llama.cpp 会在自己的服务端排队：先到的请求先跑，后到的请求
TTFT 线性增长（实测 3 并发时分别是 0.6s / 3.9s / 7.1s）。

应用层再排一次队不会提高吞吐，但能做到两件服务端排队做不到的事：

1. **公平**：按请求到达顺序排队，而不是让某个连接连续抢到槽位；
2. **分级**：用户说话（前台）永远排在记忆摘要、画像提炼（后台）前面。

所以它是“把隐式排队变成可控排队”，不是“让单卡变成并发”。

实现要点
--------
- 等待队列按 ``(priority, seq)`` 排序：priority 越小越优先，同优先级 FIFO。
- 取消等待中的请求时惰性跳过，不会泄漏槽位。
- 只包住真正的推理调用；TTS、工具执行等不占用闸门。
"""
from __future__ import annotations

import asyncio
import heapq
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Tuple

# 数值越小越优先。给后台任务留出可插入的中间档位（如 5 = 预取）。
PRIORITY_FOREGROUND = 0
PRIORITY_BACKGROUND = 10

# 这些供应商默认认为“单卡串行”。云端（maas/ark）可并发，不设闸门。
LOCAL_PROVIDERS = frozenset({"ollama", "llamacpp"})


def resolve_concurrency(provider: str, configured: int) -> int:
    """把配置值翻译成实际并发数；0 表示不限制（不建闸门）。

    ``configured`` 来自 ``LLM_MAX_CONCURRENCY``：
      -  0：自动。本地单卡 1，云端 0（不限）。
      - -1：强制不限（给开了多 slot 的 llama.cpp 或支持并发的本地服务留口子）。
      - >0：显式上限。
      - 其它负数：配置错误，直接抛 ValueError，让启动阶段就暴露。
    """
    if configured < 0:
        if configured == -1:
            return 0
        raise ValueError("LLM_MAX_CONCURRENCY 只接受 -1（不限）、0（自动）或正数")
    if configured > 0:
        return configured
    return 1 if provider in LOCAL_PROVIDERS else 0


class PriorityGate:
    """限制同时进入的调用数，并让高优先级请求先拿到槽位。"""

    def __init__(self, max_concurrency: int = 1) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        self._limit = max_concurrency
        self._in_use = 0
        # (priority, seq, future)。future 被取消后仍可能留在堆里，取出时跳过。
        self._waiters: List[Tuple[int, int, asyncio.Future]] = []
        self._seq = 0

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def waiting(self) -> int:
        return sum(1 for _, _, fut in self._waiters if not fut.done())

    async def acquire(self, priority: int = PRIORITY_FOREGROUND) -> None:
        self._prune()
        if self._in_use < self._limit and not self._waiters:
            self._in_use += 1
            return

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        heapq.heappush(self._waiters, (priority, self._seq, fut))
        self._seq += 1

        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # 刚好在取消前被调度器授予槽位：把槽位还回去，避免泄漏。
                self._in_use -= 1
                self._dispatch()
            else:
                fut.cancel()
            raise

    def release(self) -> None:
        if self._in_use <= 0:
            raise RuntimeError("PriorityGate.release() 次数多于 acquire()")
        self._in_use -= 1
        self._dispatch()

    def _prune(self) -> None:
        while self._waiters and self._waiters[0][2].done():
            heapq.heappop(self._waiters)

    def _dispatch(self) -> None:
        while self._in_use < self._limit and self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if fut.done():
                continue
            self._in_use += 1
            fut.set_result(None)

    @asynccontextmanager
    async def slot(self, priority: int = PRIORITY_FOREGROUND) -> AsyncIterator[None]:
        await self.acquire(priority)
        try:
            yield
        finally:
            self.release()
