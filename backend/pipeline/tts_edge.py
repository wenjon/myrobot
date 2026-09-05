# -*- coding: utf-8 -*-
"""Edge TTS 合成接口。

为什么要有这一层：
  浏览器自带的 Web Speech API 在 Windows 上只能用本机 SAPI 音色
  （Huihui / Kangkang / Yaoyao），是十几年前的拼接式合成，机械感很重。
  Edge TTS 走的是微软 Azure 神经网络语音（XiaoxiaoNeural 等），
  自然度高一个量级，且免费、无需 API Key。

抑扬顿挫怎么做（见 prosody.py / mp3_utils.py）：
  Edge 免费端点不接受 SSML（实测 <break>/<prosody> 会直接 NoAudioReceived），
  每次请求只能带一组全局 rate/pitch/volume。所以句内起伏靠
  「拆韵律段 → 逐段用不同参数合成 → 服务端按 mp3 帧拼接 → 时间轴整体平移」实现。
  对外接口不变：调用方拿到的仍是一个 mp3 + 一条完整时间轴。

口型怎么对齐：
  Web Speech 靠 utterance 的 boundary 事件按字驱动口型；换成音频流后没有这个事件，
  所以这里把 edge-tts 返回的 WordBoundary 元数据一起吐给前端，
  前端按 <audio> 的 currentTime 去时间轴上查当前该发哪个 viseme（见 tts.js）。
  这样口型精度反而比 Web Speech 更高——时间戳来自真实合成结果而非估算。
"""
from __future__ import annotations

import asyncio
import base64

from config import (
    TTS_PITCH, TTS_PROSODY, TTS_RATE, TTS_VOICE, TTS_VOLUME,
)
from pipeline import prosody as prosody_mod
from pipeline.mp3_utils import duration_ms, make_silence, trim_to


# 裁尾时在最后一个字之后保留的余量（ms），给尾音自然衰减，太小会显得被掐断。
TAIL_KEEP_MS = 60


class TTSUnavailable(RuntimeError):
    """edge-tts 未安装或合成失败，调用方应降级到浏览器 Web Speech。"""


async def _synth_once(
    text: str,
    voice: str,
    rate: str,
    pitch: str,
    volume: str,
    base_index: int = 0,
) -> tuple:
    """合成一个韵律段，返回 (mp3 bytes, marks)。

    base_index 是本段首字在整句中的下标，用于把 char_index 还原到整句坐标系
    （多段拼接时每段只知道自己的局部下标）。
    """
    try:
        import edge_tts
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise TTSUnavailable("edge-tts 未安装：pip install edge-tts") from exc

    kwargs = {"rate": rate, "pitch": pitch, "volume": volume}
    # boundary="WordBoundary" 必须显式指定：edge-tts >= 7 默认给的是 SentenceBoundary，
    # 一整句只回一个 mark，口型就只会动一下（实测踩过）。
    try:
        comm = edge_tts.Communicate(text, voice, boundary="WordBoundary", **kwargs)
    except TypeError:
        # 老版本没有 boundary 参数，本身就是按词回调
        comm = edge_tts.Communicate(text, voice, **kwargs)

    chunks: list[bytes] = []
    marks: list[dict] = []
    cursor = 0  # 已匹配到本段文本的位置，用于把 mark 映射回字符下标

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
                offset = int(item.get("offset", 0) / 10000)
                dur = int(item.get("duration", 0) / 10000)
                n = max(1, len(word))
                per = dur / n
                for k, ch in enumerate(word or " "):
                    marks.append({
                        "char_index": base_index + idx + k,
                        "text": ch,
                        "offset_ms": int(offset + per * k),
                        "duration_ms": int(per),
                    })
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise TTSUnavailable(f"合成失败: {type(exc).__name__}: {exc}") from exc

    if not chunks:
        raise TTSUnavailable("合成返回空音频")
    # voiced_end_ms：最后一个字发音结束的时刻。Edge 会在其后附加约 0.7~0.9s
    # 固定静音填充，多段拼接时必须据此裁掉，否则空档会累积（详见 mp3_utils.trim_to）。
    voiced_end = (marks[-1]["offset_ms"] + marks[-1]["duration_ms"]) if marks else 0
    return b"".join(chunks), marks, voiced_end


