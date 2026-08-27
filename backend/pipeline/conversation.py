"""对话上下文与记忆管理（P0 + P1 + 第 16 章记忆模块）。

本文件是「记忆」的主入口，三层存储在这里汇合（docs § 16.3）：

    L1 工作层   build_messages() 每轮重新拼出的 messages（不落盘，成本为零）
    L2 会话层   Session.history / dropped_buffer，落盘 sessions/<id>.json
    L3 长期层   Session.summary + Session.profile，落盘 long_term.json

功能清单：
- 按 session_id 管理多会话，与单个 WS 连接解耦（刷新/重连不丢记忆）；
- 滑动窗口裁剪：按轮数 + 字符预算，system 永远置顶；
- 打断安全：只在正常结束时写入 assistant，被打断的残句不入库；
- 入库清洗：assistant 文本剥离 [表情:x]/[动作:x] 与 markdown；
- 错误回滚：某轮失败时弹出已 append 的 user 消息，避免孤儿；
- 反思沉淀：被裁掉的旧对话→LLM 提炼→「摘要 + 用户画像」双字段（§ 16.6）；
- 画像冲突：值变更时不自作主张，生成待确认冲突（B 方案，§ 16.8）；
- 磁盘持久化：跳进程重启不丢记忆（§ 16.9）；
- 会话 TTL 回收。
"""
from __future__ import annotations

import json
import re
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from config import (
    SYSTEM_PROMPT,
    MAX_TURNS,
    MAX_CONTEXT_CHARS,
    ENABLE_SUMMARY,
    SUMMARY_TRIGGER_CHARS,
    SESSION_TTL,
    PROFILE_FIELDS,
    MEMORY_FLUSH_EVERY_N_TURNS,
    MEMORY_MAX_LONG_TERM_CHARS,
    PROFILE_MAX_CONFLICTS_PER_TURN,
)
from memory import (
    STORE,
    AllMemoryRetriever,
    MemoryQuery,
    ProfileConflict,
    UserProfile,
)

_TAG_RE = re.compile(r"\[(表情|emotion|Emotion|动作|action)\s*[:：][^\]]*\]")
_MD_RE = re.compile(r"[*_`#>~]")


def clean_for_memory(text: str) -> str:
    """入库前清洗：去掉动作/表情标记与 markdown，仅保留播报文本。"""
    text = _TAG_RE.sub("", text)
    text = _MD_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


