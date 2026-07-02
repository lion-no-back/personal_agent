"""
Guardrail audit logger — append-only JSONL, thread-safe.
Logs are stored in plaintext under logs/审计日志/{year}/{month}.{day}/audit.jsonl.
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from personal_agent.core.telemetry import emit_event

from .types import Action, AuditEvent, Confidence

logger = logging.getLogger(__name__)

_DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"

# Human-readable descriptions for every rule name, used as "未通过原因"
_RULE_DESCRIPTIONS: dict[str, str] = {
    # L1 InputGuard
    "INJECTION_IGNORE_PREV":  "提示词注入：要求忽略之前的指令",
    "INJECTION_FORGET":       "提示词注入：要求遗忘之前的指令/上下文",
    "INJECTION_ACT_AS":       "提示词注入：要求扮演无限制 AI",
    "INJECTION_JAILBREAK_KW": "包含越狱关键词 jailbreak",
    "INJECTION_DAN":          "包含越狱模式 Do Anything Now",
    "INJECTION_CN_IGNORE":    "提示词注入（中文）：要求忽略指令/规则",
    "INJECTION_CN_ROLE":      "角色扮演越狱（中文）：声称无限制身份",
    "INJECTION_CN_FORGET":    "提示词注入（中文）：要求忘记限制/身份",
    "INPUT_TOO_LONG":         "输入超过字符长度上限",
    # L2 ExecPreGuard
    "DESTRUCT_RM_RECURSIVE":  "执行危险命令：递归删除文件（rm -rf）",
    "DESTRUCT_DD":            "执行危险命令：低级磁盘写入（dd）",
    "DESTRUCT_MKFS":          "执行危险命令：格式化文件系统（mkfs）",
    "DESTRUCT_FORMAT_WIN":    "执行危险命令：格式化 Windows 磁盘",
    "DESTRUCT_RMDIR_WIN":     "执行危险命令：递归删除目录（rmdir /s）",
    "SENSITIVE_PRINTENV":     "尝试转储所有环境变量（printenv）",
    "SENSITIVE_READ_DOTENV":  "尝试通过 Shell 命令读取 .env 文件",
    "SENSITIVE_READ_DOTENV_WIN": "尝试通过 Windows 命令读取 .env 文件",
    "SENSITIVE_READ_DOTENV_PS":  "尝试通过 PowerShell 读取 .env 文件",
    "PKG_INSTALL_PIP":        "尝试安装 Python 包（pip/pip3 install）",
    "PKG_INSTALL_NPM":        "尝试安装 Node 包（npm install）",
    "PKG_INSTALL_APT":        "尝试安装系统包（apt install）",
    "PKG_INSTALL_YUM":        "尝试安装系统包（yum install）",
    "PKG_INSTALL_UV_ADD":     "尝试安装 Python 包（uv add）",
    "PKG_INSTALL_CONDA":      "尝试安装 Python 包（conda install）",
    # L3 ReadFilePreGuard
    "BLOCKED_PATH_DOTENV":    "尝试读取 .env 配置文件",
    "BLOCKED_PATH_SECRET":    "尝试读取 secret 类配置文件",
    "BLOCKED_PATH_CREDENTIAL":"尝试读取 credential 类配置文件",
    "BLOCKED_PATH_PASSWORD":  "尝试读取 password 类配置文件",
    "BLOCKED_PATH_CERT":      "尝试读取证书或私钥文件（.pem/.key 等）",
    "BLOCKED_PATH_CUSTOM":    "尝试读取自定义黑名单路径",
    # L4/L5 credential redaction
    "CRED_KV_ASSIGN":         "输出含凭证键值对（API_KEY=xxx 等）",
    "KEY_OPENAI":             "输出含 OpenAI API Key（sk-xxx）",
    "KEY_OPENAI_PROJ":        "输出含 OpenAI 项目 Key（sk-proj-xxx）",
    "KEY_AWS_ACCESS":         "输出含 AWS Access Key（AKIA...）",
    "KEY_GITHUB_PAT":         "输出含 GitHub Personal Access Token",
    "KEY_GITHUB_SERVER":      "输出含 GitHub Server Token",
    "KEY_SLACK_BOT":          "输出含 Slack Bot Token（xoxb-）",
    "KEY_SLACK_USER":         "输出含 Slack User Token（xoxp-）",
}


def _rule_description(rule: str) -> str:
    return _RULE_DESCRIPTIONS.get(rule, rule)


class GuardrailAuditLogger:
    """
    Thread-safe append-only audit logger.
    Writes to logs/审计日志/{year}/{month}.{day}/audit.jsonl.
    The date directory is computed on each write and cached until midnight.
    """

    def __init__(self, log_dir: Optional[Path] = None) -> None:
        self._log_dir = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
        self._lock = threading.Lock()
        self._enabled = (
            os.getenv("GUARDRAIL_AUDIT_ENABLED", "true").strip().lower()
            not in ("0", "false", "no", "off")
        )
        # Cache the current date string + resolved path to avoid mkdir on every write
        self._cached_date: str = ""
        self._cached_path: Optional[Path] = None

        if self._enabled:
            logger.info("GuardrailAuditLogger 就绪 | base=%s", self._log_dir / "审计日志")

    def _get_log_path(self) -> Path:
        """Return today's log file path, creating the directory if needed."""
        now = datetime.now()
        date_str = f"{now.year}/{now.month}.{now.day}"
        if date_str != self._cached_date:
            date_dir = self._log_dir / "审计日志" / str(now.year) / f"{now.month}.{now.day}"
            date_dir.mkdir(parents=True, exist_ok=True)
            self._cached_date = date_str
            self._cached_path = date_dir / "audit.jsonl"
        return self._cached_path  # type: ignore[return-value]

    def log(self, event: AuditEvent) -> None:
        if not self._enabled:
            return
        record = {
            "时间":     event.ts,
            "操作人员": event.session_key or "未知",
            "所做内容": event.raw_text,
            "未通过原因": _rule_description(event.rule),
            "层级":     event.layer,
            "规则":     event.rule,
            "动作":     event.action,
        }
        try:
            with self._lock:
                log_path = self._get_log_path()
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("审计日志写入失败: %s", e)

    def emit(
        self,
        *,
        session_key: str,
        layer: str,
        rule: str,
        action: Action,
        confidence: Confidence,
        raw_text: str,
        sanitized_preview: str,
    ) -> None:
        event = AuditEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            session_key=session_key,
            layer=layer,
            rule=rule,
            action=action,
            confidence=confidence,
            raw_text=raw_text,
            sanitized_preview=sanitized_preview[:120],
        )
        self.log(event)
        if str(action).upper() == "BLOCKED":
            emit_event(
                "guardrail.blocked",
                source="guardrail_audit",
                status="blocked",
                session_key=session_key,
                attrs={
                    "layer": layer,
                    "rule": rule,
                    "action": action,
                    "confidence": confidence,
                    "sanitized_preview": sanitized_preview,
                },
            )

    def emit_blocked(
        self,
        *,
        layer: str,
        rule: str,
        raw_text: str,
        sanitized_preview: str,
    ) -> None:
        """Convenience wrapper for L2/L3 tool guards: log WARNING + emit BLOCKED event."""
        logger.warning("[GUARDRAIL] %s BLOCKED %s | %s", layer, rule, sanitized_preview[:60])
        self.emit(
            session_key="",
            layer=layer,
            rule=rule,
            action="BLOCKED",
            confidence="CRITICAL",
            raw_text=raw_text,
            sanitized_preview=sanitized_preview,
        )


# ── Module-level singleton with lazy init ───────────────────────────────────

_instance: Optional[GuardrailAuditLogger] = None
_instance_lock = threading.Lock()


def get_audit_logger(log_dir: Optional[Path] = None) -> GuardrailAuditLogger:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = GuardrailAuditLogger(log_dir=log_dir)
        return _instance


def set_audit_logger(instance: GuardrailAuditLogger) -> None:
    global _instance
    with _instance_lock:
        _instance = instance
