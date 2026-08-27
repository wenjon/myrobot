"""记忆模块 · 检索器实现。

本期只有一个实现：AllMemoryRetriever（全量拼接）。

为什么全量拼接就够用（docs § 16.7）：
    原方案的「Recency × Relevance × Importance」三维打分是为了从成千上万条
    记忆里挑出最相关的几条。而本项目的长期记忆就是「1 个摘要 + 1 个画像」，
    合起来不过几百字，挑与不挑没区别，挑反而有「该带的没带上」的风险。

唯一真正需要处理的是 **字符预算**：万一摘要被 LLM 写长了，
不能让它无限占用 system。所以 max_chars 超限时按「画像优先」取舍——
画像是长期稳定的认知，摘要是可重新生成的，该裁的时候裁摘要。
"""
from __future__ import annotations

from typing import List

from .types import MemoryHit, MemoryKind, MemoryQuery, MemoryRetriever


class AllMemoryRetriever(MemoryRetriever):
    """全量拼接：把会话摘要与用户画像全部返回。

    返回顺序有意义：**画像在前、摘要在后**。
    画像（称呼/偏好/职业）直接影响回答风格，越靠前模型越不容易忽略。
    """

    def retrieve(self, session, query: MemoryQuery) -> List[MemoryHit]:
        hits: List[MemoryHit] = []
        kinds = query.kinds

        def want(kind: MemoryKind) -> bool:
            return kinds is None or kind in kinds

        if want(MemoryKind.SEMANTIC):
            # 1) 用户画像（优先）
            profile_text = session.profile.as_prompt_text()
            if profile_text:
                hits.append(MemoryHit(
                    kind=MemoryKind.SEMANTIC,
                    text=f"【用户画像】{profile_text}",
                    source="profile",
                ))
            # 2) 会话摘要
            if session.summary:
                hits.append(MemoryHit(
                    kind=MemoryKind.SEMANTIC,
                    text=f"【对话记忆摘要】{session.summary}",
                    source="summary",
                ))

        # 字符预算：从后往前丢（先丢摘要，保住画像）
        if query.max_chars > 0:
            kept: List[MemoryHit] = []
            used = 0
            for h in hits:
                if used + len(h.text) > query.max_chars:
                    continue
                kept.append(h)
                used += len(h.text)
            hits = kept

        # top_k 限制（本期不会超过 2 条，保留该逻辑以便未来换向量库）
        if query.top_k > 0:
            hits = hits[: query.top_k]
        return hits
