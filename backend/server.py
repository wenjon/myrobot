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
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from config import (
    HOST, PORT, LLM_PROVIDER, OLLAMA_MODEL, ARK_MODEL, LOG_CONTEXT, CONTEXT_LOG_FILE,
    SHOW_REAL_IP, INTERRUPT_MODE,
)
from pipeline.llm_client import stream_chat, chat_once
from pipeline.text_router import route
from pipeline.conversation import conversations
from pipeline.agent import agent_stream
from pipeline.turn_policy import classify_incoming
from tools import REGISTRY, RESOURCES, load_all

# server_app 拆出的业务辅助模块：
#   - logging：控制台+文件日志器
#   - peers：WS 客户端地址格式化
#   - dialog：单轮对话主循环
#   - notify：服务端主动下发辅助消息（如 interrupted）
from server_app.dialog import run_dialog as _run_dialog_real
from server_app.logging import get_logger
from server_app.notify import notify_interrupted as _notify_interrupted
from server_app.notify import send_json as _send_json
from server_app.peers import format_peer

# ---------------------------------------------------------------------
# 应用生命周期（lifespan）：启动时初始化工具、关闭时释放资源。
# 这是 FastAPI 新推荐的写法，取代已弱化的 @app.on_event("startup"/"shutdown")。
#   - yield 之前的代码在应用接受请求前运行；
#   - yield 之后的代码在应用关闭时运行（包括任何异常退出）。
# 必须在 FastAPI(...) 之前定义，所以放在这里。
# ---------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup 阶段：预留扩展点（如初始化定时任务 / 预热连接池等） ----
    try:
        yield
    finally:
        # ---- shutdown 阶段：释放工具占用的共享资源（HTTP client / 未来的 DB 连接池 / 串口句柄） ----
        await REGISTRY.teardown_all()
        await RESOURCES.aclose()


app = FastAPI(title="Robot Head Demo", lifespan=lifespan)
# 会话管理器是 **pipeline.conversation 里的全局单例**，不在这里新建。
# 为什么：server_app/dialog.py 在反思环节也要拿同一个管理器，
# 两边各建一个的话记忆会分家（这曾导致摘要功能静默失效）。


# =====================================================================
# 全局初始化：日志器 -> 工具框架
# =====================================================================
# 起动日志器（控制台 + 可选文件）。
LOGGER = get_logger()
LOGGER.init()
_emit = LOGGER.emit  # 保留本名以便迁移阶段少改动原有代码

# 工具框架接入日志系统，并在启动时自动发现/加载所有工具。
REGISTRY.set_logger(_emit)
# 记忆模块（含 FileStore）也接入同一套日志，便于在控制台看到
# 「从磁盘恢复了多少条历史 / 画像合并了什么 / 冲突等确认」。
conversations.set_logger(_emit)
_loaded_tools = load_all(_emit)
_emit(f"[启动] 已加载工具: {[t.name for t in REGISTRY.all()]}")


def _check_secrets() -> None:
    """启动自检：密钥已从代码中移除，换机器后必须自己填 .env。

    只做提示不阻断启动：没有 Tavily key 也能聊天，只是不能联网搜索；
    没有 Ark key 则可以把 LLM_PROVIDER 改成 ollama 跑本地模型。
    """
    from config import ARK_API_KEY, TAVILY_API_KEY

    if LLM_PROVIDER == "ark" and not ARK_API_KEY:
        _emit("[启动][警告] LLM_PROVIDER=ark 但 ARK_API_KEY 为空："
              "请将 .env.example 复制为 .env 并填入密钥，或改用 LLM_PROVIDER=ollama / llamacpp。")
    if not TAVILY_API_KEY:
        _emit("[启动][提示] TAVILY_API_KEY 未配置，web_search 联网搜索将不可用。")


_check_secrets()


def _fmt_peer(ws) -> str:
    """薄包装：转发到 server_app.peers.format_peer。"""
    return format_peer(ws)


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
# 单轮对话主循环已迁出到 server_app/dialog.py（run_dialog）。
# 为避免外部调用大量改名，保留本名薄包装。
# =====================================================================
async def _run_dialog(ws, session, text, cancel, who=""):
    """薄包装：转发到 server_app.dialog.run_dialog。"""
    return await _run_dialog_real(ws, session, text, cancel, who)


