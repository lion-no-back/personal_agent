"""Agent 与渠道账号绑定配置。

配置来源沿用 config/channels.yaml，兼容旧的单钉钉机器人 .env 配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AgentConfig:
    id: str
    name: str = ""
    system_prompt: str = ""
    system_prompt_env: str = ""
    enabled: bool = True

    @property
    def resolved_system_prompt(self) -> str:
        if self.system_prompt:
            return self.system_prompt
        if self.system_prompt_env:
            return os.getenv(self.system_prompt_env, "")
        return ""


@dataclass(frozen=True)
class DingTalkAccountConfig:
    account_id: str
    agent_id: str
    client_id_env: str
    client_secret_env: str
    enabled: bool = True

    @property
    def client_id(self) -> str:
        return os.getenv(self.client_id_env, "")

    @property
    def client_secret(self) -> str:
        return os.getenv(self.client_secret_env, "")


@dataclass(frozen=True)
class AgentBindingConfig:
    agents: dict[str, AgentConfig]
    dingtalk_accounts: list[DingTalkAccountConfig]

    def get_agent(self, agent_id: str) -> AgentConfig:
        return self.agents.get(agent_id) or self.agents["default"]


def load_agent_binding_config(project_root: Path) -> AgentBindingConfig:
    """加载 Agent 与钉钉账号绑定。

    支持两种形态：
    1. 旧配置：channels.dingtalk.enabled=true + DINGTALK_CLIENT_ID/SECRET。
    2. 新配置：channels.dingtalk.accounts + 顶层 agents.list/bindings。
    """
    raw = _load_raw(project_root)
    agents = _load_agents(raw)
    dingtalk_accounts = _load_dingtalk_accounts(raw, agents)
    return AgentBindingConfig(agents=agents, dingtalk_accounts=dingtalk_accounts)


def _load_raw(project_root: Path) -> dict[str, Any]:
    channels_yaml = project_root / "config" / "channels.yaml"
    if not channels_yaml.is_file():
        return {"channels": {"feishu": {"enabled": True}}}
    with open(channels_yaml, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_agents(raw: dict[str, Any]) -> dict[str, AgentConfig]:
    result: dict[str, AgentConfig] = {}
    for item in ((raw.get("agents") or {}).get("list") or []):
        if not isinstance(item, dict):
            continue
        agent_id = str(item.get("id") or "").strip()
        if not agent_id:
            continue
        enabled = bool(item.get("enabled", True))
        if not enabled:
            continue
        result[agent_id] = AgentConfig(
            id=agent_id,
            name=str(item.get("name") or agent_id),
            system_prompt=str(item.get("system_prompt") or ""),
            system_prompt_env=str(item.get("system_prompt_env") or ""),
            enabled=enabled,
        )

    if "default" not in result:
        result["default"] = AgentConfig(id="default", name="PersonalAgent")
    return result


def _load_dingtalk_accounts(
    raw: dict[str, Any],
    agents: dict[str, AgentConfig],
) -> list[DingTalkAccountConfig]:
    channels = raw.get("channels") or {}
    dingtalk = channels.get("dingtalk") or {}
    if not dingtalk.get("enabled", False):
        return []

    binding_map = _load_binding_map(raw)
    accounts_raw = dingtalk.get("accounts") or {}
    if not isinstance(accounts_raw, dict) or not accounts_raw:
        return [
            DingTalkAccountConfig(
                account_id="default",
                agent_id=binding_map.get("default", "default"),
                client_id_env="DINGTALK_CLIENT_ID",
                client_secret_env="DINGTALK_CLIENT_SECRET",
            )
        ]

    accounts: list[DingTalkAccountConfig] = []
    for account_id, item in accounts_raw.items():
        if not isinstance(item, dict):
            item = {}
        account_id = str(account_id).strip()
        if not account_id or not bool(item.get("enabled", True)):
            continue
        agent_id = str(item.get("agent_id") or binding_map.get(account_id) or "default")
        if agent_id not in agents:
            agents[agent_id] = AgentConfig(id=agent_id, name=agent_id)
        env_prefix = f"DINGTALK_{_env_token(account_id)}"
        accounts.append(
            DingTalkAccountConfig(
                account_id=account_id,
                agent_id=agent_id,
                client_id_env=str(item.get("client_id_env") or f"{env_prefix}_CLIENT_ID"),
                client_secret_env=str(item.get("client_secret_env") or f"{env_prefix}_CLIENT_SECRET"),
            )
        )
    return accounts


def _load_binding_map(raw: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw.get("bindings") or []:
        if not isinstance(item, dict) or item.get("type", "route") != "route":
            continue
        match = item.get("match") or {}
        if match.get("channel") != "dingtalk":
            continue
        account_id = str(match.get("account_id") or match.get("accountId") or "").strip()
        agent_id = str(item.get("agent_id") or item.get("agentId") or "").strip()
        if account_id and agent_id:
            result[account_id] = agent_id
    return result


def _env_token(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value).upper()
