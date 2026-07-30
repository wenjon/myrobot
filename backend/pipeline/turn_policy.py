"""对话轮次策略（turn-taking / barge-in）。

用于判断：当数字人正在说话时，用户新发来的一句应当
- 打断（interrupt）：明确要停/纠错/换话题，或提出了新问题；
- 附和（backchannel）：嗯、对、好的、哈哈之类，只是「我在听」，无需回应；
- 提问（question）：其它需要正经回答的内容。

分类只用轻量规则（关键词 + 长度），零成本、零延迟；
以后可替换为小模型分类器，接口保持 classify_incoming(text) 不变。
"""
from __future__ import annotations

import re

# 明确的打断/停止/纠错意图词（命中即打断）
_INTERRUPT_WORDS = [
    "停", "别说了", "别念了", "闭嘴", "打住", "等一下", "等等", "等下",
    "先别", "先停", "不是这个", "不对", "错了", "不要说", "换个", "换一个",
    "算了", "重来", "打断", "住口", "安静",
]

# 附和/应声词（只表示在听，通常无需回答）
_BACKCHANNEL_WORDS = {
    "嗯", "嗯嗯", "嗯呢", "恩", "哦", "噢", "喔", "唔",
    "对", "对对", "对的", "是", "是的", "是呀", "对呀", "没错",
    "好", "好的", "好呀", "行", "行吧", "可以", "ok", "okay", "okok",
    "哈哈", "哈哈哈", "呵呵", "嘿嘿", "嗯嗯嗯", "知道了", "了解", "收到",
    "继续", "然后呢", "嗯好", "好嘞",
}

# 去掉标点/空白后再比对
_PUNCT_RE = re.compile(r"[\s，,。.！!？?、；;：:~～…·]+")


def _normalize(text: str) -> str:
    return _PUNCT_RE.sub("", text.strip()).lower()


def classify_incoming(text: str) -> str:
    """返回 'interrupt' | 'backchannel' | 'question'。"""
    norm = _normalize(text)
    if not norm:
        return "backchannel"  # 空/纯标点：当作无意义应声

    # 1) 打断意图优先（含子串即算）
    for w in _INTERRUPT_WORDS:
        if w in norm:
            return "interrupt"

    # 2) 纯附和词（整句就是一个应声词，且较短）
    if norm in _BACKCHANNEL_WORDS:
        return "backchannel"
    # 很短(<=3字)且以附和词开头，也当附和（如「嗯好呀」）
    if len(norm) <= 3 and any(norm.startswith(w) for w in _BACKCHANNEL_WORDS):
        return "backchannel"

    # 3) 其余视为需要回答的新提问
    return "question"
