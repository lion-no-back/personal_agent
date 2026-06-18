import os


class AccessPolicy:
    """基于环境变量白名单的准入决策：决定「谁能触发机器人」。

    白名单来源：{PLATFORM}_ALLOWED_USERS（逗号分隔的用户 ID 列表）。
    - 钉钉：DINGTALK_ALLOWED_USERS（裸 staffId）
    - 飞书：FEISHU_ALLOWED_USERS（open_id，ou_xxx）

    未配置时放行所有，兼容现有部署（避免升级后机器人突然对所有人沉默）。
    """

    def __init__(self, allowed_users: frozenset[str]) -> None:
        self._allowed = allowed_users

    @classmethod
    def from_env(cls, platform: str) -> "AccessPolicy":
        raw = os.getenv(f"{platform.upper()}_ALLOWED_USERS", "")
        return cls(frozenset(u.strip() for u in raw.split(",") if u.strip()))

    @property
    def is_open(self) -> bool:
        """True 表示未配置白名单（放行所有）。供网关决定是否打告警。"""
        return not self._allowed

    def is_allowed(self, user_id: str) -> bool:
        return self.is_open or user_id in self._allowed
