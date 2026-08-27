"""记忆模块 · 检索抽象（不接向量库）。

为什么本期不上向量库（docs § 16.3）：
    当前只有一个用户，记忆完全能被「1 个 summary + 1 个 profile」覆盖，
    检索不是瓶颈；引入 embedding 反而带来冷启动、质量不稳、多层缓存等问题。

但接口先留好，让以后换向量库只需新写一个 `VectorRetriever`，
不动 `build_messages()` 等主体代码。三个概念：

    MemoryQuery     「查什么」—— 在 build_messages() 里构造
    MemoryHit       「查到了什么」—— 带来源与分数，便于未来排序/调试
    MemoryRetriever 接口，默认实现 AllMemoryRetriever（全量拼接）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class MemoryKind(str, Enum):
    """记忆类型（对应 docs § 16.2 的四类记忆）。

    本期只有 SEMANTIC（summary / profile）会被检索；
    EPISODIC 目前直接以滑动窗口形式拼在 messages 里，不过检索层。
    """
    EPISODIC = "episodic"    # 情景：具体发生过的事
    SEMANTIC = "semantic"    # 语义：提炼出的通用认知（摘要 / 画像）
    PROCEDURAL = "procedural"  # 程序：SOP / 工具（本项目在提示词与注册表里）


@dataclass
class MemoryQuery:
    """一次检索请求。

    本期 AllMemoryRetriever 会忽略 text / top_k（全量返回），
    但字段先定义好，以后 VectorRetriever 直接用得上。
    """
    text: str = ""                      # 当前用户输入（未来做语义相似度的 query）
    session_id: str = ""
    kinds: Optional[List[MemoryKind]] = None   # 限定记忆类型，None = 不限
    top_k: int = 0                      # 0 = 不限数量（全量）
    max_chars: int = 0                  # 0 = 不限字符；>0 时由检索器负责截断


@dataclass
class MemoryHit:
    """一条命中的记忆。

    score 本期恒为 1.0（全量拼接无排序）；
    未来接 Recency × Relevance × Importance 三维打分时在这里体现。
    """
    kind: MemoryKind
    text: str                            # 贴进 system 的文本
    source: str = ""                     # 来源标识（summary / profile.name …），供调试
    score: float = 1.0
    meta: Dict[str, Any] = field(default_factory=dict)


class MemoryRetriever(ABC):
    """检索器接口。实现类只需实现 retrieve()。"""

    @abstractmethod
    def retrieve(self, session, query: MemoryQuery) -> List[MemoryHit]:
        """从会话与长期记忆里取出该贴进上下文的记忆。"""
        raise NotImplementedError
