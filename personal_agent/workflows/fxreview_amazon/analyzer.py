import os
import csv
from pathlib import Path


async def _call_llm(system_prompt: str, user_message: str) -> str:
    """调用 DeepSeek API（OpenAI 兼容接口），返回分析文本"""
    try:
        from openai import AsyncOpenAI

        api_key = os.getenv("LLM_REASONING_API_KEY")
        base_url = os.getenv("LLM_REASONING_BASE_URL")
        model_id = os.getenv("LLM_REASONING_MODEL_ID")

        if not api_key:
            raise ValueError("未在 .env 中配置 LLM_REASONING_API_KEY")

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
        )

        print(f"正在调用 {model_id} API 进行分析...", flush=True)

        response = await client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.7,
            max_tokens=10000
        )

        return response.choices[0].message.content

    except Exception as e:
        print(f"[LLM 分析失败] {e}")
        return f"分析失败: {str(e)}"


def _load_prompt(name: str) -> str:
    prompt_file = Path(__file__).parent / name
    with open(prompt_file, "r", encoding="utf-8") as f:
        return f.read()


async def analyze_reviews(asin: str, artifact_dir: Path, summary: dict | None = None) -> str:
    """
    分析采集到的评论数据，使用 DeepSeek API。
    summary：采集阶段抓到的评分概况（内存传入，不再经由 xlsx 文件）。
    返回分析结果文本。
    """
    system_prompt = _load_prompt("voc_prompt.md")

    summary_text = _format_summary(summary or {})

    # 读取 reviews.csv
    reviews_file = artifact_dir / "reviews.csv"
    reviews_text = _read_reviews(reviews_file)

    user_message = f"""请对以下数据进行 VoC 分析：

## 评分汇总（来自商品评分概况）
{summary_text}

## 评论明细 (reviews.csv)
{reviews_text}

请严格按照提示词中的规则和输出格式进行分析。"""

    return await _call_llm(system_prompt, user_message)


def _format_summary(summary: dict) -> str:
    """把采集阶段抓到的评分概况 dict 格式化成喂给 LLM 的文本"""
    if not summary:
        return "[无评分概况数据]"
    lines = ["Customer Reviews"]
    if summary.get("overall_rating"):
        lines.append(summary["overall_rating"])
    if summary.get("total_ratings"):
        lines.append(summary["total_ratings"])
    for label in ("5 star", "4 star", "3 star", "2 star", "1 star"):
        if label in summary:
            lines.append(f"{label}: {summary[label]}")
    return "\n".join(lines)


def _read_csv_rows(csv_file: Path) -> list[dict]:
    """读取评论 CSV，文件不存在或读取失败时返回空列表"""
    if not csv_file.exists():
        return []
    try:
        with open(csv_file, "r", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        print(f"[错误读取 {csv_file.name}] {e}")
        return []


def _format_review(idx: int, row: dict) -> str:
    return f"""
[评论 {idx}]
评分: {row.get('rating', '')}
标题: {row.get('title', '')}
作者: {row.get('author', '')}
日期: {row.get('date', '')}
地区: {row.get('location', '')}
配色: {row.get('variant', '')}
正文: {row.get('body', '')}
"""


def _read_reviews(reviews_file: Path) -> str:
    """读取 CSV reviews 数据，返回格式化的评论文本"""
    rows = _read_csv_rows(reviews_file)
    if not rows:
        return f"[错误读取 {reviews_file.name}] 文件不存在或为空"
    return "\n".join(_format_review(idx, row) for idx, row in enumerate(rows, 1))


def save_analysis(
    analysis_text: str,
    artifact_dir: Path,
    asin: str,
    filename: str = "analysis.md",
    title: str = "产品 VoC 分析报告",
) -> Path:
    """保存分析结果为 md 文件"""
    output_file = artifact_dir / filename

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(f"**产品 ASIN:** {asin}\n\n")
        f.write(analysis_text)

    return output_file
