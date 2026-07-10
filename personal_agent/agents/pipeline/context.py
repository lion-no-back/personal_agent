from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class PipelineContext:
    """Per-message state shared by pipeline stages."""

    event: object
    on_update: Optional[Callable[[str, str], None]] = None
    stream_updates_sync_with_llm: bool = False
    push_fn: Optional[Callable[[str], None]] = None
    cancel_event: Optional[threading.Event] = None
    keep_thinking: bool = True

    session_key: str = ""
    user_id: str = ""
    memory_block: str = ""
    kb_context_block: str = ""

    final_response: str = ""
    ui_text_buffer: str = ""
    score_result: object | None = None
    task_type: str = "default"

    delivery_kind: str = ""
    response: str = ""
    stopped: bool = False

    def stop(self, response: str = "") -> None:
        self.response = response
        self.stopped = True
