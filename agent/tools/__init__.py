from __future__ import annotations

from .base import ToolDeps
from .files import make_tools as make_file_tools
from .memory import make_tools as make_memory_tools
from .social import make_tools as make_social_tools
from .status import make_notify_tool, make_status_tool
from .web_fetch import default_fetch
from .web_fetch import make_tool as make_fetch_tool
from .web_search import default_search
from .web_search import make_tool as make_search_tool


def build_tools(deps: ToolDeps) -> list[object]:
    tools: list[object] = []
    tools.append(make_search_tool(deps))
    tools.append(make_fetch_tool(deps))
    tools.extend(make_file_tools(deps))
    tools.extend(make_memory_tools(deps))
    tools.append(make_notify_tool(deps))
    tools.append(make_status_tool(deps))
    tools.extend(make_social_tools(deps))
    return tools


__all__ = ["ToolDeps", "build_tools", "default_fetch", "default_search"]
