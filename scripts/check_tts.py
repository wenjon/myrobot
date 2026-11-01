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
import json
import pathlib
import sys
import tempfile

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


# ---------------------------------------------------------------- 离线：音色注册表
def test_voice_registry():
    """覆盖 P0 的注册表规则：缺 ref、拒绝公众人物、坏 JSON、edge_voice 映射。"""
    from tts.voices import VoiceRegistry

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)

        def add(name, meta, ref=False):
            d = root / name
            d.mkdir()
            (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            if ref:
                (d / "ref.wav").write_bytes(b"RIFF")

        add("good", {"display_name": "好音色", "source": "clone", "consent": {"type": "self"}}, ref=True)
        add("noref", {"display_name": "缺样本", "source": "clone"})
        add("denied", {"display_name": "公众人物", "source": "clone",
                       "consent": {"type": "public-figure"}}, ref=True)
        add("preset", {"display_name": "预设", "source": "edge", "edge_voice": "zh-CN-XiaoxiaoNeural"})
        add("unmapped", {"display_name": "本地方案", "source": "clone"}, ref=True)
        (root / "broken").mkdir()
        (root / "broken" / "meta.json").write_text("{not json", encoding="utf-8")
        (root / "no_meta").mkdir()

        reg = VoiceRegistry(root)
        entries, diags = reg.scan()
        by_id = {e.id: e for e in entries}

        check("broken" not in by_id, "坏 meta.json 应被跳过，而不是让整次扫描失败")
        check(any("broken" in d for d in diags), "坏 meta.json 应产生诊断信息")
        check("no_meta" not in by_id, "没有 meta.json 的目录应忽略")

        check(by_id["good"].available, "有 ref.wav 的克隆音色应可用")
        check(not by_id["noref"].available and by_id["noref"].reason == "missing_ref_audio",
              "缺 ref.wav 应标记 missing_ref_audio，实际 %s" % by_id["noref"].reason)
        check(not by_id["denied"].available and by_id["denied"].reason == "consent_denied",
              "public-figure 必须拒绝加载")

        v, note = reg.resolve_for_provider("preset", "edge")
        check(v == "zh-CN-XiaoxiaoNeural" and not note, "edge 音色应映射到 edge_voice")
        v2, note2 = reg.resolve_for_provider("unmapped", "edge")
        check(v2 == "" and "回退默认" in note2, "本地音色未配 edge_voice 时应回退默认并给提示")
        v3, _ = reg.resolve_for_provider("zh-CN-YunxiNeural", "edge")
        check(v3 == "zh-CN-YunxiNeural", "未知 ID 应原样透传（兼容旧的 Edge 音色名）")


# ---------------------------------------------------------------- 离线：provider 装配
def test_provider_order():
    from tts.registry import provider_order

    check(provider_order("edge", "") == ["edge"], "edge 应只尝试 edge")
    check(provider_order("web", "") == [], "web 不应在服务端合成")
    check(provider_order("cosyvoice", "") == ["cosyvoice", "edge"], "新引擎应带回退到 edge")
    check(provider_order("edge", " cosyvoice , edge ,, cosyvoice ") == ["cosyvoice", "edge"],
          "显式顺序应去空白、去重并保序")


class _FakeProvider:
    """自检用的最小 provider：可配置探活结果、是否失败、音色清单。"""

    supports_stream = False
    supports_clone = True
    emotions = frozenset()

    def __init__(self, name="fake", *, healthy=True, fail=False, voices=None, audio=b"ID3fake"):
        self.name = name
        self._healthy = healthy
        self._fail = fail
        self._voices = list(voices or [])
        self._audio = audio
        self.last_req = None

    async def health(self):
        return self._healthy

    async def list_voices(self):
        return self._voices

    async def synthesize(self, req):
        from tts.base import TTSMark, TTSResult, TTSUnavailable

        self.last_req = req
        if self._fail:
            raise TTSUnavailable("模拟失败")
        return TTSResult(
            audio=self._audio, mime="audio/mpeg",
            marks=[TTSMark(char_index=0, text="你", offset_ms=0, duration_ms=100)],
            duration_ms=100.0, provider=self.name, voice=req.voice or "default",
        )

    async def cancel(self, seq):
        return None


def test_registry_fallback():
    """降级链、截断、音色解析、返回结构四件事一起验。"""
    from tts.base import TTSRequest, TTSUnavailable
    from tts.registry import TTSRegistry
    from tts.voices import VoiceRegistry

    with tempfile.TemporaryDirectory() as tmp:
        bad = _FakeProvider("bad", fail=True)
        good = _FakeProvider("good")
        reg = TTSRegistry(
            VoiceRegistry(tmp), order=["bad", "good"], default_voice="",
            max_chars=10,
            factories={"bad": lambda: bad, "good": lambda: good},
        )
        out = asyncio.run(reg.synthesize(TTSRequest(text="你好呀，这是一个很长的句子")))
        check(out["provider"] == "good", "第一个 provider 失败应自动降级到下一个")
        check(good.last_req.text == "你好呀，这是一个很长", "超长文本应按 max_chars 截断")
        check(out["marks"] and out["audio"], "返回结构应含音频与时间轴")

        only_bad = TTSRegistry(
            VoiceRegistry(tmp), order=["bad"], factories={"bad": lambda: _FakeProvider("bad", fail=True)},
        )
        try:
            asyncio.run(only_bad.synthesize(TTSRequest(text="你好")))
        except TTSUnavailable:
            pass
        else:
            check(False, "全部 provider 失败时应抛 TTSUnavailable")

        unhealthy = TTSRegistry(
            VoiceRegistry(tmp), order=["cold"],
            factories={"cold": lambda: _FakeProvider("cold", healthy=False)},
        )
        try:
            asyncio.run(unhealthy.synthesize(TTSRequest(text="你好")))
        except TTSUnavailable:
            pass
        else:
            check(False, "探活失败的 provider 不应被调用")


def test_voice_listing():
    """本地音色 + 引擎音色合并，字段必须补齐成前端认识的结构。"""
    from tts.registry import TTSRegistry
    from tts.voices import VoiceRegistry

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "v"
        d = root / "mine"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"display_name": "我的音色", "source": "edge",
                                                 "edge_voice": "zh-CN-XiaoyiNeural"}), encoding="utf-8")
        fake = _FakeProvider("fake", voices=[
            {"name": "v1", "locale": "zh-CN", "gender": "Female"},
            {"name": "en1", "locale": "en-US", "gender": "Male"},
        ])
        reg = TTSRegistry(VoiceRegistry(root), order=["fake"], factories={"fake": lambda: fake})
        out = asyncio.run(reg.list_voices("zh"))
        ids = {v["id"] for v in out["voices"]}
        check("mine" in ids, "本地音色应出现在清单里")
        check("v1" in ids and "en1" not in ids, "应按 locale 过滤引擎音色")
        mine = next(v for v in out["voices"] if v["id"] == "mine")
        check(mine["source"] == "edge" and mine["available"], "本地音色字段应完整且可用")
        v1 = next(v for v in out["voices"] if v["id"] == "v1")
        check(v1["source"] == "fake" and "categories" in v1, "引擎音色应补齐统一字段")


