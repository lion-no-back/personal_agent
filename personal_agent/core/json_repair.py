"""
Tool-call argument JSON repair.

LLMs occasionally emit slightly malformed JSON in tool call arguments.
This module applies a sequence of cheap fixups before giving up.

Repair ladder (each step only runs if the previous failed, and cumulative
mutations carry forward so e.g. trailing-comma + unquoted-key can both be
fixed in a single pass):
  1. json.loads — fast path, no allocation
  2. Strip trailing commas before } or ]
  3. Quote unquoted identifier keys  ({key: → {"key":)
  4. Swap single-quoted strings (only when raw had no double-quotes)
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

_RE_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_RE_UNQUOTED_KEY = re.compile(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:')


def _try_parse(s: str) -> dict | None:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def repair_tool_arguments(raw: str) -> dict:
    """Parse tool-call arguments JSON, applying lightweight repairs on failure.

    Raises ValueError if all repair attempts fail, so callers get a meaningful
    error rather than silently receiving {} and failing on missing arguments.
    """
    raw = raw.strip()
    if not raw:
        return {}

    if (result := _try_parse(raw)) is not None:
        return result

    # Steps 2 and 3 are applied cumulatively: a string with both trailing commas
    # and unquoted keys needs both transforms before it parses cleanly.
    repaired = _RE_TRAILING_COMMA.sub(r"\1", raw)
    if (result := _try_parse(repaired)) is not None:
        logger.warning("json_repair: fixed trailing comma | raw=%r", raw[:120])
        return result

    repaired = _RE_UNQUOTED_KEY.sub(r'\1"\2":', repaired)
    if (result := _try_parse(repaired)) is not None:
        logger.warning("json_repair: fixed trailing comma + unquoted keys | raw=%r", raw[:120])
        return result

    # Single-quote swap: guard against raw (not repaired) to avoid false negatives
    # when steps 2/3 injected double-quotes into repaired.
    if "'" in raw and '"' not in raw:
        if (result := _try_parse(repaired.replace("'", '"'))) is not None:
            logger.warning("json_repair: fixed single-quoted strings | raw=%r", raw[:120])
            return result

    raise ValueError(f"json_repair: all repairs failed | raw={raw[:200]!r}")
