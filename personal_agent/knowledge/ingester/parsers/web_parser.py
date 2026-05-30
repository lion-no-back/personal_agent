import logging
from pathlib import Path

from .types import ParsedElement
from .text_parser import _markdown_to_elements

logger = logging.getLogger(__name__)


def parse_html(path: Path, **kwargs) -> list[ParsedElement]:
    try:
        from markitdown import MarkItDown
        md_text = MarkItDown().convert(str(path)).text_content
        return _markdown_to_elements(md_text)
    except Exception as e:
        logger.warning("markitdown HTML parse failed (%s): %s", path.name, e)
        return []
