"""工具 schema 自检：验证推导结果与函数签名一致。

为什么需要它：
    工具的 parameters schema 是给 LLM 看的「调用说明书」，而真正执行时是把
    LLM 填的参数以 **kwargs 展开给 Python 函数。两边一旦不一致（schema 里叫
    query、函数参数叫 q），只会在用户真的触发那个工具时报 TypeError。
    这个脚本把这类错误提前到 CI / 提交前。

检查项（对注册表里的每个工具）：
    1. schema 声明的每个参数名，函数里都真存在（除非函数收 **kwargs）；
    2. 函数里没有默认值的必填参数，都在 schema 的 required 里；
    3. required 里的参数都在 properties 里有定义；
    4. 基本字段齐备：name / description 非空，parameters.type == "object"；
    5. 每个参数都有 description（只警告，不报错）。

用法：
    python scripts/check_tool_schemas.py
退出码 0 = 全部通过；1 = 有不一致。
"""
from __future__ import annotations

import inspect
import os
import sys

# 让脚本能直接 import backend/ 下的模块
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "backend"))

from tools import REGISTRY, load_all          # noqa: E402
from tools.registry import _FunctionTool      # noqa: E402
from typing import List, Tuple                # noqa: E402


# 框架注入的参数，不属于 LLM 可填参数（与 schema.py 保持一致）
_INJECTED = {"ctx", "self", "cls"}


def check_tool(tool) -> Tuple[List[str], List[str]]:
    """检查单个工具，返回 (errors, warnings)。"""
    errors: List[str] = []
    warns: List[str] = []
    name = tool.name

    # ---- 检查 4：基本字段 ----
    if not tool.description or not tool.description.strip():
        errors.append(f"{name}: description 为空（LLM 不知道何时该调用）")
    schema = tool.parameters or {}
    if schema.get("type") != "object":
        errors.append(f"{name}: parameters.type 应为 object，实际为 {schema.get('type')!r}")
    props = schema.get("properties") or {}
    required = schema.get("required") or []

    # ---- 检查 3：required 必须在 properties 里 ----
    for r in required:
        if r not in props:
            errors.append(f"{name}: required 里的 {r} 在 properties 里没定义")

    # ---- 检查 5：参数描述（警告） ----
    for pname, spec in props.items():
        if not (spec or {}).get("description"):
            warns.append(f"{name}.{pname}: 缺参数 description，LLM 容易填错")

    # ---- 与函数签名对比（仅 @tool 函数工具可取到原函数）----
    if not isinstance(tool, _FunctionTool):
        return errors, warns
    func = tool._func  # noqa: SLF001  自检脚本有意读内部字段
    sig = inspect.signature(func)
    accepts_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    func_params = {
        n: p for n, p in sig.parameters.items()
        if n not in _INJECTED
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }

    # ---- 检查 1：schema 参数必须在函数里存在 ----
    if not accepts_kwargs:
        for pname in props:
            if pname not in func_params:
                errors.append(
                    f"{name}: schema 声明了参数 {pname}，但函数 "
                    f"{func.__name__}{sig} 没有这个参数（调用时会报 TypeError）"
                )

    # ---- 检查 2：函数必填参数必须在 required 里 ----
    for pname, p in func_params.items():
        if p.default is inspect.Parameter.empty and pname not in required:
            errors.append(
                f"{name}: 函数参数 {pname} 无默认值（必填），"
                f"但不在 schema 的 required 里，LLM 可能不传导致 TypeError"
            )

    return errors, warns


def main() -> int:
    load_all()
    tools = REGISTRY.all()
    if not tools:
        print("错误：没有加载到任何工具，检查 tools/loader.py")
        return 1

    all_errors: List[str] = []
    all_warns: List[str] = []
    for t in sorted(tools, key=lambda x: x.name):
        errs, warns = check_tool(t)
        all_errors.extend(errs)
        all_warns.extend(warns)
        flag = "FAIL" if errs else "ok"
        nparams = len((t.parameters or {}).get("properties") or {})
        print(f"  [{flag}] {t.name:<14} category={t.category.value:<8} "
              f"permission={t.permission.value:<9} params={nparams}")

    print("")
    for w in all_warns:
        print(f"警告: {w}")
    if all_errors:
        print("")
        print(f"失败：发现 {len(all_errors)} 处 schema 与函数签名不一致：")
        for e in all_errors:
            print(f"  - {e}")
        return 1
    tail = f"（{len(all_warns)} 条警告）" if all_warns else ""
    print(f"OK：{len(tools)} 个工具的 schema 与函数签名均一致" + tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
