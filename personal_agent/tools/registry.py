from typing import List
from .types import ToolDef
from .exec_tool import build_exec_tool
from .read_file_tool import build_read_file_tool
from .web_fetch_tool import build_web_fetch_tool
from .web_search_tool import build_web_search_tool
from .playwright_mcp_tool import build_playwright_tools  # 1. 引入我们的 MCP 桥接工厂
from .knowledge_tool import build_knowledge_search_tool

def get_all_tools() -> List[ToolDef]:
    """
    获取系统中所有可被 LLM 调用的核心工具列表。
    添加新工具后，在此处直接补入注册列即可。
    """
    return [
        build_read_file_tool(),
        build_web_fetch_tool(),
        build_web_search_tool(),
        build_exec_tool(),
        *build_playwright_tools(),  # 2. 解包批量挂载 Playwright MCP 提供的所有工具
        build_knowledge_search_tool(),
    ]

def get_tools_openai_schema() -> List[dict]:
    """
    将工具集合转换为 OpenAI `tools` 参数所需要的基础 Schema 列表。
    """
    schema_list = []
    for tool in get_all_tools():
        schema_list.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
        })
    return schema_list
