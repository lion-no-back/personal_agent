import logging
from typing import Dict, Optional

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

from .types import ToolDef

logger = logging.getLogger(__name__)

# 按工具名缓存校验器，避免每次调用重建；schema 变化时失效重建。
# 工具定义在进程内静态固定，故缓存有意不设上限。
_validator_cache: Dict[str, Draft7Validator] = {}


def _get_validator(tool: ToolDef) -> Optional[Draft7Validator]:
    """返回工具对应的校验器；schema 非法时返回 None（由调用方 fail-open）。"""
    cached = _validator_cache.get(tool.name)
    if cached is not None and cached.schema == tool.parameters:
        return cached
    try:
        Draft7Validator.check_schema(tool.parameters)
    except SchemaError as e:
        logger.warning("工具 '%s' 的 parameters 不是合法 JSON Schema，跳过校验: %s", tool.name, e)
        return None
    validator = Draft7Validator(tool.parameters)
    _validator_cache[tool.name] = validator
    return validator


def _format_path(error) -> str:
    """把 jsonschema 错误路径格式化为可读字段名；根级返回 '(root)'。"""
    parts = [str(p) for p in error.absolute_path]
    return ".".join(parts) if parts else "(root)"


def validate_tool_args(tool: ToolDef, args: dict) -> Optional[str]:
    """
    用 tool.parameters（JSON Schema）校验 args。
    通过返回 None；失败返回可读的中文错误字符串（不含 'Error:' 前缀）。
    校验过程自身异常时 fail-open（返回 None）。
    """
    try:
        validator = _get_validator(tool)
        if validator is None:
            return None
        errors = sorted(validator.iter_errors(args), key=lambda e: list(e.absolute_path))
        if not errors:
            return None
        lines = [f"- {_format_path(e)}: {e.message}" for e in errors]
        return "参数校验失败：\n" + "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — 校验绝不阻断工具调用
        logger.warning("工具 '%s' 参数校验异常，放行: %s", tool.name, e)
        return None
