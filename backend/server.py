"""FastAPI + WebSocket 服务：编排全链路流式流水线。"""
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from config import SYSTEM_PROMPT, HOST, PORT, OLLAMA_MODEL
from pipeline.llm_client import stream_chat
from pipeline.text_router import route

app = FastAPI(title="Robot Head Demo")

FRONTEND_DIR = (Path(__file__).resolve().parent.parent / "frontend")


@app.get("/")
async def index():
    return RedirectResponse(url="/app/index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "model": OLLAMA_MODEL}


async def _run_dialog(ws: WebSocket, history, text: str, cancel: asyncio.Event):
    """跑一轮对话：LLM 流式 -> 中央调度 -> WS 推送。"""
    history.append({"role": "user", "content": text})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    assistant_text = []

    async def token_stream():
        async for tok in stream_chat(messages):
            if cancel.is_set():
                break
            assistant_text.append(tok)
            yield tok

    seq = 0
    async for event in route(token_stream()):
        if cancel.is_set():
            break
        if event["type"] == "sentence":
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

    history.append({"role": "assistant", "content": "".join(assistant_text)})
    if not cancel.is_set():
        await ws.send_text(json.dumps({"type": "llm_done"}, ensure_ascii=False))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    history = []
    current: asyncio.Task | None = None
    cancel = asyncio.Event()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")
            if mtype == "interrupt":
                cancel.set()
                if current and not current.done():
                    current.cancel()
                continue

            if mtype == "user_message":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                # 打断上一轮
                if current and not current.done():
                    cancel.set()
                    current.cancel()
                    try:
                        await current
                    except (asyncio.CancelledError, Exception):
                        pass
                cancel = asyncio.Event()

                async def runner(t=text, c=cancel):
                    try:
                        await _run_dialog(ws, history, t, c)
                    except Exception as e:  # noqa: BLE001
                        try:
                            await ws.send_text(json.dumps(
                                {"type": "error", "message": str(e)},
                                ensure_ascii=False,
                            ))
                        except Exception:
                            pass

                current = asyncio.create_task(runner())
    except WebSocketDisconnect:
        if current and not current.done():
            current.cancel()


# 静态前端
if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="app")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)
