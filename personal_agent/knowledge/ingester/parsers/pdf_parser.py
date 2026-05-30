import json
import logging
from pathlib import Path

from .types import ParsedElement

logger = logging.getLogger(__name__)

_OPENDATALOADER_TYPE_MAP = {
    "title": "heading",
    "heading": "heading",
    "section_header": "heading",
    "paragraph": "paragraph",
    "text": "paragraph",
    "table": "table",
    "figure": "image_description",
    "list_item": "list_item",
    "code": "code",
}

_UNSTRUCTURED_TYPE_MAP = {
    "Title": "heading",
    "Header": "heading",
    "NarrativeText": "paragraph",
    "Text": "paragraph",
    "Table": "table",
    "ListItem": "list_item",
    "Image": "image_description",
    "FigureCaption": "paragraph",
    "CodeSnippet": "code",
    "PageBreak": None,
    "PageNumber": None,
    "Footer": None,
}


def _from_opendataloader(data: dict) -> list[ParsedElement]:
    elements = []
    for kid in data.get("kids", []):
        raw_type = str(kid.get("type", "paragraph")).lower()
        elem_type = _OPENDATALOADER_TYPE_MAP.get(raw_type, "paragraph")
        content = str(kid.get("content", "")).strip()
        if not content:
            continue
        page_raw = kid.get("page_number") or kid.get("page_num") or kid.get("page")
        level_raw = kid.get("heading_level") or kid.get("level")
        bbox_raw = kid.get("bounding_box") or kid.get("bbox")
        elements.append(ParsedElement(
            type=elem_type,
            content=content,
            page_num=int(page_raw) if page_raw is not None else None,
            level=int(level_raw) if level_raw else None,
            bbox=list(bbox_raw) if bbox_raw else None,
        ))
    return elements


def _from_unstructured(path: Path) -> list[ParsedElement]:
    from unstructured.partition.pdf import partition_pdf
    raw = partition_pdf(
        filename=str(path),
        strategy="hi_res",
        languages=["chi_sim", "eng"],
    )
    elements = []
    for elem in raw:
        text = str(elem).strip()
        if not text:
            continue
        cat = getattr(elem, "category", "Text")
        elem_type = _UNSTRUCTURED_TYPE_MAP.get(cat, "paragraph")
        if elem_type is None:
            continue
        page_num = None
        if hasattr(elem, "metadata") and hasattr(elem.metadata, "page_number"):
            page_num = elem.metadata.page_number
        elements.append(ParsedElement(
            type=elem_type,
            content=text,
            page_num=page_num,
            level=1 if elem_type == "heading" else None,
            bbox=None,
        ))
    return elements


def parse_pdf(path: Path, **kwargs) -> list[ParsedElement]:
    try:
        import opendataloader_pdf
        result = opendataloader_pdf.convert(
            str(path),
            format=["json"],
            reading_order="xycut",
            table_method="default",
            hybrid="docling-fast",
        )
        if isinstance(result, str):
            data = json.loads(result)
        elif hasattr(result, "json") and callable(result.json):
            data = result.json()
        elif isinstance(result, dict):
            data = result
        else:
            data = {"kids": []}
        elements = _from_opendataloader(data)
        if elements:
            return elements
        raise ValueError("opendataloader_pdf returned no elements")
    except Exception as e:
        logger.warning("opendataloader_pdf failed (%s), trying unstructured hi_res", e)

    try:
        return _from_unstructured(path)
    except Exception as e:
        logger.error("unstructured fallback also failed for %s: %s", path.name, e)
        return []
