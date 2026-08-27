"""记忆模块 · 用户画像（L3 语义记忆）与冲突确认。

画像只有三个字段（config.PROFILE_FIELDS，已定稿）：
    name        称呼。例：「小王」「老师」
    preferences 偏好 / 习惯 / 注意事项。例：「喜欢简洁回答」「对青霉素过敏」
    occupation  职业 / 角色。例：「CTO」「小学生」

为什么不设 key_facts（docs § 16.6）：
    那会变成「什么都能往里塞」的垃圾桶，LLM 提炼时目标不清。
    高代价信息（如过敏史）归到 preferences 下，结构更清楚、误判更少。

--------------------------------------------------------------------
冲突处理：B 方案（人工确认，docs § 16.8）
--------------------------------------------------------------------
提炼出新值后不直接覆盖，而是分三种情况：

    新字段（旧值为空）  → 直接合并，不打扰用户
    同字段且值相同  → 什么都不做
    同字段但值变了  → 生成 ProfileConflict 暂存，下一轮随问题一起下发确认卡

超时（默认 60s）未回应一律 **保旧**。为什么不保新：
    B 方案的核心价值是「不让少数事件覆盖多数事件」。对「我以前喜欢 X，
    现在不喜欢了」这类试错语义，保旧更安全；而一次性事件（「今天吃了火锅」）
    本来就不应进 profile。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import PROFILE_FIELDS, PROFILE_CONFLICT_TIMEOUT_S


# 单个画像字段值的长度上限：防止 LLM 提炼时吐出一大段把 system 撑爆
MAX_FIELD_CHARS = 200

# 字段名 → 中文标签（拼 system 贴片与前端确认卡共用）
FIELD_LABELS = {"name": "称呼", "preferences": "偏好", "occupation": "职业"}


@dataclass
class ProfileConflict:
    """一条待用户确认的画像变更。

    source_quote 是提炼依据的原句，前端确认卡会展示它——
    没有原句，用户根本不知道「为什么突然问我这个」。
    """
    field_name: str
    old_value: str
    new_value: str
    source_quote: str = ""
    conflict_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: float = field(default_factory=time.time)

    def is_expired(self, timeout_s: int = PROFILE_CONFLICT_TIMEOUT_S) -> bool:
        """超时则视为用户未回应 → 保旧。"""
        return (time.time() - self.created_at) > timeout_s

    def to_ws(self) -> Dict[str, Any]:
        """转成 profile_conflict WS 帧负载（字段名与 docs § 16.8 一致）。"""
        return {
            "type": "profile_conflict",
            "conflict_id": self.conflict_id,
            "field": self.field_name,
            "field_label": FIELD_LABELS.get(self.field_name, self.field_name),
            "old_value": self.old_value,
            "new_value": self.new_value,
            "source_quote": self.source_quote,
        }


@dataclass
class UserProfile:
    """用户画像：key/value，沉淀于 long_term.json。"""

    fields: Dict[str, str] = field(default_factory=dict)
    # 被拒绝过的变更（审计用）：[{field, old, new, at}]
    rejected_changes: List[Dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    # ---------------- 读 ----------------
    def get(self, name: str) -> str:
        return (self.fields.get(name) or "").strip()

    def is_empty(self) -> bool:
        return not any(self.fields.get(f) for f in PROFILE_FIELDS)

    def as_prompt_text(self) -> str:
        """拼成贴进 system 的人设贴片；空画像返回空字符串。"""
        parts = []
        for f in PROFILE_FIELDS:
            v = self.get(f)
            if v:
                parts.append(f"{FIELD_LABELS.get(f, f)}：{v}")
        return "；".join(parts)

    # ---------------- 写 ----------------
    def merge(self, incoming: Dict[str, str], source_quote: str = "") -> List[ProfileConflict]:
        """合并提炼出的新画像，返回需要人工确认的冲突列表。

        只处理 PROFILE_FIELDS 里的字段，LLM 额外发挥出的字段直接丢掉。

        Args:
            incoming: LLM 提炼出的 {字段名: 值}
            source_quote: 提炼依据的原句（供确认卡展示）
        """
        conflicts: List[ProfileConflict] = []
        changed = False
        for name in PROFILE_FIELDS:
            new_v = (incoming.get(name) or "").strip()[:MAX_FIELD_CHARS]
            if not new_v:
                continue
            old_v = self.get(name)
            if not old_v:
                # 新字段：无从冲突，直接写入，不打扰用户
                self.fields[name] = new_v
                changed = True
            elif old_v != new_v:
                # 值变了：不自作主张，生成冲突等用户拍板
                conflicts.append(ProfileConflict(
                    field_name=name, old_value=old_v, new_value=new_v,
                    source_quote=source_quote[:120],
                ))
            # old_v == new_v：什么都不做
        if changed:
            self.updated_at = time.time()
        return conflicts

    def apply_conflict(self, conflict: ProfileConflict, accept: bool) -> None:
        """用户拍板后落地：accept 写新值；reject 保旧并记一笔审计。"""
        if accept:
            self.fields[conflict.field_name] = conflict.new_value
        else:
            self.rejected_changes.append({
                "field": conflict.field_name,
                "old": conflict.old_value,
                "new": conflict.new_value,
                "at": time.time(),
            })
            # 只保留最近 20 条审计，避免 long_term.json 无限膨胀
            self.rejected_changes = self.rejected_changes[-20:]
        self.updated_at = time.time()

    def clear(self) -> None:
        self.fields.clear()
        self.rejected_changes.clear()
        self.updated_at = time.time()

    # ---------------- 序列化 ----------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "fields": dict(self.fields),
            "rejected_changes": list(self.rejected_changes),
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "UserProfile":
        """从 long_term.json 还原。只收白名单字段，

        旧版本存过的 key_facts 等字段会在这里被自然丢弃（向前兼容）。
        """
        d = d or {}
        p = UserProfile()
        raw = d.get("fields") or {}
        for f in PROFILE_FIELDS:
            v = raw.get(f)
            if isinstance(v, str) and v.strip():
                p.fields[f] = v.strip()[:MAX_FIELD_CHARS]
        rc = d.get("rejected_changes")
        if isinstance(rc, list):
            p.rejected_changes = rc[-20:]
        p.updated_at = float(d.get("updated_at") or time.time())
        return p
