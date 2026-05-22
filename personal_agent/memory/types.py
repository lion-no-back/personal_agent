from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MemoryType(str, Enum):
    PREFERENCE = "preference"  # USER.md 热层：用户偏好/风格
    EXPERIENCE = "experience"  # MEMORY.md 热层：工具经验/任务流程
    CONTEXT = "context"        # context.md 冷层：长期项目情境
    EVENT = "event"            # events.md 冷层：具体事件记录


@dataclass
class MemoryEntry:
    user_id: str
    memory_type: MemoryType
    content: str
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    created_at: datetime = field(default_factory=datetime.now)
