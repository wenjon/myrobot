# -*- coding: utf-8 -*-
"""server_app.dialog: 单轮对话主循环。

本模块是服务端最重要的业务逻辑之一，从 server.py 单独拆出以减轻主文件压力。

责任：
  1) 调用 session.begin_turn() + build_messages() 组装本轮真正要发给 LLM 的上下文；
  2) 接入「工具代理」（pipeline.agent），产出 token 流；
  3) 经「中央调度」（text_router.route）分句 / 抽动作，分别推送 sentence / action 给前端；
  4) 根据 cancel.is_set() 与是否产出过句子，决定 commit 还是 rollback；
  5) 可选：在正常结束后后台触发「记忆摘要」压缩老历史。

接口：
  run_dialog(ws, session, text, cancel, who="")。参数说明：
    ws:      WebSocket 连接。
    session: 会话对象（拥有历史 / 摘要）。
    text:    本轮用户输入。
    cancel:  asyncio.Event；被 set 后本轮以及快为「被打断」收尾。
    who:     连接标识前缀（如 #a1b2c3 127.0.0.1:54321），供日志区分多客户端。

上下文提交规则（关键，影响多轮记忆是否连贯）：
  - 正常结束 → 把 user + 清洗后的 assistant 一起入库；
  - 被打断且已说部分 → 记录已说部分 + 标注「被打断」，保持上下文连贯；
  - 被打断且啥都没说 / 出错 → rollback，避免留下「孤儿」消息。
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import WebSocket

# 同业务模块、工具框架、记忆上下文接口都在同一个项目下，直接 import 即可。
from pipeline.agent import agent_stream
from pipeline.llm_client import chat_once
from pipeline.text_router import route

# 服务端「辅助」模块：上下文日志、服务端日志器。
from .logging import get_logger, log_context, log_output
from .notify import send_json


# ---------------------------------------------------------------------
# 辅助：下发待确认的画像冲突
# ---------------------------------------------------------------------
async def _notify_profile_conflicts(ws: WebSocket, session, who: str = "") -> None:
    """把本会话的待确认画像冲突下发给前端（最多 N 条）。

    超出 PROFILE_MAX_CONFLICTS_PER_TURN 的部分不下发，只写日志，下轮再评估——
    一次弹一堆确认卡比不弹更烦人。
    """
    log = get_logger()
    try:
        conflicts = session.take_conflicts_to_notify()
    except Exception:  # noqa: BLE001
        return
    if not conflicts:
        return
    total = len(session.pending_conflicts)
    if total > len(conflicts):
        log.emit(f"[记忆 {who}] 待确认画像冲突 {total} 条，本轮只下发 {len(conflicts)} 条")
    for c in conflicts:
        log.emit(f"[记忆 {who}] 下发画像冲突确认卡: {c.field_name} "
                 f"{c.old_value!r} -> {c.new_value!r} (id={c.conflict_id})")
        await send_json(ws, c.to_ws())


# ---------------------------------------------------------------------
# 主入口：一轮对话
# ---------------------------------------------------------------------
async def run_dialog(
    ws: WebSocket,
    session,
    text: str,
    cancel: asyncio.Event,
    who: str = "",
) -> None:
    """运行一轮对话（从开始到提交/回滚）。"""
    log = get_logger()

    # === 1) 开启新一轮 + 组装上下文 + 记录调试日志 ===
    # begin_turn() 会把 user 语句暂存在 pending_user，供后面 commit / rollback；
    # build_messages() 负责拼接 system + 窗口历史 + 本轮 user。
    session.begin_turn(text)
    messages = session.build_messages(text)
    log_context(session, messages, text, who)
    # 收集模型输出（含动作标记，用于日志 / 入库）
    assistant_text = []
    # 是否已向前端产出过至少一句（用于打断时判断是否需要 commit）
    produced = False

    async def emit_status(s: str) -> None:
        """推送工具执行状态给前端（如「正在联网搜索…」）。"""
        try:
            await ws.send_text(json.dumps({"type": "status", "text": s}, ensure_ascii=False))
        except Exception:
            # 连接可能已断开：静默吞掉，不影响主流程。
            pass

    async def token_stream():
        """绕过 Agent （含工具调用）产出最终答案 token；收到打断信号就停。"""
        async for tok in agent_stream(
            messages,
            session_id=session.id,
            # who 的格式为 "#<conn_id> <peer>"，取第一段作为 conn_id
            conn_id=who.split()[0].lstrip("#") if who else "",
            cancel=cancel,
            emit_status=emit_status,
            log_fn=log.emit,
        ):
            if cancel.is_set():
                # 打断了：不再产出新 token，退出生成器。
                break
            assistant_text.append(tok)
            yield tok

    # === 2) 中央调度：消费 token 流，拆出「整句」与「动作」推送 ===
    seq = 0  # 句子序号，前端可用于排序
    try:
        async for event in route(token_stream()):
            if cancel.is_set():
                break
            if event["type"] == "sentence":
                produced = True
                await ws.send_text(json.dumps(
                    {"type": "sentence", "text": event["text"], "seq": seq},
                    ensure_ascii=False,
                ))
                seq += 1
            elif event["type"] == "action":
                # 表情/动作指令：提前下发，让前端表情与语音同步
                await ws.send_text(json.dumps(
                    {"type": "action", "action": event["action"], "value": event["value"]},
                    ensure_ascii=False,
                ))
    except Exception as e:  # noqa: BLE001
        # LLM / 网络等异常：回滚本轮，并把错误告知前端
        session.rollback_turn()
        log_output(session, f"[error] {e}", who=who)
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False))
        except Exception:
            pass
        return

    # === 3) 被打断的收尾处理 ===
    if cancel.is_set():
        partial = "".join(assistant_text).strip()
        if produced and partial:
            # 已说了半句：记入历史 + 标注「被打断」，保持上下文连贯
            log_output(session, partial, "（被打断）", who=who)
            session.commit_turn(partial + "（被打断）")
        else:
            # 还没开口就被打断：直接回滚，不留半截记录
            session.rollback_turn()
            log_output(session, "(barge-in, no output, rolled back)", who=who)
        return

    # === 4) 正常结束：入库 + 通知前端本轮完成 ===
    log_output(session, "".join(assistant_text), who=who)
    session.commit_turn("".join(assistant_text))
    await ws.send_text(json.dumps({"type": "llm_done"}, ensure_ascii=False))

    # === 5) 反思：把「被裁掉」的旧历史沉淀为摘要 + 用户画像 ===
    #
    # 历史坑：这里原本写的是 `from pipeline.conversation import conversations`，
    # 而当时那个单例并不存在（实例建在 server.py 里），再加上外层的
    # `except Exception: pass`，导致**摘要功能一直静默失败、从未真正跑过**。
    # 现在单例已移到 pipeline/conversation.py 模块层，两边必然拿到同一个对象。
    #
    # 为什么反思放在本轮“交付后”：它要调一次 LLM（比较慢），
    # 放在 llm_done 之后就不会拘迟用户听到最后一句话。
    from pipeline.conversation import conversations
    try:
        await conversations.maybe_summarize(session, chat_once)
    except Exception as e:  # noqa: BLE001
        # 反思失败不能影响已经成功的本轮对话，但要把原因打出来，
        # 不能像以前那样静默吃掉（那正是上面那个 bug 潜伏很久的原因）。
        log.emit(f"[记忆] 反思环节异常（不影响本轮对话）: {type(e).__name__}: {e}")

    # === 6) 把待确认的画像冲突随本轮末尾下发（B 方案，docs § 16.8）===
    #
    # 为什么不在反思一提炼出来就立即下发：那时数字人可能正在说话，
    # 弹确认卡会打断体验。放到本轮完全结束后下发最自然。
    await _notify_profile_conflicts(ws, session, who)
