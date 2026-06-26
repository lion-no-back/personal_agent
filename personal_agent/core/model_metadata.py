"""按模型型号解析上下文长度 / 价格，用于动态计算 prompt 预算。

数据来源优先级（逐级回退，全程不抛出）：
  1. LLM_MAX_PROMPT_TOKENS 环境变量（直接作为最终预算，短路）
  2. OpenRouter 官方单模型 JSON API: GET /api/v1/model/{slug}
     取 top_provider.context_length（缺失退 data.context_length）
  3. 内置模型表 _MODEL_CONTEXT_TABLE（子串匹配）
  4. 默认 _DEFAULT_CONTEXT

预算 = context_length - output_reserve，下限 _MIN_BUDGET。
output_reserve 优先级: LLM_OUTPUT_RESERVE_TOKENS → top_provider.max_completion_tokens → _DEFAULT_RESERVE

详见 docs/superpowers/specs/2026-06-26-conversation-history-truncation-design.md
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_OPENROUTER_MODEL_URL = "https://openrouter.ai/api/v1/model/{slug}"
_FETCH_TIMEOUT = 5.0

_DEFAULT_CONTEXT = 65536
_DEFAULT_RESERVE = 8192
_MIN_BUDGET = 4096

# OpenRouter 不可达时的兜底（子串匹配，model id 子串 → context_length）
_MODEL_CONTEXT_TABLE = {
    "deepseek-v4": 1048576,
    "deepseek-reasoner": 65536,
    "deepseek-r1": 65536,
    "deepseek-v3": 65536,
    "deepseek-chat": 65536,
    "gpt-4o": 128000,
    "gpt-4.1": 128000,
}

# 进程内缓存
_meta_cache: Dict[str, Optional[dict]] = {}     # model_id -> 解析出的元数据（含 None 表示不可用）
_budget_cache: Dict[str, int] = {}              # model_id -> prompt 预算


def _slug_for(model_id: str, slug: Optional[str]) -> str:
    """推断 OpenRouter slug：显式 slug 优先；含 / 直接用；否则补 deepseek/ 前缀按需匹配。"""
    if slug:
        return slug
    if "/" in model_id:
        return model_id
    return f"deepseek/{model_id}"


def _fetch_openrouter(slug: str) -> Optional[dict]:
    """调 OpenRouter 单模型 API，返回 data 字段；任何异常/404 返回 None。"""
    try:
        headers = {}
        if key := os.getenv("OPENROUTER_API_KEY"):
            headers["Authorization"] = f"Bearer {key}"
        resp = httpx.get(
            _OPENROUTER_MODEL_URL.format(slug=slug),
            headers=headers,
            timeout=_FETCH_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("data")
    except Exception as e:
        logger.warning("OpenRouter 模型元数据获取失败 | slug=%s err=%s", slug, e)
        return None


def _resolve_meta(model_id: str, slug: Optional[str]) -> Optional[dict]:
    """解析并缓存模型元数据：{context_length, max_completion_tokens, pricing}。"""
    if model_id in _meta_cache:
        return _meta_cache[model_id]

    data = _fetch_openrouter(_slug_for(model_id, slug))
    meta: Optional[dict] = None
    if data:
        top = data.get("top_provider") or {}
        context = top.get("context_length") or data.get("context_length")
        if context:
            meta = {
                "context_length": int(context),
                "max_completion_tokens": top.get("max_completion_tokens"),
                "pricing": data.get("pricing"),
            }
    _meta_cache[model_id] = meta
    return meta


def _context_from_table(model_id: str) -> Optional[int]:
    for sub, ctx in _MODEL_CONTEXT_TABLE.items():
        if sub in model_id:
            return ctx
    return None


def get_pricing(model_id: str, slug: Optional[str] = None) -> Optional[dict]:
    """返回 {'prompt': ..., 'completion': ...}（OpenRouter 原值，字符串），不可用时 None。"""
    meta = _resolve_meta(model_id, slug)
    return meta.get("pricing") if meta else None


def get_prompt_budget(model_id: str, slug: Optional[str] = None) -> int:
    """解析该模型的 prompt token 预算（带缓存，全程不抛出）。"""
    # 1. 环境变量硬覆盖（短路，不发网络）
    if override := os.getenv("LLM_MAX_PROMPT_TOKENS"):
        try:
            return int(override)
        except ValueError:
            logger.warning("LLM_MAX_PROMPT_TOKENS 非整数，忽略 | %s", override)

    if model_id in _budget_cache:
        return _budget_cache[model_id]

    meta = _resolve_meta(model_id, slug)

    # 2/3/4. context_length: OpenRouter → 表 → 默认
    if meta:
        context = meta["context_length"]
    else:
        context = _context_from_table(model_id) or _DEFAULT_CONTEXT

    # output_reserve: env → max_completion_tokens → 默认
    reserve_env = os.getenv("LLM_OUTPUT_RESERVE_TOKENS")
    if reserve_env and reserve_env.isdigit():
        reserve = int(reserve_env)
    elif meta and meta.get("max_completion_tokens"):
        reserve = int(meta["max_completion_tokens"])
    else:
        reserve = _DEFAULT_RESERVE

    budget = max(context - reserve, _MIN_BUDGET)
    _budget_cache[model_id] = budget
    logger.info(
        "📐 prompt 预算解析 | model=%s context=%d reserve=%d budget=%d%s",
        model_id, context, reserve, budget, "" if meta else " (回退/无OpenRouter)",
    )
    return budget
