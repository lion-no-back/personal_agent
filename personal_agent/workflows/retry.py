"""Small retry helpers for workflow IO steps."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

import requests

T = TypeVar("T")

STEP_RETRY_DELAYS = (5, 10)
WORKFLOW_RETRY_DELAYS = (5,)

RETRYABLE_REQUEST_EXCEPTIONS = (
    requests.exceptions.SSLError,
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def is_retryable_request_error(exc: BaseException) -> bool:
    if isinstance(exc, RETRYABLE_REQUEST_EXCEPTIONS):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        return response is not None and response.status_code in RETRYABLE_STATUS_CODES
    return False


def retry_step(
    action: Callable[[], T],
    *,
    label: str,
    delays: tuple[int, ...] = STEP_RETRY_DELAYS,
    is_retryable: Callable[[BaseException], bool] = is_retryable_request_error,
) -> T:
    """Run a workflow IO step once, then retry after each delay if retryable."""
    for attempt in range(len(delays) + 1):
        try:
            return action()
        except Exception as exc:
            if attempt >= len(delays) or not is_retryable(exc):
                raise
            delay = delays[attempt]
            print(
                f"[WARN] {label} failed on attempt {attempt + 1}/{len(delays) + 1}: "
                f"{type(exc).__name__}: {exc}; retrying in {delay}s"
            )
            time.sleep(delay)
    raise RuntimeError(f"{label} retry loop ended unexpectedly")
