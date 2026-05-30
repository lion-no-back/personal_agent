import logging
from pathlib import Path

from .types import ParsedElement

logger = logging.getLogger(__name__)


def parse_image(path: Path, llm_client=None) -> list[ParsedElement]:
    elements: list[ParsedElement] = []

    # Strategy 1: semantic description via markitdown (LLM-driven when client provided)
    try:
        from markitdown import MarkItDown
        kwargs: dict = {}
        if llm_client is not None:
            kwargs["llm_client"] = llm_client
            kwargs["llm_model"] = "claude-haiku-4-5-20251001"
        md = MarkItDown(**kwargs).convert(str(path)).text_content.strip()
        if md:
            elements.append(ParsedElement(
                type="image_description",
                content=md,
                page_num=None,
                level=None,
                bbox=None,
            ))
    except Exception as e:
        logger.warning("markitdown image description failed (%s): %s", path.name, e)

    # Strategy 2: OCR via unstructured (independent of strategy 1)
    try:
        from unstructured.partition.image import partition_image
        raw = partition_image(
            filename=str(path),
            strategy="hi_res",
            languages=["chi_sim", "eng"],
        )
        for elem in raw:
            text = str(elem).strip()
            if text:
                elements.append(ParsedElement(
                    type="image_ocr",
                    content=text,
                    page_num=None,
                    level=None,
                    bbox=None,
                ))
    except Exception as e:
        logger.warning("unstructured OCR failed (%s): %s", path.name, e)

    return elements