# =====================================================================
# Session：单个会话的全部记忆
# =====================================================================
class Session:
    """一个会话的 L2 + L3 记忆容器。"""

    def __init__(self, session_id: str):
        self.id = session_id
        # ---- L2 情景记忆 ----
        self.history: List[Dict[str, str]] = []          # 已完成轮次的 user/assistant
        self.dropped_buffer: List[Dict[str, str]] = []   # 已裁剪待摘要的历史
        self.pending_user: Optional[str] = None          # 本轮 user，尚未确认成对
        # ---- L3 语义记忆 ----
        self.summary: str = ""                           # 会话摘要
        self.profile: UserProfile = UserProfile()        # 用户画像
        # ---- 画像冲突（B 方案）：conflict_id -> ProfileConflict ----
        self.pending_conflicts: Dict[str, ProfileConflict] = {}
        # ---- 运行时计数 ----
        self.turn_count = 0          # 累计完成轮数（用于每 N 轮刷盘）
        self.updated_at = time.time()
        # 检索器：本期是全量拼接，未来换 VectorRetriever 只改这一行
        self._retriever = AllMemoryRetriever()

    # ---------------- 轮次生命周期 ----------------
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
        self.turn_count += 1
        self.updated_at = time.time()
        # 中量级刷盘：每 N 轮落盘一次，平衢「崩溃丢记忆」与「频繁 IO」
        if MEMORY_FLUSH_EVERY_N_TURNS > 0 and self.turn_count % MEMORY_FLUSH_EVERY_N_TURNS == 0:
            self.save()

    def rollback_turn(self) -> None:
        """错误/打断且无有效输出：丢弃本轮 user，避免孤儿。"""
        self.pending_user = None
        self.updated_at = time.time()

    def clear(self) -> None:
        """清空本会话的全部记忆（含画像）并同步删盘。

        为什么要删盘：不删的话下次启动会把「已清空」的历史又读回来，
        用户会觉得「清空根本没生效」。
        """
        self.history.clear()
        self.summary = ""
        self.pending_user = None
        self.dropped_buffer.clear()
        self.profile.clear()
        self.pending_conflicts.clear()
        self.turn_count = 0
        self.updated_at = time.time()
        STORE.delete_session(self.id)
        STORE.delete_long_term()

    # ---------------- 窗口裁剪 ----------------
    def _char_count(self, msgs: List[Dict[str, str]]) -> int:
        return sum(len(m["content"]) for m in msgs)

    def _trim(self) -> List[Dict[str, str]]:
        """返回裁剪后的窗口历史，同时把被裁掉的部分挠到 dropped_buffer。"""
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

    # ---------------- L1：拼出本轮上下文 ----------------
    def build_messages(self, pending_user: str) -> List[Dict[str, str]]:
        """组装发给 LLM 的消息：system(+长期记忆) + 窗口历史 + 本轮 user。

        长期记忆（画像 + 摘要）通过检索器取出而不是直读字段，
        这样以后换向量检索时本方法一行都不用改（docs § 16.7）。
        """
        window = self._trim()

        # 从记忆层检索该贴进 system 的内容
        hits = self._retriever.retrieve(
            self,
            MemoryQuery(
                text=pending_user,
                session_id=self.id,
                max_chars=MEMORY_MAX_LONG_TERM_CHARS,
            ),
        )
        system = SYSTEM_PROMPT
        for h in hits:
            system += f"\n\n{h.text}"

        msgs = [{"role": "system", "content": system}]
        msgs.extend(window)
        msgs.append({"role": "user", "content": pending_user})
        return msgs

    def needs_summary(self) -> bool:
        return (
            ENABLE_SUMMARY
            and self._char_count(self.dropped_buffer) >= SUMMARY_TRIGGER_CHARS
        )

    # ---------------- 画像冲突（B 方案） ----------------
    def take_conflicts_to_notify(self) -> List[ProfileConflict]:
        """取出本轮该下发给前端的冲突（最多 N 条）。

        超出上限的留在 pending_conflicts 里，下轮再下发；
        已过期的先按「保旧」清理掉。
        """
        self.expire_conflicts()
        pending = list(self.pending_conflicts.values())
        return pending[:PROFILE_MAX_CONFLICTS_PER_TURN]

    def expire_conflicts(self) -> List[ProfileConflict]:
        """清理超时未回应的冲突（默认保旧），返回被清理的列表。"""
        expired = [c for c in self.pending_conflicts.values() if c.is_expired()]
        for c in expired:
            self.pending_conflicts.pop(c.conflict_id, None)
        return expired

    def resolve_conflict(self, conflict_id: str, accept: bool) -> Optional[ProfileConflict]:
        """处理前端的 profile_resolve；未知 id 返回 None。"""
        c = self.pending_conflicts.pop(conflict_id, None)
        if c is None:
            return None
        self.profile.apply_conflict(c, accept)
        self.updated_at = time.time()
        self.save()   # 画像变更是高价值信息，立即落盘
        return c

    # ---------------- 持久化 ----------------
    def to_dict(self) -> Dict[str, object]:
        """序列化 L2 部分（不含 profile，profile 属于跳会话的 L3）。"""
        return {
            "id": self.id,
            "history": self.history,
            "dropped_buffer": self.dropped_buffer,
            "summary": self.summary,
            "turn_count": self.turn_count,
            "updated_at": self.updated_at,
        }

    def load_from(self, data: Dict[str, object]) -> None:
        """从 sessions/<id>.json 还原 L2；字段缺失/类型不对一律忽略。"""
        hist = data.get("history")
        if isinstance(hist, list):
            self.history = [m for m in hist if isinstance(m, dict) and m.get("content")]
        dropped = data.get("dropped_buffer")
        if isinstance(dropped, list):
            self.dropped_buffer = [m for m in dropped if isinstance(m, dict) and m.get("content")]
        if isinstance(data.get("summary"), str):
            self.summary = data["summary"]
        if isinstance(data.get("turn_count"), int):
            self.turn_count = data["turn_count"]
        if isinstance(data.get("updated_at"), (int, float)):
            self.updated_at = float(data["updated_at"])

    def save(self) -> None:
        """落盘：L2 写会话文件，L3 写长期文件。"""
        STORE.save_session(self.id, self.to_dict())
        STORE.save_long_term({
            "profile": self.profile.to_dict(),
            "last_session_id": self.id,
            "updated_at": time.time(),
        })


