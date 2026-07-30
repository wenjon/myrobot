"""工具框架 · 核心抽象。

设计目标：让「工具怎么写」和「工具怎么被调度」彻底解耦。
新增一类工具（文档/数据库/硬件…）时，只写一个继承 Tool 的类并注册，
不需要改动对话链路（server.py / agent.py）。

三个核心概念：
- ToolCategory：工具分类（web/document/database/hardware/system），便于按需放开。
- Permission：权限级别（read/write/dangerous），用于安全闸门。
- Tool：所有工具的统一契约（name + description + JSON Schema + async run）。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional, Awaitable


class ToolCategory(str, Enum):
    """工具分类。用于按类别向 LLM 暴露/隐藏工具。"""
    WEB = "web"            # 联网检索、网页抓取
    DOCUMENT = "document"  # 解析 PDF/Word/Excel 等
    DATABASE = "database"  # 查询/写入数据库
    HARDWARE = "hardware"  # 舵机/表情/传感器等硬件 API
    SYSTEM = "system"      # 时间/回显等内置零依赖工具


class Permission(str, Enum):
    """权限级别，从低到高。执行前由注册表做闸门校验。"""
    READ = "read"            # 只读、无副作用，可自动执行
    WRITE = "write"          # 会改变外部状态，需策略允许
    DANGEROUS = "dangerous"  # 硬件动作/删除等，建议二次确认

    @property
    def level(self) -> int:
        return {"read": 0, "write": 1, "dangerous": 2}[self.value]


@dataclass
class ToolResult:
    """工具执行结果。content 是回喂给 LLM 的文本（务必是字符串）。"""
    ok: bool
    content: str
    data: Optional[dict] = None   # 结构化数据（供程序使用，可选）
    error: Optional[str] = None

    @staticmethod
    def success(content: str, data: Optional[dict] = None) -> "ToolResult":
        return ToolResult(ok=True, content=content, data=data)

    @staticmethod
    def failure(error: str) -> "ToolResult":
        return ToolResult(ok=False, content=f"[工具执行失败] {error}", error=error)


@dataclass
class ToolContext:
    """传给工具的运行时上下文。

    工具不自己建连接/句柄，而是通过 ctx 获取共享资源与信号，
    这样数据库连接池、HTTP client、串口等只初始化一次并被复用。
    """
    session_id: str = ""
    conn_id: str = ""
    cancel: Optional[asyncio.Event] = None          # 复用对话的打断信号
    emit_status: Optional[Callable[[str], Awaitable[None]]] = None  # 向前端推状态
    resources: Any = None                            # ResourceManager（见 resources.py）
    extra: Dict[str, Any] = field(default_factory=dict)

    async def status(self, text: str) -> None:
        """给前端发一条状态提示（如“正在搜索…”）。无回调时静默。"""
        if self.emit_status:
            try:
                await self.emit_status(text)
            except Exception:
                pass


class Tool(ABC):
    """所有工具的基类。

    子类需定义类属性：name / description / category / parameters(JSON Schema)，
    并实现 async run()。可选实现 setup()/teardown() 管理自身资源。
    """
    name: str = ""
    description: str = ""
    category: ToolCategory = ToolCategory.SYSTEM
    permission: Permission = Permission.READ
    parameters: Dict[str, Any] = {}   # OpenAI 风格 JSON Schema（type=object）
    timeout_s: float = 15.0

    def __init__(self) -> None:
        if not self.name:
            raise ValueError(f"{self.__class__.__name__} 缺少 name")
        if not isinstance(self.parameters, dict):
            raise ValueError(f"{self.name} 的 parameters 必须是 JSON Schema dict")

    async def setup(self, ctx: ToolContext) -> None:
        """可选：首次使用前初始化资源（默认空实现）。"""
        return None

    @abstractmethod
    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具。args 已按 schema 传入；返回 ToolResult。"""
        raise NotImplementedError

    async def teardown(self) -> None:
        """可选：服务关闭时释放资源（默认空实现）。"""
        return None

    def to_openai_schema(self) -> Dict[str, Any]:
        """导出为 OpenAI / Ark / 新版 Ollama 通用的 tools 元素。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }
