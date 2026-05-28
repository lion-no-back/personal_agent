# -*- coding: utf-8 -*-
"""
清货预警筛选脚本（由 qhwarn.py 调用，不独立运行）

筛选条件（父体级别，按店铺名称+父ASIN分组）：
  - 上架时间 早于 8个月前
  - 父体 330天以上库存总数 / 库存总数 >= 70%
  - 父体 30天销量合计 < 20单

输出列：店铺名称、图片链接、本地SKU名称、父ASIN、库存总数、
        181-330天库存数、331-365天库存数、365天以上库存数、售价
"""

import re
import pandas as pd
from pathlib import Path
from datetime import date, datetime

STOCK_COLS     = ["可售", "待入库", "预留", "计划入库", "在途"]
AGE_330P_COLS  = ["331-365天库存数", "365天以上库存数"]

LISTING_MONTHS  = 8
RATIO_THRESHOLD = 0.70
SALES_THRESHOLD = 20

OUTPUT_COLS = [
    "店铺名称", "图片链接", "本地SKU名称", "父ASIN",
    "日均销量", "库存总数",
    "181-330天库存数", "331-365天库存数", "365天以上库存数",
    "售价",
]


def _clean_sku(name: str) -> str:
    if not isinstance(name, str):
        return ""
    name = re.sub(r'\s*[（(][^)）]*[）)]\s*', ' ', name)  # 去括号内容（中英文括号）
    name = re.sub(r'\s*\d+mm\b\s*', '', name, flags=re.IGNORECASE)  # 去尺码如 38mm
    return re.sub(r' +', ' ', name).strip()


_EU_LINE_RE = re.compile(r'^(\S+ \S+)\s+([\d.]+)\s*$')
_PRICE_RE   = re.compile(r'^(.*?)([\d.]+)\s*$')


def _format_price_eu(prices: pd.Series) -> str:
    """欧洲站多国价格处理：多行父体按国家给区间，单行原样返回。"""
    all_rows: list[dict] = []  # 每个元素: {country_cur: (num, orig_line)}
    for p in prices.dropna():
        v = str(p).strip()
        if not v or v == '--':
            continue
        row: dict = {}
        for line in v.split('\n'):
            line = line.strip()
            m = _EU_LINE_RE.match(line)
            if m and m.group(1) not in row:
                row[m.group(1)] = (float(m.group(2)), line)
        if row:
            all_rows.append(row)

    if not all_rows:
        return ""

    if len(all_rows) == 1:
        return '\n'.join(orig for _, orig in all_rows[0].values())

    # 多行：并集，各国计算区间
    country_nums: dict = {}
    country_first: dict = {}
    for row in all_rows:
        for cc, (num, orig) in row.items():
            country_nums.setdefault(cc, []).append(num)
            country_first.setdefault(cc, orig)

    lines = []
    for cc, nums in country_nums.items():
        lo, hi = min(nums), max(nums)
        lines.append(country_first[cc] if lo == hi else f"{cc} {lo:g}-{hi:g}")
    return '\n'.join(lines)


def _format_price(prices: pd.Series) -> str:
    valid = [str(p).strip() for p in prices.dropna() if str(p).strip() and str(p).strip() != '--']
    if any('\n' in v for v in valid):
        return _format_price_eu(prices)

    prefix, nums, first_valid = "", [], ""
    for v in valid:
        m = _PRICE_RE.match(v)
        if m:
            if not prefix:
                prefix, first_valid = m.group(1), v
            nums.append(float(m.group(2)))

    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    return first_valid if lo == hi else f"{prefix}{lo:g}-{hi:g}"


def run(artifact_dir: Path, xls_files: list) -> "tuple[Path, int] | tuple[None, int]":
    """
    artifact_dir : 输出目录（存放 清货预警HHMMSS.xlsx）
    xls_files    : 本次导出的 .xls 文件路径列表
    """
    artifact_dir = Path(artifact_dir)
    output_file  = artifact_dir / f"清货预警{datetime.now().strftime('%H%M%S')}.xlsx"
    cutoff_date  = pd.Timestamp(date.today()) - pd.DateOffset(months=LISTING_MONTHS)

    xls_files = [Path(f) for f in xls_files]
    if not xls_files:
        raise SystemExit("[ERR] 无可用的 .xls 文件")

    print(f"读取目录: {artifact_dir}")
    frames = []

    for xls_path in sorted(xls_files):
        df = pd.read_excel(xls_path, engine="calamine", dtype=str)

        numeric_cols = STOCK_COLS + ["30天销量", "日均销量", "181-330天库存数", "331-365天库存数", "365天以上库存数"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        df["库存总数"] = df[STOCK_COLS].sum(axis=1)

        # dayfirst=True 兼容欧洲站 DD/MM/YYYY 格式；YYYY-MM-DD 无歧义不受影响
        cleaned_time = df["上架时间"].str.replace(r"\s+[A-Z]{2,5}$", "", regex=True)
        df["上架时间"] = pd.to_datetime(cleaned_time, errors="coerce", dayfirst=True, format="mixed")

        frames.append(df)
        print(f"  {xls_path.name}: {len(df)} 行")

    if not frames:
        print("[OK] 无数据")
        return

    all_data = pd.concat(frames, ignore_index=True)

    # 过滤掉父ASIN为空的行
    all_data = all_data[all_data["父ASIN"].notna() & (all_data["父ASIN"].str.strip() != "")]

    # 去重：多文件可能包含重复行（同一站点数据分批导出时出现），按(店铺名称, ASIN)去重
    if "ASIN" in all_data.columns:
        before = len(all_data)
        all_data = all_data.drop_duplicates(subset=["店铺名称", "ASIN"])
        removed = before - len(all_data)
        if removed:
            print(f"  [去重] 移除重复行 {removed} 条")

    rows = []
    for (shop, parent_asin), grp in all_data.groupby(["店铺名称", "父ASIN"], sort=False):
        # 条件1：父体最晚上架时间 > 8个月前，才纳入预警
        latest_listing = grp["上架时间"].max()
        if pd.isna(latest_listing) or latest_listing >= cutoff_date:
            continue

        # 条件2：330天以上库存占总库存 >= 70%
        total_inv  = grp["库存总数"].sum()
        inv_330p   = grp[AGE_330P_COLS].sum(axis=1).sum()
        if total_inv <= 0 or inv_330p / total_inv < RATIO_THRESHOLD:
            continue

        # 条件3：父体30天销量 < 20
        sales_30 = grp["30天销量"].sum() if "30天销量" in grp.columns else 0
        if sales_30 >= SALES_THRESHOLD:
            continue

        rows.append({
            "店铺名称":       shop,
            "图片链接":       grp["图片链接"].dropna().iloc[0] if not grp["图片链接"].dropna().empty else "",
            "本地SKU名称":    _clean_sku(grp["本地SKU名称"].dropna().iloc[0] if not grp["本地SKU名称"].dropna().empty else ""),
            "父ASIN":         parent_asin,
            "日均销量":       round(grp["日均销量"].sum() if "日均销量" in grp.columns else 0, 2),
            "库存总数":       int(total_inv),
            "181-330天库存数": int(grp["181-330天库存数"].sum()),
            "331-365天库存数": int(grp["331-365天库存数"].sum()),
            "365天以上库存数": int(grp["365天以上库存数"].sum()),
            "售价":           _format_price(grp["售价"]),
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
