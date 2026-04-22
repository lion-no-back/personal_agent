from dataclasses import dataclass
from typing import Any, Callable, Dict


@dataclass
class ToolDef:
    """
    定义一个供大语言模型（LLM）调用的工具结构。
    采用零继承的函数式设计，所有工具只需组装此对象即可。
    """
    name: str  # 工具名称，符合 OpenAI naming 规范（最好是英数字+下划线）
    description: str  # 工具的描述，模型将基于此决定是否需要使用工具
    parameters: Dict[str, Any]  # JSON Schema 格式的参数定义
    call: Callable[..., str]  # 实际执行的方法，接收通过 kwargs 传入的实参，并始终返回字符串（结果或错误信息）
