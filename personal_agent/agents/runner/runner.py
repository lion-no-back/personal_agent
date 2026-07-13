from __future__ import annotations

import concurrent.futures
import contextvars
import json
import logging
import os
import time

from ...context.message_truncation import truncate_to_budget
from ...core.json_repair import repair_tool_arguments
from ...core.task_manager import TaskCancelledError
from ...guardrails import sanitize_tool_result
from ...routing.clarification_gateway import ask_clarification
from ...tools.validation import validate_tool_args
from ..conversation_runtime import FORCE_REASONING_PATTERNS, inject_runtime_context
from .runtime import AgentRuntime
from .streaming import stream_and_update
from .tracing import emit_runner_event, event_attrs, tool_call_attrs
from .types import AgentRunResult, AgentRunSpec

logger = logging.getLogger(__name__)

_CLARIFY_TOOL_NAME = "clarify"


class AgentRunner:
    """Run the model/tool loop without pipeline delivery concerns."""

    def __init__(self, runtime: AgentRuntime | object) -> None:
        self.runtime = (
            runtime
            if isinstance(runtime, AgentRuntime)
            else AgentRuntime.from_agent(runtime)
        )

    def run(self, spec: AgentRunSpec) -> AgentRunResult:
        result = AgentRunResult()
        run_started = time.perf_counter()
        try:
            return self._run(spec, result, run_started)
        except Exception as exc:
            if not result.trace_completed:
                emit_runner_event(
                    "runner.completed",
                    status="error",
                    duration_ms=int((time.perf_counter() - run_started) * 1000),
                    attrs={
                        "stop_reason": "runner_error",
                        "iterations": result.iterations,
                        "tools_used_count": len(result.tools_used),
                        "tool_result_count": len(result.tool_results),
                        "error_type": type(exc).__name__,
                    },
                )
                result.trace_completed = True
            raise

    def _run(
        self,
        spec: AgentRunSpec,
        result: AgentRunResult,
        run_started: float,
    ) -> AgentRunResult:
        event = spec.event
        force_reasoning = any(p in event.content for p in FORCE_REASONING_PATTERNS)
        history_len_at_start = self.runtime.get_history_length(spec.session_key)

        turns_base = int(os.getenv("AGENT_MAX_TURNS_BASE", "5"))
        turns_per_tool = int(os.getenv("AGENT_TURNS_PER_TOOL", "2"))
        turns_hard = int(os.getenv("AGENT_MAX_TURNS_HARD", "30"))
        remaining = turns_base
        turn_index = 0
        emit_runner_event(
            "runner.started",
            status="ok",
            attrs={
                **event_attrs(event),
                "session_key_present": bool(spec.session_key),
                "memory_chars": len(spec.memory_block or ""),
                "kb_context_chars": len(spec.kb_context_block or ""),
                "stream_sync": spec.stream_updates_sync_with_llm,
                "push_enabled": bool(spec.push_target),
                "turns_base": turns_base,
                "turns_per_tool": turns_per_tool,
                "turns_hard": turns_hard,
            },
        )

        while remaining > 0 and turn_index < turns_hard:
            iteration_started = time.perf_counter()
            messages = self._build_messages(spec, result, turn_index)
            event_images = getattr(event, "images", None)

            turn_index += 1
            result.iterations = turn_index
            remaining -= 1
            logger.debug(
                "LLM input | session=%s turn=%s remaining=%s messages_count=%s",
                spec.session_key,
                turn_index,
                remaining,
                len(messages),
            )
            emit_runner_event(
                "runner.iteration.started",
                status="ok",
                attrs={
                    "iteration": turn_index,
                    "remaining": remaining,
                    "messages_count": len(messages),
                },
            )

            if turn_index == 1:
                if event_images:
                    result.task_type = "vision"
                elif force_reasoning:
                    result.task_type = "reasoning"
                else:
                    result.score_result = self.runtime.score(
                        messages,
                        user_text=event.content,
                    )
                    if result.score_result is not None:
                        result.task_type = result.score_result.task_type

            messages = truncate_to_budget(
                messages,
                self.runtime.get_prompt_budget(result.task_type),
            )

            self.runtime.log_llm_call(spec.session_key, turn_index, messages)

            try:
                full_response, tool_calls = stream_and_update(
                    llm=self.runtime.llm,
                    messages=messages,
                    on_update=lambda content, status: self._publish_progress(
                        spec,
                        result,
                        content,
                        status,
                    ),
                    stream_updates_sync_with_llm=spec.stream_updates_sync_with_llm,
                    tools=self.runtime.tools_schema,
                    task_type=result.task_type,
                    cancel_event=spec.cancel_event,
                    card_update_interval=self.runtime.card_update_interval,
                    stream_chars_per_tick=self.runtime.stream_chars_per_tick,
                    auth_user_message=self.runtime.llm_auth_user_message,
                )
            except Exception as exc:
                emit_runner_event(
                    "runner.completed",
                    status="error",
                    duration_ms=int((time.perf_counter() - run_started) * 1000),
                    attrs={
                        "stop_reason": "llm_error",
                        "iterations": result.iterations,
                        "error_type": type(exc).__name__,
                    },
                )
                result.trace_completed = True
                raise
            emit_runner_event(
                "runner.llm.completed",
                status="ok",
                duration_ms=int((time.perf_counter() - iteration_started) * 1000),
                attrs={
                    "iteration": turn_index,
                    "task_type": result.task_type,
                    "response_chars": len(full_response or ""),
                    "tool_call_count": len(tool_calls or []),
                },
            )

            if spec.cancel_event and spec.cancel_event.is_set():
                emit_runner_event(
                    "runner.completed",
                    status="cancelled",
                    duration_ms=int((time.perf_counter() - run_started) * 1000),
                    attrs={
                        "stop_reason": "cancelled",
                        "iterations": result.iterations,
                        "tools_used_count": len(result.tools_used),
                        "tool_result_count": len(result.tool_results),
                    },
                )
                result.trace_completed = True
                raise TaskCancelledError("任务已被用户取消")

            self.runtime.log_llm_response(
                spec.session_key,
                turn_index,
                full_response,
                tool_calls,
            )

            clarify_call = self._get_clarify_tool_call(tool_calls)
            if clarify_call is not None and tool_calls:
                if len(tool_calls) > 1:
                    logger.warning(
                        "clarify 与其他工具同时出现，仅执行 clarify | session=%s tools=%s",
                        spec.session_key,
                        [tc.get("function", {}).get("name", "") for tc in tool_calls],
                    )
                tool_calls = [clarify_call]

            if full_response or tool_calls:
                self.runtime.add_message(
                    spec.session_key,
                    "assistant",
                    content=full_response,
                    tool_calls=tool_calls if tool_calls else None,
                )
            else:
                logger.warning("LLM returned empty response | session=%s", spec.session_key)
                result.stopped = True
                result.response = ""
                result.stop_reason = "empty_response"
                emit_runner_event(
                    "runner.completed",
                    status="error",
                    duration_ms=int((time.perf_counter() - run_started) * 1000),
                    attrs={
                        "stop_reason": result.stop_reason,
                        "iterations": result.iterations,
                        "tools_used_count": len(result.tools_used),
                        "tool_result_count": len(result.tool_results),
                    },
                )
                result.trace_completed = True
                return result

            result.final_response = full_response or ""

            if tool_calls:
                additional_ui_text = (
                    result.final_response + "\n\n" if result.final_response else ""
                )
                tool_results = self._run_tools(
                    spec,
                    result,
                    tool_calls,
                    additional_ui_text,
                )
                result.tool_results.extend(tool_results)
                result.tools_used.extend(
                    item["name"] for item in tool_results if item.get("name")
                )

                clarify_stop = self._get_clarify_stop_status(tool_results)
                if clarify_stop:
                    rollback_target = max(0, history_len_at_start - 1)
                    self.runtime.rollback_to_length(spec.session_key, rollback_target)
                    if clarify_stop == "timeout":
                        result.final_response = (
                            "等待补充信息超时，本次请求已结束。你可以重新发送更完整的问题。"
                        )
                        result.stop_reason = "clarification_timeout"
                        result.delivery_kind = "clarification_timeout"
                        emit_status = "timeout"
                    else:
                        result.final_response = "已取消本次澄清，正在处理你的新请求。"
                        result.stop_reason = "clarification_cancelled"
                        result.delivery_kind = "cancelled"
                        emit_status = "cancelled"
                    result.ui_text_buffer += result.final_response
                    emit_runner_event(
                        "runner.completed",
                        status=emit_status,
                        duration_ms=int((time.perf_counter() - run_started) * 1000),
                        attrs={
                            "stop_reason": result.stop_reason,
                            "iterations": result.iterations,
                            "tools_used_count": len(result.tools_used),
                            "tool_result_count": len(result.tool_results),
                            "final_response_chars": len(result.final_response or ""),
                        },
                    )
                    result.trace_completed = True
                    return result

                if spec.cancel_event and spec.cancel_event.is_set():
                    emit_runner_event(
                        "runner.completed",
                        status="cancelled",
                        duration_ms=int((time.perf_counter() - run_started) * 1000),
                        attrs={
                            "stop_reason": "cancelled",
                            "iterations": result.iterations,
                            "tools_used_count": len(result.tools_used),
                            "tool_result_count": len(result.tool_results),
                        },
                    )
                    result.trace_completed = True
                    raise TaskCancelledError("任务已被用户取消")

                bonus = turns_per_tool * len(tool_calls)
                remaining = min(remaining + bonus, turns_hard - turn_index)
                logger.debug(
                    "Tool call turn budget +%s | remaining=%s | i=%s hard=%s",
                    bonus,
                    remaining,
                    turn_index,
                    turns_hard,
                )

                result.ui_text_buffer += additional_ui_text
                continue

            logger.info(
                "Reply completed | session=%s response_len=%s",
                spec.session_key,
                len(result.final_response),
            )
            result.ui_text_buffer += result.final_response
            suggestion = self.runtime.routing_suggestion(
                result.score_result,
                is_scheduled=getattr(event, "is_scheduled", False),
            )
            if suggestion:
                result.ui_text_buffer += suggestion
            result.delivery_kind = "success"
            result.stop_reason = "completed"
            emit_runner_event(
                "runner.completed",
                status="ok",
                duration_ms=int((time.perf_counter() - run_started) * 1000),
                attrs={
                    "stop_reason": result.stop_reason,
                    "iterations": result.iterations,
                    "tools_used_count": len(result.tools_used),
                    "tool_result_count": len(result.tool_results),
                    "final_response_chars": len(result.final_response or ""),
                },
            )
            result.trace_completed = True
            return result

        timeout_msg = (
            "\n\n> ⏳ 抱歉，经过多次尝试未能完成请求，请尝试重组您的问题。"
        )
        result.ui_text_buffer += result.final_response + timeout_msg
        result.final_response = result.final_response + timeout_msg
        result.delivery_kind = "timeout"
        result.stop_reason = "timeout"
        emit_runner_event(
            "runner.completed",
            status="timeout",
            duration_ms=int((time.perf_counter() - run_started) * 1000),
            attrs={
                "stop_reason": result.stop_reason,
                "iterations": result.iterations,
                "tools_used_count": len(result.tools_used),
                "tool_result_count": len(result.tool_results),
                "final_response_chars": len(result.final_response or ""),
            },
        )
        result.trace_completed = True
        return result

    def _build_messages(
        self,
        spec: AgentRunSpec,
        result: AgentRunResult,
        turn_index: int,
    ) -> list:
        event = spec.event
        messages = self.runtime.get_messages(spec.session_key)
        messages = inject_runtime_context(messages)

        if spec.memory_block and messages:
            messages[0] = {
                "role": "system",
                "content": messages[0]["content"] + "\n\n" + spec.memory_block,
            }

        if spec.kb_context_block and messages and turn_index == 0:
            messages[0] = {
                "role": "system",
                "content": (
                    messages[0]["content"]
                    + "\n\n"
                    + "【知识库预取摘要，如已足够请直接作答，"
                    + "如需详情可调用 knowledge_search（每次提问仅调用一次）】\n"
                    + spec.kb_context_block.removeprefix(
                        "【知识库相关内容，优先参考】\n"
                    )
                ),
            }

        file_ctx = getattr(event, "file_context", None)
        if file_ctx and messages and messages[-1]["role"] == "user":
            last_msg = dict(messages[-1])
            last_msg["content"] = f"{file_ctx}\n\n[用户请求]\n{last_msg['content']}"
            messages[-1] = last_msg

        skill_body = (
            self.runtime.resolve_skill_body(event.content)
            if turn_index == 0 and not getattr(event, "is_scheduled", False)
            else ""
        )
        if skill_body and messages and messages[-1]["role"] == "user":
            last_msg = dict(messages[-1])
            last_msg["content"] = (
                f"【当前技能完整文档】\n{skill_body}\n\n{last_msg['content']}"
            )
            messages[-1] = last_msg

        event_images = getattr(event, "images", None)
        if (
            turn_index == 0
            and event_images
            and messages
            and messages[-1]["role"] == "user"
        ):
            last_msg = dict(messages[-1])
            text = last_msg.get("content") or "请描述这张图片"
            last_msg["content"] = [
                {"type": "text", "text": text},
                *[
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    }
                    for b64 in event_images
                ],
            ]
            messages[-1] = last_msg

        return messages

    def _publish_progress(
        self,
        spec: AgentRunSpec,
        result: AgentRunResult,
        content: str,
        status: str,
    ) -> None:
        if spec.response_publisher is None:
            return
        combined = result.ui_text_buffer + content if spec.keep_thinking else content
        spec.response_publisher(combined.strip(), status)

    def _run_tools(
        self,
        spec: AgentRunSpec,
        result: AgentRunResult,
        tool_calls: list,
        additional_ui_text: str,
    ) -> list[dict]:
        func_names = [
            tc.get("function", {}).get("name", "") for tc in tool_calls
        ]
        has_clarify = _CLARIFY_TOOL_NAME in func_names
        if not has_clarify:
            status_text = (
                f"{additional_ui_text}> 🛠️ 正在执行工具："
                f"`{', '.join(func_names)}`..."
            )
            self._publish_progress(spec, result, status_text, "tool")

        results_collected = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(tool_calls), 5)
        ) as executor:
            futures = [
                executor.submit(
                    contextvars.copy_context().run,
                    self._execute_single_tool,
                    spec,
                    result,
                    tool_call,
                )
                for tool_call in tool_calls
            ]
            for future in concurrent.futures.as_completed(futures):
                results_collected.append(future.result())

        for tool_result in results_collected:
            self.runtime.add_message(
                spec.session_key,
                "tool",
                content=tool_result["result"],
                tool_call_id=tool_result["id"],
                name=tool_result["name"],
            )
            logger.info(
                "Tool execution completed | %s -> %s...",
                tool_result["name"],
                tool_result["result"][:30],
            )
            self.runtime.log_tool_result(
                spec.session_key,
                tool_result["name"],
                tool_result.get("args", {}),
                tool_result["result"],
            )
        return results_collected

    @staticmethod
    def _get_clarify_tool_call(tool_calls: list | None) -> dict | None:
        for tool_call in tool_calls or []:
            if tool_call.get("function", {}).get("name") == _CLARIFY_TOOL_NAME:
                return tool_call
        return None

    @staticmethod
    def _get_clarify_stop_status(tool_results: list[dict]) -> str:
        for tool_result in tool_results:
            if tool_result.get("name") != _CLARIFY_TOOL_NAME:
                continue
            try:
                payload = json.loads(tool_result.get("result", ""))
            except Exception:
                continue
            status = payload.get("status")
            if status in {"cancelled", "timeout"}:
                return str(status)
        return ""

    def _execute_single_tool(
        self,
        spec: AgentRunSpec,
        result: AgentRunResult,
        tool_call: dict,
    ) -> dict:
        started = time.perf_counter()
        func_name = tool_call.get("function", {}).get("name", "")
        func_args_str = tool_call.get("function", {}).get("arguments", "{}")
        tool_call_id = tool_call.get("id", "")
        log_args: dict = {}
        trace_attrs = tool_call_attrs(tool_call)
        error_kind = ""
        emit_runner_event(
            "runner.tool.started",
            status="ok",
            attrs=trace_attrs,
        )

        tool = self.runtime.tool_map.get(func_name)
        if not tool:
            tool_result = f"Error: 找不到工具 '{func_name}'"
            error_kind = "missing_tool"
        else:
            try:
                kwargs_dict = repair_tool_arguments(func_args_str)
                log_args = kwargs_dict.copy()
                validation_error = validate_tool_args(tool, kwargs_dict)
                if validation_error:
                    tool_result = f"Error: 工具 '{func_name}' {validation_error}"
                    error_kind = "validation"
                else:
                    if func_name == _CLARIFY_TOOL_NAME:
                        kwargs_dict["_tool_ctx"] = {
                            "clarify_callback": lambda question, choices: ask_clarification(
                                session_key=spec.session_key,
                                question=question,
                                choices=choices,
                                publish=lambda text, status: self._publish_progress(
                                    spec,
                                    result,
                                    text,
                                    status,
                                ),
                                cancel_event=spec.cancel_event,
                            )
                        }
                    elif spec.push_target:
                        kwargs_dict["_tool_ctx"] = {
                            "push_fn": self.runtime.make_exec_push_fn(
                                lambda msg: self.runtime.deliver_text(
                                    spec.push_target,
                                    msg,
                                )
                            ),
                        }
                    tool_result = tool.call(**kwargs_dict)
            except Exception as exc:
                tool_result = f"Error: 执行工具 '{func_name}' 失败: {str(exc)}"
                error_kind = type(exc).__name__

        tool_result = sanitize_tool_result(
            func_name,
            tool_result,
            session_key=spec.session_key,
        )
        succeeded = not (isinstance(tool_result, str) and tool_result.startswith("Error:"))
        emit_runner_event(
            "runner.tool.finished",
            status="ok" if succeeded else "error",
            duration_ms=int((time.perf_counter() - started) * 1000),
            attrs={
                **trace_attrs,
                "result_chars": len(str(tool_result or "")),
                "error_kind": error_kind,
            },
        )
        return {
            "name": func_name,
            "id": tool_call_id,
            "result": tool_result,
            "args": log_args,
        }
