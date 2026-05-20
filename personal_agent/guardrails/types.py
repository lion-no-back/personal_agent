import hashlib
from dataclasses import dataclass
from typing import Literal

Confidence = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
Action = Literal["PASSED", "WARNED", "REDACTED", "BLOCKED"]


@dataclass
class GuardResult:
    passed: bool
    action: Action = "PASSED"
    layer: str = ""
    rule: str = ""
    confidence: Confidence = "LOW"
    user_message: str = ""    # shown to user when action is BLOCKED


@dataclass
class AuditEvent:
    ts: str
    session_key: str
    layer: str
    rule: str
    action: Action
    confidence: Confidence
    raw_text: str             # plaintext content that triggered the rule
    sanitized_preview: str


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
