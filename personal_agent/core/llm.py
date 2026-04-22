import logging
import os
from typing import Any, Dict, Generator, List, Optional, Tuple

from openai import OpenAI

logger = logging.getLogger(__name__)


class PersonalAgentLLM:
    """
    大语言模型客户端，封装 OpenAI Compatible API 的调用。
    支持流式（think_stream）和非流式（think）两种调用方式。
    """

    def __init__(
        self,
        model: Optional[str] = None,
        apikey: Optional[str] = None,
        baseurl: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        # 优先使用传入参数，回退到环境变量
        self.model: Optional[str] = model or os.getenv("LLM_MODEL_ID")
        self.apikey: Optional[str] = apikey or os.getenv("LLM_API_KEY")
        self.baseurl: Optional[str] = baseurl or os.getenv("LLM_BASE_URL")

        _timeout_env = os.getenv("LLM_TIMEOUT", "120")
        self.timeout: int = timeout if timeout is not None else int(_timeout_env)

        if not all([self.model, self.apikey, self.baseurl]):
            raise ValueError(
                "模型ID、API秘钥和服务地址均为必填项，"
                "请通过参数传入或在 .env 文件中定义 "
                "LLM_MODEL_ID、LLM_API_KEY、LLM_BASE_URL。"
            )

        # 注意：使用 self.xxx，修复原始代码中传入 None 的 bug
        self.client = OpenAI(
            api_key=self.apikey,
            base_url=self.baseurl,
            timeout=self.timeout,
        )

        logger.info(
            f"✅ LLM 客户端初始化完成 | 模型: {self.model} | 服务: {self.baseurl}"
        )

    def think_stream(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Any = "auto",
    ) -> Generator[Tuple[str, str, Optional[List[Dict[str, Any]]]], None, None]:
        """
        流式调用大语言模型，逐块 yield 结果。支持 tool_calls 解析拼接。

        Args:
            messages:    消息列表，格式为 [{"role": "...", "content": "..."}]
            temperature: 生成温度，默认 0.7
            tools:       可选的工具列表 schema
            tool_choice: 工具选择策略

        Yields:
            (delta, accumulated, tool_calls) 元组：
            - delta:       本次新增的文本片段（可能是 None）
            - accumulated: 截止当前已累积的完整文本
            - tool_calls:  截止当前已被拼接的完整 tool_calls 列表


        Raises:
            Exception: LLM API 调用异常时向上抛出，由调用方处理
        """
        if not self.model:
            raise RuntimeError("模型ID未初始化，请检查配置")

        logger.info(f"🧠 流式调用 {self.model} | 消息轮数: {len(messages)}")

        kwargs = {
            "model": self.model,
            "messages": messages,  # type: ignore[arg-type]
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        response = self.client.chat.completions.create(**kwargs)

        accumulated = ""
        tool_calls: List[Dict[str, Any]] = []

        for chunk in response:
            if not chunk.choices:
                continue

            delta_obj = chunk.choices[0].delta

            # 处理 tool_calls 增量拼接
            if hasattr(delta_obj, "tool_calls") and delta_obj.tool_calls:
                for tc_chunk in delta_obj.tool_calls:
                    idx = tc_chunk.index
                    # 确保 tool_calls 列表长度足够
                    while len(tool_calls) <= idx:
                        tool_calls.append({
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""}
                        })
                    
                    tc = tool_calls[idx]
                    if tc_chunk.id:
                        tc["id"] += tc_chunk.id
                    if tc_chunk.type:
                        tc["type"] = tc_chunk.type
                    if tc_chunk.function:
                        if tc_chunk.function.name:
                            tc["function"]["name"] += tc_chunk.function.name
                        if tc_chunk.function.arguments:
                            tc["function"]["arguments"] += tc_chunk.function.arguments

            delta = delta_obj.content
            if delta:
                accumulated += delta
            
            # 无论是否有文字输出，有 tools 或文字更新时都向外 yield 一次最新全貌
            if delta is not None or tool_calls:
                yield delta or "", accumulated, tool_calls if tool_calls else None

        logger.info(f"✅ 流式响应完成 | 共 {len(accumulated)} 字符")

    def think(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
    ) -> Optional[str]:
        """
        非流式调用大语言模型，等待完整响应后返回。
        内部复用 think_stream()，保持逻辑一致。

        Args:
            messages:    消息列表
            temperature: 生成温度，默认 0.7

        Returns:
            完整的模型响应文本，调用失败时返回 None
        """
        logger.info(f"🧠 非流式调用 {self.model} | 消息轮数: {len(messages)}")

        try:
            full_text = ""
            for _delta, accumulated, _tool_calls in self.think_stream(messages, temperature):
                full_text = accumulated
            return full_text

        except Exception as e:
            logger.error(f"❌ 调用 LLM API 时发生错误：{e}")
            return None
