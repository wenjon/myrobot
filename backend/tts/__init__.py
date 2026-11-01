# -*- coding: utf-8 -*-
"""TTS 能力层：provider 抽象 + 音色注册表 + 统一入口。

对外只暴露三样东西（详见 docs/tts-voice-design.md）：
  · TTSRequest / TTSResult / TTSUnavailable —— 数据结构与错误类型；
  · TTSRegistry —— 按配置装配 provider、执行降级链；
  · VoiceRegistry —— 本地音色目录扫描。

具体引擎（edge / cosyvoice / cloud）都在各自模块里，互不 import。
"""
from __future__ import annotations

from tts.base import TTSMark, TTSProvider, TTSRequest, TTSResult, TTSUnavailable
from tts.registry import TTSRegistry, provider_order
from tts.voices import VoiceEntry, VoiceRegistry

__all__ = [
    "TTSMark", "TTSProvider", "TTSRequest", "TTSResult", "TTSUnavailable",
    "TTSRegistry", "provider_order", "VoiceEntry", "VoiceRegistry",
]
