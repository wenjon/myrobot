"""对话上下文管理（P0 + P1）。

功能：
- 按 session_id 管理多会话，与单个 WS 连接解耦（刷新/重连不丢记忆）；
- 滑动窗口裁剪：按轮数 + 字符预算，system 永远置顶；
- 打断安全：只在正常结束时写入 assistant，被打断的残句不入库；
- 入库清洗：assistant 文本剥离 [表情:x]/[动作:x] 与 markdown；
- 错误回滚：某轮失败时弹出已 append 的 user 消息，避免孤儿；
- 摘要式长期记忆（P1）：被裁掉的旧对话压缩成一段摘要注入 system 顶部；
- 会话 TTL 回收。
"""
import re
import time
from typing import Callable, Dict, List, Optional, Awaitable

from config import (
    SYSTEM_PROMPT,
    MAX_TURNS,
    MAX_CONTEXT_CHARS,
    ENABLE_SUMMARY,
    SUMMARY_TRIGGER_CHARS,
    SESSION_TTL,
)

_TAG_RE = re.compile(r"\[(表情|emotion|Emotion|动作|action)\s*[:：][^\]]*\]")
_MD_RE = re.compile(r"[*_`#>~]")


def clean_for_memory(text: str) -> str:
    """入库前清洗：去掉动作/表情标记与 markdown，仅保留播报文本。"""
    text = _TAG_RE.sub("", text)
    text = _MD_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


class Session:
    def __init__(self, session_id: str):
        self.id = session_id
        self.history: List[Dict[str, str]] = []  # 已完成轮次的 user/assistant
        self.summary: str = ""                    # 长期记忆摘要
        self.pending_user: Optional[str] = None    # 本轮 user，尚未确认成对
        self.dropped_buffer: List[Dict[str, str]] = []  # 已裁剪待摘要的历史
        self.updated_at = time.time()

    # ---- 轮次生命周期 ----
    def begin_turn(self, user_text: str) -> None:
        self.pending_user = user_text
        self.updated_at = time.time()

    def commit_turn(self, assistant_raw: str) -> None:
        """正常结束：user + 清洗后的 assistant 一起入库。"""
        assistant = clean_for_memory(assistant_raw)
        if self.pending_user is None:
            return
        self.history.append({"role": "user", "content": self.pending_user})
        if assistant:
            self.history.append({"role": "assistant", "content": assistant})
        self.pending_user = None
        self.updated_at = time.time()

    def rollback_turn(self) -> None:
        """错误/打断且无有效输出：丢弃本轮 user，避免孤儿。"""
        self.pending_user = None
        self.updated_at = time.time()

    def clear(self) -> None:
        self.history.clear()
        self.summary = ""
        self.pending_user = None
        self.dropped_buffer.clear()
        self.updated_at = time.time()

    # ---- 窗口裁剪 ----
    def _char_count(self, msgs: List[Dict[str, str]]) -> int:
        return sum(len(m["content"]) for m in msgs)

    def _trim(self) -> List[Dict[str, str]]:
        """返回裁剪后的窗口历史，同时把被裁掉的部分挪到 dropped_buffer。"""
        hist = self.history
        # 1) 按轮数裁剪（1 轮≈2 条）
        max_msgs = MAX_TURNS * 2
        if len(hist) > max_msgs:
            cut = len(hist) - max_msgs
            self.dropped_buffer.extend(hist[:cut])
            self.history = hist[cut:]
            hist = self.history
        # 2) 再按字符预算从头裁
        while hist and self._char_count(hist) > MAX_CONTEXT_CHARS:
            self.dropped_buffer.append(hist.pop(0))
        return list(self.history)

    def build_messages(self, pending_user: str) -> List[Dict[str, str]]:
        """组装发给 LLM 的消息：system(+摘要) + 窗口历史 + 本轮 user。"""
        window = self._trim()
        system = SYSTEM_PROMPT
        if self.summary:
            system += f"\n\n【对话记忆摘要】{self.summary}"
        msgs = [{"role": "system", "content": system}]
        msgs.extend(window)
        msgs.append({"role": "user", "content": pending_user})
        return msgs

    def needs_summary(self) -> bool:
        return (
            ENABLE_SUMMARY
            and self._char_count(self.dropped_buffer) >= SUMMARY_TRIGGER_CHARS
        )


class ConversationManager:
    def __init__(self):
        self._sessions: Dict[str, Session] = {}

    def get(self, session_id: str) -> Session:
        self._gc()
        s = self._sessions.get(session_id)
        if s is None:
            s = Session(session_id)
            self._sessions[session_id] = s
        return s

    def clear(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._sessions[session_id].clear()

    def _gc(self) -> None:
        now = time.time()
        expired = [k for k, v in self._sessions.items() if now - v.updated_at > SESSION_TTL]
        for k in expired:
            del self._sessions[k]

    async def maybe_summarize(
        self,
        session: Session,
        summarizer: Callable[[List[Dict[str, str]]], Awaitable[str]],
    ) -> None:
        """把 dropped_buffer 压缩进 summary（调用方提供 LLM 摘要函数）。"""
        if not session.needs_summary():
            return
        old = session.dropped_buffer
        session.dropped_buffer = []
        prior = session.summary
        convo_text = "\n".join(
            f"{'用户' if m['role']=='user' else '小柚'}：{m['content']}" for m in old
        )
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是对话记忆压缩器。把下面的历史对话浓缩成不超过120字的中文要点，"
                    "保留用户偏好、称呼、关键事实与未完成事项。只输出摘要本身，不要客套。"
                ),
            },
            {
                "role": "user",
                "content": (f"已有摘要：{prior}\n\n" if prior else "")
                + f"新的历史片段：\n{convo_text}",
            },
        ]
        try:
            summary = (await summarizer(prompt)).strip()
            if summary:
                session.summary = clean_for_memory(summary)[:400]
        except Exception:
            # 摘要失败：把旧内容放回，等下次再试，避免记忆丢失
            session.dropped_buffer = old + session.dropped_buffer
