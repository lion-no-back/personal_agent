"""
Guardrail pattern registry.
All pattern lists are pre-compiled at import time for efficiency.
"""
import os
import re
from typing import Optional


def _c(raw: list) -> list:
    """Compile a list of (pattern_str, rule_name) into (compiled, rule_name)."""
    return [(re.compile(p), name) for p, name in raw]


# ── L2: Exec pre-execution hard denylist ────────────────────────────────────
# These are always active regardless of configuration.
_EXEC_DENY_HARD = _c([
    # Recursive file deletion (Unix/Linux)
    (r"(?i)\brm\b[^|;&\n]*-[a-z]*r[a-z]*", "DESTRUCT_RM_RECURSIVE"),
    # Low-level disk write (dd)
    (r"(?i)\bdd\s+(?:if|of)=", "DESTRUCT_DD"),
    # Filesystem formatting (Unix)
    (r"(?i)\bmkfs\b", "DESTRUCT_MKFS"),
    # Windows disk formatting
    (r"(?i)\bformat\s+[a-zA-Z]:", "DESTRUCT_FORMAT_WIN"),
    # Windows recursive delete
    (r"(?i)\b(?:rd|rmdir)\s+/s\b", "DESTRUCT_RMDIR_WIN"),
    # Dump all environment variables
    (r"(?i)\bprintenv\b", "SENSITIVE_PRINTENV"),
    # Read .env files via Unix shell text commands
    (r"(?i)\b(?:cat|more|less|head|tail|tee)\s+[^\s|&;\n]*\.env\b", "SENSITIVE_READ_DOTENV"),
    # Read .env files via Windows cmd
    (r"(?i)\btype\s+[^\s|&;\n]*\.env\b", "SENSITIVE_READ_DOTENV_WIN"),
    # Read .env files via PowerShell
    (r"(?i)\bget-content\s+[^\s|&;\n]*\.env\b", "SENSITIVE_READ_DOTENV_PS"),
])

# ── L2: Exec optional denylist (disabled by default) ────────────────────────
# Enable with GUARDRAIL_EXEC_BLOCK_PKG_INSTALL=true.
# Disabled by default to preserve compatibility with the skill system
# which uses `uv pip install -r requirements.txt`.
_EXEC_DENY_PKG = _c([
    # pip / pip3 install <package> — note: pip install -r is NOT blocked
    (r"(?i)\bpip3?\s+install\s+(?!-r\b)", "PKG_INSTALL_PIP"),
    (r"(?i)\bnpm\s+install\b", "PKG_INSTALL_NPM"),
    (r"(?i)\bapt(?:-get)?\s+install\b", "PKG_INSTALL_APT"),
    (r"(?i)\byum\s+install\b", "PKG_INSTALL_YUM"),
    # uv add <package> (uv package manager)
    (r"(?i)\buv\s+add\b", "PKG_INSTALL_UV_ADD"),
    # conda install <package>
    (r"(?i)\bconda\s+install\b", "PKG_INSTALL_CONDA"),
])


_exec_denylist_cache: dict = {}


def get_exec_denylist() -> list:
    """Return the active exec denylist based on current env config (cached per env value)."""
    raw = os.getenv("GUARDRAIL_EXEC_BLOCK_PKG_INSTALL", "false").strip().lower()
    if raw not in _exec_denylist_cache:
        _exec_denylist_cache[raw] = (
            _EXEC_DENY_HARD + _EXEC_DENY_PKG
            if raw in ("1", "true", "yes", "on")
            else _EXEC_DENY_HARD
        )
    return _exec_denylist_cache[raw]


# ── L3: Read file path denylist ──────────────────────────────────────────────
_BLOCKED_PATHS = _c([
    (r"(?i)(^|[/\\])\.env(\.[a-zA-Z0-9]+)?$", "BLOCKED_PATH_DOTENV"),
    (r"(?i)(^|[/\\])[^/\\]*secret[s]?[^/\\]*\.(ya?ml|json|env|cfg|ini|conf|toml)$", "BLOCKED_PATH_SECRET"),
    (r"(?i)(^|[/\\])[^/\\]*credential[s]?[^/\\]*\.(ya?ml|json|env|cfg|ini|conf|toml)$", "BLOCKED_PATH_CREDENTIAL"),
    (r"(?i)(^|[/\\])[^/\\]*password[s]?[^/\\]*\.(ya?ml|json|env|cfg|ini|conf|toml)$", "BLOCKED_PATH_PASSWORD"),
    (r"(?i)\.(pem|key|p12|pfx|pkcs12|jks)$", "BLOCKED_PATH_CERT"),
])


