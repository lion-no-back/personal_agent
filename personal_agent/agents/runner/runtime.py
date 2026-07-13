from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .streaming import DEFAULT_LLM_AUTH_USER_MESSAGE


@dataclass(slots=True)
class AgentRuntime:
    """Explicit runtime dependencies used by AgentRunner."""

    llm: object
    context: object
    tools_schema: list
    tool_map: dict
    scorer: object | None
    interaction_logger: object | None
    card_update_interval: float
    stream_chars_per_tick: int
    llm_auth_user_message: str
    resolve_skill_body: Callable[[str], str]
    routing_suggestion: Callable[..., str]
    make_exec_push_fn: Callable
    deliver_text: Callable[[str, str], None]

    @classmethod
    def from_agent(cls, agent) -> "AgentRuntime":
        return cls(
            llm=agent._llm,
            context=agent._context,
            tools_schema=agent._tools_schema,
            tool_map=agent._tool_map,
            scorer=agent._scorer,
            interaction_logger=agent._interaction_logger,
            card_update_interval=agent._card_update_interval,
            stream_chars_per_tick=agent._stream_chars_per_tick,
            llm_auth_user_message=getattr(
                agent,
                "_llm_auth_user_message",
                DEFAULT_LLM_AUTH_USER_MESSAGE,
            ),
            resolve_skill_body=agent._resolve_skill_body,
            routing_suggestion=agent._routing_suggestion,
            make_exec_push_fn=agent._make_exec_push_fn,
            deliver_text=agent._deliver_text,
        )

    def get_messages(self, session_key: str) -> list:
        return self.context.get_messages(session_key)

    def get_history_length(self, session_key: str) -> int:
        return self.context.get_history_length(session_key)

    def rollback_to_length(self, session_key: str, target_len: int) -> None:
        self.context.rollback_to_length(session_key, target_len)

    def add_message(
        self,
        session_key: str,
        role: str,
        content: Optional[str] = None,
        **kwargs,
    ) -> None:
        self.context.add_message(session_key, role, content=content, **kwargs)

    def get_prompt_budget(self, task_type: str) -> int:
        return self.llm.get_prompt_budget(task_type)

    def score(self, messages: list, *, user_text: str):
        if self.scorer is None:
            return None
        return self.scorer.score(
            messages,
            tools=self.tools_schema,
            user_text=user_text,
        )

    def log_llm_call(self, session_key: str, turn_index: int, messages: list) -> None:
        if self.interaction_logger:
            self.interaction_logger.log_llm_call(session_key, turn_index, messages)

    def log_llm_response(
        self,
        session_key: str,
        turn_index: int,
        full_response: str,
        tool_calls: Optional[list],
    ) -> None:
        if self.interaction_logger:
            self.interaction_logger.log_llm_response(
                session_key,
                turn_index,
                full_response,
                tool_calls,
            )

    def log_tool_result(
        self,
        session_key: str,
        tool_name: str,
        args: dict,
        result: str,
    ) -> None:
        if self.interaction_logger:
            self.interaction_logger.log_tool_result(
                session_key,
                tool_name,
                args,
                result,
            )
