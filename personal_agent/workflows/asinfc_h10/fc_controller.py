from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = Path(__file__).resolve().parent
ARTIFACT_ROOT = PROJECT_ROOT / "artifact"


def run_command_stream(command: list[str]) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output_parts: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        output_parts.append(line)
    return process.wait(), "".join(output_parts)


def parse_saved_csv(output: str) -> Path | None:
    for line in output.splitlines():
        match = re.search(r"Saved CSV:\s*(.+)$", line.strip())
        if match:
            return Path(match.group(1).strip())
    return None


def parse_saved_markdown(output: str) -> Path | None:
    for line in output.splitlines():
        stripped = line.strip()
        match = re.search(r"Saved Markdown:\s*(.+)$", stripped)
        if match:
            return Path(match.group(1).strip())
        match = re.search(r"LLM Markdown 输出:\s*(.+)$", stripped)
        if match:
            return Path(match.group(1).strip())
    return None


def latest_csv_after(started_at: datetime) -> Path | None:
    if not ARTIFACT_ROOT.exists():
        return None
    candidates = [
        path
        for path in ARTIFACT_ROOT.rglob("*.csv")
        if datetime.fromtimestamp(path.stat().st_mtime) >= started_at
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def latest_markdown_after(started_at: datetime) -> Path | None:
    if not ARTIFACT_ROOT.exists():
        return None
    candidates = [
        path
        for path in ARTIFACT_ROOT.rglob("llm_output_*.md")
        if datetime.fromtimestamp(path.stat().st_mtime) >= started_at
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="先运行 Cerebro 下载 CSV，再调用 lanalyze.py 做 LLM 分析，最后输出标注后的 XLSX。")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="用于运行子脚本的 Python 解释器，默认使用当前解释器",
    )
    parser.add_argument("--timeout", type=int, default=180, help="传给 lanalyze.py 的 LLM 请求超时时间")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = datetime.now()

    cerebro_script = SCRIPT_DIR / "cerebro_keywords.py"
    analyze_script = SCRIPT_DIR / "lanalyze.py"
    csvprocess_script = SCRIPT_DIR / "csvprocess.py"

    print("正在运行 cerebro_keywords.py 下载 CSV...")
    cerebro_returncode, cerebro_output = run_command_stream([args.python, "-u", str(cerebro_script)])
    if cerebro_returncode != 0:
        print(f"cerebro_keywords.py 执行失败，退出码: {cerebro_returncode}", file=sys.stderr)
        return cerebro_returncode

    csv_path = parse_saved_csv(cerebro_output)
    if csv_path is None:
        csv_path = latest_csv_after(started_at)
    if csv_path is None:
        print("未找到 cerebro_keywords.py 生成的 CSV 路径。", file=sys.stderr)
        return 1
    if not csv_path.is_absolute():
        csv_path = (PROJECT_ROOT / csv_path).resolve()

    print(f"\n将 CSV 交给 lanalyze.py: {csv_path}")
    analyze_started_at = datetime.now()
    analyze_returncode, analyze_output = run_command_stream(
        [args.python, "-u", str(analyze_script), str(csv_path), "--timeout", str(args.timeout)]
    )
    if analyze_returncode != 0:
        print(f"lanalyze.py 执行失败，退出码: {analyze_returncode}", file=sys.stderr)
        return analyze_returncode

    markdown_path = parse_saved_markdown(analyze_output)
    if markdown_path is None:
        markdown_path = latest_markdown_after(analyze_started_at)
    if markdown_path is None:
        print("未找到 lanalyze.py 生成的 LLM Markdown 路径。", file=sys.stderr)
        return 1
    if not markdown_path.is_absolute():
        markdown_path = (PROJECT_ROOT / markdown_path).resolve()

    print(f"\n将 CSV 和 LLM Markdown 交给 csvprocess.py: {csv_path} | {markdown_path}")
    csvprocess_returncode, _ = run_command_stream(
        [args.python, "-u", str(csvprocess_script), str(csv_path), str(markdown_path)]
    )
    if csvprocess_returncode != 0:
        print(f"csvprocess.py 执行失败，退出码: {csvprocess_returncode}", file=sys.stderr)
        return csvprocess_returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
