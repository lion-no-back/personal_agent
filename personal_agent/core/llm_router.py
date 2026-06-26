"""
LLM 路由器

按任务类型将调用分配给最合适的模型，在效果、成本、速度之间取得平衡。

任务类型：
  default   — 主对话，使用能力最强的主力模型
  fast      — 轻量分类、意图识别，使用更便宜/更快的模型
  reasoning — 复杂多步推理，使用专用推理模型（可选）
  vision    — 含图片的消息，使用多模态模型（可选）

.env 配置：
  # 主力模型（必填）
  LLM_MODEL_ID / LLM_API_KEY / LLM_BASE_URL

  # 快速模型（可选）—— Key/URL 缺省时复用主力模型的配置
  LLM_FAST_MODEL_ID / LLM_FAST_API_KEY / LLM_FAST_BASE_URL

  # 推理模型（可选）—— Key/URL 缺省时复用主力模型的配置
  LLM_REASONING_MODEL_ID / LLM_REASONING_API_KEY / LLM_REASONING_BASE_URL

  # 视觉模型（可选）—— Key/URL 缺省时复用主力模型的配置
  LLM_VISION_MODEL_ID / LLM_VISION_API_KEY / LLM_VISION_BASE_URL

单模型场景：只配置主力模型时，所有任务类型均路由到它，行为与原来完全一致。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Generator, List, Optional, Tuple

from . import model_metadata
from .llm import PersonalAgentLLM

logger = logging.getLogger(__name__)


class LLMRouter:
    """
    LLM 路由器：持有若干 PersonalAgentLLM 实例，按 task_type 分发。

    对外暴露与 PersonalAgentLLM 完全兼容的 think / think_stream 接口，
    额外增加 task_type 关键字参数；未配置该类型时自动回退到 default。
    """

    def __init__(self) -> None:
        self._clients: dict[str, PersonalAgentLLM] = {}

        # ── 主力模型（必须成功，失败则向上抛出） ────────────────
        self._clients["default"] = PersonalAgentLLM()

        # ── 快速模型（可选） ─────────────────────────────────────
        if fast_model := os.getenv("LLM_FAST_MODEL_ID"):
            try:
                self._clients["fast"] = PersonalAgentLLM(
                    model=fast_model,
                    apikey=os.getenv("LLM_FAST_API_KEY") or os.getenv("LLM_API_KEY"),
                    baseurl=os.getenv("LLM_FAST_BASE_URL") or os.getenv("LLM_BASE_URL"),
                )
                logger.info("✅ 快速模型已注册 | %s", fast_model)
            except Exception as e:
                logger.warning("⚠️  快速模型初始化失败，回退到主力模型 | %s", e)

        # ── 推理模型（可选） ─────────────────────────────────────
        if reasoning_model := os.getenv("LLM_REASONING_MODEL_ID"):
            try:
                self._clients["reasoning"] = PersonalAgentLLM(
                    model=reasoning_model,
                    apikey=os.getenv("LLM_REASONING_API_KEY") or os.getenv("LLM_API_KEY"),
                    baseurl=os.getenv("LLM_REASONING_BASE_URL") or os.getenv("LLM_BASE_URL"),
                )
                logger.info("✅ 推理模型已注册 | %s", reasoning_model)
            except Exception as e:
                logger.warning("⚠️  推理模型初始化失败，回退到主力模型 | %s", e)

        # ── 视觉模型（可选） ─────────────────────────────────────
        if vision_model := os.getenv("LLM_VISION_MODEL_ID"):
            try:
                self._clients["vision"] = PersonalAgentLLM(
                    model=vision_model,
                    apikey=os.getenv("LLM_VISION_API_KEY") or os.getenv("LLM_API_KEY"),
                    baseurl=os.getenv("LLM_VISION_BASE_URL") or os.getenv("LLM_BASE_URL"),
                )
                logger.info("✅ 视觉模型已注册 | %s", vision_model)
            except Exception as e:
                logger.warning("⚠️  视觉模型初始化失败，回退到主力模型 | %s", e)

        logger.info(
            "✅ LLMRouter 初始化完成 | 已注册任务类型: %s",
            list(self._clients.keys()),
        )

    def _get(self, task_type: str) -> PersonalAgentLLM:
        """返回 task_type 对应的客户端，未配置时回退到 default。"""
        return self._clients.get(task_type) or self._clients["default"]

    # ── 与 PersonalAgentLLM 同签名，增加 task_type ────────────────

    def think_stream(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Any = "auto",
        _quiet: bool = False,
        task_type: str = "default",
    ) -> Generator[Tuple[str, str, Optional[List[Dict[str, Any]]]], None, None]:
        return self._get(task_type).think_stream(
            messages, temperature, tools, tool_choice, _quiet
        )

    def think(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        task_type: str = "default",
    ) -> Optional[str]:
        return self._get(task_type).think(messages, temperature)

    @property
    def model(self) -> Optional[str]:
        """主力模型名，供日志和调试使用。"""
        return self._clients["default"].model

    def get_model_name(self, task_type: str) -> Optional[str]:
        """返回指定任务类型对应的模型名；未配置该类型时返回 None。"""
        client = self._clients.get(task_type)
        return client.model if client else None

    def has_task_type(self, task_type: str) -> bool:
        """该任务类型是否有专属客户端（未配置 = 回退到 default，视为 False）。"""
        return task_type in self._clients

    def get_prompt_budget(self, task_type: str = "default") -> int:
        """返回该任务类型当前模型的 prompt token 预算（按模型上下文动态解析）。

        slug 来源：LLM_{TASK}_OPENROUTER_SLUG > LLM_OPENROUTER_SLUG > 由模型名推断。
        解析与缓存在 model_metadata 内完成，全程不抛出。
        """
        client = self._get(task_type)
        slug = os.getenv(f"LLM_{task_type.upper()}_OPENROUTER_SLUG") or os.getenv(
            "LLM_OPENROUTER_SLUG"
        )
        return model_metadata.get_prompt_budget(client.model, slug=slug)
