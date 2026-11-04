"""本地推理闸门自检（docs/local-inference-scheduling.md）。

纯逻辑、不请求 LLM、不需要 API Key，可直接进 CI。
覆盖：
  1. 并发数解析（本地自动串行 / 云端不限制 / 显式覆盖 / 非法值）
  2. 单槽严格串行
  3. 高优先级先拿槽位（前台压过后台）
  4. 同优先级 FIFO
  5. 取消等待者不泄漏槽位
  6. 异常退出也会归还槽位
  7. 消费者提前 break（打断）后槽位必须归还

用法：python scripts/check_llm_gate.py
退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import aclosing

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from pipeline.llm_gate import (  # noqa: E402
    PRIORITY_BACKGROUND,
    PRIORITY_FOREGROUND,
    PriorityGate,
    resolve_concurrency,
)


def check_concurrency_policy() -> None:
    assert resolve_concurrency("ollama", 0) == 1
    assert resolve_concurrency("llamacpp", 0) == 1
    assert resolve_concurrency("maas", 0) == 0
    assert resolve_concurrency("ark", 0) == 0
    assert resolve_concurrency("ollama", 3) == 3
    assert resolve_concurrency("llamacpp", -1) == 0
    try:
        resolve_concurrency("ollama", -2)
    except ValueError:
        pass
    else:
        raise AssertionError("负数并发上限应当抛 ValueError")


async def check_serial() -> None:
    gate = PriorityGate(1)
    active = 0
    peak = 0

    async def job():
        nonlocal active, peak
        async with gate.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(*[job() for _ in range(4)])
    assert peak == 1, f"单槽应严格串行，实际峰值 {peak}"
    assert gate.in_use == 0 and gate.waiting == 0


async def check_priority() -> None:
    gate = PriorityGate(1)
    order = []

    async def job(tag, priority):
        async with gate.slot(priority):
            order.append(tag)
            await asyncio.sleep(0.02)

    await gate.acquire()                 # 先占住唯一的槽
    bg = asyncio.create_task(job("bg", PRIORITY_BACKGROUND))
    await asyncio.sleep(0.01)
    fg = asyncio.create_task(job("fg", PRIORITY_FOREGROUND))
    await asyncio.sleep(0.01)
    gate.release()
    await asyncio.gather(bg, fg)

    assert order == ["fg", "bg"], f"前台应插到后台前面，实际 {order}"


async def check_fifo() -> None:
    gate = PriorityGate(1)
    order = []

    async def job(tag):
        async with gate.slot():
            order.append(tag)
            await asyncio.sleep(0.01)

    await gate.acquire()
    tasks = [asyncio.create_task(job(i)) for i in ("a", "b", "c")]
    await asyncio.sleep(0.02)
    gate.release()
    await asyncio.gather(*tasks)
    assert order == ["a", "b", "c"], f"同优先级应 FIFO，实际 {order}"


async def check_cancel_waiter() -> None:
    gate = PriorityGate(1)
    await gate.acquire()

    async def waiting_job():
        async with gate.slot():
            pass

    waiter = asyncio.create_task(waiting_job())
    await asyncio.sleep(0.01)
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass

    gate.release()
    # 取消者不应占槽：下一次 acquire 应立即成功
    await asyncio.wait_for(gate.acquire(), timeout=0.5)
    gate.release()
    assert gate.in_use == 0 and gate.waiting == 0


async def check_exception_release() -> None:
    gate = PriorityGate(1)

    try:
        async with gate.slot():
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert gate.in_use == 0
    await asyncio.wait_for(gate.acquire(), timeout=0.5)
    gate.release()


async def check_break_releases_slot() -> None:
    """消费者 break 后必须立刻归还槽位（对应「打断一次后全卡住」的回归）。"""
    gate = PriorityGate(1)

    async def stream():
        async with gate.slot():
            yield "第一句"
            yield "第二句"

    # 关键：aclosing 在 break 时抛 GeneratorExit，让 async with 的 finally 执行。
    async with aclosing(stream()) as tokens:
        async for _ in tokens:
            break

    assert gate.in_use == 0, f"break 后槽位未归还，in_use={gate.in_use}"
    assert gate.waiting == 0
    await asyncio.wait_for(gate.acquire(), timeout=0.5)
    gate.release()


async def check_nested_break_releases_slot() -> None:
    """两层异步生成器（router 套 agent）提前关闭也不能泄漏，顺序同 dialog.py。"""
    gate = PriorityGate(1)

    async def agent_like():
        async with gate.slot():
            yield "你"
            yield "好"

    async def route_like(source):
        async for token in source:
            yield {"type": "token", "text": token}

    tokens = agent_like()
    events = route_like(tokens)
    async with aclosing(tokens), aclosing(events) as stream:
        async for _ in stream:
            break

    assert gate.in_use == 0, f"嵌套 break 后槽位未归还，in_use={gate.in_use}"
    assert gate.waiting == 0
    await asyncio.wait_for(gate.acquire(), timeout=0.5)
    gate.release()


def main() -> None:
    check_concurrency_policy()
    asyncio.run(check_serial())
    asyncio.run(check_priority())
    asyncio.run(check_fifo())
    asyncio.run(check_cancel_waiter())
    asyncio.run(check_exception_release())
    asyncio.run(check_break_releases_slot())
    asyncio.run(check_nested_break_releases_slot())
    print("llm gate ok: policy / serial / priority / fifo / cancel / exception / break")


if __name__ == "__main__":
    main()
