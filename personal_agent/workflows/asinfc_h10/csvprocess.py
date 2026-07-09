from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

KEYWORD_COLUMN = "Keyword Phrase"
SEARCH_VOLUME_COLUMN = "Search Volume"
MIN_SEARCH_VOLUME = 50

APPLE_TERMS = ("apple", "iwatch")
BAND_TERMS = ("band", "bands", "strap", "wristband", "bracelet")

INVALID_XLSX_CHARS = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F]")


@dataclass(frozen=True)
class AttributeTerm:
    rank: int
    label: str
    aliases: tuple[str, ...]


def markdown_section(markdown_text: str, heading: str) -> str:
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    lines = markdown_text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if re.match(pattern, line.strip()):
            start = index + 1
            break
    if start is None:
        return ""

    end = len(lines)
    for index in range(start, len(lines)):
        if re.match(r"^##\s+", lines[index].strip()):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def markdown_subsection(markdown_text: str, heading: str) -> str:
    pattern = rf"^###\s+{re.escape(heading)}\s*$"
    lines = markdown_text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if re.match(pattern, line.strip()):
            start = index + 1
            break
    if start is None:
        return ""

    end = len(lines)
    for index in range(start, len(lines)):
        if re.match(r"^#{1,3}\s+", lines[index].strip()):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def markdown_table_rows(markdown_text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        rows.append(cells)
    if rows and any(cell in {"排名", "属性词", "频次"} for cell in rows[0]):
        rows = rows[1:]
    return rows


def strip_count_suffix(value: str) -> str:
    return re.sub(r"\s*[:：]\s*[\d,]+\s*$", "", value).strip()


def extract_top3_labels(markdown_text: str) -> list[str]:
    top5_section = markdown_subsection(markdown_text, "频次排名前5")
    rows = markdown_table_rows(top5_section)
    labels: list[str] = []
    for row in rows:
        if len(labels) >= 3:
            break
        if len(row) >= 3 and re.fullmatch(r"\d+", row[0].strip()):
            labels.append(row[1].strip())

    if labels:
        return labels

    top3_section = markdown_section(markdown_text, "属性词频次前三")
    for line in top3_section.splitlines():
        match = re.match(r"^\s*(?:\d+[.)]|[-*])\s*(.+?)\s*$", line)
        if not match:
            continue
        labels.append(strip_count_suffix(match.group(1)))
        if len(labels) >= 3:
            break
    return labels


def split_aliases(raw_alias_text: str) -> list[str]:
    normalized = re.sub(r"[、,，;；|]", "/", raw_alias_text)
    aliases: list[str] = []
    for part in normalized.split("/"):
        alias = re.sub(r"\s+", " ", part).strip().lower()
        if alias and re.search(r"[a-z0-9]", alias):
            aliases.append(alias)
    return aliases


def aliases_from_label(label: str) -> tuple[str, ...]:
    aliases: list[str] = []
    for group in re.findall(r"\(([^)]*[A-Za-z0-9][^)]*)\)", label):
        aliases.extend(split_aliases(group))
    if not aliases:
        aliases.extend(split_aliases(label))

    deduped: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        if alias not in seen:
            deduped.append(alias)
            seen.add(alias)
    return tuple(deduped)


def load_attribute_terms(markdown_path: Path) -> list[AttributeTerm]:
    markdown_text = markdown_path.read_text(encoding="utf-8-sig")
    labels = extract_top3_labels(markdown_text)
    terms: list[AttributeTerm] = []
    for rank, label in enumerate(labels[:3], start=1):
        aliases = aliases_from_label(label)
        if aliases:
            terms.append(AttributeTerm(rank=rank, label=label, aliases=aliases))
    if not terms:
        raise ValueError("未能从 LLM Markdown 中解析出属性词频次前三。")
    return terms


def contains_term(text: str, terms: Iterable[str]) -> bool:
    normalized = text.lower()
    for term in terms:
        escaped = re.escape(term.lower())
        if re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", normalized):
            return True
    return False


def parse_search_volume(value: str) -> int | None:
    stripped = str(value).strip().replace(",", "")
    if not stripped or stripped == "-":
        return None
    if re.fullmatch(r"\d+", stripped):
        return int(stripped)
    if not re.fullmatch(r"\d+\.0+", stripped):
        return None
    return int(float(stripped))


def matching_attribute_rank(keyword: str, attributes: list[AttributeTerm]) -> int | None:
    for attribute in attributes:
        if contains_term(keyword, attribute.aliases):
            return attribute.rank
    return None


def should_color_row(row: dict[str, str], attributes: list[AttributeTerm]) -> int | None:
    keyword = row.get(KEYWORD_COLUMN, "")
    search_volume = parse_search_volume(row.get(SEARCH_VOLUME_COLUMN, ""))
    if search_volume is None or search_volume <= MIN_SEARCH_VOLUME:
        return None
    if not contains_term(keyword, APPLE_TERMS):
        return None
    if not contains_term(keyword, BAND_TERMS):
        return None
    return matching_attribute_rank(keyword, attributes)


def safe_xlsx_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, str):
        return INVALID_XLSX_CHARS.sub("", value)
    return value


def column_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def xlsx_text(value: object) -> str:
    return html.escape(str(safe_xlsx_value(value)), quote=False)


def xlsx_cell(row_index: int, column_index: int, value: object, style_id: int = 0) -> str:
    reference = f"{column_letter(column_index)}{row_index}"
    style = f' s="{style_id}"' if style_id else ""
    text = xlsx_text(value)
    if text == "":
        return f'<c r="{reference}"{style}/>'
    return f'<c r="{reference}" t="inlineStr"{style}><is><t>{text}</t></is></c>'


