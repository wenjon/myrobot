# -*- coding: utf-8 -*-
"""韵律规划：把一句话拆成若干「韵律段」，逐段指定语速/音高/音量/后置停顿。

## 抑扬顿挫到底是什么
听感上的「播音腔」不是单纯更慢或更低，而是三件事同时成立：
  1. 语调曲线（抑扬）——句首起势稍高，句中平稳推进，句末降调收束；
     疑问句反过来，句尾必须上扬，否则听着像自言自语。
  2. 轻重对比（顿挫的「挫」）——语气词、强调词加重且放慢；
     附加成分（括注、语尾助词）减弱且加快，形成层次而不是一条直线。
  3. 停顿层级（顿挫的「顿」）——逗号 < 分号/冒号 < 句号，
     停顿长度必须有明显区分度，全都一样长就会变成机器人念稿。

## 为什么要自己算
Edge 免费端点不接受 SSML（实测传 <break>/<prosody> 直接 NoAudioReceived），
每次请求只能带一组全局 rate/pitch/volume。所以「句内变化」只能靠
拆段 + 逐段不同参数 + 服务端拼接来实现，本模块负责产出这份拆分方案。

## 与表情系统的关系
LLM 已经会输出 [表情:开心] 等标记，这里把表情映射成一层「情绪基调」偏置
（开心整体偏快偏高，悲伤偏慢偏低），叠加在句内曲线之上。
于是表情不只改脸，也改声音，视听一致。
"""
from __future__ import annotations

import re

# 断句标点 -> 后置停顿毫秒。层级差距刻意拉开，这是「顿挫」的来源。
PAUSE_BY_PUNCT = {
    "，": 130, ",": 130, "、": 90,
    "；": 210, ";": 210, "：": 190, ":": 190,
    "。": 260, "！": 240, "!": 240, "？": 270, "?": 270,
    "…": 320, "—": 240,
}
# 段末无标点时的默认换气
DEFAULT_PAUSE = 80

_CLAUSE_SPLIT = re.compile(r"([，,、；;：:。！？!?…—])")

# 需要加重放慢的强调词（语气副词 / 程度副词）——重音落在这些词上最像人说话
_STRESS_WORDS = ("特别", "非常", "超级", "真的", "太", "最", "绝对", "必须",
                 "一定", "根本", "完全", "居然", "竟然", "千万", "务必", "极其")
# 句首语气词：单独成段、拖长，是口语生动感的关键
_INTERJECTIONS = ("哇", "啊", "唉", "咦", "嗯", "哦", "噢", "呀", "嘿", "哈")