def test_result_contract():
    """TTSResult.to_legacy() 必须与旧接口同构（前端零改动的前提）。"""
    import base64
    from tts.base import TTSMark, TTSResult

    r = TTSResult(audio=b"abc", mime="audio/mpeg",
                  marks=[TTSMark(0, "你", 0, 120)], duration_ms=123.45,
                  provider="edge", voice="v", segments=2, style="broadcast")
    d = r.to_legacy()
    check(base64.b64decode(d["audio"]) == b"abc", "audio 必须 base64 往返一致")
    check(d["marks"][0]["char_index"] == 0 and d["marks"][0]["duration_ms"] == 120,
          "marks 字段名应与前端消费格式一致")
    check(d["duration_ms"] == 123.5 or d["duration_ms"] == 123.4,
          "duration_ms 应保留一位小数，实际 %s" % d["duration_ms"])
    for key in ("mime", "voice", "provider", "segments", "style", "marks_source"):
        check(key in d, "旧接口结构缺少字段 %s" % key)


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
    test_voice_registry()
    test_provider_order()
    test_registry_fallback()
    test_voice_listing()
    test_result_contract()
    print("离线检查完成（帧解析 / 韵律规划 / 情绪覆盖 / 音色注册表 / 降级链 / 返回契约）")
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
