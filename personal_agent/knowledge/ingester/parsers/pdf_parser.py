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
    "list": "list_item",
    "list item": "list_item",
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


def _extract_element(kid: dict) -> list[ParsedElement]:
    """Extract one or more ParsedElements from a single JSON node.

    opendataloader_pdf uses a nested structure for list elements:
      { "type": "list", "content": "", "list items": [ {"content": "..."}, ... ] }
    The top-level content is empty; the actual text lives in the child nodes.
    We expand those children into individual list_item elements so nothing is lost.
    """
    raw_type = str(kid.get("type", "paragraph")).lower()
    elem_type = _OPENDATALOADER_TYPE_MAP.get(raw_type, "paragraph")

    page_raw = kid.get("page number") or kid.get("page_number") or kid.get("page_num")
    level_raw = kid.get("heading level") or kid.get("heading_level")
    bbox_raw = kid.get("bounding box") or kid.get("bounding_box") or kid.get("bbox")
    try:
        level = int(level_raw) if level_raw is not None else None
    except (ValueError, TypeError):
        level = None
    try:
        page_num = int(page_raw) if page_raw is not None else None
    except (ValueError, TypeError):
        page_num = None

    # Expand nested list items (opendataloader_pdf list structure)
    list_items = kid.get("list items") or kid.get("list_items") or []
    if list_items:
        results = []
        for item in list_items:
            item_content = str(item.get("content", "")).strip()
            if not item_content:
                continue
            item_page_raw = item.get("page number") or item.get("page_number") or page_num
            try:
                item_page = int(item_page_raw) if item_page_raw is not None else page_num
            except (ValueError, TypeError):
                item_page = page_num
            item_bbox = item.get("bounding box") or item.get("bounding_box") or bbox_raw
            results.append(ParsedElement(
                type="list_item",
                content=item_content,
                page_num=item_page,
                level=level,
                bbox=list(item_bbox) if item_bbox else None,
            ))
        return results

    content = str(kid.get("content", "")).strip()
    if not content:
        return []
    return [ParsedElement(
        type=elem_type,
        content=content,
        page_num=page_num,
        level=level,
        bbox=list(bbox_raw) if bbox_raw else None,
    )]


def _from_opendataloader(data: dict) -> list[ParsedElement]:
    elements = []
    for kid in data.get("kids", []):
        elements.extend(_extract_element(kid))
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
    import os
    from ...java_bootstrap import ensure_java
    # Silence JUL console output — garbles on Windows and duplicates Python-level logging.
    _log_cfg = Path(__file__).parent.parent.parent / "java_logging.properties"
    os.environ.setdefault(
        "JAVA_TOOL_OPTIONS",
        f"-Djava.util.logging.config.file={_log_cfg} -Dfile.encoding=UTF-8",
    )
    ensure_java()
    try:
        import opendataloader_pdf
        path = Path(path)
        opendataloader_pdf.convert(
            str(path),
            format=["json"],
            reading_order="xycut",
            table_method="default",
        )
        # convert() writes {stem}.json alongside the input file and returns None
        json_out = path.with_suffix(".json")
        if not json_out.exists():
            raise ValueError("opendataloader_pdf produced no output file")
        data = json.loads(json_out.read_text(encoding="utf-8"))
        json_out.unlink()
        # opendataloader also writes a {stem}_images/ folder; delete it so the
        # file watcher does not treat the extracted images as new source files.
        import shutil
        images_dir = path.with_name(path.stem + "_images")
        if images_dir.is_dir():
            shutil.rmtree(images_dir)
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