_blocked_path_cache: dict = {}  # env_raw -> combined pattern list


def is_blocked_path(path: str) -> Optional[str]:
    """Return the rule name if the path is on the denylist, else None."""
    extra_raw = os.getenv("GUARDRAIL_BLOCKED_PATHS", "").strip()
    if extra_raw not in _blocked_path_cache:
        extra = (
            [
                (re.compile(p.strip()), "BLOCKED_PATH_CUSTOM")
                for p in extra_raw.split(",")
                if p.strip()
            ]
            if extra_raw
            else []
        )
        _blocked_path_cache[extra_raw] = _BLOCKED_PATHS + extra
    for pattern, rule in _blocked_path_cache[extra_raw]:
        if pattern.search(path):
            return rule
    return None


# ── L1: Input injection / jailbreak patterns ────────────────────────────────
_INPUT_INJECTION = _c([
    # English: classic jailbreak phrases
    (r"(?i)\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?|rules?)\b", "INJECTION_IGNORE_PREV"),
    (r"(?i)\bforget\s+(?:all\s+)?(?:previous|prior|above|your)\s+(?:instructions?|prompts?|context|training)\b", "INJECTION_FORGET"),
    (r"(?i)\bact\s+as\s+(?:a\s+|an\s+)?(?:DAN|evil|jailbroken|unrestricted|unfiltered)\b", "INJECTION_ACT_AS"),
    (r"(?i)\bjailbreak(?:ed)?\b", "INJECTION_JAILBREAK_KW"),
    (r"(?i)\bdo\s+anything\s+now\b", "INJECTION_DAN"),
    # Chinese: ignore/forget/role-switch jailbreak
    (r"忽略.{0,10}(?:之前|前面|所有|上述).{0,5}(?:指令|提示词|规则|约束|限制)", "INJECTION_CN_IGNORE"),
    (r"(?:现在你是|你要扮演|角色扮演).{0,20}(?:没有限制|无限制|不受约束|完全自由)", "INJECTION_CN_ROLE"),
    (r"(?:忘记|抛开|无视).{0,5}(?:你的)?.{0,5}(?:指令|限制|规则|约束|身份)", "INJECTION_CN_FORGET"),
])


def check_input_injection(text: str) -> Optional[tuple]:
    """Return (rule, matched_text) if injection is detected, else None."""
    for pattern, rule in _INPUT_INJECTION:
        m = pattern.search(text)
        if m:
            return (rule, m.group(0))
    return None


# ── L4/L5: High-confidence credential patterns ──────────────────────────────
_CRED_PATTERNS_HIGH = _c([
    # env-var style key=value assignment
    (
        r"(?i)(?:api_?key|api_?secret|access_?key|secret_?key|auth_?token"
        r"|access_?token|app_?secret|app_?key)\s*[=:]\s*\S{8,}",
        "CRED_KV_ASSIGN",
    ),
    # OpenAI API key prefixes
    (r"\bsk-[a-zA-Z0-9]{20,}\b", "KEY_OPENAI"),
    (r"\bsk-proj-[a-zA-Z0-9_-]{20,}\b", "KEY_OPENAI_PROJ"),
    # AWS access key
    (r"\bAKIA[A-Z0-9]{16}\b", "KEY_AWS_ACCESS"),
    # GitHub personal access tokens
    (r"\bghp_[a-zA-Z0-9]{36,}\b", "KEY_GITHUB_PAT"),
    (r"\bghs_[a-zA-Z0-9]{36,}\b", "KEY_GITHUB_SERVER"),
    # Slack tokens
    (r"\bxoxb-[0-9A-Za-z-]{24,}\b", "KEY_SLACK_BOT"),
    (r"\bxoxp-[0-9A-Za-z-]{24,}\b", "KEY_SLACK_USER"),
])

REDACT_PLACEHOLDER = os.getenv("GUARDRAIL_REDACT_PLACEHOLDER", "[内容已屏蔽]")


def redact_credentials(text: str) -> tuple:
    """
    Replace HIGH-confidence credential patterns with REDACT_PLACEHOLDER.
    Returns (sanitized_text, list_of_triggered_rule_names).
    """
    result = text
    triggered = []
    for pattern, rule in _CRED_PATTERNS_HIGH:
        new_result, n = pattern.subn(REDACT_PLACEHOLDER, result)
        if n > 0:
            triggered.append(rule)
            result = new_result
    return result, triggered