# =====================================================================
# 反思（提炼）提示词与解析
# =====================================================================
# 为什么要求 JSON：旧版只要一段纯文本摘要，现在需要同时拿到
# 「摘要」与「画像」两部分（docs § 16.6），结构化输出最好拆。
_REFLECT_SYSTEM = (
    "你是对话记忆提炼器。阅读历史对话，输出一个 JSON（不要代码块、不要解释）：\n"
    '{"summary": "不超过120字的中文要点", '
    '"profile": {"name": "", "preferences": "", "occupation": ""}, '
    '"quote": "支持 profile 判断的用户原句"}\n'
    "summary：保留关键事实与未完成事项。\n"
    "profile：只填用户明确表达过的长期信息——name 是称呼，"
    "preferences 是偏好/习惯/注意事项，occupation 是职业角色。\n"
    "重要：不确定就留空字符串，绝对不要猜。一次性事件（如「今天吃了火锅」）"
    "不是偏好，不要写进 profile。"
)


def _parse_reflection(raw: str) -> Tuple[str, Dict[str, str], str]:
    """解析提炼结果，返回 (summary, profile_dict, quote)。

    LLM 并不总是严格输出 JSON（可能包 ```json 代码块、可能带前缀废话），
    所以先抓最外层大括号再解。彻底解不出来就**降级当纯文本摘要**，
    而不是丢掉整次提炼——摘要比画像容错率高，能抢回一部分就抢。
    """
    text = (raw or "").strip()
    if not text:
        return "", {}, ""
    # 去掉 markdown 代码块围栏
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                summary = str(data.get("summary") or "").strip()
                quote = str(data.get("quote") or "").strip()
                prof_raw = data.get("profile")
                profile: Dict[str, str] = {}
                if isinstance(prof_raw, dict):
                    for f in PROFILE_FIELDS:
                        v = prof_raw.get(f)
                        if isinstance(v, str) and v.strip():
                            profile[f] = v.strip()
                return summary, profile, quote
        except Exception:  # noqa: BLE001  JSON 壍了就走下面的降级分支
            pass
    # 降级：整段当摘要，画像本轮不提
    return text, {}, ""


