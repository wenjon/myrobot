# -*- coding: utf-8 -*-
"""本地音色注册表（P0）。

目录约定（详见 docs/tts-voice-design.md §6）：

    voices/<id>/
      meta.json     # 必填：显示名、来源、授权、provider 等
      ref.wav       # 克隆/微调音色必填（*.wav 已被 .gitignore 忽略）
      ref.txt       # 可选：参考音频文本
      preview.mp3   # 可选：试听

设计原则：
  · 扫描是**只读且容错**的 —— 一个坏 meta.json 不能让整个音色列表 502；
  · 缺文件不等于崩，标记 available=false + reason，UI 自行灰显；
  · 授权校验在加载期完成，public-figure 一律拒绝（合规要求）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


VALID_SOURCES = {"edge", "clone", "trained", "cloud"}
CLONE_SOURCES = {"clone", "trained"}


@dataclass(slots=True)
class VoiceEntry:
    id: str
    display_name: str
    locale: str = "zh-CN"
    gender: str = ""
    source: str = "clone"
    provider: str = ""
    ref_audio: str = ""
    ref_text: str = ""
    edge_voice: str = ""      # source=edge 或本地音色想映射到 Edge 预设时使用
    tags: list[str] = field(default_factory=list)
    consent: dict = field(default_factory=dict)
    license: str = ""
    available: bool = True
    reason: str = ""

    def to_api(self) -> dict:
        """转成 /api/tts/voices 的条目。

        `name` 保留旧字段（前端旧代码用它显示/回填），`id` 是新字段。
        """
        return {
            "name": self.id,
            "id": self.id,
            "display_name": self.display_name,
            "locale": self.locale,
            "gender": self.gender,
            "source": self.source,
            "provider": self.provider,
            "available": self.available,
            "reason": self.reason,
            "tags": list(self.tags),
            # 旧结构里的两个字段，保持前端下拉框不炸
            "categories": [],
            "personalities": list(self.tags),
        }


class VoiceRegistry:
    """扫描并查询 voices/ 目录。无外部依赖，可在自检里用临时目录直接测。"""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    # ---------------------------------------------------------------- 扫描
    def scan(self) -> tuple[list[VoiceEntry], list[str]]:
        """返回 (entries, diagnostics)。diagnostics 是给人看的告警，不影响其它音色。"""
        entries: list[VoiceEntry] = []
        diagnostics: list[str] = []
        if not self.root.is_dir():
            return entries, diagnostics

        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            meta_path = child / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                raw = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception as exc:                      # noqa: BLE001
                diagnostics.append(f"{child.name}: meta.json 解析失败（{type(exc).__name__}: {exc}）")
                continue
            if not isinstance(raw, dict):
                diagnostics.append(f"{child.name}: meta.json 顶层必须是对象")
                continue
            entry, notes = self._build_entry(child, raw)
            diagnostics.extend(notes)
            entries.append(entry)
        return entries, diagnostics

    def _build_entry(self, directory: Path, raw: dict) -> tuple[VoiceEntry, list[str]]:
        notes: list[str] = []
        voice_id = directory.name
        declared = str(raw.get("id") or voice_id).strip()
        if declared != voice_id:
            notes.append(f"{voice_id}: meta.json 的 id={declared!r} 与目录名不一致，以目录名为准")

        source = str(raw.get("source") or "").strip().lower()
        if not source:
            ref_hint = str(raw.get("ref_audio") or "ref.wav")
            source = "clone" if (directory / ref_hint).is_file() else "edge"
        if source not in VALID_SOURCES:
            notes.append(f"{voice_id}: 未知 source={source!r}，按 clone 处理")
            source = "clone"

        ref_audio = str(raw.get("ref_audio") or ("ref.wav" if source in CLONE_SOURCES else "")).strip()
        provider = str(raw.get("provider") or ("edge" if source == "edge" else "")).strip().lower()

        consent = raw.get("consent") if isinstance(raw.get("consent"), dict) else {}
        consent_type = str(consent.get("type") or "").strip().lower()

        available, reason = True, ""
        if consent_type == "public-figure":
            available, reason = False, "consent_denied"
        elif source in CLONE_SOURCES and (not ref_audio or not (directory / ref_audio).is_file()):
            available, reason = False, "missing_ref_audio"

        tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
        entry = VoiceEntry(
            id=voice_id,
            display_name=str(raw.get("display_name") or voice_id).strip(),
            locale=str(raw.get("locale") or "zh-CN").strip(),
            gender=str(raw.get("gender") or "").strip(),
            source=source,
            provider=provider,
            ref_audio=ref_audio,
            ref_text=str(raw.get("ref_text") or "").strip(),
            edge_voice=str(raw.get("edge_voice") or "").strip(),
            tags=[str(t) for t in tags],
            consent=consent,
            license=str(raw.get("license") or "").strip(),
            available=available,
            reason=reason,
        )
        return entry, notes

    # ---------------------------------------------------------------- 查询
    def entries(self) -> list[VoiceEntry]:
        return self.scan()[0]

    def get(self, voice_id: str) -> VoiceEntry | None:
        if not voice_id:
            return None
        for entry in self.entries():
            if entry.id == voice_id:
                return entry
        return None

    def list_api(self) -> tuple[list[dict], list[str]]:
        entries, diagnostics = self.scan()
        return [e.to_api() for e in entries], diagnostics

    def resolve_for_provider(self, voice_id: str, provider: str) -> tuple[str, str]:
        """把逻辑音色 ID 解析成某个 provider 认识的音色名。

        返回 (voice_for_provider, note)。空字符串表示「让 provider 用默认音色」。
        P0 只有 Edge：本地音色若没写 edge_voice，就回落到全局 TTS_VOICE，
        避免把 "xiaoyou" 直接甩给微软接口导致 400。
        """
        entry = self.get(voice_id)
        if entry is None:
            return voice_id, ""
        if not entry.available:
            return "", f"音色 {entry.id} 不可用（{entry.reason}），已回退默认音色"
        if provider == "edge":
            if entry.edge_voice:
                return entry.edge_voice, ""
            return "", f"音色 {entry.id} 未配置 edge_voice，已回退默认音色"
        return entry.id, ""
