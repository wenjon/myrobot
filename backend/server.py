"""FastAPI + WebSocket 服务：编排全链路流式流水线 + 上下文管理。"""
import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from config import HOST, PORT, OLLAMA_MODEL, LOG_CONTEXT, CONTEXT_LOG_FILE
from pipeline.llm_client import stream_chat, chat_once
from pipeline.text_router import route
from pipeline.conversation import ConversationManager

app = FastAPI(title="Robot Head Demo")
conversations = ConversationManager()

# ---- 上下文日志：同时输出到控制台与文件 ----
_log_fh = None
if LOG_CONTEXT and CONTEXT_LOG_FILE:
    try:
        os.makedirs(os.path.dirname(CONTEXT_LOG_FILE), exist_ok=True)
        _log_fh = open(CONTEXT_LOG_FILE, "a", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 无法打开上下文日志文件: {e}")
        _log_fh = None


def _emit(line: str):
    if not LOG_CONTEXT:
        return
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def _log_context(session, messages, user_text):
    ts = datetime.now().strftime("%H:%M:%S")
    lines = ["\n" + "=" * 70,
             f"[{ts}][会话 {session.id[:8]}] 用户输入: {user_text}",
             f"[发送给 LLM 的上下文 · 共 {len(messages)} 条 · "
             f"约 {sum(len(m['content']) for m in messages)} 字]"]
    for i, m in enumerate(messages):
        role = {"system": "系统", "user": "用户", "assistant": "小柚"}.get(m["role"], m["role"])
        content = m["content"].replace("\n", " ")
        if len(content) > 200:
            content = content[:200] + f"…(+{len(m['content']) - 200}字)"
        lines.append(f"  {i:>2}. [{role}] {content}")
    if session.summary:
        lines.append(f"[当前记忆摘要] {session.summary}")
    lines.append("-" * 70)
    _emit("\n".join(lines))


def _log_output(session, assistant_raw, note=""):
    ts = datetime.now().strftime("%H:%M:%S")
    _emit(f"[{ts}][会话 {session.id[:8]}] 模型输出{note}: {assistant_raw.strip()}\n" + "=" * 70)


FRONTEND_DIR = (Path(__file__).resolve().parent.parent / "frontend")


@app.get("/")
async def index():
    return RedirectResponse(url="/app/index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "model": OLLAMA_MODEL}


async def _run_dialog(ws: WebSocket, session, text: str, cancel: asyncio.Event):
    """跑一轮对话：LLM 流式 -> 中央调度 -> WS 推送。上下文安全提交。"""
    session.begin_turn(text)
    messages = session.build_messages(text)
    _log_context(session, messages, text)

    assistant_text = []
    produced = False

    async def token_stream():
        async for tok in stream_chat(messages):
            if cancel.is_set():
                break
            assistant_text.append(tok)
            yield tok

    seq = 0
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
                await ws.send_text(json.dumps(
                    {"type": "action", "action": event["action"], "value": event["value"]},
                    ensure_ascii=False,
                ))
    except Exception as e:  # noqa: BLE001
        session.rollback_turn()
        _log_output(session, f"[出错] {e}")
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False))
        except Exception:
            pass
        return

    if cancel.is_set():
        partial = "".join(assistant_text).strip()
        if produced and partial:
            _log_output(session, partial, "（被打断）")
            session.commit_turn(partial + "（被打断）")
        else:
            session.rollback_turn()
            _log_output(session, "(打断，无产出，已回滚)")
        return

    _log_output(session, "".join(assistant_text))
    session.commit_turn("".join(assistant_text))
    await ws.send_text(json.dumps({"type": "llm_done"}, ensure_ascii=False))

    try:
        await conversations.maybe_summarize(session, chat_once)
    except Exception:
        pass


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    session = None
    queue: asyncio.Queue = asyncio.Queue()
    cancel = asyncio.Event()
    worker_current = {"cancel": None}

    async def worker():
        """顺序处理消息队列：连打多句会排队，而不是丢弃未开始的轮次。"""
        while True:
            text = await queue.get()
            if text is None:
                break
            c = asyncio.Event()
            worker_current["cancel"] = c
            try:
                await _run_dialog(ws, session, text, c)
            except Exception:
                session.rollback_turn()
            finally:
                worker_current["cancel"] = None
                queue.task_done()

    worker_task = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")

            # 首条消息绑定会话
            if session is None:
                sid = msg.get("session") or uuid.uuid4().hex
                session = conversations.get(sid)
                await ws.send_text(json.dumps({"type": "session", "session": sid}, ensure_ascii=False))
                worker_task = asyncio.create_task(worker())

            if mtype == "interrupt":
                # 打断当前正在说的一轮，并清空排队
                c = worker_current.get("cancel")
                if c:
                    c.set()
                while not queue.empty():
                    try:
                        queue.get_nowait(); queue.task_done()
                    except Exception:
                        break
                continue

            if mtype == "clear":
                c = worker_current.get("cancel")
                if c:
                    c.set()
                while not queue.empty():
                    try:
                        queue.get_nowait(); queue.task_done()
                    except Exception:
                        break
                conversations.clear(session.id)
                await ws.send_text(json.dumps({"type": "cleared"}, ensure_ascii=False))
                continue

            if mtype == "user_message":
                text = (msg.get("text") or "").strip()
                if text:
                    await queue.put(text)  # 入队，顺序处理，不丢消息
    except WebSocketDisconnect:
        pass
    finally:
        c = worker_current.get("cancel")
        if c:
            c.set()
        if worker_task and not worker_task.done():
            await queue.put(None)
            try:
                await asyncio.wait_for(worker_task, timeout=2)
            except Exception:
                worker_task.cancel()


if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="app")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)
