---
name: cost-export
description: 从MABAN导出采购成本。当飞书用户发来导出成本意图后，使用此技能。
type: default
---

# 采购成本导出

## 使用场景

当用户表达导出成本、成本导出等相似意图时，使用此技能

## 工作流程

1. 使用 exec 工具安装依赖（前台执行，必须等待完成再进行下一步）
   - command: `uv pip install -r requirements.txt`
   - workdir: 本技能说明开头「脚本目录」字段标注的绝对路径
   - background: false（不得设为后台）
   - 若退出码非 0，立即停止并告知用户"依赖安装失败，请检查服务器环境"，不得继续执行步骤 2

2. 使用 exec 工具执行主控脚本（后台执行，因运行时间较长）
   - command: `python mcontroller.py`
   - workdir: 本技能说明开头「脚本目录」字段标注的绝对路径
   - background: true