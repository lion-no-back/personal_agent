"""
PersonalAgent 启动入口

组件初始化顺序（按依赖关系从底层到顶层）：
  1. 加载 .env 环境变量
  2. 配置日志
  3. PersonalAgentLLM        —— 基础设施层：LLM 客户端
  4. ConversationContext     —— 上下文层：多会话历史 + 消息去重
  5. TaskManager             —— 任务层：后台任务状态追踪
  6. ConversationAgent       —— Agent 层：业务逻辑
  7. FeishuGateway           —— 接入层：飞书 WebSocket 长连接（阻塞启动）
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（与 main.py 同级，固定加载此处的 .env，避免从其它 cwd 启动时读不到配置）
_PROJECT_ROOT = Path(__file__).resolve().parent
_DOTENV_PATH = _PROJECT_ROOT / ".env"


def setup_logging() -> None:
    """
    配置全局日志格式。
    日志级别优先读取环境变量 LOG_LEVEL，默认 INFO。
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    # 降低第三方库的日志噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("lark_oapi").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
#   技能加载辅助函数
# ──────────────────────────────────────────────────────────────


def _parse_skill_frontmatter(content: str) -> tuple:
    """解析 SKILL.md 的 YAML frontmatter，返回 (name, description, body)。"""
    name = description = ""
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
            body = parts[2].strip()
    return name, description, body


def _load_skills(project_root: Path) -> tuple[str, dict]:
    """
    扫描 personal_agent/skills/*/SKILL.md，分离元数据与详细文档。

    Returns:
        meta_prompt:  仅含名称/描述/脚本路径的简短系统提示词，body 不在其中
        skill_bodies: {skill_name: {"body": str, "description": str}}
                      供 ConversationAgent 检测到意图时按需瞬态注入
    """
    skills_dir = project_root / "personal_agent" / "skills"
    if not skills_dir.is_dir():
        return "", {}

    meta_sections = []
    skill_bodies: dict = {}

    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            content = skill_md.read_text(encoding="utf-8")
            name, description, body = _parse_skill_frontmatter(content)
            if not name:
                logger.warning(f"SKILL.md 缺少 name 字段，已跳过: {skill_md}")
                continue

            lines = [f"【技能：{name}】"]
            if description:
                lines.append(f"描述：{description}")

            scripts_dir = skill_md.parent / "scripts"
            if scripts_dir.is_dir():
                lines.append(
                    f"脚本目录（执行脚本时必须使用此绝对路径）：{scripts_dir.resolve()}"
                )
            else:
                lines.append(f"技能目录：{skill_md.parent.resolve()}")

            meta_sections.append("\n".join(lines))

            # body 单独缓存，不写入 system prompt，由 ConversationAgent 按需注入
            if body:
                skill_bodies[name] = {"body": body, "description": description}

            logger.debug(f"已加载技能: {name}")
        except Exception as e:
            logger.warning(f"加载技能 {skill_md} 失败: {e}")

    if not meta_sections:
        return "", {}

    meta_prompt = (
        "【可用技能列表】\n"
        "当用户意图匹配以下技能时，必须严格按照该技能的工作流程执行，"
        "禁止自行探索文件系统或改变执行方式：\n\n"
        + "\n\n".join(meta_sections)
    )
    return meta_prompt, skill_bodies


