import csv
import os
import sys
from datetime import datetime


OUTPUT_COLUMNS = [
    "站点",
    "request-date",
    "order-id",
    "order-source",
    "order-type",
    "order-status",
    "last-updated-date",
    "sku",
    "fnsku",
    "disposition",
    "requested-quantity",
    "cancelled-quantity",
    "disposed-quantity",
    "shipped-quantity",
    "in-process-quantity",
    "removal-fee",
    "currency",
    "销毁数量",
    "采购单价",
    "销毁货值",
    "单位RMB",
]

# request-date 到 currency，与原始 CSV 表头一一对应
SOURCE_COLUMNS = OUTPUT_COLUMNS[1:17]


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def _safe_int(val: str) -> int:
    try:
        return int(round(float(val)))
    except (ValueError, TypeError):
        return 0


def _extract_shop_name(filepath: str) -> str:
    """文件名格式为 {shop_name}_{原始文件名}，取第一个 _ 之前的部分"""
    return os.path.basename(filepath).split("_")[0]


def _read_csv(filepath: str) -> list:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            with open(filepath, "r", encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    return []


def process_directory(input_dir: str, price_dict: dict = None) -> str:
    """
    Merge all removal report CSVs in input_dir into merged.csv.
    price_dict: {msku: float_price} from cost.get_prices(); when provided,
    填入采购单价 and 计算销毁货值.
    """
    if price_dict is None:
        price_dict = {}

    EXCLUDED_CSVS = {"merged.csv", "task_cache.csv", "error_cache.csv"}
    csv_files = sorted(
        os.path.join(input_dir, f)
        for f in os.listdir(input_dir)
        if f.lower().endswith(".csv")
        and f.lower() not in EXCLUDED_CSVS
        and not f.lower().startswith("batch_")
    )

    if not csv_files:
        _log(f"目录中无 CSV 文件: {input_dir}")
        return ""

    _log(f"找到 {len(csv_files)} 个文件，开始合并...")
    output_path = os.path.join(input_dir, "merged.csv")
    total_rows = 0

    with open(output_path, "w", newline="", encoding="utf-8-sig") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()

        for filepath in csv_files:
            shop_name = _extract_shop_name(filepath)
            rows = _read_csv(filepath)
            if not rows:
                _log(f"  跳过（空文件或编码无法识别）: {os.path.basename(filepath)}")
                continue

            _log(f"  {shop_name}: {len(rows)} 行")
            for row in rows:
                disposed = _safe_int(row.get("disposed-quantity", ""))
                shipped = _safe_int(row.get("shipped-quantity", ""))
                destroyed_qty = disposed + shipped

                msku = (row.get("sku") or "").strip()
                unit_price = price_dict.get(msku, "")

                if unit_price != "":
                    destroyed_value = round(destroyed_qty * unit_price, 4)
                else:
                    destroyed_value = ""

                out_row = {"站点": shop_name}
                for col in SOURCE_COLUMNS:
                    out_row[col] = row.get(col, "")
                out_row["销毁数量"] = destroyed_qty
                out_row["采购单价"] = unit_price
                out_row["销毁货值"] = destroyed_value
                out_row["单位RMB"] = ""
                writer.writerow(out_row)
                total_rows += 1

    _log(f"合并完成：共 {total_rows} 行 → {output_path}")
    return output_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python process.py <directory>")
        sys.exit(1)
    process_directory(sys.argv[1])


if __name__ == "__main__":
    main()
