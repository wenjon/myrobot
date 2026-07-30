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
    description=(
        "仅用于联调测试工具调用链路是否连通，返回会在文本前加 echo: 前缀。"
        # 不要用它向用户输出东西，该工具不会在前端发声；
        # 它只是调试用：检查请求 LLM 调工具 -> 后端执行 -> 结果回馈是否打通。
    ),
    category=ToolCategory.SYSTEM,
    # dangerous：默认 read 权限下不会暴露给 LLM。避免 LLM 把它当作“说话工具”调用。
    # 如需联调测试，手动设 TOOL_MAX_PERMISSION=dangerous 即可。
    permission=Permission.DANGEROUS,
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
