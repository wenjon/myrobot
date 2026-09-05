# -*- coding: utf-8 -*-
"""Edge TTS 合成接口。

为什么要有这一层：
  浏览器自带的 Web Speech API 在 Windows 上只能用本机 SAPI 音色
  （Huihui / Kangkang / Yaoyao），是十几年前的拼接式合成，机械感很重。
  Edge TTS 走的是微软 Azure 神经网络语音（XiaoxiaoNeural 等），
  自然度高一个量级，且免费、无需 API Key。

口型怎么对齐：
  Web Speech 靠 utterance 的 boundary 事件按字驱动口型；换成音频流后没有这个事件，
  所以这里把 edge-tts 返回的 WordBoundary 元数据一起吐给前端，
  前端按 <audio> 的 currentTime 去时间轴上查当前该发哪个 viseme（见 tts.js）。
  这样口型精度反而比 Web Speech 更高——时间戳来自真实合成结果而非估算。
"""
from __future__ import annotations

import asyncio
import base64

from config import TTS_PITCH, TTS_RATE, TTS_VOICE, TTS_VOLUME


class TTSUnavailable(RuntimeError):
    """edge-tts 未安装或合成失败，调用方应降级到浏览器 Web Speech。"""


async def synthesize(
    text: str,
    voice: str = "",
    rate: str = "",
    pitch: str = "",
    volume: str = "",
) -> dict:
    """把 text 合成为 mp3，返回 {audio(base64), marks:[{char_index, offset_ms, duration_ms, text}]}。

    offset/duration 单位是毫秒（edge-tts 原始单位是 100 纳秒的 tick，这里已换算）。
    """
    text = (text or "").strip()
    if not text:
        raise TTSUnavailable("空文本")

    try:
        import edge_tts
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise TTSUnavailable("edge-tts 未安装：pip install edge-tts") from exc

    kwargs = {
        "rate": rate or TTS_RATE,
        "pitch": pitch or TTS_PITCH,
        "volume": volume or TTS_VOLUME,
    }
    # boundary="WordBoundary" 必须显式指定：edge-tts >= 7 默认给的是 SentenceBoundary，
    # 一整句只回一个 mark，口型就只会动一下（实测踩过）。
    try:
        comm = edge_tts.Communicate(text, voice or TTS_VOICE, boundary="WordBoundary", **kwargs)
    except TypeError:
        # 老版本没有 boundary 参数，本身就是按词回调
        comm = edge_tts.Communicate(text, voice or TTS_VOICE, **kwargs)

    chunks: list[bytes] = []
    marks: list[dict] = []
    cursor = 0  # 已匹配到原文的位置，用于把 mark 映射回 text 的字符下标

    try:
        async for item in comm.stream():
            kind = item.get("type")
            if kind == "audio":
                chunks.append(item["data"])
            elif kind == "WordBoundary":
                word = item.get("text") or ""
                # edge-tts 给的是"词"文本而非下标，需自己在原文里定位。
                # 从 cursor 往后找，避免同一个字反复命中开头那次。
                idx = text.find(word, cursor) if word else -1
                if idx < 0:
                    idx = cursor
                else:
                    cursor = idx + len(word)
                # 中文一个"词"常含 2~4 字（如"你好""小柚"），若整词只给一个 mark，
                # 口型就只动一次。这里把词时长按字均分，展开成逐字 mark，
                # 让 viseme 逐字变化——这是口型自然度的关键一步。
                offset_ms = int(item.get("offset", 0) / 10000)
                duration_ms = int(item.get("duration", 0) / 10000)
                n = max(1, len(word))
                per = duration_ms / n
                for k, ch in enumerate(word or " "):
                    marks.append({
                        "char_index": idx + k,
                        "text": ch,
                        "offset_ms": int(offset_ms + per * k),
                        "duration_ms": int(per),
                    })
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise TTSUnavailable(f"合成失败: {type(exc).__name__}: {exc}") from exc

    if not chunks:
        raise TTSUnavailable("合成返回空音频")

    return {
        "audio": base64.b64encode(b"".join(chunks)).decode("ascii"),
        "mime": "audio/mpeg",
        "marks": marks,
        "voice": voice or TTS_VOICE,
    }


async def list_voices(locale_prefix: str = "zh") -> list[dict]:
    """列出可用音色，供前端下拉框/调试用。"""
    try:
        import edge_tts
    except ImportError as exc:
        raise TTSUnavailable("edge-tts 未安装") from exc

    voices = await edge_tts.list_voices()
    out = []
    for v in voices:
        if locale_prefix and not v.get("Locale", "").startswith(locale_prefix):
            continue
        tag = v.get("VoiceTag") or {}
        out.append({
            "name": v.get("ShortName"),
            "locale": v.get("Locale"),
            "gender": v.get("Gender"),
            "categories": tag.get("ContentCategories") or [],
            "personalities": tag.get("VoicePersonalities") or [],
        })
    out.sort(key=lambda x: x["name"] or "")
    return out
