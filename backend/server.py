"""FastAPI + WebSocket 服务：机器人头部对话的服务端编排。

职责概览：
- 提供 WebSocket 端点 /ws，承载「全链路流式」对话；
- 每来一句用户文本，就调用本地 LLM（Ollama）流式生成，
  经中央调度（text_router）清洗/分句/抽取动作标记后，逐条推给前端；
- 通过 ConversationManager 管理多轮上下文（滑动窗口 + 摘要 + 打断安全）；
- 把每轮的「实际上下文」与「模型输出」打印到控制台并写入日志文件，便于调试。

数据流：
  前端文本 → 入队 → worker 取出 → _run_dialog
    → LLM 流式 token → route() 分句/抽动作 → WS 推送 sentence/action → 前端播报
"""
import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from config import HOST, PORT, LLM_PROVIDER, OLLAMA_MODEL, ARK_MODEL, LOG_CONTEXT, CONTEXT_LOG_FILE
from pipeline.llm_client import stream_chat, chat_once
from pipeline.text_router import route
from pipeline.conversation import ConversationManager

app = FastAPI(title="Robot Head Demo")
# 全局唯一的会话管理器：按 session_id 维护每个用户的多轮历史。
conversations = ConversationManager()


# =====================================================================
# 上下文日志：把「发给 LLM 的上下文」和「模型输出」同时打到控制台 + 文件
# 由 config 的 LOG_CONTEXT / CONTEXT_LOG_FILE 控制，方便排查上下文问题。
# =====================================================================
_log_fh = None  # 日志文件句柄；打开失败或未启用时为 None
if LOG_CONTEXT and CONTEXT_LOG_FILE:
    try:
        # 确保日志目录存在，再以追加模式打开
        os.makedirs(os.path.dirname(CONTEXT_LOG_FILE), exist_ok=True)
        _log_fh = open(CONTEXT_LOG_FILE, "a", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 无法打开上下文日志文件: {e}")
        _log_fh = None


def _emit(line: str):
    """统一输出一行日志：控制台 print + 写文件（若启用）。"""
    if not LOG_CONTEXT:
        return
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()  # 立即落盘，保证前台 tail 时能实时看到


def _log_context(session, messages, user_text, who=""):
    """打印本轮「真实发送给 LLM 的完整上下文」，用于核对多轮记忆是否正确。

    who: 连接标识前缀（如 "#a1b2c3 127.0.0.1:54321"），用于区分是哪个客户端。
    """
    ts = datetime.now().strftime("%H:%M:%S")
    lines = ["\n" + "=" * 70,
             f"[{ts}][{who}][会话 {session.id[:8]}] 用户输入: {user_text}",
             f"[发送给 LLM 的上下文 · 共 {len(messages)} 条 · "
             f"约 {sum(len(m['content']) for m in messages)} 字]"]
    # 逐条列出 system / 历史 user / 历史 assistant / 本轮 user
    for i, m in enumerate(messages):
        role = {"system": "系统", "user": "用户", "assistant": "小柚"}.get(m["role"], m["role"])
        content = m["content"].replace("\n", " ")
        # 过长内容截断显示，避免刷屏
        if len(content) > 200:
            content = content[:200] + f"…(+{len(m['content']) - 200}字)"
        lines.append(f"  {i:>2}. [{role}] {content}")
    if session.summary:
        lines.append(f"[当前记忆摘要] {session.summary}")
    lines.append("-" * 70)
    _emit("\n".join(lines))


def _log_output(session, assistant_raw, note="", who=""):
    """打印本轮模型输出（note 用于标注「被打断」等状态，who 为连接标识前缀）。"""
    ts = datetime.now().strftime("%H:%M:%S")
    _emit(f"[{ts}][{who}][会话 {session.id[:8]}] 模型输出{note}: {assistant_raw.strip()}\n" + "=" * 70)


# 前端静态资源目录（与 backend 同级的 frontend/）
FRONTEND_DIR = (Path(__file__).resolve().parent.parent / "frontend")


# =====================================================================
# HTTP 路由
# =====================================================================
@app.get("/")
async def index():
    """根路径重定向到前端页面。"""
    return RedirectResponse(url="/app/index.html")


@app.get("/api/health")
async def health():
    """健康检查：确认服务在线并回报当前使用的模型名。"""
    model = ARK_MODEL if LLM_PROVIDER == "ark" else OLLAMA_MODEL
    return {"ok": True, "provider": LLM_PROVIDER, "model": model}


# =====================================================================
# 单轮对话核心：LLM 流式 → 中央调度 → WS 推送，并做上下文安全提交
# =====================================================================
async def _run_dialog(ws: WebSocket, session, text: str, cancel: asyncio.Event, who: str = ""):
    """处理一轮对话。

    参数:
        ws:      WebSocket 连接，用于把结果推给前端。
        session: 该用户的会话对象（持有历史/摘要）。
        text:    本轮用户输入。
        cancel:  打断信号；一旦被 set，就尽快停止本轮。

    上下文提交规则（关键）：
        - 正常结束 → 把 user + 清洗后的 assistant 一起写入历史；
        - 被打断且已说了部分 → 记录已说部分并标注「被打断」；
        - 被打断且啥都没说 / 出错 → 回滚本轮 user，避免留下「孤儿」消息。
    """
    # 1) 开启新一轮，并组装本轮真正要发给 LLM 的消息（system + 窗口历史 + 本轮 user）
    session.begin_turn(text)
    messages = session.build_messages(text)
    _log_context(session, messages, text, who)

    assistant_text = []   # 收集模型完整输出（含动作标记，用于日志/入库）
    produced = False      # 是否已向前端产出过至少一句（用于打断时判断）

    async def token_stream():
        """把 Ollama 的流式 token 逐个吐出；收到打断信号就停。"""
        async for tok in stream_chat(messages):
            if cancel.is_set():
                break
            assistant_text.append(tok)
            yield tok

    # 2) 让中央调度消费 token 流，产出「整句」与「动作指令」，分别推给前端
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
        # LLM/网络等异常：回滚本轮，并把错误告知前端
        session.rollback_turn()
        _log_output(session, f"[出错] {e}", who=who)
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False))
        except Exception:
            pass
        return

    # 3) 被打断的收尾处理
    if cancel.is_set():
        partial = "".join(assistant_text).strip()
        if produced and partial:
            # 已经说了半句：记入历史并标注，保持上下文连贯
            _log_output(session, partial, "（被打断）", who=who)
            session.commit_turn(partial + "（被打断）")
        else:
            # 还没开口就被打断：直接回滚，不留半截记录
            session.rollback_turn()
            _log_output(session, "(打断，无产出，已回滚)", who=who)
        return

    # 4) 正常结束：写入历史 + 通知前端本轮结束
    _log_output(session, "".join(assistant_text), who=who)
    session.commit_turn("".join(assistant_text))
    await ws.send_text(json.dumps({"type": "llm_done"}, ensure_ascii=False))

    # 5) 视情况把被裁掉的旧历史压缩成「记忆摘要」（长期记忆，不阻塞主流程）
    try:
        await conversations.maybe_summarize(session, chat_once)
    except Exception:
        pass


