from dataclasses import dataclass, field


@dataclass
class ParsedElement:
    type: str             # heading|paragraph|table|image_description|image_ocr|list_item|code
    content: str
    page_num: int | None
    level: int | None     # heading 级别 1-6
    bbox: list | None     # [x1,y1,x2,y2]，PDF 溯源用
    metadata: dict = field(default_factory=dict)
    group_id: str | None = None  # 同一视觉单元共享 group_id（一张图/一个表）
