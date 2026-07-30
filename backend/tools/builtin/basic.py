"""内置零依赖工具：get_time / echo。

用于跑通「注册→LLM决定调用→执行→回喂→流式答」闭环，不依赖任何外网。
"""
from datetime import datetime

from ..registry import tool
from ..base import ToolCategory, Permission


@tool(
    name="get_time",
    description="获取当前的日期和时间。当用户询问现在几点、今天日期、星期几时使用。",
    category=ToolCategory.SYSTEM,
    permission=Permission.READ,
    parameters={
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "时区名（可选），如 'Asia/Shanghai'。默认使用服务器本地时间。",
            }
        },
        "required": [],
    },
)
async def get_time(timezone: str = ""):
    now = datetime.now()
    weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]
    return f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{weekday}）"


@tool(
    name="echo",
    description="原样返回传入的文本，用于测试工具调用链路是否连通。",
    category=ToolCategory.SYSTEM,
    permission=Permission.READ,
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要回显的文本"},
        },
        "required": ["text"],
    },
)
async def echo(text: str):
    return f"echo: {text}"
