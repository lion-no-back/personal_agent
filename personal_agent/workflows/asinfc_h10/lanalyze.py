from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

SYSTEM_PROMPT = "你是一个严谨的亚马逊关键词分析助手，只基于用户提供的 CSV 内容做分析。"

ANALYSIS_PROMPT = """这是根据亚马逊产品aisn反查出来的关键词，请根据以下条件对表格关键词做一个词频分析
1、人群特征（男性、女性）
2、手表的型号ultra 11 10 9 8 7 6 5 4 手表49 46 45 44 42 41 40等
3、仅列出属性词本身（如：尼龙/编织、硅胶、金属、弹性/拉伸、运动、磁吸、透气、防水、舒适、睡眠等），标注每个属性词的出现频次，仅展示**频次排名前5**的属性词，以及**频次排名最后1位**的属性词

最后基于表格内关键词做一个简要分析和总结。

请严格用 Markdown 返回，不要返回 JSON，不要添加代码块。按以下结构输出：
# 关键词分析结果

## 人群特征
- 男性：频次
- 女性：频次

## 手表型号与尺寸
- 型号/尺寸：频次

## 属性词频次
### 频次排名前5
| 排名 | 属性词 | 频次 |
| --- | --- | --- |
属性词列必须使用“中文属性词 (english terms)”格式，例如：
| 1 | 尼龙/编织 (braided、woven、nylon) | 355 |
| 2 | 运动 (sport、athletic) | 276 |
| 3 | 弹性/拉伸 (stretch、elastic、stretchy) | 218 |

### 频次排名最后2位
| 属性词 | 频次 |
| --- | --- |

## 属性词频次前三
必须复用“频次排名前5”表格中属性词列的完整文本，保留英文半角括号：
1. 尼龙/编织 (braided、woven、nylon): 355
2. 运动 (sport、athletic): 276
3. 弹性/拉伸 (stretch、elastic、stretchy): 218

## 分析总结
最后基于表格内关键词做一个简要分析和总结。
分析总结段落中，如果提到从 CSV 中提取的关键词词根，必须只使用 CSV 原文里实际出现过的原语言单词，例如使用 nylon、stretch、sleep 等原词；不要在分析总结段落中使用“尼龙”“拉伸”“睡眠”等中文属性词来代替这些词根。
如果某个词根没有在 CSV 原文中出现，不要把它写进分析总结。"""

ATTRIBUTE_ENGLISH_ALIASES = [
    (("尼龙/编织", "编织/尼龙", "尼龙", "编织"), "braided、woven、nylon"),
    (("弹性/拉伸", "拉伸/弹性", "弹性", "拉伸"), "stretch、elastic、stretchy"),
    (("硅胶/硅", "硅/硅胶", "硅胶", "硅胶/硅胶", "硅"), "silicone"),
    (("运动",), "sport、athletic"),
    (("磁吸", "磁性", "磁力"), "magnetic"),
    (("防水",), "waterproof"),
    (("透气",), "breathable"),
    (("金属",), "metal"),
    (("睡眠",), "sleep/sleeping"),
    (("舒适",), "comfortable"),
]


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f".env 文件不存在: {path}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def env_value(env: dict[str, str], key: str) -> str:
    value = os.environ.get(key) or env.get(key)
    if not value:
        raise ValueError(f".env 中缺少必要配置项: {key}")
    return value.strip()


def read_csv_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"CSV 文件不存在: {path}")
    if not path.is_file():
        raise ValueError(f"输入路径不是文件: {path}")
    return path.read_text(encoding="utf-8-sig")


def csv_stats(csv_text: str) -> tuple[int, list[str]]:
    reader = csv.reader(csv_text.splitlines())
    rows = list(reader)
    if not rows:
        return 0, []
    return max(len(rows) - 1, 0), rows[0]


def build_user_prompt(csv_text: str) -> str:
    return f"""{ANALYSIS_PROMPT}

以下是 CSV 原文：

```csv
{csv_text}
```"""


def call_llm(
    *,
    api_key: str,
    base_url: str,
    model: str,
    csv_text: str,
    timeout: int,
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(csv_text)},
        ],
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise RuntimeError(f"LLM API 请求失败: {response.status_code} {response.text}") from error

    data = response.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"LLM 响应结构异常: {data}") from error


def strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:markdown|md)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped


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


def ensure_attribute_english(term: str) -> str:
    if re.search(r"\([^)]*[A-Za-z][^)]*\)", term):
        return term
    compact_term = re.sub(r"\s+", "", term)
    for aliases, english_terms in ATTRIBUTE_ENGLISH_ALIASES:
        if any(alias in compact_term for alias in aliases):
            return f"{term} ({english_terms})"
    return term


def top3_from_top5_table(markdown_text: str) -> list[tuple[str, str]]:
    section = markdown_subsection(markdown_text, "频次排名前5")
    rows = markdown_table_rows(section)
    top3: list[tuple[str, str]] = []
    for row in rows[:3]:
        if len(row) >= 3:
            top3.append((ensure_attribute_english(row[1]), row[2]))
    return top3


