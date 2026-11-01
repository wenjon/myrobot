# -*- coding: utf-8 -*-
"""TTS provider 的公共契约（P0）。

为什么需要这一层（根因见 docs/tts-voice-design.md §2.2）：
  1. 依赖体量差异大 —— Edge 是纯 HTTP；CosyVoice 要 torch + 数 GB 权重。
     后者绝不能 import 进 FastAPI 进程，否则冷启动慢一个量级、OOM 还会拖垮对话。
  2. 输出格式不同 —— Edge 返回 mp3 + 词边界；本地模型多为 PCM，时间轴来源也不同。
  3. 失败模式不同 —— 网络限流 vs 显存不足，需要可配置的降级链而不是写死的 try/except。

本文件刻意不依赖任何具体引擎，方便单独测试与复用。
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable


class TTSUnavailable(RuntimeError):
    """所有 provider 都不可用，或单次合成失败。

    调用方（server.py）捕获后返回 ok=False，前端据此降级到 Web Speech——
    宁可音色差一点，也不能让数字人变哑。
    """


@dataclass(slots=True)
class TTSRequest:
    """一次合成请求。字段与旧 synthesize() 的关键字参数一一对应。"""

    text: str
    voice: str = ""
    rate: str = ""            # Edge 语法：+10% / -5%
    pitch: str = ""           # Edge 语法：+20Hz
    volume: str = ""          # Edge 语法：+0%
    emotion: str = ""         # 中文表情名，如「开心」；用于韵律/情感指令
    style: str = ""           # broadcast / natural / flat
    seq: str = ""             # 句序号，用于取消在途合成（P2）与日志关联


@dataclass(slots=True)
class TTSMark:
    """逐字口型时间轴。字段名与前端 tts.js 现有消费格式保持一致。"""

    char_index: int
    text: str
    offset_ms: int
    duration_ms: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class TTSResult:
    """一次合成的完整结果。

    这是旧返回字典（audio/mime/marks/duration_ms/segments/style/voice）的超集；
    `to_legacy()` 负责转回旧格式，保证前端零改动。
    """

    audio: bytes
    mime: str
    marks: list[TTSMark]
    duration_ms: float
    provider: str
    voice: str
    segments: int = 1
    style: str = ""
    sample_rate: int = 0      # PCM 必填；mp3 可为 0
    channels: int = 1
    marks_source: str = "provider"   # provider | estimated
    timings: dict = field(default_factory=dict)

    def to_legacy(self) -> dict:
        """转成 server.py 旧接口的返回结构（audio 用 base64）。"""
        return {
            "audio": base64.b64encode(self.audio).decode("ascii"),
            "mime": self.mime,
            "marks": [m.to_dict() for m in self.marks],
            "marks_source": self.marks_source,
            "voice": self.voice,
            "provider": self.provider,
            "duration_ms": round(self.duration_ms, 1),
            "segments": self.segments,
            "style": self.style,
        }


@runtime_checkable
class TTSProvider(Protocol):
    """一个可插拔的合成引擎。

    P0 只要求实现 health / list_voices / synthesize / cancel；
    流式能力（supports_stream）在 P2 使用，接口先固定下来避免后续改协议。
    """

    name: str
    supports_stream: bool
    supports_clone: bool
    emotions: frozenset[str]

    async def health(self) -> bool:
        """探活。必须快速返回，且不得因网络阻塞启动流程。"""

    async def list_voices(self) -> list[dict]:
        """返回该引擎可选音色；结构由各 provider 决定，registry 负责归一。"""

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        """合成一次。失败必须抛 TTSUnavailable（可附带原因）。"""

    async def cancel(self, seq: str) -> None:
        """取消在途合成（软打断用）；不支持时允许空实现。"""
