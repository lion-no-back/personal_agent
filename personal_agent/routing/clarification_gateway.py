from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CLARIFY_TIMEOUT_SECONDS = 600


@dataclass
class ClarificationEntry:
    clarify_id: str
    session_key: str
    question: str
    choices: list[str] | None
    created_at: float = field(default_factory=time.monotonic)
    event: threading.Event = field(default_factory=threading.Event)
    status: str = "pending"
    response: str = ""
    reason: str = ""


_lock = threading.Lock()
_by_id: dict[str, ClarificationEntry] = {}
_by_session: dict[str, str] = {}


def build_clarification_text(question: str, choices: list[str] | None = None) -> str:
    lines = ["**需要确认**", ""]
    lines.extend(_split_question(question))
    if choices:
        lines.extend(["", "你可以回复："])
        lines.extend(
            f"{index}. {choice}"
            for index, choice in enumerate(choices[:4], 1)
            if str(choice).strip()
        )
    return "\n".join(lines).strip()


def ask_clarification(
    *,
    session_key: str,
    question: str,
    choices: list[str] | None,
    publish,
    cancel_event: threading.Event | None = None,
    timeout_seconds: int = DEFAULT_CLARIFY_TIMEOUT_SECONDS,
) -> str:
    entry = register_pending_clarification(session_key, question, choices)
    publish(build_clarification_text(question, choices), "processing")
    payload = wait_for_clarification(
        entry.clarify_id,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )
    return json.dumps(payload, ensure_ascii=False)


def register_pending_clarification(
    session_key: str,
    question: str,
    choices: list[str] | None,
) -> ClarificationEntry:
    entry = ClarificationEntry(
        clarify_id=uuid.uuid4().hex,
        session_key=session_key,
        question=question,
        choices=choices,
    )
    with _lock:
        previous_id = _by_session.get(session_key)
        if previous_id:
            previous = _by_id.get(previous_id)
            if previous and previous.status == "pending":
                previous.status = "cancelled"
                previous.reason = "superseded"
                previous.event.set()
        _by_id[entry.clarify_id] = entry
        _by_session[session_key] = entry.clarify_id
    logger.info("澄清已登记 | session=%s clarify_id=%s", session_key, entry.clarify_id)
    return entry


def wait_for_clarification(
    clarify_id: str,
    *,
    timeout_seconds: int,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1, timeout_seconds)
    while True:
        with _lock:
            entry = _by_id.get(clarify_id)
        if entry is None:
            return {"status": "cancelled", "reason": "missing_entry"}

        if entry.event.wait(timeout=0.25):
            return _consume_entry(entry)

        if cancel_event is not None and cancel_event.is_set():
            cancel_pending_clarification(
                entry.session_key,
                reason="agent_cancelled",
                clarify_id=clarify_id,
            )
            continue

        if time.monotonic() >= deadline:
            cancel_pending_clarification(
                entry.session_key,
                reason="timeout",
                status="timeout",
                clarify_id=clarify_id,
            )
            continue


def resolve_text_response_for_session(session_key: str, text: str) -> bool:
    response = (text or "").strip()
    if not response:
        return False
    entry = _get_pending_for_session(session_key)
    if entry is None:
        return False
    with _lock:
        if entry.status != "pending":
            return False
        entry.status = "answered"
        entry.response = response
        entry.event.set()
    logger.info("澄清已收到用户回复 | session=%s clarify_id=%s", session_key, entry.clarify_id)
    return True


def cancel_pending_clarification(
    session_key: str,
    *,
    reason: str,
    status: str = "cancelled",
    clarify_id: str | None = None,
) -> bool:
    entry = _get_pending_for_session(session_key)
    if entry is None:
        return False
    if clarify_id is not None and entry.clarify_id != clarify_id:
        return False
    with _lock:
        if entry.status != "pending":
            return False
        entry.status = status
        entry.reason = reason
        entry.event.set()
    logger.info(
        "澄清已取消 | session=%s clarify_id=%s reason=%s",
        session_key,
        entry.clarify_id,
        reason,
    )
    return True


def has_pending_clarification(session_key: str) -> bool:
    return _get_pending_for_session(session_key) is not None


def _get_pending_for_session(session_key: str) -> ClarificationEntry | None:
    with _lock:
        clarify_id = _by_session.get(session_key)
        entry = _by_id.get(clarify_id or "")
        if entry is None or entry.status != "pending":
            return None
        return entry


def _consume_entry(entry: ClarificationEntry) -> dict[str, Any]:
    with _lock:
        _by_id.pop(entry.clarify_id, None)
        current_id = _by_session.get(entry.session_key)
        if current_id == entry.clarify_id:
            _by_session.pop(entry.session_key, None)
    payload: dict[str, Any] = {
        "status": entry.status,
        "question": entry.question,
        "choices_offered": entry.choices or [],
    }
    if entry.status == "answered":
        payload["user_response"] = entry.response
    if entry.reason:
        payload["reason"] = entry.reason
    return payload


def _split_question(question: str) -> list[str]:
    text = str(question or "").strip()
    if not text:
        return ["请补充一下你的具体需求，我再继续处理。"]
    parts: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char in "？?":
            part = text[start : index + 1].strip()
            if part:
                parts.append(part)
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts or [text]