# =====================================================================
# ConversationManager：多会话管理 + 反思调度
# =====================================================================
class ConversationManager:
    """全局会话管理器。对外接口与改造前保持一致。"""

    def __init__(self):
        self._sessions: Dict[str, Session] = {}
        self._log: Optional[Callable[[str], None]] = None

    def set_logger(self, log_fn: Callable[[str], None]) -> None:
        """注入日志函数（复用 server 的 _emit），同时给存储层用。"""
        self._log = log_fn
        STORE.set_logger(log_fn)

    def _emit(self, line: str) -> None:
        if self._log:
            self._log(line)

    def get(self, session_id: str) -> Session:
        """取会话；内存没有则尝试从磁盘恢复（跳重启不丢记忆）。"""
        self._gc()
        s = self._sessions.get(session_id)
        if s is None:
            s = Session(session_id)
            self._restore(s)
            self._sessions[session_id] = s
        return s

    def _restore(self, session: Session) -> None:
        """从磁盘恢复 L2 与 L3。任何失败都只降级为「没记忆」。"""
        data = STORE.load_session(session.id)
        if data:
            session.load_from(data)
            self._emit(f"[记忆] 会话 {session.id[:8]} 已从磁盘恢复："
                       f"{len(session.history)} 条历史，摘要 {len(session.summary)} 字")
        lt = STORE.load_long_term()
        if lt:
            session.profile = UserProfile.from_dict(lt.get("profile"))
            if not session.profile.is_empty():
                self._emit(f"[记忆] 已加载用户画像：{session.profile.as_prompt_text()}")

    def clear(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._sessions[session_id].clear()
            self._emit(f"[记忆] 会话 {session_id[:8]} 已清空（含磁盘）")

    def _gc(self) -> None:
        """TTL 回收：从内存踢出前先落盘，否则空闲超时的会话记忆会相当于被丢掉。"""
        now = time.time()
        expired = [k for k, v in self._sessions.items() if now - v.updated_at > SESSION_TTL]
        for k in expired:
            try:
                self._sessions[k].save()
            except Exception:  # noqa: BLE001
                pass
            del self._sessions[k]

    # ---------------- 反思：情景 → 语义 ----------------
    async def maybe_summarize(
        self,
        session: Session,
        summarizer: Callable[[List[Dict[str, str]]], Awaitable[str]],
    ) -> List[ProfileConflict]:
        """把 dropped_buffer 压缩进 summary，并提炼用户画像。

        返回本次产生的**待确认冲突**列表（可能为空）。
        调用方提供 LLM 摘要函数（一般是 pipeline.llm_client.chat_once）。

        不频繁调 LLM：仅在 dropped_buffer 累计达阈值（默认 1200 字）时触发一次。
        """
        if not session.needs_summary():
            return []

        old = session.dropped_buffer
        session.dropped_buffer = []
        prior = session.summary
        convo_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else '小柚'}：{m['content']}" for m in old
        )
        prompt = [
            {"role": "system", "content": _REFLECT_SYSTEM},
            {
                "role": "user",
                "content": (f"已有摘要：{prior}\n\n" if prior else "")
                + f"新的历史片段：\n{convo_text}",
            },
        ]
        try:
            raw = await summarizer(prompt)
        except Exception as e:  # noqa: BLE001
            # 提炼失败：把旧内容放回，等下次再试，避免记忆丢失
            session.dropped_buffer = old + session.dropped_buffer
            self._emit(f"[记忆] 反思失败，已回滚待摘要缓冲: {e}")
            return []

        summary, prof, quote = _parse_reflection(raw)
        if summary:
            session.summary = clean_for_memory(summary)[:400]
            self._emit(f"[记忆] 摘要已更新（{len(session.summary)} 字）: {session.summary[:60]}")

        conflicts: List[ProfileConflict] = []
        if prof:
            # merge 内部：新字段直接写，值变了才产生冲突
            conflicts = session.profile.merge(prof, source_quote=quote)
            for c in conflicts:
                session.pending_conflicts[c.conflict_id] = c
            if conflicts:
                self._emit(f"[记忆] 画像冲突 {len(conflicts)} 条待用户确认: "
                           + "; ".join(f"{c.field_name}: {c.old_value} -> {c.new_value}" for c in conflicts))
            else:
                self._emit(f"[记忆] 画像已合并（无冲突）: {session.profile.as_prompt_text()}")

        session.save()
        return conflicts


# 全局单例。为什么要定在这里：
#   server.py 与 server_app/dialog.py 都需要拿到**同一个**管理器。
#   之前 dialog.py 里写的 `from pipeline.conversation import conversations` 并不存在（实例建在
#   server.py 里），而外层又包了 try/except，导致**摘要/反思一直静默失效、从未跑过**。
#   现在单例定在模块里，server.py 直接 import 它，两边必然是同一个对象。
conversations = ConversationManager()
