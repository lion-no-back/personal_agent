"""用户级月度 token 用量与费用追踪。

按 (发送人, 自然月[北京时区], 模型, 来源) 累加 in/out/cache token、调用次数与费用。
费用双策略：provider 返回的真实 cost 优先，否则用 OpenRouter 拉取的单价 × token 兜底；
两者都拿不到则记 unpriced（费用贡献 0）。

归属机制：两个 contextvar 在 gateway worker 线程入口 set，LLM 层收尾处读取归账。
失败全程吞掉，绝不阻断主流程（Rule 7：失败要响 —— 记 WARNING）。

设计文档：docs/superpowers/specs/2026-06-26-usage-cost-tracking-design.md
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import model_metadata
from .telemetry import emit_event

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "usage.db"
_TZ = ZoneInfo("Asia/Shanghai")

# ── USD→CNY 汇率（多源冗余，按序尝试，第一个成功即用）────────────────────
# 每项：(名称, url, 解析函数)。源间相互独立（不同机房/数据源），避免单点不可用。
# 再加备用源只需往列表里加一行。
_FX_SOURCES = [
    # 主源：Frankfurter（ECB 央行参考价，34 币种，工作日更新）
    ("frankfurter",
     "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY",
     lambda j: float(j["rates"]["CNY"])),
    # 备用1：Currency-api / fawazahmed0（200+ 币种含加密，jsDelivr CDN，每日更新）
    ("currency-api",
     "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
     lambda j: float(j["usd"]["cny"])),
    # 备用2：exchangerate-api 开放端点（160+ 币种，每日更新）
    ("er-api",
     "https://open.er-api.com/v6/latest/USD",
     lambda j: float(j["rates"]["CNY"])),
]
_FX_SANITY = (3.0, 20.0)      # USD/CNY 合理区间，超出视为源返回垃圾值，丢弃
_FX_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "fx_cache.json"
_FX_TTL_SECONDS = 12 * 3600   # 内存缓存有效期（汇率每个工作日才变，半天足够）
_fx_mem: dict = {}           # {"rate": float, "fetched_at": epoch}

# ── 每请求作用域 contextvar ───────────────────────────────────────────────
# current_user：在 gateway worker 线程入口 set，(user_id, display_name)
# current_source：当前调用来源，默认 chat；各子系统用 usage_source() 声明
current_user: contextvars.ContextVar[Optional[tuple[str, str]]] = contextvars.ContextVar(
    "usage_current_user", default=None
)
current_source: contextvars.ContextVar[str] = contextvars.ContextVar(
    "usage_current_source", default="chat"
)


def set_current_user(user_id: str, display_name: str) -> contextvars.Token:
    """在 worker 线程入口 set，返回 token 供 finally reset。"""
    return current_user.set((user_id, display_name or ""))


def reset_current_user(token: contextvars.Token) -> None:
    with contextlib.suppress(Exception):
        current_user.reset(token)


@contextlib.contextmanager
def usage_source(source: str):
    """声明当前代码块内 LLM 调用的来源，如 with usage_source('retrieval')。"""
    token = current_source.set(source)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            current_source.reset(token)


def month_key(now: Optional[datetime] = None) -> str:
    """返回北京时区的自然月 key，如 '2026-06'。"""
    dt = now or datetime.now(_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    return dt.astimezone(_TZ).strftime("%Y-%m")


def _fetch_fx_rate() -> Optional[float]:
    """按序尝试各汇率源，第一个返回合理正值的即用；全失败返回 None。"""
    import httpx
    lo, hi = _FX_SANITY
    for name, url, parse in _FX_SOURCES:
        try:
            resp = httpx.get(url, timeout=5.0)
            if resp.status_code != 200:
                logger.warning("汇率源 %s 非 200（%s），尝试下一个", name, resp.status_code)
                continue
            rate = parse(resp.json())
            if rate and lo < rate < hi:
                logger.info("USD→CNY 汇率来自 %s = %.4f", name, rate)
                return rate
            logger.warning("汇率源 %s 返回异常值 %s，丢弃", name, rate)
        except Exception as e:
            logger.warning("汇率源 %s 获取失败 | err=%s", name, e)
    return None


def _save_fx_disk(rate: float) -> None:
    try:
        _FX_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FX_CACHE_PATH.write_text(
            json.dumps({"rate": rate, "date": datetime.now(_TZ).strftime("%Y-%m-%d")}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_fx_disk() -> Optional[float]:
    try:
        return float(json.loads(_FX_CACHE_PATH.read_text(encoding="utf-8"))["rate"])
    except Exception:
        return None


def _usd_to_cny_rate() -> Optional[float]:
    """USD→CNY 汇率。优先级：env 显式覆盖 > 多源实拉(缓存12h) > 磁盘缓存 > None。

    全部失效返回 None（不编死值），调用方据此仅显示美元并提示重新配置。
    """
    # 1. 显式覆盖（写死/离线/确定性场景）
    env = os.getenv("USAGE_USD_TO_CNY")
    if env:
        try:
            return float(env)
        except ValueError:
            logger.warning("USAGE_USD_TO_CNY 非数字，忽略 | %s", env)

    # 2. 内存缓存未过期
    now = time.time()
    if _fx_mem and now - _fx_mem.get("fetched_at", 0) < _FX_TTL_SECONDS:
        return _fx_mem["rate"]

    # 3. 多源实拉
    rate = _fetch_fx_rate()
    if rate:
        _fx_mem.update(rate=rate, fetched_at=now)
        _save_fx_disk(rate)
        return rate

    # 4. 磁盘缓存兜底（上次成功拉到的）
    disk = _load_fx_disk()
    if disk:
        return disk

    # 5. 全部失效 → 返回 None，不编死值；由调用方仅显示美元并提示重新配置
    return None


class UsageTracker:
    """SQLite 持久化的用量记账器（线程安全、单连接复用）。"""

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        logger.info("✅ UsageTracker 就绪 | db=%s", db_path)

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage (
                user_id           TEXT NOT NULL,
                month             TEXT NOT NULL,
                model             TEXT NOT NULL,
                source            TEXT NOT NULL,
                in_tokens         INTEGER NOT NULL DEFAULT 0,
                out_tokens        INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd          REAL    NOT NULL DEFAULT 0,
                calls             INTEGER NOT NULL DEFAULT 0,
                unpriced_calls    INTEGER NOT NULL DEFAULT 0,
                display_name      TEXT,
                updated_at        TEXT NOT NULL,
                PRIMARY KEY (user_id, month, model, source)
            )
            """
        )
        self._conn.commit()

    # ── 成本计算：按 OpenRouter 模型单价 × token ─────────────────────
    @staticmethod
    def _compute_cost(model, in_tokens, out_tokens, slug) -> tuple[float, int]:
        # OpenRouter 按 slug 查到的 per-token 单价（拉不到时 model_metadata 自动走磁盘缓存）
        pricing = model_metadata.get_pricing(model, slug)
        if pricing:
            try:
                p = float(pricing.get("prompt") or 0)
                c = float(pricing.get("completion") or 0)
                if p or c:
                    return in_tokens * p + out_tokens * c, 0
            except (TypeError, ValueError):
                pass
        return 0.0, 1  # 未计价（OpenRouter 与磁盘缓存都拿不到价）

    # ── 记账 ─────────────────────────────────────────────────────────
    def record(
        self,
        *,
        user_id: str,
        display_name: str,
        source: str,
        model: str,
        in_tokens: int,
        out_tokens: int,
        cache_read_tokens: int = 0,
        slug: Optional[str] = None,
    ) -> None:
        """累加一次调用的用量与费用。任何失败只记 WARNING，不抛。"""
        try:
            if not user_id or not model:
                return
            cost_usd, unpriced = self._compute_cost(
                model, in_tokens or 0, out_tokens or 0, slug
            )
            now = datetime.now(_TZ)
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO usage (user_id, month, model, source, in_tokens, out_tokens,
                        cache_read_tokens, cost_usd, calls, unpriced_calls, display_name, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(user_id, month, model, source) DO UPDATE SET
                        in_tokens         = in_tokens         + excluded.in_tokens,
                        out_tokens        = out_tokens        + excluded.out_tokens,
                        cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
                        cost_usd          = cost_usd          + excluded.cost_usd,
                        calls             = calls             + 1,
                        unpriced_calls    = unpriced_calls    + excluded.unpriced_calls,
                        display_name      = excluded.display_name,
                        updated_at        = excluded.updated_at
                    """,
                    (
                        user_id, month_key(now), model, source,
                        in_tokens or 0, out_tokens or 0, cache_read_tokens or 0,
                        cost_usd, unpriced, display_name or "", now.isoformat(),
                    ),
                )
                self._conn.commit()
            emit_event(
                "usage.recorded",
                source="usage_tracker",
                status="ok",
                attrs={
                    "model": model,
                    "source": source,
                    "in_tokens": in_tokens or 0,
                    "out_tokens": out_tokens or 0,
                    "cache_read_tokens": cache_read_tokens or 0,
                    "cost_usd": cost_usd,
                    "unpriced": bool(unpriced),
                },
            )
        except Exception as e:
            logger.warning("UsageTracker.record 失败 | user=%s model=%s err=%s", user_id, model, e)

    def record_from_context(
        self,
        *,
        model: str,
        in_tokens: int,
        out_tokens: int,
        cache_read_tokens: int = 0,
        slug: Optional[str] = None,
    ) -> None:
        """从 contextvar 读取归属并记账。无绑定用户（系统/定时任务）则跳过。"""
        cu = current_user.get()
        if cu is None:
            return
        user_id, name = cu
        self.record(
            user_id=user_id, display_name=name, source=current_source.get(),
            model=model, in_tokens=in_tokens, out_tokens=out_tokens,
            cache_read_tokens=cache_read_tokens, slug=slug,
        )

    # ── 查询 ─────────────────────────────────────────────────────────
    def aggregate(self, user_id: str, month: str) -> dict:
        """聚合某用户某月（跨 model/source）的合计。"""
        try:
            with self._lock:
                row = self._conn.execute(
                    """
                    SELECT COALESCE(SUM(in_tokens), 0), COALESCE(SUM(out_tokens), 0),
                           COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(cost_usd), 0),
                           COALESCE(SUM(calls), 0), COALESCE(SUM(unpriced_calls), 0),
                           MAX(display_name)
                    FROM usage WHERE user_id = ? AND month = ?
                    """,
                    (user_id, month),
                ).fetchone()
        except Exception as e:
            logger.warning("UsageTracker.aggregate 失败 | user=%s err=%s", user_id, e)
            row = None
        if not row:
            row = (0, 0, 0, 0.0, 0, 0, "")
        return {
            "in_tokens": row[0], "out_tokens": row[1], "cache_read_tokens": row[2],
            "cost_usd": row[3], "calls": row[4], "unpriced_calls": row[5],
            "display_name": row[6] or "",
        }

    def format_report(self, user_id: str, display_name: str = "") -> str:
        """渲染 /用量 回复：本月至今 + 上月完整。"""
        now = datetime.now(_TZ)
        cur_m = now.strftime("%Y-%m")
        prev_m = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        cur = self.aggregate(user_id, cur_m)
        last = self.aggregate(user_id, prev_m)
        name = display_name or cur["display_name"] or last["display_name"] or user_id
        rate = _usd_to_cny_rate()

        def cost(usd: float) -> str:
            # 汇率可用 → 美元 + 人民币；汇率失效（rate is None）→ 仅美元
            if rate is None:
                return f"≈ ${usd:.4f}"
            return f"≈ ${usd:.4f}（¥{usd * rate:.2f}）"

        def block(label: str, m: str, agg: dict) -> str:
            # 每月：标题 + 两行（输入/输出 一行，调用/费用 一行），不再 4 字段挤一行。
            # 行内仅在两项间用全角竖线 ｜ 分隔（区别于千分位逗号）。
            return (
                f"**{label} {m}**\n"
                f"输入 {agg['in_tokens']:,} ｜ 输出 {agg['out_tokens']:,} tokens\n"
                f"调用 {agg['calls']} 次 ｜ 费用 {cost(agg['cost_usd'])}"
            )

        parts = [
            f"📊 {name} 的用量",
            block("本月", cur_m, cur),
            block("上月", prev_m, last),
        ]
        if cur["unpriced_calls"]:
            parts.append(f"⚠️ 本月 {cur['unpriced_calls']} 次调用未计价，费用未计入")
        if rate is None:
            parts.append("⚠️ 汇率转换失效，请重新配置（当前仅显示美元）")
        return "\n".join(parts)


# ── 工作流子进程用：纯收集，不落库 ────────────────────────────────────────
# 子进程在独立进程里跑、用自己的 OpenAI client，无法访问主进程的 contextvar 与 DB。
# 故子进程只把每次调用的 usage 收集进列表，由 run() 结果回传主进程统一记账。
_collected: list[dict] = []


def collect_usage(
    model: str,
    in_tokens: int,
    out_tokens: int,
    cache_read_tokens: int = 0,
) -> None:
    """子进程内收集一次调用的 usage（不落库）。"""
    _collected.append({
        "model": model,
        "in_tokens": in_tokens or 0,
        "out_tokens": out_tokens or 0,
        "cache_read_tokens": cache_read_tokens or 0,
    })


def drain_collected() -> list[dict]:
    """取出并清空已收集的 usage（子进程退出前由 subprocess_runner 调用）。"""
    global _collected
    items = _collected
    _collected = []
    return items


# ── 模块级单例（懒加载） ──────────────────────────────────────────────────
_tracker: Optional[UsageTracker] = None
_tracker_lock = threading.Lock()


def get_tracker() -> UsageTracker:
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = UsageTracker()
    return _tracker
