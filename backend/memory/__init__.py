"""记忆模块（docs 第 16 章）。

分层（§ 16.3）：
    L1 工作层  → 每轮重新拼的 messages（在 pipeline/conversation.py）
    L2 会话层  → Session 对象 + sessions/<id>.json
    L3 长期层  → UserProfile + 会话摘要，存 long_term.json

对外只露这几个名字，内部模块划分可以自由调整。
"""
from .types import MemoryKind, MemoryQuery, MemoryHit, MemoryRetriever
from .retriever import AllMemoryRetriever
from .profile import UserProfile, ProfileConflict, FIELD_LABELS
from .store import FileStore, STORE

__all__ = [
    "MemoryKind", "MemoryQuery", "MemoryHit", "MemoryRetriever",
    "AllMemoryRetriever",
    "UserProfile", "ProfileConflict", "FIELD_LABELS",
    "FileStore", "STORE",
]