# =====================================================================
# WebSocket 端点：接收前端消息，用「队列 + 单 worker」保证顺序处理
# =====================================================================
# ---------------------------------------------------------------------
# WS 协议总览（完整定义见 docs/.../05_*）：
#   C -> S: hello / user_message / interrupt / clear / ping / profile_resolve
#   S -> C: session / sentence / action / status / llm_done / error / cleared /
#           interrupted / profile_conflict / profile_resolved
# 连接生命周期： accept -> 绑定会话 -> 开 worker -> 处理 -> 优雅关闭
# ---------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    conn_id = uuid.uuid4().hex[:6]
    peer = _fmt_peer(ws)
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

            # ---- 画像冲突确认（B 方案，docs § 16.8）----
            # 前端确认卡点「确认/拒绝」后会发这个帧。不走对话队列：
            # 它只改记忆、不触发 LLM，即使数字人正在说话也能立即处理。
            if mtype == "profile_resolve":
                cid = (msg.get("conflict_id") or "").strip()
                action = (msg.get("action") or "").strip()
                accept = action == "accept"
                c = session.resolve_conflict(cid, accept)
                if c is None:
                    # 未知/已过期的 id：可能是用户隔了很久才点，或重复提交
                    _emit(f"[记忆 #{conn_id}] profile_resolve 未知或已过期的 conflict_id={cid!r}")
                else:
                    _emit(f"[记忆 #{conn_id}] 画像冲突已{'接受' if accept else '拒绝'}: "
                          f"{c.field_name} {c.old_value!r} -> {c.new_value!r}")
                # 无论成功与否都回一帧，让前端能销毁确认卡（不致于永久悬在那里）
                await _send_json(ws, {
                    "type": "profile_resolved",
                    "conflict_id": cid,
                    "action": "accept" if accept else "reject",
                    "ok": c is not None,
                })
                continue

            # ---- 普通用户消息：按轮次策略决定「继续说 / 打断 / 排队」----
            if mtype == "user_message":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                busy = worker_current.get("cancel") is not None  # 数字人是否正在说

                if INTERRUPT_MODE == "queue" or not busy:
                    # 排队模式，或当前空闲：直接入队顺序处理（旧行为）。
                    await queue.put(text)
                    continue

                if INTERRUPT_MODE == "always":
                    # 硬打断：任何新消息都立即中止当前播报，再处理新句。
                    _emit(f"[轮次 #{conn_id}] always 打断当前播报，处理新消息: {text}")
                    c = worker_current.get("cancel")
                    if c:
                        c.set()
                        # 通知前端做自然收尾（渐弱 + “我在听”倾听表情）
                        await _notify_interrupted(ws, reason="always")
                    _drain_queue()
                    await queue.put(text)
                    continue

                # smart 模式：先判断这句是「附和 / 打断 / 新提问」
                kind = classify_incoming(text)
                if kind == "backchannel":
                    # 附和词（嗯/对/好的）：数字人正在说，不打断也不排队，只记录。
                    _emit(f"[轮次 #{conn_id}] smart 判定为附和，继续说不打断: {text}")
                    continue
                # 打断词 或 新提问：中止当前轮 + 清队，再处理这句（barge-in）。
                _emit(f"[轮次 #{conn_id}] smart 判定为 {kind}，打断当前播报并处理: {text}")
                c = worker_current.get("cancel")
                if c:
                    c.set()
                    # 通知前端做自然收尾（渐弱 + “我在听”倾听表情）
                    await _notify_interrupted(ws, reason=kind)
                _drain_queue()
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


# ---------------------------------------------------------------------
# 应用生命周期（lifespan）：启动时初始化工具、关闭时释放资源。
# 这是 FastAPI 新推荐的写法，取代已弱化的 @app.on_event("startup"/"shutdown")。
#   - yield 之前的代码在应用接受请求前运行；
#   - yield 之后的代码在应用关闭时运行（包括任何异常退出）。
# ---------------------------------------------------------------------
if __name__ == "__main__":
    # 直接运行本文件即启动服务（开发用）。生产可用 uvicorn 命令另行部署。
    import uvicorn

    # proxy_headers：True 时信任 cloudflared 等反代的转发头以显示真实来源 IP；
    # SHOW_REAL_IP=0 时关闭，来源恒为本机 127.0.0.1。
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False,
                proxy_headers=SHOW_REAL_IP)