def xlsx_row(row_index: int, cells: list[str]) -> str:
    return f'<row r="{row_index}">{"".join(cells)}</row>'


def build_sheet_xml(
    *,
    rows: list[dict[str, str]],
    fieldnames: list[str],
    color_by_row: dict[int, int],
) -> str:
    max_row = len(rows) + 1
    max_column = len(fieldnames)
    dimension = f"A1:{column_letter(max_column)}{max_row}"

    column_xml = []
    for column_index, fieldname in enumerate(fieldnames, start=1):
        max_width = len(fieldname)
        for row in rows[:200]:
            max_width = max(max_width, len(str(row.get(fieldname, ""))))
        width = min(max_width + 2, 60)
        column_xml.append(f'<col min="{column_index}" max="{column_index}" width="{width}" customWidth="1"/>')

    sheet_rows = [
        xlsx_row(
            1,
            [xlsx_cell(1, column_index, fieldname, 1) for column_index, fieldname in enumerate(fieldnames, start=1)],
        )
    ]
    rank_style_ids = {1: 2, 2: 3, 3: 4}
    for row_index, row in enumerate(rows, start=2):
        rank = color_by_row.get(row_index)
        first_column_style = rank_style_ids.get(rank or 0, 0)
        cells = []
        for column_index, fieldname in enumerate(fieldnames, start=1):
            style_id = first_column_style if column_index == 1 else 0
            cells.append(xlsx_cell(row_index, column_index, row.get(fieldname, ""), style_id))
        sheet_rows.append(xlsx_row(row_index, cells))

    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="{dimension}"/>
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>
      <selection pane="bottomLeft"/>
    </sheetView>
  </sheetViews>
  <cols>{"".join(column_xml)}</cols>
  <sheetData>{"".join(sheet_rows)}</sheetData>
  <autoFilter ref="{dimension}"/>
</worksheet>'''


def styles_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/></font>
    <font><b/><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/></font>
  </fonts>
  <fills count="6">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFD9EAF7"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFF0000"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF00B050"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFC000"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="5">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"/>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1"/>
    <xf numFmtId="0" fontId="0" fillId="5" borderId="0" xfId="0" applyFill="1"/>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
  <dxfs count="0"/>
  <tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>'''


def default_output_path(csv_path: Path) -> Path:
    return csv_path.with_name(f"{csv_path.stem}_processed.xlsx")


def write_xlsx(
    *,
    rows: list[dict[str, str]],
    fieldnames: list[str],
    output_path: Path,
    color_by_row: dict[int, int],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>''',
        )
        archive.writestr(
            "_rels/.rels",
            '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>''',
        )
        archive.writestr(
            "xl/workbook.xml",
            '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="processed" sheetId="1" r:id="rId1"/></sheets>
</workbook>''',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>''',
        )
        archive.writestr("xl/styles.xml", styles_xml())
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            build_sheet_xml(rows=rows, fieldnames=fieldnames, color_by_row=color_by_row),
        )
        archive.writestr(
            "docProps/core.xml",
            '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:dcterms="http://purl.org/dc/terms/"
  xmlns:dcmitype="http://purl.org/dc/dcmitype/"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>csvprocess.py</dc:creator>
  <cp:lastModifiedBy>csvprocess.py</cp:lastModifiedBy>
</cp:coreProperties>''',
        )
        archive.writestr(
            "docProps/app.xml",
            '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
  xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>csvprocess.py</Application>
</Properties>''',
        )


def process_csv(csv_path: Path, markdown_path: Path, output_path: Path) -> tuple[Path, dict[int, int], list[AttributeTerm]]:
    attributes = load_attribute_terms(markdown_path)
    with csv_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            raise ValueError("CSV 文件为空或缺少表头。")
        fieldnames = list(reader.fieldnames)
        missing_columns = [column for column in (KEYWORD_COLUMN, SEARCH_VOLUME_COLUMN) if column not in fieldnames]
        if missing_columns:
            raise ValueError(f"CSV 缺少必要列: {', '.join(missing_columns)}")
        rows = list(reader)

    color_by_row: dict[int, int] = {}
    counts_by_rank = {1: 0, 2: 0, 3: 0}
    for data_index, row in enumerate(rows, start=2):
        rank = should_color_row(row, attributes)
        if rank is None:
            continue
        color_by_row[data_index] = rank
        counts_by_rank[rank] += 1

    write_xlsx(rows=rows, fieldnames=fieldnames, output_path=output_path, color_by_row=color_by_row)
    return output_path, counts_by_rank, attributes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据 LLM 属性词前三标注 Cerebro CSV，并输出 .xlsx。")
    parser.add_argument("csv_path", type=Path, help="cerebro_keywords.py 输出的 CSV 路径")
    parser.add_argument("markdown_path", type=Path, help="lanalyze.py 输出的 LLM Markdown 路径")
    parser.add_argument("--output", type=Path, default=None, help="输出 .xlsx 路径，默认在 CSV 同目录生成 *_processed.xlsx")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        csv_path = args.csv_path.resolve()
        markdown_path = args.markdown_path.resolve()
        output_path = (args.output or default_output_path(csv_path)).resolve()
        result_path, counts_by_rank, attributes = process_csv(csv_path, markdown_path, output_path)
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print("Attribute terms:")
    for attribute in attributes:
        print(f"{attribute.rank}. {attribute.label} -> {', '.join(attribute.aliases)}")
    print(
        "Colored rows: "
        f"rank1={counts_by_rank[1]}, rank2={counts_by_rank[2]}, rank3={counts_by_rank[3]}"
    )
    print(f"Saved XLSX: {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