async def synthesize(
    text: str,
    voice: str = "",
    rate: str = "",
    pitch: str = "",
    volume: str = "",
    emotion: str = "",
    style: str = "",
) -> dict:
    """把 text 合成为 mp3，返回 {audio(base64), marks:[...], duration_ms, segments}。

    韵律模式（style != "flat"）下会拆成多段分别合成再拼接，
    每段用不同的 rate/pitch/volume 形成句内起伏；段间插入精确长度静音做停顿层级。
    调用方拿到的仍是单个 mp3 + 单条时间轴，与单段模式完全同构。

    显式传入 rate/pitch/volume 时视为"手动指定"，走单段模式不做韵律
    （调试面板试听音色用，避免韵律干扰判断）。
    """
    text = (text or "").strip()
    if not text:
        raise TTSUnavailable("空文本")

    voice = voice or TTS_VOICE
    manual = bool(rate or pitch or volume)
    style = style or ("flat" if manual else TTS_PROSODY)

    if style == "flat":
        audio, marks, voiced_end = await _synth_once(
            text, voice,
            rate or TTS_RATE, pitch or TTS_PITCH, volume or TTS_VOLUME,
        )
        # 单段也裁尾：Edge 固定附加约 0.8s 静音，不裁会让句间白等近一秒。
        trimmed, kept = trim_to(audio, voiced_end + TAIL_KEEP_MS)
        if not trimmed:
            trimmed, kept = audio, duration_ms(audio)
        return {
            "audio": base64.b64encode(trimmed).decode("ascii"),
            "mime": "audio/mpeg",
            "marks": marks,
            "voice": voice,
            "duration_ms": kept,
            "segments": 1,
            "style": "flat",
        }

    segments = prosody_mod.plan(text, emotion=emotion or "平静", style=style)
    if not segments:
        raise TTSUnavailable("韵律规划为空")

    # 各段并发合成：段与段之间没有依赖，串行会让延迟随段数线性增长
    # （实测 3 段串行 ~5s，并发后回落到接近单段耗时）。
    char_bases = []
    acc = 0
    for seg in segments:
        char_bases.append(acc)
        acc += len(seg.text)

    results = await asyncio.gather(*[
        _synth_once(seg.text, voice, seg.rate, seg.pitch, seg.volume, base_index=base)
        for seg, base in zip(segments, char_bases)
    ])

    parts: list[bytes] = []
    all_marks: list[dict] = []
    timeline_ms = 0.0   # 已拼接音频的累计时长，作为后续段 mark 的平移量

    for seg, (audio, marks, voiced_end) in zip(segments, results):
        # 裁掉 Edge 附加的尾部静音，只留 TAIL_KEEP_MS 余量给尾音自然衰减。
        # 不裁的话每段都会多出约 0.8s 空白，3 段句子就凭空多 2.4s。
        trimmed, kept_ms = trim_to(audio, voiced_end + TAIL_KEEP_MS)
        if not trimmed:                     # 兜底：极短段可能没有 mark
            trimmed, kept_ms = audio, duration_ms(audio)

        for m in marks:
            m["offset_ms"] = int(m["offset_ms"] + timeline_ms)
        all_marks.extend(marks)

        parts.append(trimmed)
        timeline_ms += kept_ms

        # 段间停顿：用同格式静音帧精确补齐，这是"顿挫"里的"顿"
        if seg.pause_ms > 0:
            silence = make_silence(trimmed or audio, seg.pause_ms)
            if silence:
                parts.append(silence)
                timeline_ms += duration_ms(silence)

    combined = b"".join(parts)
    return {
        "audio": base64.b64encode(combined).decode("ascii"),
        "mime": "audio/mpeg",
        "marks": all_marks,
        "voice": voice,
        "duration_ms": round(timeline_ms, 1),
        "segments": len(segments),
        "style": style,
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
