"""
赛狐产品分析 - 对导出的 Excel 文件打亏损标签

规则（亏损类型列插入到 ACoAS 列之前）：
  毛利润 < -1          且 ACoAS >= 20%              → 高ACoAS
  毛利润 < -1          且 ACoAS < 20% 且 退款率 >= 12% → 高退款
  毛利润 < -1          且 ACoAS < 20% 且 退款率 < 12%  → 其他
  毛利率 > 0% 且 <= 8%                               → 低利润
  其余                                               → 空
"""

import os
import sys
from datetime import datetime

import openpyxl

COL_PROFIT_PRICE = "毛利润"
COL_PROFIT_RATE  = "毛利率"
COL_ACOAS        = "ACoAS"
COL_REFUND_RATE  = "退款率"
NEW_COL          = "亏损类型"


def _classify(profit_price, profit_rate, acoas, refund_rate) -> str:
    if profit_price is not None and profit_price < -1:
        if acoas is not None and acoas >= 0.20:
            return "高ACoAS"
        if refund_rate is not None and refund_rate >= 0.12:
            return "高退款"
        return "其他"
    if profit_rate is not None and 0 < profit_rate <= 0.08:
        return "低利润"
    return ""


def process_file(src_path: str, dst_path: str):
    wb_src = openpyxl.load_workbook(src_path)
    ws_src = wb_src.active

    header = [cell.value for cell in next(ws_src.iter_rows(min_row=1, max_row=1))]
    pp_idx = header.index(COL_PROFIT_PRICE)
    pr_idx = header.index(COL_PROFIT_RATE)
    ac_idx = header.index(COL_ACOAS)
    rr_idx = header.index(COL_REFUND_RATE)

    wb_dst = openpyxl.Workbook()
    ws_dst = wb_dst.active
    ws_dst.append(header[:ac_idx] + [NEW_COL] + header[ac_idx:])

    for row in ws_src.iter_rows(min_row=2, values_only=True):
        label = _classify(row[pp_idx], row[pr_idx], row[ac_idx], row[rr_idx])
        if label:
            ws_dst.append(list(row[:ac_idx]) + [label] + list(row[ac_idx:]))

    os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
    wb_dst.save(dst_path)
    print(f"    已保存 → {dst_path}")


def _dst_fname(src_fname: str) -> str:
    """YYH_2026-06-01_2026-06-07.xlsx → YYH_亏损链接_2026-06-01_2026-06-07.xlsx"""
    stem, ext = os.path.splitext(src_fname)
    parts = stem.split("_", 1)
    if len(parts) == 2:
        brand, rest = parts
        return f"{brand}_亏损链接_{rest}{ext}"
    return f"{stem}_亏损链接{ext}"


def process_dir(input_dir: str) -> str:
    processed_dir = os.path.join(input_dir, "processed" + datetime.now().strftime("%H%M%S"))
    files = [f for f in os.listdir(input_dir) if f.endswith(".xlsx")]
    print(f"[*] ksprocess: 共 {len(files)} 个文件，输出到 {processed_dir}")
    ok = 0
    for fname in files:
        print(f"[>] 处理 {fname}")
        try:
            process_file(
                os.path.join(input_dir, fname),
                os.path.join(processed_dir, _dst_fname(fname)),
            )
            ok += 1
        except Exception as e:
            print(f"    [!] 失败: {e}")
    print(f"[+] 完成：成功 {ok} / {len(files)} 个")
    return processed_dir


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: python ksprocess.py <输入目录>")
        sys.exit(1)
    process_dir(sys.argv[1])
