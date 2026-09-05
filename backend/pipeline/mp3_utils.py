# -*- coding: utf-8 -*-
"""MP3 帧级工具：测时长、构造静音、无损拼接。

为什么需要它：
    Edge 免费端点不接受 SSML（实测传 <break>/<prosody> 会 NoAudioReceived），
    唯一能控的是每次请求的 rate/pitch/volume 三个全局参数。
    要做出句内抑扬顿挫，只能把一句话拆成若干韵律段、逐段用不同参数合成，
    再在服务端把 mp3 拼起来。拼接需要知道每段的精确时长（用于平移词边界时间轴），
    以及能插入精确长度的静音（用于停顿层级）。

为什么可以直接按字节拼 mp3：
    Edge 返回的是 MPEG-1/2 Layer III 裸流（实测 24kHz 单声道，无 ID3、无 VBR 头），
    每帧自带完整帧头、可独立解码，因此帧序列首尾相接即为合法 mp3。
    实测拼接后重新解析，帧数与总时长严格等于各段之和。
"""
from __future__ import annotations

# MPEG 音频帧头查表。
# 注意：MPEG-1 与 MPEG-2/2.5 的 Layer III 码率表**完全不同**，且每帧采样数也不同
# （1152 vs 576）。Edge 返回的正是 MPEG-2 Layer III / 24kHz / 48kbps（帧头 FF F3 64 C4，
# 帧长 144 字节 / 24ms）。早期实现只用 MPEG-1 表，把 48kbps 误读成 80kbps，
# 算出的帧长偏大 → 跳过真帧、误判假帧 → 时长少算约 20%，导致多段拼接后
# 时间轴平移量不足、口型越走越慢（实测句1 少算 580ms）。
_BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_BITRATES_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
_SAMPLE_RATES_V1 = {0: 44100, 1: 48000, 2: 32000, 3: 0}
_SAMPLE_RATES_V2 = {0: 22050, 1: 24000, 2: 16000, 3: 0}


def iter_frames(data: bytes):
    """逐帧产出 (起始偏移, 帧字节数, 帧时长ms)。非法字节会被跳过（容错同步）。"""
    i = 0
    n = len(data)
    # 跳过 ID3v2（Edge 目前不带，但别人的音频可能带）
    if data[:3] == b"ID3" and n >= 10:
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) \
             | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        i = 10 + size

    while i + 4 <= n:
        # 帧同步字：11 个 1
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
            i += 1
            continue
        b1, b2 = data[i + 1], data[i + 2]
        version = (b1 >> 3) & 0x03      # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
        layer = (b1 >> 1) & 0x03        # 1=Layer III
        if layer != 1:
            i += 1
            continue
        br_table = _BITRATES_V1_L3 if version == 3 else _BITRATES_V2_L3
        bitrate = br_table[(b2 >> 4) & 0x0F]
        sr_index = (b2 >> 2) & 0x03
        sample_rate = _SAMPLE_RATES_V1[sr_index] if version == 3 else _SAMPLE_RATES_V2[sr_index]
        if version == 0 and sample_rate:
            sample_rate //= 2           # MPEG2.5 再减半
        if not bitrate or not sample_rate:
            i += 1
            continue
        padding = (b2 >> 1) & 0x01
        samples_per_frame = 1152 if version == 3 else 576
        frame_len = int(samples_per_frame / 8 * bitrate * 1000 / sample_rate) + padding
        if frame_len <= 4:
            i += 1
            continue
        # 校验：按算出的帧长跳过去应当还是同步字（或已到流尾）。
        # 不校验的话，音频数据里偶然出现的 FF Ex 会被当成帧头，算出离谱的帧长。
        nxt = i + frame_len
        if nxt + 2 <= n and not (data[nxt] == 0xFF and (data[nxt + 1] & 0xE0) == 0xE0):
            i += 1
            continue
        yield i, frame_len, samples_per_frame / sample_rate * 1000
        i += frame_len


def duration_ms(data: bytes) -> float:
    """按帧累加得到精确时长（比按码率估算可靠，Edge 是 CBR 但仍以帧为准）。"""
    return round(sum(f[2] for f in iter_frames(data)), 1)


def make_silence(reference: bytes, target_ms: float) -> bytes:
    """构造与 reference 同格式的静音，长度向最近的整数帧取整。

    做法：复用参考音频的首帧帧头（保证采样率/码率/声道一致），数据体填 0。
    Layer III 全零数据体解码即为无声，主流解码器（含浏览器）均可正常处理。
    因此静音粒度 = 单帧时长，24kHz 下约 24ms，对停顿控制足够精细。
    """
    if target_ms <= 0:
        return b""
    first = next(iter_frames(reference), None)
    if first is None:
        return b""
    offset, frame_len, frame_ms = first
    header = reference[offset:offset + 4]
    count = int(round(target_ms / frame_ms))
    if count <= 0:
        return b""
    return (header + b"\x00" * (frame_len - 4)) * count


def trim_to(data: bytes, keep_ms: float) -> tuple:
    """按整帧截断，保留前 keep_ms 毫秒。返回 (音频, 实际保留ms)。

    用途：Edge 每次合成都会在尾部附加约 0.7~0.9s 的固定静音填充。
    单段播放时无感，但多段拼接会把这些静音累积成长空档
    （实测 3 段句子多出 2.4s）。所以每段都要按"最后一个词边界 + 少量余量"裁掉尾巴。
    截断以帧为单位，不会破坏 mp3 结构。
    """
    if keep_ms <= 0:
        return b"", 0.0
    kept = []
    acc = 0.0
    for offset, frame_len, frame_ms in iter_frames(data):
        if acc >= keep_ms:
            break
        kept.append(data[offset:offset + frame_len])
        acc += frame_ms
    return b"".join(kept), round(acc, 1)
