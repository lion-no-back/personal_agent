"""
PersonalAgent 启动入口

组件初始化顺序（按依赖关系从底层到顶层）：
  1. 加载 .env 环境变量
  2. 配置日志
  3. PersonalAgentLLM        —— 基础设施层：LLM 客户端
  4. ConversationContext     —— 上下文层：多会话历史 + 消息去重
  5. TaskManager             —— 任务层：后台任务状态追踪
  6. ConversationAgent       —— Agent 层：业务逻辑
  7. 多网关启动              —— 读 config/channels.yaml，每个 gateway 独立 daemon 线程
"""

import logging
import logging.handlers
import os
import queue
import sys
import threading
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

    _fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    _datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=level,
        format=_fmt,
        datefmt=_datefmt,
        stream=sys.stdout,
    )

    _log_dir = _PROJECT_ROOT / "logs" / "app"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(
        _log_dir / "agent.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    _file_handler.setLevel(level)
    _file_handler.setFormatter(logging.Formatter(_fmt, datefmt=_datefmt))
    logging.getLogger().addHandler(_file_handler)

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


def _load_channels_config(project_root: Path) -> dict:
    """读取 config/channels.yaml，返回 channels 字典。找不到文件时回退为只启用飞书。"""
    channels_yaml = project_root / "config" / "channels.yaml"
    if not channels_yaml.is_file():
        logger.warning("未找到 config/channels.yaml，回退为仅启用飞书")
        return {"feishu": {"enabled": True}}
    try:
        import yaml  # PyYAML，已被 scheduler 依赖
        with open(channels_yaml, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("channels", {})
    except Exception as e:
        logger.error("解析 channels.yaml 失败: %s，回退为仅启用飞书", e)
        return {"feishu": {"enabled": True}}


def _build_gateways(project_root, agent, context, task_manager, file_stash) -> list:
    """
    按 channels.yaml 的启用配置，逐个实例化 gateway。
    初始化失败时记录错误并跳过（不中断其他 gateway 的启动）。
    """
    from personal_agent.gateway.base_gateway import BaseGateway

    channels = _load_channels_config(project_root)
    gateways: list[BaseGateway] = []

    if channels.get("feishu", {}).get("enabled", False):
        # 飞书首次配置向导
        from personal_agent.core.setup_wizard import needs_setup, run_feishu_setup_wizard
        if needs_setup():
            logger.info("未检测到飞书凭证，启动首次配置向导...")
            if not run_feishu_setup_wizard():
                logger.error("❌ 飞书配置未完成，跳过飞书 gateway")
            else:
                _try_add_feishu(gateways, agent, context, task_manager, file_stash)
        else:
            _try_add_feishu(gateways, agent, context, task_manager, file_stash)

    if channels.get("dingtalk", {}).get("enabled", False):
        try:
            from personal_agent.gateway.dingtalk_gateway import DingTalkGateway
            gw = DingTalkGateway(
                agent=agent,
                context=context,
                task_manager=task_manager,
                file_stash=file_stash,
            )
            gateways.append(gw)
            logger.info("✅ DingTalkGateway 就绪")
        except Exception as e:
            logger.error("❌ 钉钉网关初始化失败: %s", e)

    return gateways


def _try_add_feishu(gateways, agent, context, task_manager, file_stash) -> None:
    try:
        from personal_agent.gateway.feishu_gateway import FeishuGateway
        gw = FeishuGateway(
            agent=agent,
            context=context,
            task_manager=task_manager,
            file_stash=file_stash,
        )
        gateways.append(gw)
        logger.info("✅ FeishuGateway 就绪")
    except Exception as e:
        logger.error("❌ 飞书网关初始化失败: %s", e)


def _heal_venv() -> None:
    """清除 METADATA 文件缺失的残缺 dist-info 目录。

    uv pip install 在解析阶段会扫描所有已装包的元数据，遇到 METADATA 文件缺失的
    dist-info 目录会直接退出，导致无关包的安装也失败。根因是历史上 uv sync 删除了
    元数据文件（DLL 因进程锁无法删除，模块本身仍可用）。
    修复方式：删除残缺的 dist-info 目录，让 uv 把该包视为未安装并重新装到正确版本，
    而不是补写假 METADATA（假文件会让版本检测误判已安装正确版本）。
    """
    import shutil as _shutil
    import sysconfig
    site_dir = Path(sysconfig.get_path("purelib"))
    for dist_info in site_dir.glob("*.dist-info"):
        if (dist_info / "METADATA").exists():
            continue
        logger.info("[startup] 移除残缺 dist-info: %s", dist_info.name)
        _shutil.rmtree(dist_info, ignore_errors=True)


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

    # ── Step 2.5: venv 自愈 ──────────────────────────────────
    # 在任何组件初始化之前修复残缺的 dist-info，避免后续 uv pip install 因读不到
    # 包元数据而崩溃。仅补写缺失的 METADATA 文件，不触发任何包重装。
    _heal_venv()

    logger.info("=" * 60)
    logger.info("  PersonalAgent 启动中...")
    logger.info("=" * 60)

    # ── Step 3: 初始化 LLM 路由器 ────────────────────────────
    # 主力模型读取 LLM_MODEL_ID / LLM_API_KEY / LLM_BASE_URL（必填）
    # 可选配置快速模型（LLM_FAST_*）和推理模型（LLM_REASONING_*）
    try:
        from personal_agent.core.llm_router import LLMRouter

        llm = LLMRouter()
        logger.info(f"✅ LLM 路由器就绪 | 主力模型: {llm.model}")
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

    # ── 加载外置行为规范 (prompts/system.md) ────────────────────
    _system_md = _PROJECT_ROOT / "prompts" / "system.md"
    if _system_md.is_file():
        extra_prompts.append(_system_md.read_text(encoding="utf-8").strip())
        logger.info(f"✅ 已加载外置 system prompt | {_system_md}")
    else:
        logger.debug("未发现 prompts/system.md，跳过外置提示词加载")

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

    # ── Step 5.5: 初始化日志记录器 ───────────────────────────
    from personal_agent.core.interaction_logger import InteractionLogger
    from personal_agent.guardrails.audit_logger import GuardrailAuditLogger, set_audit_logger

    logs_dir = _PROJECT_ROOT / "logs"
    interaction_logger = InteractionLogger(log_dir=logs_dir)
    logger.info(f"✅ InteractionLogger 就绪 | 日志目录: {logs_dir}")

    set_audit_logger(GuardrailAuditLogger(log_dir=logs_dir))
    logger.info("✅ GuardrailAuditLogger 就绪 | path=%s", logs_dir / "guardrail_audit.jsonl")

    # ── Step 6: 初始化对话 Agent ─────────────────────────────
    # card_update_interval 读取 .env 中的 CARD_UPDATE_INTERVAL，默认 0.5s
    from personal_agent.agents.conversation_agent import ConversationAgent
    from personal_agent.core.request_scorer import RequestScorer

    _routing_mode = os.getenv("LLM_ROUTING_MODE", "auto").strip().lower()
    _routing_threshold = (
        float(t) if (t := os.getenv("LLM_ROUTING_THRESHOLD", "").strip()) else None
    )
    scorer = RequestScorer(strategy=_routing_mode, threshold_override=_routing_threshold)

    agent = ConversationAgent(
        llm=llm,
        context=context,
        card_update_interval=float(os.getenv("CARD_UPDATE_INTERVAL", "0.5")),
        task_manager=task_manager,
        interaction_logger=interaction_logger,
        skill_bodies=skill_bodies,
        scorer=scorer,
    )
    logger.info("✅ ConversationAgent 就绪")

    # ── Step 6.5: 初始化记忆模块 ─────────────────────────────
    from personal_agent.memory import MemoryModule

    memory_store_dir = _PROJECT_ROOT / os.getenv("MEMORY_STORE_DIR", "memory_store")
    memory_module = MemoryModule(store_dir=memory_store_dir, llm=llm)
    memory_module.start()
    agent.set_memory(memory_module)
    logger.info("✅ MemoryModule 就绪 | 存储目录: %s", memory_store_dir)

    # ── Step 6.6: 初始化文件暂存管理器 ───────────────────────
    from personal_agent.core.file_stash import FileStash

    file_stash = FileStash(artifact_dir=_PROJECT_ROOT / "artifact")
    file_stash.start_disk_cleanup_timer(
        extra_dirs=[_PROJECT_ROOT / ".playwright-mcp"]
    )
    logger.info("✅ FileStash 就绪")

    # ── Step 7: 初始化所有 Gateway ────────────────────────────
    # 启用列表读 config/channels.yaml，凭证只读 .env
    from personal_agent.gateway.base_gateway import BaseGateway

    gateways: list[BaseGateway] = _build_gateways(
        _PROJECT_ROOT, agent, context, task_manager, file_stash
    )
    if not gateways:
        logger.error("❌ 没有任何 gateway 启用，请检查 config/channels.yaml")
        sys.exit(1)

    # file_stash remind 回调：只注册有 send_stash_remind_card 的 gateway（飞书）
    for gw in gateways:
        if hasattr(gw, "send_stash_remind_card"):
            file_stash.set_remind_callback(gw.send_stash_remind_card)

    # ── Step 7.5: 启动调度器（定时任务 + 心跳引擎） ──────────
    from personal_agent.scheduler import SchedulerDaemon

    # 广播 notify_fn：尝试所有 gateway，各自按 session_key 前缀决定是否发送
    def _broadcast_notify(session_key: str, text: str) -> None:
        for gw in gateways:
            try:
                gw.send_text(session_key, text)
            except NotImplementedError:
                logger.debug("%s.send_text 未实现，跳过", gw.platform)
            except Exception as exc:
                logger.error("%s.send_text 失败: %s", gw.platform, exc)

    def _broadcast_send_file(session_key: str, file_path: str, filename: str) -> None:
        for gw in gateways:
            try:
                gw.send_file(session_key, file_path, filename)
            except NotImplementedError:
                logger.debug("%s.send_file 未实现，跳过", gw.platform)
            except Exception as exc:
                logger.error("%s.send_file 失败: %s", gw.platform, exc)

    def _broadcast_send_image(session_key: str, image_path: str) -> None:
        for gw in gateways:
            try:
                gw.send_image(session_key, image_path)
            except NotImplementedError:
                logger.debug("%s.send_image 未实现，跳过", gw.platform)
            except Exception as exc:
                logger.error("%s.send_image 失败: %s", gw.platform, exc)

    agent._notify_fn = _broadcast_notify
    agent._send_file_fn = _broadcast_send_file
    agent._send_image_fn = _broadcast_send_image
    logger.info("✅ 按需 workflow 投递回调已注入 Agent")

    scheduler = SchedulerDaemon(
        config_path=_PROJECT_ROOT / "config" / "scheduler.yaml"
    )
    scheduler.start(
        agent=agent,
        notify_fn=_broadcast_notify,
        send_file_fn=_broadcast_send_file,
        alert_callback=None,
    )
    logger.info("✅ SchedulerDaemon 就绪")

    # ── Step 7.6: 初始化知识库 ────────────────────────────────
    try:
        import personal_agent.knowledge as _kb_mod
        from personal_agent.knowledge import KnowledgeBase

        _kb_instance = KnowledgeBase(
            db_path=_PROJECT_ROOT / "knowledge_docs" / "kb.sqlite",
            wiki_dir=_PROJECT_ROOT / "knowledge_docs" / "wiki",
            sources_dir=_PROJECT_ROOT / "knowledge_docs" / "sources",
            llm=llm,
        )  # embed model is read from EMBED_MODEL_ID env var inside embedder.py
        _kb_instance.start_watcher()
        _kb_mod.kb = _kb_instance
        logger.info("✅ KnowledgeBase 就绪 | 监听: %s", _kb_instance.sources_dir)
    except Exception as e:
        logger.error("❌ KnowledgeBase 初始化失败（知识库不可用）: %s", e)

    # ── Step 8: 多 Gateway 并行启动 ──────────────────────────
    # 每个 gateway 独立 daemon 线程：daemon=True 保证 sys.exit() 可即时终止进程
    # exception queue：监控线程生命周期，任一线程退出则主线程感知并退出
    _exc_q: queue.Queue = queue.Queue()

    def _run_gateway(gw: BaseGateway) -> None:
        try:
            gw.start()
            # start() 正常返回仅在 Ctrl+C 后（SDK 处理 KeyboardInterrupt 并 return）
            _exc_q.put(None)
        except Exception as exc:
            _exc_q.put(exc)

    gw_threads = [
        threading.Thread(
            target=_run_gateway,
            args=(gw,),
            name=f"{gw.platform}-gw",
            daemon=True,
        )
        for gw in gateways
    ]

    platform_names = ", ".join(gw.platform for gw in gateways)
    logger.info("=" * 60)
    logger.info("  所有组件初始化完成，启动网关: %s", platform_names)
    logger.info("  发送 /help 给机器人查看可用指令（含 /resume /export /tasks）")
    logger.info("  按 Ctrl+C 退出")
    logger.info("=" * 60)

    for t in gw_threads:
        t.start()

    # 主线程阻塞：任一 gateway 线程退出（正常或异常）即解除阻塞
    result = _exc_q.get()
    if result is None:
        logger.info("Gateway 正常退出")
        sys.exit(0)
    else:
        logger.critical("Gateway 异常退出: %s", result, exc_info=result)
        sys.exit(1)


if __name__ == "__main__":
    main()
