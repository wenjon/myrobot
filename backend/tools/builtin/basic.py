"""内置零依赖工具：get_time / echo。

用于跑通「注册→LLM决定调用→执行→回喂→流式答」闭环，不依赖任何外网。

本文件同时作为「推导式工具写法」的样例：
@tool 不传 name/description/parameters，它们全部从函数名、docstring、
类型注解自动推导（见 tools/schema.py）。
"""
from datetime import datetime

from ..registry import tool
from ..base import ToolCategory, Permission


@tool(category=ToolCategory.SYSTEM, permission=Permission.READ)
async def get_time(timezone: str = "") -> str:
    """获取当前的日期和时间。当用户询问现在几点、今天日期、星期几时使用。

    Args:
        timezone: 时区名（可选），如 'Asia/Shanghai'。默认使用服务器本地时间。
    """
    now = datetime.now()
    weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]
    return f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{weekday}）"


# dangerous 权限：默认 read 上限下不会暂露给 LLM，
# 避免 LLM 把它当成「说话工具」调用（它不会在前端发声）。
# 需联调时手动设 TOOL_MAX_PERMISSION=dangerous 即可。
@tool(category=ToolCategory.SYSTEM, permission=Permission.DANGEROUS)
async def echo(text: str) -> str:
    """仅用于联调测试工具调用链路是否连通，返回会在文本前加 echo: 前缀。

    Args:
        text: 要回显的文本
    """
    return f"echo: {text}"
