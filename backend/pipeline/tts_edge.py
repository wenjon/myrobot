# -*- coding: utf-8 -*-
"""兼容层：旧的 Edge TTS 入口，现已迁到 `backend/tts/edge.py`。

为什么保留这个文件：
  · scripts/tts_preview.py、scripts/check_tts.py 等仍按老路径 import；
  · 换实现时不想让所有调用方跟着改，薄包装是最小代价。

新代码请直接 `from tts.edge import EdgeProvider`，或走 `tts.registry.TTSRegistry`。
"""
from __future__ import annotations

from tts.base import TTSUnavailable
from tts.edge import TAIL_KEEP_MS, EdgeProvider, list_voices, synthesize

__all__ = ["TAIL_KEEP_MS", "EdgeProvider", "TTSUnavailable", "list_voices", "synthesize"]
