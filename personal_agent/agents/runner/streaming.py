from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ..conversation_runtime import is_llm_auth_error

logger = logging.getLogger(__name__)

DEFAULT_LLM_AUTH_USER_MESSAGE = (
    "⚠️ **无法调用 AI 服务**：接口密钥未通过验证。\n\n"
    "请检查环境变量 <text_tag color='neutral'>LLM_API_KEY</text_tag> 与 "
    "<text_tag color='neutral'>LLM_BASE_URL</text_tag> 是否属于同一服务商且填写正确，"
    "修改后需重启服务。"
)


def _format_llm_error(exc: Exception) -> str:
    return (
        "抱歉，AI 调用过程中出现了错误，请稍后重试。\n\n"
        f"> 错误信息：`{exc}`"
    )


def stream_and_update(
    *,
    llm,
    messages: list,
    on_update: Optional[Callable[[str, str], None]],
    stream_updates_sync_with_llm: bool = False,
    tools: Optional[list] = None,
    task_type: str = "default",
    cancel_event: Optional[threading.Event] = None,
    card_update_interval: float = 0.3,
    stream_chars_per_tick: int = 10,
    auth_user_message: str = DEFAULT_LLM_AUTH_USER_MESSAGE,
) -> tuple[str, Optional[list]]:
    """Call the LLM stream and publish throttled progress frames."""
    buf: dict = {"text": "", "error": None, "has_tools": False}
    stop = threading.Event()
    has_content = threading.Event()
    flusher_thread: Optional[threading.Thread] = None

    if on_update and stream_updates_sync_with_llm:
        try:
            full_response = ""
            final_tool_calls = None
            gen = llm.think_stream(messages, tools=tools, task_type=task_type)
            for _delta, accumulated, tcs in gen:
                if cancel_event and cancel_event.is_set():
                    gen.close()
                    return full_response, None
                full_response = accumulated
                final_tool_calls = tcs
                if accumulated:
                    on_update(accumulated, "generating")
            if not final_tool_calls:
                on_update(full_response or "", "generating")
            return full_response, final_tool_calls
        except Exception as exc:
            if is_llm_auth_error(exc):
                logger.error(
                    "LLM API authentication failed: %s",
                    exc,
                    exc_info=False,
                )
                on_update(auth_user_message, "error")
                return "", None
            error_msg = _format_llm_error(exc)
            on_update(error_msg, "error")
            logger.error("LLM streaming call failed: %s", exc, exc_info=True)
            raise

    if on_update:

        def _flusher() -> None:
            display_idx = 0
            has_content.wait()

            while True:
                full_text = buf["text"]
                err = buf["error"]

                if err is not None:
                    on_update(err, "error")
                    return

                llm_done = stop.is_set()
                avail = len(full_text)

                if llm_done:
                    on_update(full_text, "generating")
                    return

                if avail > display_idx:
                    display_idx = min(display_idx + stream_chars_per_tick, avail)
                    on_update(full_text[:display_idx] + " ▌", "generating")

                time.sleep(card_update_interval)

        flusher_thread = threading.Thread(
            target=_flusher,
            daemon=True,
            name="card-flusher",
        )
        flusher_thread.start()

    try:
        full_response = ""
        final_tool_calls = None
        for _delta, accumulated, tcs in llm.think_stream(
            messages,
            tools=tools,
            task_type=task_type,
        ):
            full_response = accumulated
            final_tool_calls = tcs
            if tcs:
                buf["has_tools"] = True
            if accumulated:
                buf["text"] = accumulated
            has_content.set()

        buf["text"] = full_response
        return full_response, final_tool_calls

    except Exception as exc:
        if is_llm_auth_error(exc):
            logger.error(
                "LLM API authentication failed: %s",
                exc,
                exc_info=False,
            )
            buf["error"] = auth_user_message
            return "", None

        error_msg = _format_llm_error(exc)
        buf["error"] = error_msg
        logger.error("LLM streaming call failed: %s", exc, exc_info=True)
        raise

    finally:
        if flusher_thread is not None:
            has_content.set()
            stop.set()
            flusher_thread.join(timeout=30)
