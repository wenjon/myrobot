"""工具框架 · 注册表与执行分发。

ToolRegistry 负责：
- 注册工具（类实例）并防重名；
- 按分类/权限筛选，导出 OpenAI 风格 tools 清单给 LLM；
- 统一执行：参数校验 + 超时 + 权限闸门 + 异常隔离 + 日志。

同时提供 @tool 装饰器，让「简单函数」也能快速注册成工具，
无需写完整的类。name / description / parameters 三项均可省略，
省略时由 schema.py 从函数签名与 docstring 自动推导（详见该模块）。
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Dict, List, Optional, Sequence

from .base import Tool, ToolCategory, ToolContext, ToolResult, Permission
from .schema import schema_from_function, describe_from_function


class _FunctionTool(Tool):
    """把一个 async 函数包装成 Tool（供 @tool 装饰器使用）。"""

    def __init__(self, func: Callable, *, name: str, description: str,
                 category: ToolCategory, permission: Permission,
                 parameters: Dict[str, Any], timeout_s: float):
        self.name = name
        self.description = description
        self.category = category
        self.permission = permission
        self.parameters = parameters
        self.timeout_s = timeout_s
        self._func = func
        # 函数是否声明了 ctx 参数？注册时反射一次并缓存，
        # 避免每次调用工具都做一次 inspect.signature。
        self._wants_ctx = "ctx" in inspect.signature(func).parameters
        super().__init__()

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 函数可选择是否接收 ctx：声明了就传，没声明就不传。
        if self._wants_ctx:
            result = self._func(**args, ctx=ctx)
        else:
            result = self._func(**args)
        if inspect.isawaitable(result):
            result = await result
        # 允许函数直接返回字符串或 ToolResult
        if isinstance(result, ToolResult):
            return result
        return ToolResult.success(str(result))


class ToolRegistry:
    """全局工具注册表。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        self._log: Optional[Callable[[str], None]] = None

    def set_logger(self, log_fn: Callable[[str], None]) -> None:
        """注入日志函数（复用 server 的 _emit，统一控制台/文件输出）。"""
        self._log = log_fn

    def _emit(self, line: str) -> None:
        if self._log:
            self._log(line)

    # ---- 注册 ----
    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"工具重名: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def tool(self, *, name: Optional[str] = None, description: Optional[str] = None,
             category: ToolCategory = ToolCategory.SYSTEM,
             permission: Permission = Permission.READ,
             parameters: Optional[Dict[str, Any]] = None,
             timeout_s: float = 15.0):
        """装饰器：把 async/sync 函数注册成工具。

        name / description / parameters 三项都是**可选**的，省略时自动推导：
        - name        <- 函数名
        - description <- docstring 首段
        - parameters  <- 函数签名的类型注解 + docstring 的 Args: 段

        两种写法长期共存：简单工具靠推导少写十几行样板；
        遇到嵌套对象 / enum / 复杂数组等推不出的结构，显式传 parameters= 覆盖。

        推导失败（如忘写 docstring 导致 description 为空）会直接报错，
        因为空描述的工具 LLM 根本不知道何时该调，属于必须在启动时暴露的错误。
        """
        def deco(func: Callable) -> Callable:
            final_name = name or func.__name__
            final_desc = description if description is not None else describe_from_function(func)
            if not final_desc:
                raise ValueError(
                    f"工具 {final_name} 缺少描述：请写 docstring 首段，"
                    f"或给 @tool 传 description=。"
                )
            final_params = parameters if parameters is not None else schema_from_function(func)
            self.register(_FunctionTool(
                func, name=final_name, description=final_desc, category=category,
                permission=permission, parameters=final_params,
                timeout_s=timeout_s,
            ))
            return func
        return deco

    # ---- 查询/导出 ----
    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def schemas(self, *, categories: Optional[Sequence[ToolCategory]] = None,
                max_permission: Permission = Permission.DANGEROUS) -> List[Dict[str, Any]]:
        """导出可暴露给 LLM 的 tools 清单。

        - categories：只导出指定分类（None=全部）；
        - max_permission：只导出不超过该权限级别的工具（默认全放开）。
        """
        out = []
        for t in self._tools.values():
            if categories is not None and t.category not in categories:
                continue
            if t.permission.level > max_permission.level:
                continue
            out.append(t.to_openai_schema())
        return out

    # ---- 执行 ----
    async def execute(self, name: str, args: Dict[str, Any], ctx: ToolContext,
                      *, max_permission: Permission = Permission.DANGEROUS) -> ToolResult:
        """执行一个工具：权限闸门 + 超时 + 异常隔离，全程日志。"""
        tool = self._tools.get(name)
        who = f"#{ctx.conn_id}" if ctx.conn_id else ""
        if tool is None:
            self._emit(f"[工具 {who}] 未知工具: {name}")
            return ToolResult.failure(f"未知工具: {name}")

        # 权限闸门：超过允许级别直接拒绝（如默认不放开 hardware 写操作）
        if tool.permission.level > max_permission.level:
            self._emit(f"[工具 {who}] 拒绝执行 {name}（权限 {tool.permission.value} 超过上限 {max_permission.value}）")
            return ToolResult.failure(f"工具 {name} 需要更高权限，已被安全策略拦截")

        self._emit(f"[工具 {who}] 调用 {name} 参数={args}")
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        try:
            result = await asyncio.wait_for(tool.run(args, ctx), timeout=tool.timeout_s)
        except asyncio.TimeoutError:
            self._emit(f"[工具 {who}] {name} 超时(> {tool.timeout_s}s)")
            return ToolResult.failure(f"工具 {name} 执行超时")
        except Exception as e:  # noqa: BLE001
            self._emit(f"[工具 {who}] {name} 异常: {e}")
            return ToolResult.failure(f"{type(e).__name__}: {e}")
        dt = (loop.time() - t0) * 1000
        preview = (result.content or "")[:120].replace("\n", " ")
        self._emit(f"[工具 {who}] {name} 完成 ok={result.ok} 耗时={dt:.0f}ms 结果={preview}")
        return result

    async def teardown_all(self) -> None:
        for t in self._tools.values():
            try:
                await t.teardown()
            except Exception:
                pass


# 全局单例：整个进程共用一个注册表。
REGISTRY = ToolRegistry()
tool = REGISTRY.tool  # 顶层便捷装饰器
