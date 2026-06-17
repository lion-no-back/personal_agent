from abc import ABC, abstractmethod


class BaseGateway(ABC):
    """
    所有平台网关的统一契约。

    职责：
    - 定义 start()：阻塞当前线程维持长连接（asyncio 型网关实现为 asyncio.run(...)）
    - 定义 send_text() / send_file()：主动发送接口，供调度器 notify_fn 使用
    - 提供 make_session_key()：统一 session key 格式，防止跨平台 ID 碰撞
    """

    @staticmethod
    def make_session_key(platform: str, id_type: str, raw_id: str) -> str:
        """
        统一 session key 格式：{platform}:{id_type}:{raw_id}

        示例：
          feishu:user:ou_xxxxxxx
          feishu:group:oc_xxxxxxx
          dingtalk:user:employee123
          dingtalk:group:cid_xxxxxxx
        """
        return f"{platform}:{id_type}:{raw_id}"

    @property
    @abstractmethod
    def platform(self) -> str:
        """平台唯一标识，用作 session key 前缀，例如 'feishu' / 'dingtalk'。"""

    @abstractmethod
    def start(self) -> None:
        """
        阻塞当前线程，维持与平台服务器的长连接。
        - 同步阻塞型（飞书 lark.ws.Client）：直接调用 SDK start()
        - asyncio 型（钉钉 dingtalk-stream）：实现为 asyncio.run(self._async_start())
        对 main.py 的通用启动循环完全透明。
        """

    @abstractmethod
    def send_text(self, session_key: str, text: str) -> None:
        """
        向指定会话发送纯文本消息。
        session_key 格式见 make_session_key()，调度器 notify_fn 使用此接口。
        """

    @abstractmethod
    def send_file(self, session_key: str, file_path: str, filename: str) -> None:
        """
        向指定会话发送文件。
        各平台实现不同（如钉钉需要两步：先上传 media 拿 media_id 再发消息）。
        未实现的平台应抛出 NotImplementedError，调用方负责 per-gateway 隔离。
        """

    @abstractmethod
    def send_image_text(self, session_key: str, image_path: str, text: str) -> None:
        """
        向指定会话发送「图片 + 文字」合并的一条消息（图在前、文字在后）。
        各平台实现不同（钉钉：上传图拿 media_id，用 sampleMarkdown 内嵌 ![](media_id) + 文字）。
        未实现的平台应抛出 NotImplementedError，调用方负责 per-gateway 隔离。
        """