def main() -> None:
    # ── Step 1: 加载 .env ────────────────────────────────────
    # 优先读取与 main.py 同目录的 .env（不依赖当前工作目录）。
    # override=False：若操作系统/启动器里已设置同名变量，则不会用 .env 覆盖。
    if _DOTENV_PATH.is_file():
        load_dotenv(_DOTENV_PATH, override=False)
    else:
        load_dotenv(override=False)

    # ── Step 2: 配置日志 ─────────────────────────────────────
    setup_logging()

    logger.info("=" * 60)
    logger.info("  PersonalAgent 启动中...")
    logger.info("=" * 60)

    # ── Step 3: 初始化 LLM 客户端 ────────────────────────────
    # 读取 .env 中的 LLM_MODEL_ID、LLM_API_KEY、LLM_BASE_URL
    try:
        from personal_agent.core.llm import PersonalAgentLLM

        llm = PersonalAgentLLM()
        logger.info(f"✅ LLM 客户端就绪 | 模型: {llm.model}")
    except ValueError as e:
        logger.error(f"❌ LLM 初始化失败：{e}")
        logger.error(
            "请检查 .env 文件中的 LLM_MODEL_ID、LLM_API_KEY、LLM_BASE_URL 配置"
        )
        sys.exit(1)

    # ── Step 4: 初始化会话上下文管理器 ───────────────────────
    # max_history 读取 .env 中的 AGENT_MAX_HISTORY，默认 20 条
    # 自动融合读取 .env 中的 AGENT_SYSTEM_PROMPT 以及项目的 AGENTS.md 约束
    from personal_agent.context.conversation_context import ConversationContext
    import re

    import platform

    extra_prompts = []
    # 注入运行环境信息，指导 LLM 使用正确的系统命令
    _os = platform.system()
    if _os == "Windows":
        _env_hint = (
            f"【运行环境】操作系统：Windows {platform.release()}，"
            "Shell：PowerShell / cmd。"
            "请使用 Windows 命令（dir、type、where、icacls 等），"
            "禁止使用 Linux/Unix 命令（find、head、grep、ls、cat 等）。"
        )
    else:
        _env_hint = (
            f"【运行环境】操作系统：{_os} {platform.release()}，"
            "Shell：bash/sh。"
            "请使用标准 Unix 命令（find、grep、ls、cat 等）。"
        )
    extra_prompts.append(_env_hint)
    if env_prompt := os.getenv("AGENT_SYSTEM_PROMPT"):
        extra_prompts.append(env_prompt)
        
    agents_md = _PROJECT_ROOT / "AGENTS.md"
    if agents_md.is_file():
        content = agents_md.read_text(encoding='utf-8')
        # 仅提取"处理原则"部分，不把"开发环境(uv run)"等无用信息发给大模型浪费 token
        match = re.search(r'##\s*处理原则\n([\s\S]+?)(?=\n##\s|$)', content)
        if match:
            extra_prompts.append(f"【核心处理原则】\n{match.group(1).strip()}")

    # ── 加载技能列表 (skills/*/SKILL.md) ────────────────────────
    skills_prompt, skill_bodies = _load_skills(_PROJECT_ROOT)
    if skills_prompt:
        extra_prompts.append(skills_prompt)
        logger.info(
            f"✅ 技能列表已注入系统提示词 | {len(skill_bodies)} 个技能含完整文档（按需注入）"
        )
    else:
        logger.info("未发现任何 SKILL.md，技能列表为空")
        skill_bodies = {}

    context = ConversationContext(
        max_history=int(os.getenv("AGENT_MAX_HISTORY", "20")),
        extra_system_prompt="\n\n".join(extra_prompts) if extra_prompts else None,
    )
    logger.info("✅ ConversationContext 就绪")

    # ── Step 5: 初始化后台任务管理器 ─────────────────────────
    from personal_agent.core.task_manager import TaskManager

    task_manager = TaskManager()
    logger.info("✅ TaskManager 就绪")

    # ── Step 5.5: 初始化交互日志记录器 ───────────────────────
    from personal_agent.core.interaction_logger import InteractionLogger

    logs_dir = _PROJECT_ROOT / "logs"
    interaction_logger = InteractionLogger(log_dir=logs_dir)
    logger.info(f"✅ InteractionLogger 就绪 | 日志目录: {logs_dir}")

    # ── Step 6: 初始化对话 Agent ─────────────────────────────
    # card_update_interval 读取 .env 中的 CARD_UPDATE_INTERVAL，默认 0.5s
    from personal_agent.agents.conversation_agent import ConversationAgent

    agent = ConversationAgent(
        llm=llm,
        context=context,
        card_update_interval=float(os.getenv("CARD_UPDATE_INTERVAL", "0.5")),
        task_manager=task_manager,
        interaction_logger=interaction_logger,
        skill_bodies=skill_bodies,
    )
    logger.info("✅ ConversationAgent 就绪")

    # ── Step 6.5: 初始化文件暂存管理器 ───────────────────────
    from personal_agent.core.file_stash import FileStash

    file_stash = FileStash(artifact_dir=_PROJECT_ROOT / "artifact")
    file_stash.start_disk_cleanup_timer()
    logger.info("✅ FileStash 就绪")

    # ── Step 7: 初始化飞书网关并启动（阻塞） ─────────────────
    # 若飞书凭证缺失，先运行首次配置向导；向导通过 os.environ 直接同步，无需重载 .env
    from personal_agent.core.setup_wizard import needs_setup, run_feishu_setup_wizard

    if needs_setup():
        logger.info("未检测到飞书凭证，启动首次配置向导...")
        if not run_feishu_setup_wizard():
            logger.error("❌ 飞书配置未完成，退出")
            sys.exit(1)

    try:
        from personal_agent.gateway.feishu_gateway import FeishuGateway

        gateway = FeishuGateway(
            agent=agent, 
            context=context, 
            task_manager=task_manager,
            file_stash=file_stash
        )
        file_stash.set_remind_callback(gateway.send_stash_remind_card)
        logger.info("✅ FeishuGateway 就绪")
    except ValueError as e:
        logger.error(f"❌ 飞书网关初始化失败：{e}")
        logger.error("请检查 .env 文件中的 FEISHU_APP_ID、FEISHU_APP_SECRET 配置")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("  所有组件初始化完成，正在连接飞书...")
    logger.info("  发送 /help 给机器人查看可用指令（含 /resume /export /tasks）")
    logger.info("  按 Ctrl+C 退出")
    logger.info("=" * 60)

    # start() 内部调用 lark.ws.Client.start()，会阻塞当前线程
    # 通过 WebSocket 长连接持续接收飞书推送的消息事件
    gateway.start()


if __name__ == "__main__":
    main()
