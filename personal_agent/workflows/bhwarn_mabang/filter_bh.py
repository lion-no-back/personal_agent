# -*- coding: utf-8 -*-
"""
补货预警筛选脚本（由 bhwarn.py 调用，不独立运行）

筛选条件（ASIN 行级别）：
  - 可售天数 < 60
  - 备注不含「清货下架」
  - 30天销量 > 5

输出列：店铺名称、图片链接、本地SKU名称、父ASIN、日均销量、ASIN、
        库存总数、可售天数、预计补货量（日均销量×70−库存总数）、备注
"""

import re
import pandas as pd
from pathlib import Path
from datetime import datetime

STOCK_COLS   = ["可售", "待入库", "预留", "计划入库", "在途"]
SALES_MIN    = 5
SELLABLE_MAX = 60
RESTOCK_DAYS = 70

OUTPUT_COLS = [
    "店铺名称", "图片链接", "本地SKU名称", "父ASIN",
    "日均销量", "ASIN", "库存总数", "可售天数", "预计补货量", "备注",
]


def _clean_sku(name: str) -> str:
    if not isinstance(name, str):
        return ""
    name = re.sub(r'\s*[（(][^)）]*[）)]\s*', ' ', name)
    name = re.sub(r'\s*\d+mm\b\s*', '', name, flags=re.IGNORECASE)
    return re.sub(r' +', ' ', name).strip()


def run(artifact_dir: Path, xls_files: list) -> "tuple[Path, int] | tuple[None, int]":
    """
    artifact_dir : 输出目录（存放 补货预警HHMMSS.xlsx）
    xls_files    : 本次导出的 .xls 文件路径列表
    """
    artifact_dir = Path(artifact_dir)
    output_file  = artifact_dir / f"补货预警{datetime.now().strftime('%H%M%S')}.xlsx"

    xls_files = [Path(f) for f in xls_files]
    if not xls_files:
        raise SystemExit("[ERR] 无可用的 .xls 文件")

    print(f"读取目录: {artifact_dir}")
    frames = []

    for xls_path in sorted(xls_files):
        df = pd.read_excel(xls_path, engine="calamine", dtype=str)

        numeric_cols = STOCK_COLS + ["30天销量", "日均销量", "可售天数"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        df["库存总数"] = df[STOCK_COLS].sum(axis=1)

        frames.append(df)
        print(f"  {xls_path.name}: {len(df)} 行")

    if not frames:
        print("[OK] 无数据")
        return None, 0

    df = pd.concat(frames, ignore_index=True)

    # 过滤掉父ASIN为空的行
    df = df[df["父ASIN"].notna() & (df["父ASIN"].str.strip() != "")]

    # 去重：同一店铺同一ASIN只保留一行
    if "ASIN" in df.columns:
        before = len(df)
        df = df.drop_duplicates(subset=["店铺名称", "ASIN"])
        removed = before - len(df)
        if removed:
            print(f"  [去重] 移除重复行 {removed} 条")

    # 行级筛选
    mask = pd.Series(True, index=df.index)

    if "备注" in df.columns:
        mask &= ~df["备注"].fillna("").str.contains("清货下架", na=False)

    if "30天销量" in df.columns:
        mask &= df["30天销量"] > SALES_MIN

    if "可售天数" in df.columns:
        mask &= df["可售天数"] < SELLABLE_MAX

    filtered = df[mask].copy()

    if filtered.empty:
        print("[OK] 无符合条件的记录，未生成文件")
        return None, 0

    filtered["预计补货量"] = (filtered["日均销量"] * RESTOCK_DAYS - filtered["库存总数"]).round().astype(int)
    filtered["备注"]      = filtered["预计补货量"].apply(lambda x: "关注货件送达时间" if x < 0 else "")
    filtered["本地SKU名称"] = filtered["本地SKU名称"].apply(_clean_sku)
    filtered["日均销量"]  = filtered["日均销量"].round(2)
    filtered["库存总数"]  = filtered["库存总数"].astype(int)
    filtered["可售天数"]  = filtered["可售天数"].astype(int)

    result = filtered[OUTPUT_COLS].copy()

    # 按店铺名称原始出现顺序排序
    shop_order = {s: i for i, s in enumerate(result["店铺名称"].unique())}
    result = result.iloc[result["店铺名称"].map(shop_order).argsort(kind="stable")].reset_index(drop=True)

    try:
        result.to_excel(output_file, index=False, engine="openpyxl")
    except PermissionError:
        raise RuntimeError(f"无法写入文件，请先关闭 Excel 中的 {output_file.name} 后重试")

    hit_count = len(result)
    print(f"\n[OK] 共 {hit_count} 条记录命中 -> {output_file}")
    return output_file, hit_count
