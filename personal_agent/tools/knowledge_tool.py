import logging

from .types import ToolDef

logger = logging.getLogger(__name__)


def build_knowledge_search_tool() -> ToolDef:
    def _call(query: str, top_k: int = 5, **kwargs) -> str:
        import personal_agent.knowledge as _kb_mod

        instance = _kb_mod.kb
        if instance is None:
            return "知识库未初始化，请联系管理员。"
        try:
            results = instance.search_sync(query, top_k=top_k)
        except Exception as e:
            logger.error("knowledge_search 失败: %s", e, exc_info=True)
            return f"检索出错：{e}"
        if not results:
            return "知识库中未找到相关内容。"
        parts = []
        for i, r in enumerate(results, 1):
            src = r.chunk.source_path
            page = f"，第{r.chunk.page_num}页" if r.chunk.page_num else ""
            body = r.chunk.compiled or r.chunk.content[:400]
            parts.append(f"**{i}. {r.chunk.title}** [来源: {src}{page}]\n{body}")
        return "\n\n---\n\n".join(parts)

    return ToolDef(
        name="knowledge_search",
        description=(
            "检索用户个人知识库中的文档内容（用户曾导入的任意主题文件：PDF/Word/TXT/图片等）。"
            "适用于用户明确要求查阅知识库，或需要补充检索更多相关文档时。"
            "不适用：实时数据（股价、天气）、知识库从未导入过相关内容时。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索问题，用自然语言描述"},
                "top_k": {"type": "integer", "default": 5, "description": "返回结果数"},
            },
            "required": ["query"],
        },
        call=_call,
    )
