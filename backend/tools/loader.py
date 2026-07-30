"""工具框架 · 插件自动发现。

启动时扫描 tools/ 下的子包（builtin/web/document/database/hardware…），
逐个 import，触发其中的 @tool 装饰器或 register() 调用完成注册。

新增一类工具 = 新建一个 .py 文件放进对应子目录，无需改本文件。
"""
from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType
from typing import List


# 会被扫描的工具子包（按分类组织）。新增分类目录时在此登记即可。
_TOOL_PACKAGES = [
    "tools.builtin",
    "tools.web",
    # 未来: "tools.document", "tools.database", "tools.hardware"
]


def load_all(log_fn=None) -> List[str]:
    """导入所有工具模块，返回已加载模块名列表。"""
    loaded: List[str] = []
    for pkg_name in _TOOL_PACKAGES:
        try:
            pkg: ModuleType = importlib.import_module(pkg_name)
        except ModuleNotFoundError:
            continue
        # 遍历该包下的所有子模块并 import
        if hasattr(pkg, "__path__"):
            for mod in pkgutil.iter_modules(pkg.__path__):
                full = f"{pkg_name}.{mod.name}"
                try:
                    importlib.import_module(full)
                    loaded.append(full)
                except Exception as e:  # noqa: BLE001
                    if log_fn:
                        log_fn(f"[工具加载] 跳过 {full}: {e}")
    if log_fn:
        log_fn(f"[工具加载] 已加载模块: {loaded}")
    return loaded
