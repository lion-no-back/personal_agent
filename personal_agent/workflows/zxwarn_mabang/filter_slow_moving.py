# -*- coding: utf-8 -*-
"""
滞销预警筛选脚本（由 zhixiaowarn.py 调用，不独立运行）

筛选条件：
  - 可售天数 > 270
  - 库存总数（可售+待入库+预留+计划入库+在途）> 20
  - 上架时间 早于 90 天前

输出列：店铺名称、图片链接、本地SKU名称、ASIN、库存总数、
        181-330天库存数、331-365天库存数、365天以上库存数、售价
"""

import pandas as pd
from pathlib import Path
from datetime import date, datetime, timedelta

STOCK_COLS   = ["可售", "待入库", "预留", "计划入库", "在途"]
FILTER_DAYS  = 270
FILTER_STOCK = 20
OUTPUT_COLS  = [
    "店铺名称", "图片链接", "本地SKU名称", "父ASIN", "ASIN",
    "库存总数",
    "181-330天库存数", "331-365天库存数", "365天以上库存数",
    "售价",
]


def run(artifact_dir: Path, xls_files: list) -> "tuple[Path, int] | tuple[None, int]":
    """
    artifact_dir : 输出目录（存放 滞销预警HHMMSS.xlsx）
    xls_files    : 本次导出的 .xls 文件路径列表（由 mabang_export 传入，避免读到旧文件）
    """
    artifact_dir = Path(artifact_dir)
    output_file  = artifact_dir / f"滞销预警{datetime.now().strftime('%H%M%S')}.xlsx"
    cutoff_date  = pd.Timestamp(date.today() - timedelta(days=90))

    xls_files = [Path(f) for f in xls_files]
    if not xls_files:
        raise SystemExit("[ERR] 无可用的 .xls 文件")

    print(f"读取目录: {artifact_dir}")
    frames = []

    for xls_path in sorted(xls_files):
        df = pd.read_excel(xls_path, engine="calamine", dtype=str)

        numeric_cols = STOCK_COLS + ["可售天数", "181-330天库存数", "331-365天库存数", "365天以上库存数"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        df["库存总数"] = df[STOCK_COLS].sum(axis=1)
        # 去掉末尾时区缩写（PDT / GMT / UTC / EST 等），再解析为 tz-naive
        cleaned = df["上架时间"].str.replace(r"\s+[A-Z]{2,5}$", "", regex=True)
        df["上架时间"] = pd.to_datetime(cleaned, errors="coerce", dayfirst=True, format="mixed")

        mask = (
            (df["可售天数"] > FILTER_DAYS) &
            (df["库存总数"] > FILTER_STOCK) &
            (df["上架时间"] < cutoff_date)
        )

        hit = df.loc[mask, OUTPUT_COLS].copy()
        print(f"  {xls_path.name}: 共 {len(df)} 行，命中 {len(hit)} 行")
        if not hit.empty:
            frames.append(hit)

    if not frames:
        print("[OK] 无符合条件的记录，未生成文件")
        return None, 0

    result = pd.concat(frames, ignore_index=True)
    try:
        result.to_excel(output_file, index=False, engine="openpyxl")
    except PermissionError:
        raise RuntimeError(f"无法写入文件，请先关闭 Excel 中的 {output_file.name} 后重试")
    hit_count = len(result)
    print(f"\n[OK] 共命中 {hit_count} 条 -> {output_file}")
    return output_file, hit_count
