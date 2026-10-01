import re

# 已知表情/动作名单。必须与 frontend/src/main.js 的 EMOTION_SET / ACTION_MAP、
# head3d.js 的 EXPR / OVERLAYS 一致（scripts/check_expressions.py 会比对）。
# 解析器**只认名单内的词**，所以 [重要]、[1]、[求点头] 这类正常方括号内容不会被误吞。
EMOTIONS = {
    "平静", "开心", "悲伤", "生气", "惊讶", "疑惑", "害羞", "调皮", "无语", "思考",
    "尴尬", "得意", "委屈", "惊恐", "厌恶", "困倦", "撒娇", "期待",
}
ACTIONS = {
    "点头", "摇头", "歪头", "看左", "看右", "看上", "看下", "环视", "对视",
    "眨眼", "眨左眼", "眨右眼", "挑眉", "单挑眉", "皱眉", "鼓腮", "撅嘴", "吐舌",
    "咬唇", "努嘴",
}

# 类型判别关键字：写在方括号里的普通词，用来告诉调度器这是表情还是动作。
# 除了 表情/动作，还认"情绪"等近义说法——模型并不会严格照 prompt 用词。
_KIND_HINTS = {
    "表情": "表情", "emotion": "表情", "expr": "表情", "情绪": "表情",
    "动作": "动作", "action": "动作", "gesture": "动作",
}

# ---- 标记语法（实测大模型会写出下列**全部**写法，只支持第一种老写法曾导致：
#      动作不触发 + 标记被原样念出来，见 docs 第 5 章）：
#   [表情:开心]  类型前缀 + 冒号（prompt 教的规范写法）
#   [歪头:歪头]  名称:名称（模型最常写的一种，老语法完全不认）
#   [眨眼:1.0]   名称:数字（强度）
#   [开心]       裸名称
# 统一用这一个正则扫 `[内容]`，再在 _classify 里判别类型。
# 长名优先（"眨左眼"不能先命中"眨眼"）；末位 (?![^\[\]]*\]) 排除 [求点头] 这类
# 前缀夹带其他字的方括号，只认整体就是标记的。
# 正则只负责扫出方括号候选，类型判别全部交给 _classify_bracket——
# 这样模型自创的类型词（"手势"、"心情"…）也能认，只要名字在名单里。
MARKER_RE = re.compile(r"\[([^\[\]]+)\]")


def _classify_bracket(inner: str):
    """把一个方括号的内容判成 ('表情'|'动作', 值)，不是标记则返回 None。

    判别始终以**名字**为准：只要值在名单里就认，前缀是什么类型词都无所谓。
    所以 [表情:开心] / [歪头:歪头] / [手势:摇头] / [眨眼:1.0] / [开心] 全部命中，
    而 [重要] / [1] / [求点头] / [时间:12:00] 一律判为 None，安全留在正文里。
    """
    inner = inner.strip()
    if not inner:
        return None

    # (a) 裸名称：[开心] / [点头]
    if inner in EMOTIONS:
        return ("表情", inner)
    if inner in ACTIONS:
        return ("动作", inner)

    head, sep, tail = inner.replace("：", ":").partition(":")
    if not sep:
        return None
    head, tail = head.strip(), tail.strip()

    def by_name(name):
        if name in EMOTIONS:
            return ("表情", name)
        if name in ACTIONS:
            return ("动作", name)
        return None

    def as_value(name):
        # 名字优先：值在名单里就用它，保证 [歪头:歪头] / [手势:摇头] 得到规范值
        if name in EMOTIONS:
            return ("表情", name)
        if name in ACTIONS:
            return ("动作", name)
        return None

    # (b) "名称:名称" 或 "类型:名称" —— 取冒号右侧作为名字
    hit = as_value(tail)
    if hit:
        return hit

    # (c) 类型词在冒号左侧
    kind = _KIND_HINTS.get(head.lower())
    if kind:
        # 值在名单里最好；不在（如 [动作:挥手]，"挥手"没实现）也必须当标记剥离——
        # 前端对未知值会静默忽略，但**绝不能把它念出来**。这是本次 bug 的另一半。
        return (kind, tail or head)
    # (d) "名称:数字"（强度），如 [眨眼:1.0]
    if not tail or tail.replace(".", "").isdigit():
        hit = as_value(head)
        if hit:
            return hit
    return None

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
        # 1) 先抽取所有完整的动作标记（提前下发，让表情与语音同步）。
        #    正则一次扫出所有 [x] / [x:y] 候选，再逐个判别；判不出来的
        #    （如 [重要]）留在 buf 里当普通正文，不消耗。
        #    必须逐个处理并把命中前的正文先 flush：模型会把标记夹在句尾
        #    （"你好呀！[开心]"），先 flush 才能让这句话尽早开口。
        while True:
            found = None
            for m in MARKER_RE.finditer(buf):
                # 取整个匹配去掉两侧方括号，交给 _classify_bracket 判别。
                # 不能只取某个捕获组：带冒号那支的第 1 组只有"类型词"，不含值。
                classified = _classify_bracket(m.group(0)[1:-1])
                if classified:
                    found = (classified, m.start(), m.end())
                    break
            if not found:
                break
            (kind_norm, value), start, end = found
            pre = _clean(buf[:start])
            sentences, pre_rem = _flush_ready(pre)
            for s in sentences:
                yield {"type": "sentence", "text": s}
            yield {"type": "action", "action": kind_norm, "value": value}
            buf = pre_rem + buf[end:]
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
