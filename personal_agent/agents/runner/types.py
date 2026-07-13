from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

ProgressPublisher = Callable[[str, str], None]


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for one model/tool loop execution."""

    event: object
    session_key: str
    memory_block: str = ""
    kb_context_block: str = ""
    response_publisher: Optional[ProgressPublisher] = None
    stream_updates_sync_with_llm: bool = False
    push_target: Optional[str] = None
    cancel_event: Optional[threading.Event] = None
    keep_thinking: bool = True


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of one model/tool loop execution."""

    final_response: str = ""
    ui_text_buffer: str = ""
    score_result: object | None = None
    task_type: str = "default"
    delivery_kind: str = ""
    response: str = ""
    stopped: bool = False
    stop_reason: str = ""
    tool_results: list[dict] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    iterations: int = 0
    trace_completed: bool = False
