# -*- coding: utf-8 -*-
"""TTS 韵律链路自检（离线 + 在线两段）。

固化本次踩到的三个坑，防止回归：
  1. MPEG-2 与 MPEG-1 的 Layer III 码率表不同、每帧采样数不同。
     用错表会把 48kbps 读成 80kbps，时长少算约 20%，
     多段拼接后时间轴平移不足，口型越走越慢。
  2. edge-tts >= 7 默认 boundary="SentenceBoundary"，一句只回一个 mark。
     必须显式传 WordBoundary，否则口型只动一下。
  3. Edge 每段音频尾部附加约 0.7~0.9s 固定静音。多段拼接必须裁掉，
     否则空档累积（实测 3 段多出 2.4s）。

离线部分（不联网）总是执行；在线部分需要能访问 Edge 服务，失败只警告不算错，
以免 CI 在断网环境下红掉。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

errors: list[str] = []
warnings: list[str] = []


def check(cond, msg):
    if not cond:
        errors.append(msg)
    return cond


# ---------------------------------------------------------------- 离线：帧解析
def test_frame_parsing():
    from pipeline.mp3_utils import duration_ms, iter_frames, make_silence, trim_to

    # 构造 MPEG-2 Layer III / 24kHz / 48kbps 帧头（Edge 的真实格式）
    # FF F3 64 C4 -> MPEG2, Layer III, 48kbps, 24000Hz, 无 padding -> 144 字节 / 24ms
    header = bytes([0xFF, 0xF3, 0x64, 0xC4])
    frame = header + b"\x00" * 140
    data = frame * 10

    frames = list(iter_frames(data))
    check(len(frames) == 10, "MPEG-2 帧解析：应识别 10 帧，实际 %d" % len(frames))
    check(all(f[1] == 144 for f in frames),
          "MPEG-2 帧长应为 144 字节，实际 %s" % sorted({f[1] for f in frames}))
    check(abs(duration_ms(data) - 240) < 1,
          "10 帧应为 240ms，实际 %.1fms（码率表用错会算成别的值）" % duration_ms(data))
    check(sum(f[1] for f in frames) == len(data),
          "帧应完整覆盖所有字节，未覆盖 %d 字节" % (len(data) - sum(f[1] for f in frames)))

    # MPEG-1 也要仍然正确（FF FB 90 00 -> MPEG1 L3 128kbps 44100Hz）
    m1 = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413
    f1 = list(iter_frames(m1 * 3))
    check(len(f1) == 3, "MPEG-1 帧解析：应识别 3 帧，实际 %d" % len(f1))
    check(abs(f1[0][2] - 26.12) < 0.1,
          "MPEG-1 帧时长应约 26.12ms，实际 %.2f" % f1[0][2])

    # 静音构造
    sil = make_silence(data, 240)
    check(abs(duration_ms(sil) - 240) < 25, "静音长度应约 240ms，实际 %.1f" % duration_ms(sil))
    check(len(sil) % 144 == 0, "静音应为整数帧")

    # 裁剪
    cut, kept = trim_to(data, 100)
    check(abs(kept - 96) < 25, "裁到 100ms 应保留约 96ms（整帧），实际 %.1f" % kept)
    check(len(cut) < len(data), "裁剪后应变短")
    check(trim_to(data, 0) == (b"", 0.0), "裁到 0 应返回空")


# ---------------------------------------------------------------- 离线：韵律规划
def test_prosody_plan():
    from pipeline.prosody import plan

    # 空文本
    check(plan("") == [], "空文本应返回空段列表")

    # flat 恒为单段
    check(len(plan("你好，世界。今天真好。", style="flat")) == 1, "flat 风格应只有 1 段")

    # 多小句应拆段，且首段音高 > 末段（起势高、收束降调）
    segs = plan("各位观众晚上好，欢迎收看今天的新闻，我是主播小柚。")
    check(len(segs) == 3, "三个小句应拆成 3 段，实际 %d" % len(segs))
    if len(segs) == 3:
        first_pitch = int(segs[0].pitch.rstrip("Hz"))
        last_pitch = int(segs[-1].pitch.rstrip("Hz"))
        check(first_pitch > last_pitch,
              "陈述句应起势高、收束低：首段 %+dHz 应大于末段 %+dHz" % (first_pitch, last_pitch))
        check(segs[-1].pause_ms == 0, "末段不应有后置停顿（句间由上层控制）")
        check(segs[0].pause_ms > 0, "非末段应有后置停顿")

    # 疑问句尾必须升调（不能沿用陈述句的降调）
    q = plan("你今天过得怎么样，开心吗？")
    check(int(q[-1].pitch.rstrip("Hz")) > 0,
          "疑问句末段应升调，实际 %s" % q[-1].pitch)

    # 语气词应被剥成独立段并拖长
    ex = plan("哇，这个也太厉害了吧！")
    check(len(ex) >= 2 and ex[0].text.startswith("哇"),
          "句首语气词应独立成段，实际首段=%r" % (ex[0].text if ex else None))
    if ex:
        check(int(ex[0].rate.rstrip("%")) < 0, "语气词段应放慢，实际 %s" % ex[0].rate)

    # 停顿层级：句号 > 逗号
    comma = plan("前面一句，后面一句。")
    check(comma[0].pause_ms > 0, "逗号后应有停顿")

    # 情绪偏置方向正确
    happy = plan("今天真好。", emotion="开心", style="flat")[0]
    sad = plan("今天真好。", emotion="悲伤", style="flat")[0]
    check(int(happy.rate.rstrip("%")) > int(sad.rate.rstrip("%")),
          "开心应比悲伤语速快：%s vs %s" % (happy.rate, sad.rate))
    check(int(happy.pitch.rstrip("Hz")) > int(sad.pitch.rstrip("Hz")),
          "开心应比悲伤音高高：%s vs %s" % (happy.pitch, sad.pitch))

    # 参数不得越界（Edge 对极端值会拒绝）
    for emo in ("惊恐", "困倦", "生气", "悲伤"):
        for seg in plan("哇，特别特别厉害，真的太棒了！", emotion=emo):
            r = int(seg.rate.rstrip("%")); pi = int(seg.pitch.rstrip("Hz")); v = int(seg.volume.rstrip("%"))
            check(-40 <= r <= 40, "[%s] rate 越界: %s" % (emo, seg.rate))
            check(-60 <= pi <= 60, "[%s] pitch 越界: %s" % (emo, seg.pitch))
            check(-40 <= v <= 40, "[%s] volume 越界: %s" % (emo, seg.volume))

    # 段文本拼接必须还原原句（不能丢字）
    text = "哇，这个也太厉害了吧！我是真的服了。"
    joined = "".join(s.text for s in plan(text))
    check(joined == text, "分段应无损还原原句：\n  原 %r\n  拼 %r" % (text, joined))


# ---------------------------------------------------------------- 离线：情绪表一致性
def test_emotion_coverage():
    """prosody.EMOTION_BIAS 应覆盖 head3d.js 里的全部表情，否则该表情没有声音变化。"""
    import re
    from pipeline.prosody import EMOTION_BIAS

    head = (ROOT / "frontend/src/head3d.js").read_text(encoding="utf-8")
    i = head.index("const EXPR =")
    i = head.index("{", i)
    depth = 0
    for j in range(i, len(head)):
        if head[j] == "{":
            depth += 1
        elif head[j] == "}":
            depth -= 1
            if depth == 0:
                block = head[i + 1:j]
                break
    names = set(re.findall(r"^\s*'([^']+)':\s*[{(]", block, re.M))
    missing = names - set(EMOTION_BIAS)
    check(not missing,
          "以下表情缺少声音基调（prosody.EMOTION_BIAS）：%s" % ", ".join(sorted(missing)))
    extra = set(EMOTION_BIAS) - names
    if extra:
        warnings.append("EMOTION_BIAS 有多余项（head3d.js 无此表情）：%s" % ", ".join(sorted(extra)))


# ---------------------------------------------------------------- 在线：真实合成
async def test_online():
    from pipeline.tts_edge import TTSUnavailable, synthesize

    text = "各位观众晚上好，欢迎收看今天的新闻，我是主播小柚。"
    try:
        result = await synthesize(text, style="broadcast", emotion="平静")
    except TTSUnavailable as exc:
        warnings.append("在线合成跳过（%s）" % exc)
        return
    except Exception as exc:                                  # noqa: BLE001
        warnings.append("在线合成跳过（%s: %s）" % (type(exc).__name__, exc))
        return

    marks = result["marks"]
    check(result["segments"] >= 2, "该句应拆成多段，实际 %d" % result["segments"])
    check(bool(marks), "应返回词边界时间轴")
    check(all(marks[i]["offset_ms"] <= marks[i + 1]["offset_ms"] for i in range(len(marks) - 1)),
          "时间轴必须单调递增（拼接平移量算错会回退）")
    check(all(0 <= m["char_index"] < len(text) for m in marks),
          "char_index 必须落在原文范围内")
    check(marks[-1]["offset_ms"] <= result["duration_ms"],
          "最后一个 mark（%dms）不应超过音频时长（%.0fms）"
          % (marks[-1]["offset_ms"], result["duration_ms"]))
    restored = "".join(text[m["char_index"]] for m in marks)
    expected = "".join(c for c in text if c not in "，。！？、；：…—")
    check(restored == expected,
          "口型字序应与原文一致：\n  期望 %r\n  实际 %r" % (expected, restored))
    print("  [在线] %d 段 / %.0fms / %d marks，字序与时间轴校验通过"
          % (result["segments"], result["duration_ms"], len(marks)))


def main():
    test_frame_parsing()
    test_prosody_plan()
    test_emotion_coverage()
    print("离线检查完成（帧解析 / 韵律规划 / 情绪覆盖）")
    asyncio.run(test_online())

    for warn in warnings:
        print("  [warn] " + warn)
    if errors:
        print("\n发现 %d 处问题：" % len(errors))
        for e in errors:
            print("  [x] " + e)
        sys.exit(1)
    print("\nOK：TTS 韵律链路检查通过")


if __name__ == "__main__":
    main()