# =====================================================================
# WebSocket 端点：接收前端消息，用「队列 + 单 worker」保证顺序处理
# =====================================================================
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    conn_id = uuid.uuid4().hex[:6]
    peer = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
    _emit(f"[WS #{conn_id}] 新连接来自 {peer}")

    session = None                       # 本连接绑定的会话，收到 hello 后确定
    queue: asyncio.Queue = asyncio.Queue()  # 待处理的用户消息队列
    worker_current = {"cancel": None}    # 记录「当前正在处理的那轮」的打断信号

    async def worker():
        """后台协程：串行地从队列取消息并处理。

        为什么用队列串行处理？
        - LLM 首字较慢，用户可能连打好几句；
        - 若来一句就打断上一句，未开口的那轮会被丢弃、上下文缺失；
        - 因此改为「排队依次说」，只有点「打断」按钮才主动中止。
        """
        while True:
            text = await queue.get()
            if text is None:  # 收到停止哨兵，退出
                break
            c = asyncio.Event()
            worker_current["cancel"] = c  # 暴露给外层，供 interrupt/clear 触发
            try:
                who = f"#{conn_id} {peer}"
                await _run_dialog(ws, session, text, c, who)
            except Exception:
                session.rollback_turn()
            finally:
                worker_current["cancel"] = None
                queue.task_done()

    def _drain_queue():
        """清空排队中还没处理的消息（用于打断/清空场景）。"""
        while not queue.empty():
            try:
                queue.get_nowait()
                queue.task_done()
            except Exception:
                break

    worker_task = None
    try:
        while True:
            # ---- 接收并解析前端的一条 JSON 消息 ----
            raw = await ws.receive_text()
            # 原始帧日志：无论能否解析，都打印收到的原始内容，
            # 用于区分「服务端根本没收到」还是「收到了但格式/类型不对」。
            _emit(f"[WS-RECV #{conn_id}] {raw[:200]}")
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                _emit(f"[WS-RECV #{conn_id}] JSON 解析失败，已忽略该帧")
                continue
            mtype = msg.get("type")

            # ---- 会话绑定：只认 hello，绝不用其它帧来绑定会话 ----
            # 关键隔离点：每个 WS 连接必须先发 hello 才能确定自己的会话；
            # 这样心跳 ping / interrupt / clear 等无 session 字段的帧，
            # 不会误触发「生成随机新会话」，也不会串到别的连接。
            if session is None:
                if mtype != "hello":
                    _emit(f"[WS #{conn_id}] 未绑定会话前收到 {mtype!r}，已忽略，等待 hello")
                    continue
                sid = (msg.get("session") or "").strip() or uuid.uuid4().hex
                session = conversations.get(sid)
                had = bool((msg.get("session") or "").strip())
                _emit(f"[WS #{conn_id}] 绑定会话 {sid[:8]}（客户端提供旧会话: {had}）")
                await ws.send_text(json.dumps({"type": "session", "session": sid}, ensure_ascii=False))
                worker_task = asyncio.create_task(worker())
                continue

            # ---- 打断：中止当前这轮 + 清空排队 ----
            if mtype == "interrupt":
                c = worker_current.get("cancel")
                if c:
                    c.set()
                _drain_queue()
                continue

            # ---- 清空：打断 + 清队 + 清空该会话的历史记忆 ----
            if mtype == "clear":
                c = worker_current.get("cancel")
                if c:
                    c.set()
                _drain_queue()
                conversations.clear(session.id)
                await ws.send_text(json.dumps({"type": "cleared"}, ensure_ascii=False))
                continue

            # ---- 普通用户消息：入队，交给 worker 顺序处理 ----
            if mtype == "user_message":
                text = (msg.get("text") or "").strip()
                if text:
                    await queue.put(text)
    except WebSocketDisconnect:
        # 前端断开：走 finally 收尾
        pass
    finally:
        # 优雅关闭：中止当前轮，投递停止哨兵，等 worker 退出（超时则强制取消）
        c = worker_current.get("cancel")
        if c:
            c.set()
        if worker_task and not worker_task.done():
            await queue.put(None)
            try:
                await asyncio.wait_for(worker_task, timeout=2)
            except Exception:
                worker_task.cancel()


# 静态资源禁用缓存：避免手机/浏览器一直用旧版前端 JS 导致行为异常。
@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/app") and (path.endswith(".js") or path.endswith(".html") or path == "/app" or path.endswith("/")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# 挂载前端静态资源（放在路由定义之后，避免覆盖 API 路由）
if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="app")


if __name__ == "__main__":
    # 直接运行本文件即启动服务（开发用）。生产可用 uvicorn 命令另行部署。
    import uvicorn

    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)


