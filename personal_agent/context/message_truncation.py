"""消息列表的 token 预算裁剪（纯函数，无 I/O）。

为 deepseek 系等无官方 tokenizer 的 OpenAI 兼容模型提供：
- estimate_tokens：字符启发式估算单条消息 token 数
- truncate_to_budget：按 token 预算裁剪，保护 system、工具调用组成对进退、剥除孤儿 tool

详见 docs/superpowers/specs/2026-06-26-conversation-history-truncation-design.md
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

# 字符 → token 估算系数
_CJK_CHARS_PER_TOKEN = 1.5   # 中日韩表意文字更密
_ASCII_CHARS_PER_TOKEN = 4.0
_IMAGE_TOKENS = 1000         # 每张图片的固定估算
_MSG_OVERHEAD_TOKENS = 4     # 每条消息的分隔符开销


def _is_cjk(ch: str) -> bool:
    """粗判一个字符是否属于中日韩统一表意文字区间。"""
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF      # CJK 统一表意文字
        or 0x3400 <= code <= 0x4DBF   # 扩展 A
        or 0x3000 <= code <= 0x303F   # CJK 标点
        or 0xFF00 <= code <= 0xFFEF   # 全角符号
    )


def _text_tokens(text: str) -> float:
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk / _CJK_CHARS_PER_TOKEN + other / _ASCII_CHARS_PER_TOKEN


def estimate_tokens(message: Dict[str, Any]) -> int:
    """字符启发式估算单条消息的 token 数（含固定开销）。"""
    text = ""
    images = 0

    content = message.get("content")
    if isinstance(content, str):
        text += content
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text += part.get("text", "") or ""
            elif part.get("type") == "image_url":
                images += 1

    # assistant 的工具调用：name + arguments 的 JSON 串
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        text += str(fn.get("name", "")) + str(fn.get("arguments", ""))

    # tool 消息的辅助字段
    text += str(message.get("name", "")) + str(message.get("tool_call_id", ""))

    return int(_text_tokens(text)) + images * _IMAGE_TOKENS + _MSG_OVERHEAD_TOKENS


def _strip_leading_orphan_tools(body: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """剥除开头的孤儿 tool 消息（其 assistant(tool_calls) 已不在窗口内）。"""
    i = 0
    while i < len(body) and body[i].get("role") == "tool":
        i += 1
    return body[i:]


def truncate_to_budget(
    messages: List[Dict[str, Any]], max_tokens: int
) -> List[Dict[str, Any]]:
    """按 token 预算裁剪消息列表。

    - system（首条）单独保护，永不裁剪。
    - 无条件剥除开头的孤儿 tool 消息（修复 OpenAI 400）。
    - 从最旧开始丢；丢到 assistant(tool_calls) 时连带丢其后配对的 tool 消息。
    - 不做半截截断（最小范围）。
    """
    if not messages:
        return messages

    has_system = messages[0].get("role") == "system"
    system = messages[:1] if has_system else []
    body = list(messages[1:]) if has_system else list(messages)

    body = _strip_leading_orphan_tools(body)

    def total() -> int:
        return sum(estimate_tokens(m) for m in system) + sum(estimate_tokens(m) for m in body)

    while body and total() > max_tokens and len(body) > 1:
        dropped = body.pop(0)
        # 丢弃带 tool_calls 的 assistant 时，连带丢掉其后配对的 tool 消息
        if dropped.get("tool_calls"):
            while body and body[0].get("role") == "tool":
                body.pop(0)
        body = _strip_leading_orphan_tools(body)

    return system + body
