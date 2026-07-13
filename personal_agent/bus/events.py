"""Standard message events shared by gateways, agents, and schedulers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Sequence


def normalize_targets(targets: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(targets, str):
        value = targets.strip()
        return (value,) if value else ()
    normalized: list[str] = []
    for target in targets:
        value = str(target).strip()
        if value:
            normalized.append(value)
    return tuple(normalized)


@dataclass(frozen=True)
class InboundMessage:
    """A platform-neutral message entering the agent runtime."""

    source: str
    message_id: str
    chat_id: str
    chat_type: str
    content: str
    sender_id: str
    sender_name: str = ""
    thread_id: str | None = None
    parent_id: str | None = None
    attached_files: list[Any] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    is_scheduled: bool = False
    account_id: str = ""
    agent_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_legacy_event(self) -> SimpleNamespace:
        """Return the shape currently consumed by ConversationAgent."""

        return SimpleNamespace(
            message_id=self.message_id,
            chat_id=self.chat_id,
            chat_type=self.chat_type,
            content=self.content,
            sender_open_id=self.sender_id,
            sender_name=self.sender_name,
            thread_id=self.thread_id,
            parent_id=self.parent_id,
            attached_files=list(self.attached_files),
            images=list(self.images),
            is_scheduled=self.is_scheduled,
            account_id=self.account_id,
            agent_id=self.agent_id,
            file_context=self.metadata.get("file_context"),
            source=self.source,
            metadata=dict(self.metadata),
        )


@dataclass(frozen=True)
class OutboundText:
    targets: tuple[str, ...]
    text: str
    kind: Literal["text"] = "text"

    @classmethod
    def to(cls, targets: str | Sequence[str], text: str) -> "OutboundText":
        return cls(targets=normalize_targets(targets), text=text)


@dataclass(frozen=True)
class OutboundFile:
    targets: tuple[str, ...]
    file_path: str
    filename: str = ""
    kind: Literal["file"] = "file"

    @classmethod
    def to(
        cls,
        targets: str | Sequence[str],
        file_path: str | Path,
        filename: str = "",
    ) -> "OutboundFile":
        path = Path(file_path)
        return cls(
            targets=normalize_targets(targets),
            file_path=str(path),
            filename=filename or path.name,
        )


@dataclass(frozen=True)
class OutboundImageText:
    targets: tuple[str, ...]
    image_path: str
    text: str
    kind: Literal["image_text"] = "image_text"

    @classmethod
    def to(
        cls,
        targets: str | Sequence[str],
        image_path: str | Path,
        text: str,
    ) -> "OutboundImageText":
        return cls(
            targets=normalize_targets(targets),
            image_path=str(image_path),
            text=text,
        )


@dataclass(frozen=True)
class ProgressUpdate:
    """A user-visible progress update for an active interaction."""

    target: str
    content: str
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)
    kind: Literal["progress"] = "progress"


OutboundEvent = OutboundText | OutboundFile | OutboundImageText | ProgressUpdate


@dataclass(frozen=True)
class DeliveryAttempt:
    target: str
    gateway: str
    matched: bool
    succeeded: bool
    error: str = ""


@dataclass(frozen=True)
class DeliveryReport:
    event_kind: str
    attempts: tuple[DeliveryAttempt, ...] = ()

    @property
    def matched(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.matched)

    @property
    def succeeded(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.succeeded)

    @property
    def failed(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.matched and not attempt.succeeded)
