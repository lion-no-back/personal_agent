from __future__ import annotations

from typing import Protocol

from .context import PipelineContext


class PipelineStage(Protocol):
    """A single message-processing stage."""

    name: str

    def run(self, ctx: PipelineContext) -> None:
        ...
