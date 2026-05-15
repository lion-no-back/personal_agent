"""
RequestScorer — 本地多维打分，决定将请求路由到哪个模型档位。

借鉴 ClawRouter 多档本地评分策略，纯 Python 实现，< 0.5ms 延迟，零额外 API 调用。

档位：
  default   — 主力模型（大多数请求）
  reasoning — 推理模型（复杂推理/规划/数学，需在 .env 配置 LLM_REASONING_MODEL_ID）

策略模式（LLM_ROUTING_MODE 环境变量）：
  economy   — 保守，阈值 0.80，极少触发 reasoning
  auto      — 均衡，阈值 0.60（默认）
  quality   — 激进，阈值 0.35，更多请求走 reasoning
  disabled  — 关闭智能路由，全走 default

日志：每次决策均在 DEBUG 级别输出维度明细，便于观察和调阈值。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_STRATEGY_THRESHOLDS: dict[str, float] = {
    "economy":  0.80,
    "auto":     0.60,
    "quality":  0.35,
    "disabled": float("inf"),
}

_BORDERLINE_MARGIN = 0.15  # threshold ± 此值区间内视为"边界区"，可提示换强力模型

# ── 各维度关键词表（模块级常量，避免每次 score() 重建）────────────────
_PLAN_HIGH   = ("规划", "方案", "架构", "设计", "流程", "步骤", "路线图", "策略", "蓝图")
_PLAN_MED    = ("分析", "对比", "评估", "讨论", "权衡", "利弊", "优缺点", "比较")
_PLAN_LOW    = ("总结", "归纳", "梳理", "整理", "汇总")
_MATH        = ("计算", "求解", "推导", "证明", "公式", "数学", "积分", "微分", "概率", "统计")
_REASON      = ("推理", "逻辑", "因果", "为什么会", "矛盾", "反驳", "论证", "反例")
_CODE_HIGH   = ("```", "代码审查", "重构", "算法", "时间复杂度", "设计模式", "并发", "死锁")
_CODE_LOW    = ("代码", "函数", "bug", "报错", "错误", "实现", "程序", "脚本")
_OUT         = ("写一份", "生成报告", "写报告", "详细", "完整", "全面", "系统性", "深入", "文档")
_DOMAIN      = ("医学", "法律", "金融", "学术", "研究", "专业", "论文", "技术文档", "合规")


@dataclass
class ScoreResult:
    score: float              # 综合分 0.0–1.0
    task_type: str            # "default" 或 "reasoning"
    is_borderline: bool       # 是否处于边界区
    dimensions: dict[str, float]   # 各维度得分（用于日志明细）
    strategy: str             # 当前策略名
    threshold: float          # 使用的阈值


class RequestScorer:
    """
    基于 8 个本地可测量维度，输出路由决策。

    维度与权重上限：
      1. input_length        (0.20) — 输入长度
      2. planning_complexity (0.25) — 多步规划 / 分析
      3. reasoning_math      (0.20) — 推理 / 数学
      4. code_complexity     (0.15) — 代码复杂度
      5. output_complexity   (0.10) — 预期输出复杂度
      6. domain_expertise    (0.10) — 专业领域
      7. context_depth       (0.10) — 对话深度（历史轮数）
      8. tool_richness       (0.05) — 工具数量
    各维度满分叠加约 1.15，最终 min(total, 1.0)。
    """

    def __init__(
        self,
        strategy: str = "auto",
        threshold_override: float | None = None,
    ) -> None:
        self.strategy = strategy if strategy in _STRATEGY_THRESHOLDS else "auto"
        self.threshold = (
            threshold_override
            if threshold_override is not None
            else _STRATEGY_THRESHOLDS[self.strategy]
        )
        logger.info(
            "✅ RequestScorer 初始化 | strategy=%s threshold=%.2f",
            self.strategy,
            self.threshold,
        )

    # ── 公共接口 ──────────────────────────────────────────────

    def score(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        user_text: str | None = None,
    ) -> ScoreResult:
        """
        对一组消息进行打分，返回 ScoreResult。

        Args:
            messages:  完整的 messages 列表（含 system / history / 当前用户消息）
            tools:     本次调用携带的工具 schema 列表
            user_text: 原始用户文本（优先使用）。不传时从 messages 末尾提取。
                       调用方应传入 event.content 以避免评分吃到瞬态注入内容
                       （文件上下文、技能文档等会拼入 messages[-1] 但不属于用户意图）。
        """
        text = user_text if user_text is not None else self._last_user_text(messages)
        dims = self._compute_dimensions(text, messages, tools)
        total = min(sum(dims.values()), 1.0)

        if self.strategy == "disabled":
            task_type = "default"
            borderline = False
        else:
            task_type = "reasoning" if total >= self.threshold else "default"
            borderline = abs(total - self.threshold) <= _BORDERLINE_MARGIN

        result = ScoreResult(
            score=round(total, 3),
            task_type=task_type,
            is_borderline=borderline,
            dimensions={k: round(v, 3) for k, v in dims.items()},
            strategy=self.strategy,
            threshold=self.threshold,
        )

        if logger.isEnabledFor(logging.DEBUG):
            dim_str = " ".join(f"{k}:{v:.3f}" for k, v in result.dimensions.items())
            logger.debug(
                "[Router] score=%.3f → %s | %s | strategy=%s threshold=%.2f borderline=%s",
                result.score,
                result.task_type,
                dim_str,
                result.strategy,
                result.threshold,
                result.is_borderline,
            )

        return result

    # ── 内部工具 ──────────────────────────────────────────────

    @staticmethod
    def _last_user_text(messages: list[dict]) -> str:
        """提取最后一条 user 消息的文本内容（兼容多模态格式）。"""
        for m in reversed(messages):
            if m.get("role") != "user":
                continue
            content = m.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
        return ""

    def _compute_dimensions(
        self,
        text: str,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> dict[str, float]:
        dims: dict[str, float] = {}

        # 1. 输入长度（0 – 0.20）
        n = len(text)
        if n >= 600:
            dims["input_length"] = 0.20
        elif n >= 300:
            dims["input_length"] = 0.15
        elif n >= 150:
            dims["input_length"] = 0.10
        else:
            dims["input_length"] = 0.05

        # 2. 多步规划复杂度（0 – 0.25）
        if any(kw in text for kw in _PLAN_HIGH):
            dims["planning_complexity"] = 0.25
        elif any(kw in text for kw in _PLAN_MED):
            dims["planning_complexity"] = 0.15
        elif any(kw in text for kw in _PLAN_LOW):
            dims["planning_complexity"] = 0.08
        else:
            dims["planning_complexity"] = 0.0

        # 3. 推理 / 数学（0 – 0.20）
        if any(kw in text for kw in _MATH):
            dims["reasoning_math"] = 0.20
        elif any(kw in text for kw in _REASON):
            dims["reasoning_math"] = 0.15
        else:
            dims["reasoning_math"] = 0.0

        # 4. 代码复杂度（0 – 0.15）
        if any(kw in text for kw in _CODE_HIGH):
            dims["code_complexity"] = 0.15
        elif any(kw in text for kw in _CODE_LOW):
            dims["code_complexity"] = 0.08
        else:
            dims["code_complexity"] = 0.0

        # 5. 预期输出复杂度（0 – 0.10）
        dims["output_complexity"] = 0.10 if any(kw in text for kw in _OUT) else 0.0

        # 6. 专业领域（0 – 0.10）
        dims["domain_expertise"] = 0.10 if any(kw in text for kw in _DOMAIN) else 0.0

        # 7. 对话深度（0 – 0.10）
        user_turns = sum(1 for m in messages if m.get("role") == "user")
        dims["context_depth"] = min(user_turns / 15.0, 0.10)

        # 8. 工具丰富度（0 – 0.05）
        dims["tool_richness"] = 0.05 if (tools and len(tools) > 5) else 0.0

        return dims