# 情绪基调偏置：(语速%, 音高Hz, 音量%)
EMOTION_BIAS = {
    "平静": (0, 0, 0),
    "开心": (6, 12, 6), "得意": (2, 8, 6), "调皮": (8, 14, 4),
    "疑惑": (-6, 8, -2),
    "期待": (4, 10, 4), "撒娇": (-2, 16, 0), "惊讶": (8, 18, 8),
    "惊恐": (14, 22, 10), "生气": (6, -6, 12), "厌恶": (-4, -8, 2),
    "悲伤": (-10, -14, -6), "委屈": (-8, 6, -8), "困倦": (-16, -12, -10),
    "无语": (-6, -10, -4), "思考": (-8, -4, -2), "尴尬": (-4, 2, -4),
    "害羞": (-6, 6, -8),
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Segment:
    """一个韵律段：一次 Edge 合成请求 + 其后的静音。"""

    __slots__ = ("text", "rate", "pitch", "volume", "pause_ms")

    def __init__(self, text, rate, pitch, volume, pause_ms):
        self.text = text
        self.rate = rate
        self.pitch = pitch
        self.volume = volume
        self.pause_ms = pause_ms

    def as_kwargs(self) -> dict:
        return {"rate": self.rate, "pitch": self.pitch, "volume": self.volume}

    def __repr__(self):
        return "Segment(%r, %s, %s, %s, +%dms)" % (
            self.text, self.rate, self.pitch, self.volume, self.pause_ms)


def _split_clauses(text: str):
    """按标点切分小句，标点归到前一段（停顿要发生在标点之后）。"""
    parts = [p for p in _CLAUSE_SPLIT.split(text) if p]
    clauses = []
    for p in parts:
        if _CLAUSE_SPLIT.fullmatch(p) and clauses:
            clauses[-1] += p
        else:
            clauses.append(p)
    return clauses or ([text] if text else [])


def _peel_interjection(clause: str):
    """把句首语气词剥成独立段。「哇，这个太厉害了」的"哇"要单独拖一下才生动。"""
    for word in _INTERJECTIONS:
        if clause.startswith(word):
            rest = clause[len(word):]
            # 后面紧跟的标点一并带走
            while rest and rest[0] in PAUSE_BY_PUNCT:
                word += rest[0]
                rest = rest[1:]
            if rest.strip():
                return word, rest
    return "", clause


def plan(text: str, emotion: str = "平静", style: str = "broadcast") -> list:
    """产出韵律段列表。

    style:
        broadcast —— 播音/主播腔：起伏明显、句末降调收束、停顿层级清晰
        natural   —— 日常口语：起伏温和，接近原声
        flat      —— 不做韵律（单段，等价于旧行为），用于对照或省流量
    """
    text = (text or "").strip()
    if not text:
        return []

    d_rate, d_pitch, d_vol = EMOTION_BIAS.get(emotion, (0, 0, 0))
    # natural 只取一半情绪偏置，flat 直接单段返回
    if style == "flat":
        return [Segment(text, "%+d%%" % d_rate, "%+dHz" % d_pitch, "%+d%%" % d_vol, 0)]
    gain = 1.0 if style == "broadcast" else 0.5

    clauses = _split_clauses(text)
    ends_question = text.rstrip().endswith(("？", "?"))
    ends_exclaim = text.rstrip().endswith(("！", "!"))

    # 先展开成 (小句文本, 是否语气词段) 的扁平列表
    units = []
    for clause in clauses:
        interj, rest = _peel_interjection(clause)
        if interj:
            units.append((interj, True))
        if rest.strip():
            units.append((rest, False))
    if not units:
        units = [(text, False)]

    total = len(units)
    segments = []
    for idx, (chunk, is_interj) in enumerate(units):
        is_first = idx == 0
        is_last = idx == total - 1

        # ---- 1) 句内基础语调曲线 ----
        if is_interj:
            # 语气词：拖长 + 抬高，情绪的抓手
            rate, pitch, vol = -18, 20, 8
        elif total == 1:
            # 单段句：整体略慢一点，给口型留时间，句末靠 Edge 自带的降调
            rate, pitch, vol = -4, 0, 0
        elif is_first:
            rate, pitch, vol = -2, 12, 6          # 起势
        elif is_last:
            rate, pitch, vol = -9, -16, -3        # 收束降调
        else:
            # 句中：奇偶交替做轻微起伏，避免"每段都一样"的平板感
            rate, pitch, vol = (0, 4, 0) if idx % 2 else (2, -2, -1)

        # ---- 2) 句型修正：疑问必升，感叹加强（覆盖上面的收束降调）----
        if is_last and not is_interj:
            if ends_question:
                rate, pitch, vol = -3, 22, 4
            elif ends_exclaim:
                rate, pitch, vol = -6, 15, 10

        # ---- 3) 强调词加重 ----
        if not is_interj and any(w in chunk for w in _STRESS_WORDS):
            rate -= 6
            pitch += 6
            vol += 6

        # ---- 4) 叠加情绪基调 + 风格强度 ----
        rate = _clamp(int(rate * gain) + d_rate, -40, 40)
        pitch = _clamp(int(pitch * gain) + d_pitch, -60, 60)
        vol = _clamp(int(vol * gain) + d_vol, -40, 40)

        # ---- 5) 后置停顿：按结尾标点定层级，句末段不补（由上层句间控制）----
        tail = chunk[-1:]
        if is_last:
            pause = 0
        elif is_interj:
            pause = 150
        else:
            pause = PAUSE_BY_PUNCT.get(tail, DEFAULT_PAUSE)
        pause = int(pause * gain)

        segments.append(Segment(chunk, "%+d%%" % rate, "%+dHz" % pitch, "%+d%%" % vol, pause))

    return segments