def print_markdown_top3(markdown_text: str) -> None:
    top3 = top3_from_top5_table(markdown_text)
    if top3:
        print("\n属性词频次前三：")
        for index, (term, count) in enumerate(top3, start=1):
            print(f"{index}. {term}: {count}")
        return

    section = markdown_section(markdown_text, "属性词频次前三")
    print("\n属性词频次前三：")
    if not section:
        print("请查看 Markdown 输出文件中的“属性词频次前三”章节。")
        return
    for line in section.splitlines():
        line = line.strip()
        if line:
            print(line)


@dataclass(frozen=True)
class TextRun:
    text: str
    bold: bool = False


@dataclass(frozen=True)
class RenderLine:
    runs: tuple[TextRun, ...] = ()
    indent: int = 0
    spacing_before: int = 0


def markdown_inline_runs(text: str) -> list[TextRun]:
    parts = re.split(r"(\*\*)", text)
    runs: list[TextRun] = []
    bold = False
    for part in parts:
        if part == "**":
            bold = not bold
            continue
        if part:
            runs.append(TextRun(part, bold))
    return runs


def split_text_units(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_./+%-]+|\s+|.", text)


def text_width(draw: Any, text: str, font: Any) -> int:
    if not text:
        return 0
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return int(right - left)


def runs_width(draw: Any, runs: Iterable[TextRun], normal_font: Any, bold_font: Any) -> int:
    return sum(text_width(draw, run.text, bold_font if run.bold else normal_font) for run in runs)


def wrap_runs(
    *,
    runs: list[TextRun],
    draw: Any,
    normal_font: Any,
    bold_font: Any,
    max_width: int,
    indent: int = 0,
    continuation_indent: int = 0,
) -> list[RenderLine]:
    lines: list[RenderLine] = []
    current_runs: list[TextRun] = []
    current_width = 0
    current_indent = indent
    available_width = max_width - current_indent

    for run in runs:
        font = bold_font if run.bold else normal_font
        for unit in split_text_units(run.text):
            if not current_runs and unit.isspace():
                continue
            unit_width = text_width(draw, unit, font)
            if current_runs and current_width + unit_width > available_width:
                lines.append(RenderLine(tuple(current_runs), current_indent))
                current_runs = []
                current_width = 0
                current_indent = continuation_indent
                available_width = max_width - current_indent
                if unit.isspace():
                    continue
            current_runs.append(TextRun(unit, run.bold))
            current_width += unit_width

    if current_runs:
        lines.append(RenderLine(tuple(current_runs), current_indent))
    return lines


def choose_font_path(candidates: Iterable[str]) -> Path | None:
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def load_report_fonts(body_size: int, title_size: int) -> tuple[Any, Any, Any]:
    from PIL import ImageFont

    normal_path = choose_font_path(
        [
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\simsun.ttc",
            r"C:\Windows\Fonts\arial.ttf",
        ]
    )
    bold_path = choose_font_path(
        [
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simsun.ttc",
        ]
    )
    if normal_path is None:
        raise RuntimeError("未找到可用字体，无法生成中文 PNG。")
    if bold_path is None:
        bold_path = normal_path
    return (
        ImageFont.truetype(str(normal_path), body_size),
        ImageFont.truetype(str(bold_path), body_size),
        ImageFont.truetype(str(bold_path), title_size),
    )


def layout_summary_lines(
    *,
    summary: str,
    draw: Any,
    normal_font: Any,
    bold_font: Any,
    max_text_width: int,
) -> list[RenderLine]:
    lines: list[RenderLine] = []
    for raw_line in summary.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            lines.append(RenderLine(spacing_before=30))
            continue

        list_match = re.match(r"^(\s*(?:[-*+]\s+|\d+[.)]\s+))(.*)$", line)
        if list_match:
            prefix, body = list_match.groups()
            prefix_runs = [TextRun(prefix)]
            body_runs = markdown_inline_runs(body)
            continuation_indent = runs_width(draw, prefix_runs, normal_font, bold_font)
            wrapped = wrap_runs(
                runs=prefix_runs + body_runs,
                draw=draw,
                normal_font=normal_font,
                bold_font=bold_font,
                max_width=max_text_width,
                continuation_indent=continuation_indent,
            )
        else:
            wrapped = wrap_runs(
                runs=markdown_inline_runs(line),
                draw=draw,
                normal_font=normal_font,
                bold_font=bold_font,
                max_width=max_text_width,
            )
        lines.extend(wrapped)
    return lines


def line_height(font: Any) -> int:
    top, bottom = font.getmetrics()
    return top + bottom


def page_output_path(base_path: Path, page_index: int, page_count: int) -> Path:
    if page_count == 1:
        return base_path
    return base_path.with_name(f"{base_path.stem}_{page_index:02d}{base_path.suffix}")


def split_lines_into_pages(
    *,
    lines: list[RenderLine],
    body_line_height: int,
    title_block_height: int,
    padding_y: int,
    max_page_height: int,
) -> list[list[RenderLine]]:
    pages: list[list[RenderLine]] = []
    current: list[RenderLine] = []
    current_height = title_block_height + padding_y
    limit = max_page_height - padding_y

    for line in lines:
        line_block_height = (body_line_height if line.runs else 0) + line.spacing_before
        if current and current_height + line_block_height > limit:
            pages.append(current)
            current = []
            current_height = title_block_height + padding_y
        current.append(line)
        current_height += line_block_height
    if current:
        pages.append(current)
    return pages or [[]]


