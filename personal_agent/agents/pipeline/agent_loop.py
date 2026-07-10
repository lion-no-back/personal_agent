from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import os

from ...context.message_truncation import truncate_to_budget
from ...core.json_repair import repair_tool_arguments
from ...core.task_manager import TaskCancelledError
from ...guardrails import sanitize_tool_result
from ...tools.validation import validate_tool_args
from ..conversation_runtime import FORCE_REASONING_PATTERNS, inject_runtime_context
from .context import PipelineContext

logger = logging.getLogger(__name__)


class AgentLoopStage:
    name = "agent_loop"

    def __init__(self, agent) -> None:
        self.agent = agent

    def run(self, ctx: PipelineContext) -> None:
        event = ctx.event
        agent = self.agent

        force_reasoning = any(p in event.content for p in FORCE_REASONING_PATTERNS)

        turns_base = int(os.getenv("AGENT_MAX_TURNS_BASE", "5"))
        turns_per_tool = int(os.getenv("AGENT_TURNS_PER_TOOL", "2"))
        turns_hard = int(os.getenv("AGENT_MAX_TURNS_HARD", "30"))
        remaining = turns_base
        turn_index = 0

        while remaining > 0 and turn_index < turns_hard:
            messages = agent._context.get_messages(ctx.session_key)
            messages = inject_runtime_context(messages)

            if ctx.memory_block and messages:
                messages[0] = {
                    "role": "system",
                    "content": messages[0]["content"] + "\n\n" + ctx.memory_block,
                }

            if ctx.kb_context_block and messages and turn_index == 0:
                messages[0] = {
                    "role": "system",
                    "content": (
                        messages[0]["content"]
                        + "\n\n"
                        + "【知识库预取摘要，如已足够请直接作答，"
                        + "如需详情可调用 knowledge_search（每次提问仅调用一次）】\n"
                        + ctx.kb_context_block.removeprefix(
                            "【知识库相关内容，优先参考】\n"
                        )
                    ),
                }

            file_ctx = getattr(event, "file_context", None)
            if file_ctx and messages and messages[-1]["role"] == "user":
                last_msg = dict(messages[-1])
                last_msg["content"] = (
                    f"{file_ctx}\n\n[用户请求]\n{last_msg['content']}"
                )
                messages[-1] = last_msg

            skill_body = (
                agent._resolve_skill_body(event.content)
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

            turn_index += 1
            remaining -= 1
            logger.debug(
                "LLM 输入 | session=%s 轮次=%s 剩余=%s messages_count=%s",
                ctx.session_key,
                turn_index,
                remaining,
                len(messages),
            )

            def custom_on_update(content: str, status: str) -> None:
                if ctx.on_update:
                    combined = (
                        ctx.ui_text_buffer + content
                        if ctx.keep_thinking
                        else content
                    )
                    ctx.on_update(combined.strip(), status)

            if turn_index == 1:
                if event_images:
                    ctx.task_type = "vision"
                elif force_reasoning:
                    ctx.task_type = "reasoning"
                elif agent._scorer:
                    ctx.score_result = agent._scorer.score(
                        messages,
                        tools=agent._tools_schema,
                        user_text=event.content,
                    )
                    ctx.task_type = ctx.score_result.task_type

            messages = truncate_to_budget(
                messages,
                agent._llm.get_prompt_budget(ctx.task_type),
            )

            if agent._interaction_logger:
                agent._interaction_logger.log_llm_call(
                    ctx.session_key,
                    turn_index,
                    messages,
                )
            full_response, tool_calls = agent._stream_and_update(
                messages,
                custom_on_update,
                ctx.stream_updates_sync_with_llm,
                tools=agent._tools_schema,
                task_type=ctx.task_type,
                cancel_event=ctx.cancel_event,
            )
            if ctx.cancel_event and ctx.cancel_event.is_set():
                raise TaskCancelledError("任务已被用户取消")
            if agent._interaction_logger:
                agent._interaction_logger.log_llm_response(
                    ctx.session_key,
                    turn_index,
                    full_response,
                    tool_calls,
                )

            if full_response or tool_calls:
                agent._context.add_message(
                    ctx.session_key,
                    "assistant",
                    content=full_response,
                    tool_calls=tool_calls if tool_calls else None,
                )
            else:
                logger.warning("⚠️  LLM 返回空响应 | session=%s", ctx.session_key)
                ctx.stop("")
                return

            ctx.final_response = full_response or ""

            if tool_calls:
                additional_ui_text = (
                    ctx.final_response + "\n\n" if ctx.final_response else ""
                )

                def execute_single_tool(tool_call: dict) -> dict:
                    func_name = tool_call.get("function", {}).get("name", "")
                    func_args_str = tool_call.get("function", {}).get(
                        "arguments",
                        "{}",
                    )
                    tool_call_id = tool_call.get("id", "")
                    log_args: dict = {}

                    tool = agent._tool_map.get(func_name)
                    if not tool:
                        result = f"Error: 找不到工具 '{func_name}'"
                    else:
                        try:
                            kwargs_dict = repair_tool_arguments(func_args_str)
                            log_args = kwargs_dict.copy()
                            validation_error = validate_tool_args(tool, kwargs_dict)
                            if validation_error:
                                result = f"Error: 工具 '{func_name}' {validation_error}"
                            else:
                                if ctx.push_fn is not None:
                                    kwargs_dict["_tool_ctx"] = {
                                        "push_fn": agent._make_exec_push_fn(
                                            ctx.push_fn
                                        ),
                                    }
                                result = tool.call(**kwargs_dict)
                        except Exception as exc:
                            result = (
                                f"Error: 执行工具 '{func_name}' 失败: {str(exc)}"
                            )
                    result = sanitize_tool_result(
                        func_name,
                        result,
                        session_key=ctx.session_key,
                    )
                    return {
                        "name": func_name,
                        "id": tool_call_id,
                        "result": result,
                        "args": log_args,
                    }

                func_names = [
                    tc.get("function", {}).get("name", "") for tc in tool_calls
                ]
                status_text = (
                    f"{additional_ui_text}> 🛠️ 正在执行工具："
                    f"`{', '.join(func_names)}`..."
                )
                custom_on_update(status_text, "tool")

                results_collected = []
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(tool_calls), 5)
                ) as executor:
                    futures = [
                        executor.submit(
                            contextvars.copy_context().run,
                            execute_single_tool,
                            tool_call,
                        )
                        for tool_call in tool_calls
                    ]
                    for future in concurrent.futures.as_completed(futures):
                        results_collected.append(future.result())

                if ctx.cancel_event and ctx.cancel_event.is_set():
                    raise TaskCancelledError("任务已被用户取消")

                for result in results_collected:
                    agent._context.add_message(
                        ctx.session_key,
                        "tool",
                        content=result["result"],
                        tool_call_id=result["id"],
                        name=result["name"],
                    )
                    logger.info(
                        "🛠️ 工具执行完成 | %s -> %s...",
                        result["name"],
                        result["result"][:30],
                    )
                    if agent._interaction_logger:
                        agent._interaction_logger.log_tool_result(
                            ctx.session_key,
                            result["name"],
                            result.get("args", {}),
                            result["result"],
                        )

                bonus = turns_per_tool * len(tool_calls)
                remaining = min(remaining + bonus, turns_hard - turn_index)
                logger.debug(
                    "工具调用追加预算 +%s | 剩余=%s | i=%s hard=%s",
                    bonus,
                    remaining,
                    turn_index,
                    turns_hard,
                )

                ctx.ui_text_buffer += additional_ui_text
                continue

            logger.info(
                "✅ 回复完成 | session=%s response_len=%s",
                ctx.session_key,
                len(ctx.final_response),
            )
            ctx.ui_text_buffer += ctx.final_response
            suggestion = agent._routing_suggestion(
                ctx.score_result,
                is_scheduled=getattr(event, "is_scheduled", False),
            )
            if suggestion:
                ctx.ui_text_buffer += suggestion
            ctx.delivery_kind = "success"
            return

        timeout_msg = "\n\n> ⏰ 抱歉，经过多次尝试未能完成请求，请尝试重组您的问题。"
        ctx.ui_text_buffer += ctx.final_response + timeout_msg
        ctx.final_response = ctx.final_response + timeout_msg
        ctx.delivery_kind = "timeout"
