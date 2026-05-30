from pathlib import Path
from typing import Callable

from . import image_parser, office_parser, pdf_parser, text_parser, web_parser

PARSER_ROUTES: dict[str, Callable] = {
    ".pdf": pdf_parser.parse_pdf,
    ".docx": office_parser.parse_office,
    ".doc": office_parser.parse_office,
    ".pptx": office_parser.parse_office,
    ".ppt": office_parser.parse_office,
    ".xlsx": office_parser.parse_excel,
    ".xls": office_parser.parse_excel,
    ".csv": office_parser.parse_excel,
    ".jpg": image_parser.parse_image,
    ".jpeg": image_parser.parse_image,
    ".png": image_parser.parse_image,
    ".webp": image_parser.parse_image,
    ".gif": image_parser.parse_image,
    ".md": text_parser.parse_text,
    ".txt": text_parser.parse_text,
    ".html": web_parser.parse_html,
    ".htm": web_parser.parse_html,
}


def get_parser(path: Path) -> Callable | None:
    return PARSER_ROUTES.get(path.suffix.lower())
