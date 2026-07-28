import re
# 动作标记：只匹配 表情/emotion/Emotion/动作/action
ACTION_RE = re.compile(r"\[(表情|emotion|Emotion|动作|action)\s*[:：]\s*([^\]]+)\]")
# 可能是标记开头的未闭合片段（用于暂缓分句，避免把半个标记当句子发出）
PARTIAL_RE = re.compile(r"\[[^\]]*$")
MARKDOWN_RE = re.compile(r"[*_`#>~]")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF]", flags=re.UNICODE)

from config import SENTENCE_MIN_LEN, SENTENCE_MAX_LEN, STRONG_PUNCT, WEAK_PUNCT

def _clean(text: str) -> str:
    text = EMOJI_RE.sub("", text)
    text = MARKDOWN_RE.sub("", text)
    return text

def _flush_ready(buf: str, force: bool = False):
    out = []; i = 0; start = 0
    while i < len(buf):
        ch = buf[i]; seg_len = i - start + 1
        if ch in STRONG_PUNCT:
            seg = buf[start : i + 1].strip()
            if seg: out.append(seg)
            start = i + 1
        elif ch in WEAK_PUNCT and seg_len >= SENTENCE_MIN_LEN:
            seg = buf[start : i + 1].strip()
            if seg: out.append(seg)
            start = i + 1
        elif seg_len >= SENTENCE_MAX_LEN:
            seg = buf[start : i + 1].strip()
            if seg: out.append(seg)
            start = i + 1
        i += 1
    remaining = buf[start:]
    if force and remaining.strip():
        out.append(remaining.strip())
        remaining = ""
    return out, remaining

async def route(token_stream):
    buf = ""
    async for token in token_stream:
        buf += token
        # 1) 先抽取所有完整的动作标记（提前下发）
        while True:
            m = ACTION_RE.search(buf)
            if not m: break
            kind, value = m.group(1), m.group(2).strip()
            kind_norm = "表情" if kind in ("表情", "emotion", "Emotion") else "动作"
            pre = _clean(buf[: m.start()])
            sentences, pre_rem = _flush_ready(pre)
            for s in sentences:
                yield {"type": "sentence", "text": s}
            yield {"type": "action", "action": kind_norm, "value": value}
            buf = pre_rem + buf[m.end():]
        # 2) 普通分句：但如果尾部有未闭合的 '[...'（可能是半个标记），先留住不发
        partial = PARTIAL_RE.search(buf)
        if partial:
            head, tail = buf[: partial.start()], buf[partial.start():]
        else:
            head, tail = buf, ""
        cleaned = _clean(head)
        sentences, head_rem = _flush_ready(cleaned)
        for s in sentences:
            yield {"type": "sentence", "text": s}
        buf = head_rem + tail
    # 收尾 flush
    sentences, _ = _flush_ready(_clean(buf), force=True)
    for s in sentences:
        yield {"type": "sentence", "text": s}
