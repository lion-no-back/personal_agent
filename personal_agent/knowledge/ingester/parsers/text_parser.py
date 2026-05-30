import re
from pathlib import Path

from .types import ParsedElement


def parse_text(path: Path, **kwargs) -> list[ParsedElement]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".md":
        return _markdown_to_elements(text)
    return _plain_to_elements(text)


def _plain_to_elements(text: str) -> list[ParsedElement]:
    elements = []
    for para in re.split(r"\n\s*\n", text):
        content = para.strip()
        if content:
            elements.append(ParsedElement(
                type="paragraph", content=content,
                page_num=None, level=None, bbox=None,
            ))
    return elements


def _markdown_to_elements(text: str) -> list[ParsedElement]:
    elements: list[ParsedElement] = []
    lines = text.splitlines()
    in_code = False
    code_buf: list[str] = []
    table_buf: list[str] = []
    para_buf: list[str] = []

    def flush_para() -> None:
        content = "\n".join(para_buf).strip()
        para_buf.clear()
        if content:
            elements.append(ParsedElement(
                type="paragraph", content=content,
                page_num=None, level=None, bbox=None,
            ))

    def flush_table() -> None:
        content = "\n".join(table_buf).strip()
        table_buf.clear()
        if content:
            elements.append(ParsedElement(
                type="table", content=content,
                page_num=None, level=None, bbox=None,
            ))

    def flush_code() -> None:
        content = "\n".join(code_buf).strip()
        code_buf.clear()
        if content:
            elements.append(ParsedElement(
                type="code", content=content,
                page_num=None, level=None, bbox=None,
            ))

    for line in lines:
        if re.match(r"^```", line):
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_para()
                flush_table()
                in_code = True
            continue

        if in_code:
            code_buf.append(line)
            continue

        if line.startswith("|"):
            # skip separator rows like |---|---|
            if re.match(r"^\|[\s\-:|]+\|", line):
                continue
            flush_para()
            table_buf.append(line)
            continue
        elif table_buf:
            flush_table()

        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush_para()
            elements.append(ParsedElement(
                type="heading", content=m.group(2).strip(),
                page_num=None, level=len(m.group(1)), bbox=None,
            ))
            continue

        m = re.match(r"^[ \t]*[-*+]\s+(.*)", line)
        if m:
            flush_para()
            elements.append(ParsedElement(
                type="list_item", content=m.group(1).strip(),
                page_num=None, level=None, bbox=None,
            ))
            continue

        m = re.match(r"^[ \t]*\d+\.\s+(.*)", line)
        if m:
            flush_para()
            elements.append(ParsedElement(
                type="list_item", content=m.group(1).strip(),
                page_num=None, level=None, bbox=None,
            ))
            continue

        if not line.strip():
            flush_para()
            continue

        para_buf.append(line)

    flush_para()
    flush_table()
    if in_code and code_buf:
        flush_code()

    return elements
