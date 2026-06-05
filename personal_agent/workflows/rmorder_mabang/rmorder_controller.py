import csv
import os

from . import rmorder, cost, process


def _extract_mskus(output_dir: str) -> list:
    """Extract unique MSKU values (sku column) from all downloaded CSV files."""
    seen = set()
    unique = []
    csv_files = [
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if f.lower().endswith(".csv") and f.lower() != "merged.csv"
    ]
    for filepath in csv_files:
        for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
            try:
                with open(filepath, "r", encoding=enc, newline="") as f:
                    for row in csv.DictReader(f):
                        msku = (row.get("sku") or "").strip()
                        if msku and msku not in seen:
                            seen.add(msku)
                            unique.append(msku)
                break
            except UnicodeDecodeError:
                continue
    return unique


def main() -> str:
    # Step 1: download removal reports
    output_dir = rmorder.main()

    if not output_dir:
        raise RuntimeError("rmorder 未返回输出目录")

    # Step 2: extract MSKUs and fetch purchase prices
    mskus = _extract_mskus(output_dir)
    print(f"\n[INFO] 从下载文件中提取到 {len(mskus)} 个唯一 MSKU")

    price_dict = {}
    if mskus:
        price_dict = cost.get_prices(mskus, output_dir)
    else:
        print("[WARN] 未找到任何 MSKU，跳过价格查询")

    # Step 3: merge CSVs and fill in prices → merged.csv
    process.process_directory(output_dir, price_dict)

    return output_dir


if __name__ == "__main__":
    main()
