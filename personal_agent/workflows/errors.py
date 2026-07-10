"""Structured workflow errors for subprocess payloads."""

from __future__ import annotations

from typing import Any


class WorkflowBusinessError(Exception):
    """An expected workflow failure that can be shown to users safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "WORKFLOW_BUSINESS_ERROR",
        user_message: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.user_message = user_message or message
        self.retryable = retryable
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": f"{self.code}: {self}",
            "error_code": self.code,
            "error_type": self.__class__.__name__,
            "message": self.user_message,
            "user_message": self.user_message,
            "retryable": self.retryable,
            "details": self.details,
        }


def mabang_export_failed_error(workflow_name: str, failed_sites: list[str]) -> WorkflowBusinessError:
    sites_text = "、".join(failed_sites) if failed_sites else "未知站点"
    user_message = (
        f"❌【{workflow_name}】马帮数据导出失败\n"
        f"失败站点：{sites_text}\n"
        "可能原因：马帮导出或文件下载链路不稳定，例如 SSL 连接被服务端提前断开、网络抖动，或马帮服务短暂异常。\n"
        "系统已处理：失败步骤已按 5s、10s 自动重试。\n"
        "建议：稍后重新触发；如果连续失败，请到马帮后台检查对应站点是否能正常导出和下载。"
    )
    return WorkflowBusinessError(
        f"Mabang export failed for sites: {sites_text}",
        code="MABANG_EXPORT_FAILED",
        user_message=user_message,
        retryable=True,
        details={
            "stage": "mabang_export",
            "failed_sites": failed_sites,
            "workflow_name": workflow_name,
        },
    )


def mabang_network_failed_error(workflow_name: str, exc: BaseException) -> WorkflowBusinessError:
    user_message = (
        f"❌【{workflow_name}】马帮连接失败\n"
        "失败阶段：登录、激活 AMZ 会话或导出前置请求。\n"
        "可能原因：网络抖动、SSL 连接被服务端提前断开，或马帮服务短暂异常。\n"
        "系统已处理：失败步骤已按 5s、10s 自动重试。\n"
        "建议：稍后重新触发；如果连续失败，请先确认马帮后台可以正常打开并登录。"
    )
    return WorkflowBusinessError(
        f"Mabang network step failed: {type(exc).__name__}: {exc}",
        code="MABANG_NETWORK_FAILED",
        user_message=user_message,
        retryable=True,
        details={
            "stage": "mabang_network",
            "workflow_name": workflow_name,
            "exception_type": type(exc).__name__,
            "exception": str(exc),
        },
    )
