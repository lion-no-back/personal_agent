# exportcontroller.py
import os
from . import config  # 导入全局配置
from .msku_export import run_export
from .cost import run_cost
from .upload import run_upload

SELLFOX_USERNAME = os.environ.get("SELLFOX_USERNAME", "")
SELLFOX_PASSWORD = os.environ.get("SELLFOX_PASSWORD", "")

def main():
    # 【改造点】不再自己生成目录，直接使用总控分配的统一目录
    output_dir = config.WORKSPACE_DIR
    
    print("="*60)
    print(f"🚀 [更新主控] 初始化完成，使用统一输出目录: {output_dir}")
    print("="*60)

    print(f"\n▶️ [更新阶段 1/3] 开始执行 export 导出流程...")
    export_csv_path = run_export(output_dir)

    if not export_csv_path or not os.path.exists(export_csv_path):
        print("\n❌ [更新主控] export 执行失败或未生成 CSV 文件，流程已终止。")
        return

    print(f"\n▶️ [更新阶段 2/3] 开始执行 cost 成本计算与分片流程...")
    # 注意：此处的 cost 或 upload 逻辑将把"赛狐报错记录*.xlsx"输出到 output_dir 中
    run_cost(input_csv=export_csv_path, output_dir=output_dir)

    print(f"\n▶️ [更新阶段 3/3] 开始执行 upload3 批量上传流程...")
    run_upload(output_dir=output_dir, username=SELLFOX_USERNAME, password=SELLFOX_PASSWORD)

    print("\n✅ [更新主控] 执行完毕！")

if __name__ == "__main__":
    main()