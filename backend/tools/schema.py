"""工具框架·从函数签名自动推导 JSON Schema。

为什么要这个模块：
    手写 OpenAI 风格的 parameters schema 很啰嗜，一个两参数的工具就要 14 行样板，
    而且它与函数签名是两份重复信息，很容易写歪（schema 里叫 query、
    函数参数叫 q），而且只会在运行时报 TypeError 才被发现。

做法：
    用 inspect 反射读函数的参数名/类型注解/默认值，再从 docstring 里取
    描述文字，拼成 schema。推导结果与手写等价，但工具作者只需写函数本身。

支持的 docstring 风格（Google 风格，项目内统一用这种）：

    async def web_search(query: str, top_k: int = 5) -> str:
        \"\"\"联网搜索实时信息。

        可以写多行，空行之前的全部内容都作为 description。

        Args:
            query: 搜索关键词或问题
            top_k: 返回结果条数（默认 5，最多 10）
        \"\"\"

推导规则：
    - name        <- 函数名 func.__name__
    - description <- docstring 首段（直到第一个空行或 Args: 为止）
    - properties  <- 参数名 + 类型注解映射成 JSON 类型
    - 参数 description <- Args: 段里对应行
    - required    <- 没有默认值的参数
    - 自动忽略 ctx / self / cls（框架注入的参数，不属于 LLM 可填参数）

边界：
    嵌套对象、enum 枚举、数组 items 类型这些复杂结构推不出来，
    遇到时给 @tool 显式传 parameters= 手写覆盖即可（两种写法长期共存）。
"""
from __future__ import annotations

import inspect
import re
import typing
from typing import Any, Callable, Dict, List, Optional, Tuple


# 框架自己注入的参数名，不应暂露给 LLM。
_INJECTED_PARAMS = {"ctx", "self", "cls"}

# Python 类型 -> JSON Schema 类型。
_TYPE_MAP: Dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _json_type(annotation: Any) -> Tuple[str, Optional[Dict[str, Any]]]:
    """把 Python 类型注解映射成 (json_type, extra)。

    extra 用于携带额外字段，如 List[str] 会带上 items。
    无注解或认不出的类型一律当 string（最安全的默认，LLM 总能填字符串）。
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return "string", None

    # Optional[X] / Union[X, None] -> 剥掉 None 取 X
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _json_type(args[0])
        return "string", None

    # List[X] / list[X] -> array，并推出 items 类型
    if origin in (list, List):
        args = typing.get_args(annotation)
        if args:
            item_type, _ = _json_type(args[0])
            return "array", {"items": {"type": item_type}}
        return "array", {"items": {"type": "string"}}

    if origin in (dict, Dict):
        return "object", None

    # 普通类（含 bool 先于 int 判定，因为 bool 是 int 的子类）
    if annotation is bool:
        return "boolean", None
    mapped = _TYPE_MAP.get(annotation)
    if mapped:
        return mapped, None

    # 枚举类 -> string + enum 候选值
    if inspect.isclass(annotation) and issubclass(annotation, __import__("enum").Enum):
        return "string", {"enum": [e.value for e in annotation]}

    return "string", None


def parse_docstring(doc: Optional[str]) -> Tuple[str, Dict[str, str]]:
    """拆解 docstring，返回 (首段描述, {参数名: 描述})。

    首段 = 开头到第一个空行或 Args:/Parameters: 之前的内容（单行合并）。
    参数描述 = Args: 段下形如 "name: 说明" 或 "name (str): 说明" 的行，
                 支持接续行（缩进更深则归入上一个参数）。
    """
    if not doc:
        return "", {}
    lines = inspect.cleandoc(doc).split("\n")

    desc_parts: List[str] = []
    args: Dict[str, str] = {}
    in_args = False
    desc_done = False   # 首段已结束（遇空行），但仍需继续扫描后面的 Args: 段
    current: Optional[str] = None

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        # 进入 Args: 段
        if re.match(r"^(Args|Arguments|Parameters|参数)\s*[:：]\s*$", stripped, re.IGNORECASE):
            in_args = True
            current = None
            continue
        # 遇到其他段落标题（Returns/Raises…）则退出 Args 段
        if re.match(r"^(Returns?|Raises?|Yields?|Examples?|Note|返回|异常|示例|注意)\s*[:：]", stripped, re.IGNORECASE):
            in_args = False
            current = None
            continue

        if in_args:
            if not stripped:
                continue
            # "name: 说明" 或 "name (str): 说明"
            m = re.match(r"^([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*[:：]\s*(.*)$", stripped)
            if m:
                current = m.group(1)
                args[current] = m.group(2).strip()
            elif current:
                # 接续行：拼到上一个参数的描述后面
                args[current] = (args[current] + " " + stripped).strip()
            continue

        # 不在 Args: 段：正在收集首段描述。
        # 空行代表首段结束，但不能提前 break——Args: 段通常就在空行之后。
        if not stripped:
            if desc_parts:
                desc_done = True
            continue
        # 首段之后的补充正文不再计入 description，
        # 只给 LLM 最简洁的一段工具说明（长描述反而干扰选工具）。
        if desc_done:
            continue
        desc_parts.append(stripped)

    return " ".join(desc_parts).strip(), args


def schema_from_function(func: Callable) -> Dict[str, Any]:
    """从函数签名 + docstring 推导 OpenAI 风格的 parameters schema。"""
    _, arg_docs = parse_docstring(func.__doc__)
    sig = inspect.signature(func)

    # 类型注解可能是字符串（from __future__ import annotations），先尝试解析真实类型
    try:
        hints = typing.get_type_hints(func)
    except Exception:  # noqa: BLE001  引用了未导入类型时降级处理
        hints = {}

    props: Dict[str, Any] = {}
    required: List[str] = []

    for pname, param in sig.parameters.items():
        if pname in _INJECTED_PARAMS:
            continue
        # *args / **kwargs 无法表达为 schema，跳过
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        annotation = hints.get(pname, param.annotation)
        json_type, extra = _json_type(annotation)
        entry: Dict[str, Any] = {"type": json_type}
        if extra:
            entry.update(extra)
        if pname in arg_docs:
            entry["description"] = arg_docs[pname]
        props[pname] = entry

        if param.default is inspect.Parameter.empty:
            required.append(pname)

    schema: Dict[str, Any] = {"type": "object", "properties": props}
    # required 为空时也显式写出，与既有手写 schema 的习惯保持一致
    schema["required"] = required
    return schema


def describe_from_function(func: Callable) -> str:
    """取 docstring 首段作为工具 description。"""
    desc, _ = parse_docstring(func.__doc__)
    return desc