def render_summary_png(summary: str, png_path: Path) -> list[Path]:
    from PIL import Image, ImageDraw

    width = 1600
    max_page_height = 2400
    padding_x = 110
    padding_y = 90
    body_size = 38
    title_size = 58
    body_color = (31, 41, 55)
    title_color = (17, 24, 39)
    background = (255, 255, 255)

    normal_font, bold_font, title_font = load_report_fonts(body_size=body_size, title_size=title_size)
    measuring_image = Image.new("RGB", (width, 200), background)
    measuring_draw = ImageDraw.Draw(measuring_image)

    max_text_width = width - padding_x * 2
    title_height = line_height(title_font)
    body_line_height = line_height(normal_font) + 18
    title_block_height = title_height + 62
    lines = layout_summary_lines(
        summary=summary,
        draw=measuring_draw,
        normal_font=normal_font,
        bold_font=bold_font,
        max_text_width=max_text_width,
    )
    pages = split_lines_into_pages(
        lines=lines,
        body_line_height=body_line_height,
        title_block_height=title_block_height,
        padding_y=padding_y,
        max_page_height=max_page_height,
    )

    output_paths: list[Path] = []
    for page_number, page_lines in enumerate(pages, start=1):
        content_height = title_block_height + padding_y
        for line in page_lines:
            content_height += (body_line_height if line.runs else 0) + line.spacing_before
        height = min(max(content_height, 720), max_page_height)
        image = Image.new("RGB", (width, height), background)
        draw = ImageDraw.Draw(image)

        title = "关键词分析总结" if len(pages) == 1 else f"关键词分析总结 ({page_number}/{len(pages)})"
        draw.text((padding_x, padding_y), title, font=title_font, fill=title_color)
        y = padding_y + title_block_height
        for line in page_lines:
            y += line.spacing_before
            if not line.runs:
                continue
            x = padding_x + line.indent
            for run in line.runs:
                font = bold_font if run.bold else normal_font
                draw.text((x, y), run.text, font=font, fill=body_color)
                x += text_width(draw, run.text, font)
            y += body_line_height

        output_path = page_output_path(png_path, page_number, len(pages))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path)
        output_paths.append(output_path)
    return output_paths


def screenshot_summary(summary: str, png_path: Path) -> list[Path]:
    try:
        return render_summary_png(summary, png_path)
    except Exception as error:
        print(f"生成分析总结 PNG 失败: {error}", file=sys.stderr)
        return []


def write_outputs(markdown_text: str, output_dir: Path) -> tuple[Path, list[Path]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = output_dir / f"llm_output_{stamp}.md"
    png_path = output_dir / f"analysis_summary_{stamp}.png"

    md_path.write_text(markdown_text, encoding="utf-8")

    summary = markdown_section(markdown_text, "分析总结")
    screenshot_paths: list[Path] = []
    if summary:
        screenshot_paths = screenshot_summary(summary, png_path)

    return md_path, screenshot_paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="发送 Cerebro CSV 到 LLM，并输出属性词前三和分析总结截图。")
    parser.add_argument("csv_path", type=Path, help="cerebro_keywords.py 下载生成的 CSV 文件路径")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV_PATH, help="包含 LLM 配置的 .env 路径")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="分析结果输出目录；默认与输入 CSV 放在同一目录",
    )
    parser.add_argument("--timeout", type=int, default=180, help="LLM 请求超时时间，单位秒")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        env = load_env(args.env)
        model = env_value(env, "LLM_WORKFLOW_MODEL_ID")
        api_key = env_value(env, "LLM_WORKFLOW_API_KEY")
        base_url = env_value(env, "LLM_WORKFLOW_BASE_URL")
        csv_text = read_csv_text(args.csv_path)
        print(f"LLM 模型: {model}")
        print("正在调用 LLM 分析...")

        llm_started = time.perf_counter()
        raw_text = call_llm(
            api_key=api_key,
            base_url=base_url,
            model=model,
            csv_text=csv_text,
            timeout=args.timeout,
        )
        print(f"[lanalyze] llm_call_seconds={time.perf_counter() - llm_started:.2f}", flush=True)
        markdown_text = strip_markdown_fences(raw_text)
        print_markdown_top3(markdown_text)
        output_started = time.perf_counter()
        output_dir = args.output_dir or args.csv_path.parent
        md_path, screenshot_paths = write_outputs(markdown_text, output_dir)
        print(f"[lanalyze] save_and_screenshot_seconds={time.perf_counter() - output_started:.2f}", flush=True)

        print(f"\nLLM Markdown 输出: {md_path}")
        print(f"Saved Markdown: {md_path}")
        if screenshot_paths:
            for screenshot_path in screenshot_paths:
                print(f"分析总结截图: {screenshot_path}")
        else:
            print("未生成分析总结截图。")
        return 0
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
