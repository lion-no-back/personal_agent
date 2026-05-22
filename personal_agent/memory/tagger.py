"""
确定性标签抽取：从文本中提取结构化标签，零 LLM 成本。
用于记忆写入时打标，为未来向量检索做预筛选基础。
"""

import re

_EXT_PATTERN = re.compile(r"\.(py|sh|xlsx|xls|csv|txt|pdf|json|yaml|yml|md)\b", re.I)

_TOOL_TAGS: dict[str, str] = {
    "exec":      "tool:exec",
    "web_fetch": "tool:web_fetch",
    "read_file": "tool:read_file",
}

_KEYWORD_TAGS: dict[str, str] = {
    "导出":  "action:export",
    "下载":  "action:download",
    "执行":  "action:exec",
    "安装":  "action:install",
    "部署":  "action:deploy",
    "路径":  "topic:path",
    "目录":  "topic:directory",
    "依赖":  "topic:dependency",
    "技能":  "topic:skill",
    "马帮":  "system:maban",
    "飞书":  "platform:feishu",
    "后台":  "mode:background",
    "前台":  "mode:foreground",
    "报告":  "report:general",
    "周报":  "report:weekly",
    "月报":  "report:monthly",
    "会话":  "topic:session",
    "清空":  "action:clear",
    "重启":  "action:restart",
}


def extract_tags(text: str) -> list[str]:
    """从文本提取确定性标签列表，结果已去重并排序。"""
    tags: set[str] = set()

    for tool, tag in _TOOL_TAGS.items():
        if tool in text:
            tags.add(tag)

    for ext in _EXT_PATTERN.findall(text):
        tags.add(f"ext:{ext.lower()}")

    for kw, tag in _KEYWORD_TAGS.items():
        if kw in text:
            tags.add(tag)

    return sorted(tags)
