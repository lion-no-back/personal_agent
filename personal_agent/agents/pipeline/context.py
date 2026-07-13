from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

ProgressPublisher = Callable[[str, str], None]


@dataclass
class PipelineContext:
    """Per-message state shared by pipeline stages."""

    event: object
    response_publisher: Optional[ProgressPublisher] = None
    stream_updates_sync_with_llm: bool = False
    push_target: Optional[str] = None
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

    def publish_progress(self, content: str, status: str) -> None:
        if self.response_publisher is not None:
            self.response_publisher(content, status)
