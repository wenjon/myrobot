# -*- coding: utf-8 -*-
"""生成各音色试听样本，方便挑选后把参数写进 .env 的 TTS_VOICE。

用法：python scripts/tts_preview.py [输出目录]
默认输出到 voice_preview/（已在 .gitignore 中，音频不入库）。
"""
import asyncio
import pathlib
import sys

TEXT = "你好呀，我是小柚！今天想聊点什么呢？哇，这个也太厉害了吧，我超开心的。"

# (音色, rate, pitch, 说明)
CANDIDATES = [
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


async def main() -> None:
    import edge_tts

    root = pathlib.Path(__file__).resolve().parent.parent
    out_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else root / "voice_preview"
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for i, (voice, rate, pitch, note) in enumerate(CANDIDATES, 1):
        name = "%02d_%s.mp3" % (i, voice.replace("zh-", ""))
        comm = edge_tts.Communicate(TEXT, voice, rate=rate, pitch=pitch)
        await comm.save(str(out_dir / name))
        print("%-34s rate=%-5s pitch=%-6s %s" % (name, rate, pitch, note))
        lines.append("%-34s TTS_VOICE=%-32s TTS_RATE=%-5s TTS_PITCH=%-6s %s"
                     % (name, voice, rate, pitch, note))

    (out_dir / "README.txt").write_text(
        "试听样本\n试听文本：%s\n\n%s\n\n挑好后把对应三个变量写进仓库根目录 .env，重启后端生效。\n"
        % (TEXT, "\n".join(lines)),
        encoding="utf-8")
    print("\n输出目录:", out_dir.resolve())


if __name__ == "__main__":
    asyncio.run(main())
