import os
from .types import ToolDef


def build_read_file_tool() -> ToolDef:
    """构建读取文件内容的工具"""

    def read_file_call(file_path: str, **kwargs) -> str:
        try:
            if not os.path.isabs(file_path):
                # 假设基于当前工作目录，这里你也可以从环境中读取固定项目路径
                file_path = os.path.abspath(file_path)

            if not os.path.exists(file_path):
                return f"Error: 文件未找到，路径不存在 '{file_path}'"

            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 对于超大文件做一定的截断（例如最多返回前面的 8200 字符），防止撑爆上下文 Token
            # 不过根据你需要，可以通过截取或者返回特定行来实现。目前简单返回全部。
            if len(content) > 10000:
                summary = content[:10000] + f"\n\n...(省略后 {len(content)-10000} 字符，因文件过大)..."
                return f"文件 '{file_path}' 内容读取成功:\n\n{summary}"

            return f"文件 '{file_path}' 内容读取成功:\n\n{content}"

        except Exception as e:
            # 捕获所有异常并作为字符串抛回给 LLM
            return f"Error: 读取文件时发生异常 '{str(e)}'"

    return ToolDef(
        name="read_file",
        description="读取指定非二进制文件的内容文本。用于分析和理解本地文件（如 .md, .py 源代码等）。",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要读取的文件的路径。可以是绝对路径，也可以是相对于项目根目录的相对路径。"
                }
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
        call=read_file_call,
    )
