# -*- coding: utf-8 -*-
"""TTS provider 装配与降级链（P0）。

职责只有三件事：
  1. 按 `TTS_PROVIDER_ORDER` 顺序把 provider 建出来并探活；
  2. 合成时从前往后试，失败自动换下一个（Edge 是 P0 唯一实现）；
  3. 把本地音色注册表与各引擎的音色清单合并成一份 /api/tts/voices 响应。

刻意不做的事：不碰 HTTP、不 import 具体引擎（除 Edge），不缓存音频。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Callable

from tts.base import TTSProvider, TTSRequest, TTSUnavailable
from tts.voices import VoiceRegistry


def provider_order(engine: str, explicit: str = "") -> list[str]:
    """决定尝试顺序；同名去重，保持声明顺序。

    TTS_PROVIDER_ORDER 优先；否则从旧的 TTS_ENGINE 推导：
      · web      → 空列表（服务端不合成，前端直接用 Web Speech）
      · edge     → ["edge"]（与旧行为完全一致）
      · 其它     → [该引擎, "edge"]（新引擎失败时仍能出声）
    """
    raw = [p.strip().lower() for p in (explicit or "").split(",") if p.strip()]
    if not raw:
        name = (engine or "edge").strip().lower()
        if name == "web":
            return []
        raw = ["edge"] if name == "edge" else [name, "edge"]

    seen: set[str] = set()
    order: list[str] = []
    for name in raw:
        if name not in seen:
            seen.add(name)
            order.append(name)
    return order


class TTSRegistry:
    """进程内单例式注册表；构造参数全部显式传入，便于自检时注入假 provider。"""

    def __init__(
        self,
        voice_registry: VoiceRegistry,
        order: list[str] | None = None,
        default_voice: str = "",
        max_chars: int = 200,
        factories: dict[str, Callable[[], TTSProvider]] | None = None,
    ):
        self.voices = voice_registry
        self.order = list(order or ["edge"])
        self.default_voice = default_voice
        self.max_chars = max(1, int(max_chars))
        self._factories = dict(factories or {})
        self._providers: dict[str, TTSProvider] = {}
        self._healthy: dict[str, bool] = {}
        self._ready = False
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 装配
    def _provider(self, name: str) -> TTSProvider | None:
        if name in self._providers:
            return self._providers[name]
        factory = self._factories.get(name)
        if factory is None and name == "edge":
            from tts.edge import EdgeProvider
            factory = EdgeProvider
        if factory is None:
            # cosyvoice / cloud 在 P1/P2 接入；现在静默跳过，不影响 Edge 降级。
            self._healthy[name] = False
            return None
        try:
            provider = factory()
        except Exception:                                  # noqa: BLE001
            self._healthy[name] = False
            return None
        self._providers[name] = provider
        return provider

    async def ensure_ready(self) -> dict:
        """探活一次并缓存结果。并发调用只跑一次。"""
        if self._ready:
            return self.status()
        async with self._lock:
            if self._ready:
                return self.status()
            for name in self.order:
                provider = self._provider(name)
                if provider is None:
                    continue
                try:
                    self._healthy[name] = bool(await provider.health())
                except Exception:                          # noqa: BLE001
                    self._healthy[name] = False
            self._ready = True
        return self.status()

    def status(self) -> dict:
        return {
            "order": list(self.order),
            "ready": self._ready,
            "available": [n for n in self.order if self._healthy.get(n)],
            "providers": {n: bool(self._healthy.get(n)) for n in self.order},
            "voice_dir": str(self.voices.root),
            "default_voice": self.default_voice,
        }

    # ---------------------------------------------------------------- 合成
    async def synthesize(self, req: TTSRequest) -> dict:
        """按降级链合成，返回旧接口结构（外加 provider / marks_source）。"""
        text = (req.text or "").strip()
        if not text:
            raise TTSUnavailable("空文本")
        if len(text) > self.max_chars:
            # 超长文本先截断：宁可少说一句，也不要让单句合成把队列拖死。
            text = text[: self.max_chars]
        req = replace(req, text=text)

        if not self._ready:
            await self.ensure_ready()

        errors: list[str] = []
        notes: list[str] = []
        for name in self.order:
            if not self._healthy.get(name):
                continue
            provider = self._provider(name)
            if provider is None:
                continue

            voice, note = self.voices.resolve_for_provider(req.voice or self.default_voice, name)
            if note:
                notes.append(note)
            attempt = replace(req, voice=voice)
            try:
                result = await provider.synthesize(attempt)
            except TTSUnavailable as exc:
                errors.append(f"{name}: {exc}")
                continue
            except Exception as exc:                       # noqa: BLE001
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                continue

            out = result.to_legacy()
            if notes:
                out["warnings"] = notes
            return out

        if not self.order:
            raise TTSUnavailable("TTS_ENGINE=web：服务端不合成，前端应使用 Web Speech")
        detail = "；".join(errors) if errors else "没有可用的 provider（检查依赖/配置）"
        raise TTSUnavailable(detail)

    async def cancel(self, seq: str) -> None:
        for name in self.order:
            provider = self._providers.get(name)
            if provider is None:
                continue
            try:
                await provider.cancel(seq)
            except Exception:                              # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 音色
    async def list_voices(self, locale_prefix: str = "zh") -> dict:
        """合并本地注册表 + 各引擎音色。任何一路失败都只记 warning。"""
        voices, diagnostics = self.voices.list_api()
        warnings: list[str] = []

        if not self._ready:
            await self.ensure_ready()

        for name in self.order:
            if not self._healthy.get(name):
                continue
            provider = self._provider(name)
            if provider is None:
                continue
            try:
                items = await provider.list_voices()
            except Exception as exc:                       # noqa: BLE001
                warnings.append(f"{name}: 音色清单获取失败（{type(exc).__name__}: {exc}）")
                continue
            for item in items or []:
                voice = self._normalize_voice(item, name)
                if locale_prefix and not str(voice.get("locale", "")).startswith(locale_prefix):
                    continue
                voices.append(voice)

        # 去重：同 source + id 只留第一条（本地注册表优先于引擎回传）
        merged: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for voice in voices:
            key = (str(voice.get("source") or ""), str(voice.get("id") or voice.get("name") or ""))
            if key in seen:
                continue
            seen.add(key)
            merged.append(voice)
        merged.sort(key=lambda v: (str(v.get("source") or ""), str(v.get("name") or "")))

        out = {
            "ok": bool(merged) or not warnings,
            "voices": merged,
            "warnings": warnings,
            "diagnostics": diagnostics,
            "providers": self.status()["available"],
        }
        if not merged and warnings:
            out["error"] = warnings[0]
        return out

    @staticmethod
    def _normalize_voice(item: dict, provider: str) -> dict:
        """把各引擎的差异字段补齐成前端认识的统一结构。"""
        src = dict(item or {})
        name = str(src.get("name") or src.get("id") or "")
        src.setdefault("id", name)
        src.setdefault("name", name)
        src.setdefault("display_name", name)
        src.setdefault("source", provider)
        src.setdefault("provider", provider)
        src.setdefault("available", True)
        src.setdefault("reason", "")
        src.setdefault("tags", [])
        src.setdefault("categories", [])
        src.setdefault("personalities", [])
        return src
