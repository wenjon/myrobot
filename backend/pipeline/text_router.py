"""文本解析与中央调度模块（核心枢纽）。

职责：
- 实时接收 LLM 增量 token；
- 清洗（去 markdown/emoji/多余空白）；
- 抽取行内动作标记 [表情:x] / [动作:x]，作为 action 提前下发；
- 智能分句，均衡长短句，就绪即产出。

以异步生成器形式产出事件 dict：
  {"type": "action", "action": "表情", "value": "开心"}
  {"type": "sentence", "text": "你好呀"}
"""
import re
from typing import AsyncIterator, Dict

from config import SENTENCE_MIN_LEN, SENTENCE_MAX_LEN, STRONG_PUNCT, WEAK_PUNCT

# 行内动作标记：[表情:开心] [动作:点头]
ACTION_RE = re.compile(r"\[(表情|动作|emotion|action)\s*[:：]\s*([^\]]+)\]")
# emoji / 杂符清理
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF]",
    flags=re.UNICODE,
)
MD_RE = re.compile(r"[*_`#>~]")


def _clean(text: str) -> str:
    text = EMOJI_RE.sub("", text)
    text = MD_RE.sub("", text)
    return text


def _flush_ready(buf: str, force: bool = False):
    """从缓冲里切出可发送的句子，返回 (sentences, remaining)。"""
    out = []
    i = 0
    start = 0
    while i < len(buf):
        ch = buf[i]
        seg_len = i - start + 1
        if ch in STRONG_PUNCT:
            seg = buf[start : i + 1].strip()
            if seg:
                out.append(seg)
            start = i + 1
        elif ch in WEAK_PUNCT and seg_len >= SENTENCE_MIN_LEN:
            seg = buf[start : i + 1].strip()
            if seg:
                out.append(seg)
            start = i + 1
        elif seg_len >= SENTENCE_MAX_LEN:
            seg = buf[start : i + 1].strip()
            if seg:
                out.append(seg)
            start = i + 1
        i += 1
    remaining = buf[start:]
    if force and remaining.strip():
        out.append(remaining.strip())
        remaining = ""
    return out, remaining


async def route(token_stream: AsyncIterator[str]) -> AsyncIterator[Dict]:
    """核心中央调度：消费 token 流，产出 action / sentence 事件。"""
    buf = ""
    async for token in token_stream:
        buf += token
        # 先抽取完整的动作标记（提前下发）
        while True:
            m = ACTION_RE.search(buf)
            if not m:
                break
            kind, value = m.group(1), m.group(2).strip()
            kind_norm = "表情" if kind in ("表情", "emotion") else "动作"
            # 标记之前的文本先按分句处理
            pre = _clean(buf[: m.start()])
            sentences, pre_rem = _flush_ready(pre)
            for s in sentences:
                yield {"type": "sentence", "text": s}
            # 动作提前下发
            yield {"type": "action", "action": kind_norm, "value": value}
            # 剩余未成句部分 + 标记之后内容，拼回缓冲
            buf = pre_rem + buf[m.end():]
        # 普通分句
        cleaned = _clean(buf)
        sentences, buf = _flush_ready(cleaned)
        for s in sentences:
            yield {"type": "sentence", "text": s}
    # 收尾 flush
    sentences, _ = _flush_ready(_clean(buf), force=True)
    for s in sentences:
        yield {"type": "sentence", "text": s}
