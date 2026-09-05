# -*- coding: utf-8 -*-
"""生成试听样本，方便挑选音色与韵律风格后写进 .env。

用法：
    python scripts/tts_preview.py              # 音色对比 + 韵律 A/B
    python scripts/tts_preview.py 输出目录

默认输出到 voice_preview/（已在 .gitignore 中，音频不入库）。
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

TEXT = "你好呀，我是小柚！今天想聊点什么呢？哇，这个也太厉害了吧，我超开心的。"

# 音色候选：(音色, rate, pitch, 说明)
VOICES = [
    ("zh-CN-XiaoyiNeural",   "+8%",  "+10Hz", "活泼少女·当前默认，卡通/小说向，最贴合小柚"),
    ("zh-CN-XiaoyiNeural",   "+0%",  "+0Hz",  "活泼少女·原声参数"),
    ("zh-CN-XiaoxiaoNeural", "+0%",  "+0Hz",  "温暖女声·新闻/小说向，最稳最百搭"),
    ("zh-CN-XiaoxiaoNeural", "+10%", "+20Hz", "温暖女声·调快调高，年轻化"),
    ("zh-CN-YunxiaNeural",   "+0%",  "+0Hz",  "男童声·可爱路线"),
    ("zh-CN-YunxiNeural",    "+0%",  "+0Hz",  "少年男声·阳光活泼"),
    ("zh-TW-HsiaoChenNeural", "+0%", "+0Hz",  "台湾女声·亲切软糯"),
    ("zh-HK-HiuGaaiNeural",  "+0%",  "+0Hz",  "粤语女声"),
    ("zh-CN-liaoning-XiaobeiNeural", "+0%", "+0Hz", "东北方言女声·幽默"),
]

# 韵律 A/B：(文本, 表情)
PROSODY_CASES = [
    ("各位观众晚上好，欢迎收看今天的新闻，我是主播小柚。", "平静"),
    ("哇，这个也太厉害了吧！", "开心"),
    ("今天想聊点什么呢？", "期待"),
    ("我有点累了，想歇一会儿。", "困倦"),
    ("对不起……我可能真的做错了。", "悲伤"),
]


async def main() -> None:
    import edge_tts
    from pipeline.tts_edge import synthesize
    import base64

    out_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "voice_preview"
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = ["【一】音色对比（同一段文本，无韵律，只比音色本身）"]
    print(lines[0])
    for i, (voice, rate, pitch, note) in enumerate(VOICES, 1):
        name = "V%02d_%s.mp3" % (i, voice.replace("zh-", ""))
        comm = edge_tts.Communicate(TEXT, voice, rate=rate, pitch=pitch)
        await comm.save(str(out_dir / name))
        line = "  %-32s TTS_VOICE=%-32s TTS_RATE=%-5s TTS_PITCH=%-6s %s" % (
            name, voice, rate, pitch, note)
        print(line)
        lines.append(line)

    lines.append("")
    lines.append("【二】韵律 A/B（同一音色，_A 无韵律 vs _B 播音腔，成对试听）")
    print("\n" + lines[-1])
    for i, (text, emotion) in enumerate(PROSODY_CASES, 1):
        for tag, style in (("A_flat", "flat"), ("B_broadcast", "broadcast")):
            result = await synthesize(text, emotion=emotion, style=style)
            name = "P%d_%s.mp3" % (i, tag)
            (out_dir / name).write_bytes(base64.b64decode(result["audio"]))
            if tag.startswith("B"):
                line = "  P%d  [表情:%s] %s  (%d段 / %.0fms)" % (
                    i, emotion, text, result["segments"], result["duration_ms"])
                print(line)
                lines.append(line)

    (out_dir / "README.txt").write_text(
        "myrobot 语音试听样本\n"
        "音色对比文本：%s\n\n%s\n\n"
        "挑好后写进仓库根目录 .env（TTS_VOICE / TTS_RATE / TTS_PITCH / TTS_PROSODY），重启后端生效。\n"
        "也可以直接在页面调试面板的两个下拉框里现场切换。\n" % (TEXT, "\n".join(lines)),
        encoding="utf-8")
    print("\n输出目录:", out_dir.resolve())


if __name__ == "__main__":
    asyncio.run(main())
