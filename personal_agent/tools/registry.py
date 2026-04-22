from typing import List
from .types import ToolDef
from .read_file_tool import build_read_file_tool


def get_all_tools() -> List[ToolDef]:
    """
    获取系统中所有可被 LLM 调用的核心工具列表。
    添加新工具后，在此处直接补入注册列即可。
    """
    return [
        build_read_file_tool(),
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
