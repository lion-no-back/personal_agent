# -*- coding: utf-8 -*-
"""
补货预警筛选脚本（由 bhwarn.py 调用，不独立运行）

筛选条件（父体级别，按店铺名称+父ASIN分组）：
  - 备注：组内任意一行含「清货下架」→ 跳过整组
  - 父体30天销量合计 > 5
  - 父体可售天数合计 < 60

输出列：店铺名称、图片链接、本地SKU名称、父ASIN、
        库存总数、可售天数、预计补货量
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
    "日均销量", "库存总数", "可售天数", "预计补货量", "备注",
]


def _clean_sku(name: str) -> str:
    if not isinstance(name, str):
        return ""
    name = re.sub(r'\s*[（(][^)）]*[）)]\s*', ' ', name)  # 去括号内容（中英文括号）
    name = re.sub(r'\s*\d+mm\b\s*', '', name, flags=re.IGNORECASE)  # 去尺码如 38mm
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
        return

    all_data = pd.concat(frames, ignore_index=True)

    # 过滤掉父ASIN为空的行
    all_data = all_data[all_data["父ASIN"].notna() & (all_data["父ASIN"].str.strip() != "")]

    # 去重：同一站点同一ASIN只保留一行
    if "ASIN" in all_data.columns:
        before = len(all_data)
        all_data = all_data.drop_duplicates(subset=["店铺名称", "ASIN"])
        removed = before - len(all_data)
        if removed:
            print(f"  [去重] 移除重复行 {removed} 条")

    rows = []
    for (shop, parent_asin), grp in all_data.groupby(["店铺名称", "父ASIN"], sort=False):
        # 条件1：任意一行备注含「清货下架」→ 跳过整组
        if "备注" in grp.columns and grp["备注"].fillna("").str.contains("清货下架", na=False).any():
            continue

        # 条件2：父体30天销量合计 > 5
        sales_30 = grp["30天销量"].sum() if "30天销量" in grp.columns else 0
        if sales_30 <= SALES_MIN:
            continue

        # 条件3：父体可售天数合计 < 60
        sellable_days = grp["可售天数"].sum() if "可售天数" in grp.columns else 0
        if sellable_days >= SELLABLE_MAX:
            continue

        total_inv     = grp["库存总数"].sum()
        daily_avg_sum = grp["日均销量"].sum() if "日均销量" in grp.columns else 0
        restock_qty   = round(daily_avg_sum * RESTOCK_DAYS - total_inv)

        rows.append({
            "店铺名称":   shop,
            "图片链接":   grp["图片链接"].dropna().iloc[0] if not grp["图片链接"].dropna().empty else "",
            "本地SKU名称": _clean_sku(grp["本地SKU名称"].dropna().iloc[0] if not grp["本地SKU名称"].dropna().empty else ""),
            "父ASIN":     parent_asin,
            "日均销量":   round(daily_avg_sum, 2),
            "库存总数":   int(total_inv),
            "可售天数":   int(sellable_days),
            "预计补货量": int(restock_qty),
            "备注":       "关注货件送达时间" if restock_qty < 0 else "",
        })

    if not rows:
        print("[OK] 无符合条件的父体记录，未生成文件")
        return None, 0

    result = pd.DataFrame(rows, columns=OUTPUT_COLS)
    _order = {s: i for i, s in enumerate(result["店铺名称"].unique())}
    result = result.iloc[result["店铺名称"].map(_order).argsort(kind="stable")].reset_index(drop=True)
    try:
        result.to_excel(output_file, index=False, engine="openpyxl")
    except PermissionError:
        raise RuntimeError(f"无法写入文件，请先关闭 Excel 中的 {output_file.name} 后重试")
    hit_count = len(result)
    print(f"\n[OK] 共 {hit_count} 个父体命中 -> {output_file}")
    return output_file, hit_count
