# -*- coding: utf-8 -*-
"""中央调度（text_router）标记解析自检。

背景：大模型**不会**照 system prompt 写 `[表情:开心]`，实测它会写出：
    [疑惑:疑惑] 发送什么呀？ [歪头:歪头] 我没听懂呢。 [眨眼:眨眼] 能再说清楚点吗？
老解析只认 `表情/emotion/动作/action` 四个类型词，于是上面三处里
「[疑惑:疑惑]」能识别，而「[歪头:歪头]」「[眨眼:眨眼]」既不驱动动作、
又被原样当成正文送去 TTS 念出来。这就是「数字人不动 + 把标记念出来」的根因。
本脚本把当时抓到的真实字符串固化成用例，防止回归。

用法：
    python scripts/check_text_router.py
退出码 0 = 全部通过；1 = 有用例失败。

设计取舍：纯静态，不请求 LLM、不需要密钥，所以能进 CI。
所有用例都按「逐字符喂入」跑——这是最坏情况，能顺带验证跨 token 切割的标记。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from pipeline.text_router import route  # noqa: E402

failures = []


def run(text: str):
    """逐字符流式喂入，返回 route() 产出的事件列表（模拟真实 token 流）。"""
    async def gen():
        for ch in text:
            yield ch

    async def main():
        return [ev async for ev in route(gen())]

    return asyncio.run(main())


def check(label, text, want_actions, want_sentences):
    got = run(text)
    actions = [(e["action"], e["value"]) for e in got if e["type"] == "action"]
    sentences = [e["text"] for e in got if e["type"] == "sentence"]
    if actions != want_actions:
        failures.append(f"{label}: 动作不符\n     期望 {want_actions}\n     实际 {actions}")
    if sentences != want_sentences:
        failures.append(f"{label}: 句子不符\n     期望 {want_sentences}\n     实际 {sentences}")


# ---- 1) 真实日志里出现过的三种输出（核心回归用例）----
check("日志真实①",
      "[疑惑:疑惑] 发送什么呀？ [歪头:歪头] 我没听懂呢。 [眨眼:眨眼] 能再说清楚点吗？",
      [("表情", "疑惑"), ("动作", "歪头"), ("动作", "眨眼")],
      ["发送什么呀？", "我没听懂呢。", "能再说清楚点吗？"])

check("日志真实②",
      "[开心:开心] 你好呀！ [眨眼:眨眼] 我是小柚。 [期待:期待] 今天想聊点什么？",
      [("表情", "开心"), ("动作", "眨眼"), ("表情", "期待")],
      ["你好呀！", "我是小柚。", "今天想聊点什么？"])

# ---- 2) 各种写法都要认 ----
check("规范前缀 [表情:X]/[动作:X]",
      "[表情:开心]你好呀。[动作:点头]好的。",
      [("表情", "开心"), ("动作", "点头")], ["你好呀。", "好的。"])

check("裸名称 [开心]/[点头]",
      "[开心]你好。[点头]好的。",
      [("表情", "开心"), ("动作", "点头")], ["你好。", "好的。"])

check("名称:强度 [眨眼:1.0]（值应是名字，不是数字）",
      "[眨眼:1.0]嘿嘿。",
      [("动作", "眨眼")], ["嘿嘿。"])

check("模型自创类型词 [手势:摇头]",
      "[手势:摇头]没有。",
      [("动作", "摇头")], ["没有。"])

check("长名优先 [眨左眼] 不能被 [眨眼] 抢先",
      "[眨左眼]嘿。[眨右眼]哈。",
      [("动作", "眨左眼"), ("动作", "眨右眼")], ["嘿。", "哈。"])

# 值未实现但类型词明确：必须剥离，绝不能念出来（前端会静默忽略）。
# 只断言「无残留 + action 正确」，不断言分句边界（去标记后空格归属会变）。
for label, text, want_action, want_joined in [
    ("未实现的动作 [动作:挥手]", "我是小柚 [动作:挥手]。", ("动作", "挥手"), "我是小柚 。"),
    ("未实现的表情 [表情:兴奋]", "[表情:兴奋]太好了！", ("表情", "兴奋"), "太好了！"),
]:
    got = run(text)
    actions = [(e["action"], e["value"]) for e in got if e["type"] == "action"]
    joined = "".join(e["text"] for e in got if e["type"] == "sentence")
    if actions != [want_action]:
        failures.append(f"{label}: 动作不符，期望 {[want_action]}，实际 {actions}")
    if joined != want_joined:
        failures.append(f"{label}: 正文不符，期望 {want_joined!r}，实际 {joined!r}")

check("标记在句尾（必须照样下发，否则数字人不动）",
      "你好呀！[开心]",
      [("表情", "开心")], ["你好呀！"])

# ---- 3) 误吞防护：正常方括号文本必须原样保留，且绝不能产生 action ----
# 这里只断言「内容一字不少 + 无 action」，不断言分句边界：
# 冒号本身是语法的次级断句符（WEAK_PUNCT 含 ':'），正文里的冒号照常断句是既有行为。
for label, text in [
    ("[重要]/[1]", "[重要]这段要留[1]住。"),
    ("[求点头]", "请问[求点头]怎么走？"),
    ("[时间:12:00]", "会议[时间:12:00]开始。"),
    ("数值区间 [1-2]", "章节[1-2]都看过。"),
]:
    got = run(text)
    actions = [e for e in got if e["type"] == "action"]
    joined = "".join(e["text"] for e in got if e["type"] == "sentence")
    if actions:
        failures.append(f"误吞防护 {label}: 不该产生 action，实际 {actions}")
    if joined != text:
        failures.append(f"误吞防护 {label}: 正文被改动\n     期望 {text!r}\n     实际 {joined!r}")

# ---- 4) 报告 ----
total = 12
if failures:
    print("标记解析自检失败 %d 项：" % len(failures))
    for f in failures:
        print("  [x] " + f)
    sys.exit(1)
print(f"OK：中央调度标记解析 {total} 项用例全部通过（含真实日志字符串与误吞防护）")