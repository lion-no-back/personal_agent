"""通过品牌名查询 Amazon 销量榜 / 新品榜排名。

逻辑:
  1. 用浏览器抓销量榜、新品榜 top 100, 各取 asin + 排名 + 图片。
  2. 对每个上榜商品并发拉详情页, 从 byline "Visit the X Store" 取真实品牌
     (关键: 匿名访问下榜单列表标题不含品牌名, 品牌只能从详情页可靠获取),
     同时取详情页 Best Sellers Rank 里本类目的销量榜排名, 以及 "Sold by" 卖家名。
  3. 按品牌匹配并输出:
     - 销量榜命中: 图片 / 店铺 / 销量榜排名 (/ 新品榜排名)。
     - 仅新品榜命中: 图片 / 店铺 / 今日销量榜(详情页排名) / 新品榜排名。

店铺名由下方 BRANDS 映射表给出; BXUXOHS 对应两个店铺 (Yihaiqian/Yangrunzhe),
列表/品牌名层面无法区分, 按 SOLD_BY_MAP 用详情页 "Sold by" 细分到具体店铺。
"""
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config  # noqa: F401 — 在导入 scrapling 前把浏览器缓存指向工作流目录
from scrapling.fetchers import DynamicSession, FetcherSession

# 强制终端输出为 UTF-8, 避免 Windows GBK 控制台下中文乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# ---------------------------------------------------------------- 配置
BRANDS = {
    "BXUXOHS": ["Yihaiqian", "Yangrunzhe"],
    "ALMNVO": ["Yihaiqian"],
    "SOLOLUP": ["HSP"],
    "YuiYuKa": ["Xiutai"],
    "LEIXIUER": ["LEIXIUER"],
    "MYAPULUA": ["Liansheng"],
    "Wowstrap": ["Yangrunzhe"],
    "DMVEIMAL": ["Yunjian"],
    "EIHAIHIS": ["YYH"],
}

# BXUXOHS 对应两个店铺, 列表/品牌名层面无法区分, 需进详情页按 "Sold by" 细分。
# 映射关系固定 (仅这两种已知值); 出现未知值时按"未知"处理, 不臆测归属。
SOLD_BY_MAP = {
    "BXUXOHS": {
        "LeisureBands-US": "Yihaiqian",
        "Yangrunzhe": "Yangrunzhe",
    },
}

@dataclass(frozen=True)
class Site:
    """单个 Amazon 站点的抓取配置。品牌表 (BRANDS/SOLD_BY_MAP) 与英文正则跨站共享, 不放这里。

    locale_cookies 仅在详情页 HTTP 抓取时注入, 强制页面渲染英文以复用英文正则
    (DE 用 lc-acbde=en_GB; US 默认英文, 留空)。榜单页/截图按结构选择器解析, 与
    语言无关, 故不注入 locale。
    """
    order: int            # 输出/喜报排序: 小的先发完再发大的 (US=0, DE=1)
    flag: str             # 喜报头部旗帜 emoji
    name_cn: str          # 喜报头部站点名
    bestsellers_url: str
    new_releases_url: str
    dp_base: str          # 详情页前缀, 直接拼 asin
    category_id: str      # 详情页定位本类目 BSR 排名用
    locale_cookies: dict  # 详情页 HTTP 请求注入的语言 cookie


SITE_US = Site(
    order=0, flag="🇺🇸", name_cn="美国站",
    bestsellers_url="https://www.amazon.com/gp/bestsellers/wireless/11591904011",
    new_releases_url="https://www.amazon.com/gp/new-releases/wireless/11591904011",
    dp_base="https://www.amazon.com/dp/",
    category_id="11591904011",   # Smartwatch Bands 子类 id
    locale_cookies={},
)
SITE_DE = Site(
    order=1, flag="🇩🇪", name_cn="德国站",
    bestsellers_url="https://www.amazon.de/gp/bestsellers/ce-de/8561056031",
    new_releases_url="https://www.amazon.de/gp/new-releases/ce-de/8561056031",
    dp_base="https://www.amazon.de/dp/",
    category_id="8561056031",    # Smartwatch Replacement Bands 子类 id
    locale_cookies={"lc-acbde": "en_GB"},  # 强制 amazon.de 渲染英文, 复用英文正则
)
SITES = [SITE_US, SITE_DE]

PAGES = [1, 2]  # 每个榜单抓 2 页 (top ~100)

# ---------------------------------------------------------------- 路径 / 日志
# 统一存到项目根 artifact/rankbb_<日期>/<时间>/：日志在 logs/ 子目录，截图直接放该目录。
# 路径与日志文件按"每次运行"独立（见 init_run_paths），避免 agent 长驻进程下多次播报共用同一目录。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

RUN_TS = ""          # 截图文件名用的运行时间戳（init_run_paths 时赋值）
LOG_DIR = SHOT_DIR = None
log_filepath = None
_fh = None

logger = logging.getLogger("rank_checker")
logger.setLevel(logging.INFO)   # INFO 级：DEBUG 细节（详情页/跳号缺口/滚动）不落文件
logger.propagate = False         # 只写自己的日志文件，不冒泡到 agent 控制台
logging.getLogger("scrapling").setLevel(logging.WARNING)


def init_run_paths():
    """为本次运行生成独立的 artifact 目录并切换日志文件。run_pipeline 开头调用。"""
    global RUN_TS, LOG_DIR, SHOT_DIR, log_filepath, _fh
    now = datetime.now()
    RUN_TS = now.strftime("%Y%m%d_%H%M%S")
    run_dir = _PROJECT_ROOT / "artifact" / f"rankbb_{now:%Y%m%d}" / f"{now:%H%M%S}"
    LOG_DIR = run_dir / "logs"
    SHOT_DIR = run_dir
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_filepath = str(LOG_DIR / f"rank_{RUN_TS}.log")
    if _fh is not None:
        logger.removeHandler(_fh)
        _fh.close()
    _fh = logging.FileHandler(log_filepath, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_fh)

CARD_SELECTOR = "div[id^='p13n-asin-index-']"
TITLE_SELECTORS = [
    "div[class*='p13n-sc-css-line-clamp']",
    "span[class*='p13n-sc-css-line-clamp']",
    "div.p13n-sc-truncate",
    "span.p13n-sc-truncate",
]


_COUNT_JS = 'document.querySelectorAll("div[id^=\\"p13n-asin-index-\\"]").length'
PAGE_TARGET = 50  # 榜单每页满载卡片数
STALL_ROUNDS = 5  # 卡片数"涨过后又"连续这么多轮零增长, 判定该次加载的 XHR 卡死, 提前止损


def scroll_to_load_all(page):
    """加载满每页 50 张卡片, 并对卡住的懒加载提前止损。

    榜单初始只渲染 30 张, 其余通过滚动触发的 `nextPage` XHR 分批加载
    (实测序列: 30→30→30→38→46→50)。关键是用真实滚轮事件**匀速、持续**
    地往下滚, 每轮固定等待让 XHR 落地; 切勿中途跳到页面底部 —— 那会让
    后续滚轮失效、再也滚不过触发区。开头停在 30 的"预热期"属正常, 需耐心。

    早停: 偶发某次页面加载的 nextPage XHR 会卡死 (滚到底也只有 46), 继续滚
    无用 —— 该次加载的会话已卡死, 唯一解药是 fetch_page 重新导航整页重抓。
    所以一旦卡片数**已经涨过初始值、之后又**连续 STALL_ROUNDS 轮零增长, 立即
    退出交给重抓, 把代价从"白滚满 30 轮(~30s)"降到"卡住后再多滚 ~4s 即退"。
    仅在"涨过之后"才计卡住, 是为了不误伤开头停在 30 的正常预热期。
    """
    initial = page.evaluate(_COUNT_JS)
    count = initial
    stall = 0
    for _ in range(30):
        if count >= PAGE_TARGET:
            break
        page.mouse.wheel(0, 1200)  # 真实滚轮事件, 匀速下滚
        page.wait_for_timeout(800)  # 固定等待 nextPage XHR 返回并渲染
        new_count = page.evaluate(_COUNT_JS)
        if new_count > count:
            count = new_count
            stall = 0  # 有新卡片进来, 重置卡住计数
        elif count > initial:
            # 已加载过一些卡片却又停止增长 → 该次加载的 XHR 卡死, 早停去重抓
            stall += 1
            if stall >= STALL_ROUNDS:
                logger.debug("滚动早停: 卡在 %d 个连续 %d 轮无增长, 放弃去重抓", count, stall)
                break
        # count 仍等于 initial (预热期) 不计卡住, 保持耐心
    logger.debug("滚动结束: 卡片数=%d", count)  # count 即当前卡片数 (只增不减), 无需再 evaluate


def _full_size_image(url: str) -> str:
    """去掉 Amazon 缩略图尺寸修饰 (._AC_UL300_.jpg -> .jpg), 得到原图链接。"""
    if not url:
        return ""
    return re.sub(r"\._[^/]*?(\.\w+)(?:$|\?)", r"\1", url)


def parse_items(page_result, label, page_num):
    items = []
    cards = page_result.css(CARD_SELECTOR)
    logger.debug("[%s 第%d页] 卡片节点 %d 个", label, page_num, len(cards))

    for card in cards:
        rank_el = card.css("span.zg-bdg-text").first
        rank = rank_el.get_all_text(strip=True).lstrip("#") if rank_el else ""

        title = ""
        for sel in TITLE_SELECTORS:
            el = card.css(sel).first
            if el:
                title = el.get_all_text(separator=" ", strip=True).strip()
                if title:
                    break

        image_url = _full_size_image(card.css("img::attr(src)").get(""))
        asin_el = card.css("[data-asin]").first
        asin = asin_el.attrib.get("data-asin", "") if asin_el else ""

        if not rank or not title:
            logger.warning("[%s 第%d页] 跳过异常卡片 rank=%r title=%r asin=%r",
                           label, page_num, rank, title[:40], asin)
            continue
        items.append({"rank": rank, "title": title, "image_url": image_url, "asin": asin})

    logger.debug("[%s 第%d页] 解析 %d / %d", label, page_num, len(items), len(cards))
    return items


def _rank_gaps(items):
    """返回页内排名跳号缺口 (有缺口说明懒加载不全)。"""
    ranks = sorted(int(i["rank"]) for i in items if i["rank"].isdigit())
    if not ranks:
        return {0}
    return set(range(ranks[0], ranks[-1] + 1)) - set(ranks)


def fetch_page(session, page_url, label, pg, attempts=3):
    """抓取单页, 最多重试 attempts 次, 保留"无跳号缺口且数量最多"的结果。

    满 50 即视为完整、立即返回; 若类目本身不足 50, 多次重试数量会稳定
    收敛到真实值 (取最大值), 从而区分"真没有"和"漏加载"。
    """
    best, best_n = [], -1
    for attempt in range(1, attempts + 1):
        page = session.fetch(page_url, page_action=scroll_to_load_all)
        items = parse_items(page, label, pg)
        gaps = _rank_gaps(items)
        logger.debug("[%s 第%d页] 尝试%d: %d 个, 跳号缺口=%s",
                     label, pg, attempt, len(items), sorted(gaps)[:8])
        if not gaps and len(items) > best_n:
            best, best_n = items, len(items)
        if not gaps and len(items) >= PAGE_TARGET:
            break  # 满页且连续 → 完整
        if attempt < attempts:
            print(f"    第{attempt}次不完整({len(items)}个), 重试...")
    return best


def scrape_ranking(session, url, label):
    by_asin = {}
    for pg in PAGES:
        page_url = url if pg == 1 else f"{url}?pg={pg}"
        print(f"  {label} 第{pg}页 ...")
        logger.debug("抓取: %s", page_url)
        for item in fetch_page(session, page_url, label, pg):
            by_asin.setdefault(item["asin"], item)  # 按 ASIN 去重, 保留首见排名
        print(f"    累计 {len(by_asin)} 个商品")

    items = list(by_asin.values())
    all_ranks = sorted(int(i["rank"]) for i in items if i["rank"].isdigit())
    gaps = _rank_gaps(items) if all_ranks else set()  # 复用 _rank_gaps, 不再内联同款集合差
    logger.debug("[%s] 共 %d 个商品, 排名 %s-%s, 整体跳号缺口=%s",
                 label, len(items), all_ranks[0] if all_ranks else "-",
                 all_ranks[-1] if all_ranks else "-", sorted(gaps)[:8])
    if gaps:
        print(f"    ⚠ {label} 排名存在缺口: {sorted(gaps)[:8]}")
    return items


_STORE_RE = re.compile(r"Visit the (.+?) Store")
_BRAND_LABEL_RE = re.compile(r"Brand[:：]\s*([A-Za-z0-9.\-&' ]{2,30}?)(?:\s{2,}|\s\||$)")


def _match_brand(text):
    """从一段文字里匹配品牌名 ("Visit the X Store" 或 "Brand: X"); 取不到返回 None。"""
    m = _STORE_RE.search(text) or _BRAND_LABEL_RE.search(text)
    return m.group(1).strip() if m else None


def _extract_brand(page, full_text):
    """从详情页提取品牌。

    byline (`a#bylineInfo` 普通 / `a#visitStoreDesktopUrl` 高级) 文字有两种形式:
    "Visit the X Store" 或 "Brand: X"。在 byline 元素自身的干净文本上匹配二者,
    再退到 href `/stores/<品牌>/`, 最后退到全页文本 (full_text) 兜底。
    """
    for sel in ("a#bylineInfo", "a#visitStoreDesktopUrl"):
        el = page.css(sel).first
        if not el:
            continue
        brand = _match_brand(el.get_all_text(separator=" ", strip=True))
        if brand:
            return brand
        hm = re.search(r"/stores/([^/]+)/page", el.attrib.get("href", ""))
        if hm:
            return hm.group(1).strip()
    return _match_brand(full_text)


def brand_matches(target, resolved):
    """目标品牌与详情页解析出的品牌是否一致 (不区分大小写, 允许整词包含)。"""
    if not resolved:
        return False
    rl, tl = resolved.lower(), target.lower()
    return tl == rl or re.search(r"\b" + re.escape(tl) + r"\b", rl) is not None


def _extract_bsr_rank(page, full_text, category_id):
    """从详情页取本类目 (Smartwatch Bands) 的销量榜排名数字; 取不到返回 None。

    用 href 含类目 id 的链接文字作为类目名, 再在全页文本 (full_text) 里匹配
    "#<数字> in <类目名>", 兼容 detailBullets 与 productDetails 两种版式。
    """
    cat_name = None
    for a in page.css(f'a[href*="{category_id}"]'):
        href = a.attrib.get("href", "")
        if "bestsellers" in href or "/zg" in href or "hrsr" in href:
            cat_name = a.get_all_text(strip=True)
            break
    if not cat_name:
        return None
    m = re.search(r"#\s*([\d,]+)\s+in\s+" + re.escape(cat_name), full_text)
    return m.group(1).replace(",", "") if m else None


def _extract_sold_by(page):
    """从详情页买盒取 "Sold by" 卖家名 (`#sellerProfileTriggerId`); 取不到返回 None。"""
    el = page.css("#sellerProfileTriggerId").first
    return el.get_all_text(strip=True) if el else None


_BLOCK_MARKERS = ("Enter the characters you see", "Robot Check", "api-services-support@amazon.com",
                   "Sorry, we just need to make sure you're not a robot", "Continue shopping")

# 每次请求随机选一个浏览器 TLS 指纹 (而不是每次都用同一个 "chrome"),
# 让 198 个请求不会呈现"完全一样的客户端指纹"这种机器特征。
_IMPERSONATE_POOL = ["chrome", "firefox", "safari", "edge"]

# 亚马逊自家遥测/补全埋点域名: block_ads(~3500 广告域) 覆盖不到这些, 但它们会
# 持续后台请求。我们用 page_action 滚动来确定性驱动懒加载, 不依赖网络空闲,
# 故把这些噪声域名挡掉, 减少无关请求 (配合关掉 network_idle)。
_BLOCKED_DOMAINS = {
    "amazon-adsystem.com", "unagi.amazon.com",
    "fls-na.amazon.com", "completion.amazon.com",
}


_thread_local = threading.local()
_sessions_lock = threading.Lock()
_all_sessions = []


def _get_thread_session():
    """每个 worker 线程独享一个持久 FetcherSession (复用连接池/TLS, 避免每次
    one-off 请求都重新握手)。同一个 session 只会被创建它的那个线程访问,
    不存在跨线程并发访问, 故不需要加锁。"""
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = FetcherSession(stealthy_headers=True, timeout=30).__enter__()
        _thread_local.session = sess
        with _sessions_lock:
            _all_sessions.append(sess)
    return sess


def _close_all_sessions():
    """resolve_all 的 ThreadPoolExecutor 退出 (所有 worker 线程已结束) 后调用,
    释放本轮创建的所有线程级 session。"""
    for sess in _all_sessions:
        try:
            sess.__exit__(None, None, None)
        except Exception:
            pass
    _all_sessions.clear()


def resolve_product(asin, site, attempts=3):
    """纯 HTTP 拉商品详情页, 返回 {brand, bsr_rank, sold_by}。

    品牌取 byline "Visit the X Store" (匿名访问下榜单列表标题不含品牌, 详情页才可靠)。
    site.locale_cookies 注入语言偏好 (DE 强制英文), 使 byline/BSR/Sold by 文案为英文,
    复用英文正则。

    HTTP 状态 200 不代表拿到了真实商品页 (可能是验证码/拦截页), 所以用
    `#productTitle` 是否存在校验内容真实性; 校验失败按软失败重试, 并记录
    诊断信息 (文本长度/是否命中拦截关键词), 避免"状态200但内容是空的"被
    误判为成功而不重试。
    """
    url = f"{site.dp_base}{asin}"
    page = None
    for i in range(1, attempts + 1):
        try:
            candidate = _get_thread_session().get(
                url, impersonate=_IMPERSONATE_POOL, cookies=site.locale_cookies or None)
        except Exception as e:
            logger.warning("详情页拉取异常 asin=%s 第%d次: %s", asin, i, e)
            continue
        if candidate.status != 200:
            logger.warning("详情页 asin=%s 第%d次 status=%s", asin, i, candidate.status)
            continue
        if candidate.css("#productTitle").first:
            page = candidate
            break
        text = candidate.get_all_text(separator=" ", strip=True)
        hit_marker = next((m for m in _BLOCK_MARKERS if m in text), None)
        logger.warning(
            "详情页 asin=%s 第%d次 status=200 但无#productTitle (软失败), 文本长度=%d, 拦截关键词=%s",
            asin, i, len(text), hit_marker,
        )
    if page is None:
        logger.error("详情页拉取失败 asin=%s (重试%d次后仍无有效商品页)", asin, attempts)
        return {"brand": None, "bsr_rank": None, "sold_by": None}

    full_text = page.get_all_text(separator=" ", strip=True)  # 全页文本只序列化一次, 供品牌兜底与BSR共用
    brand = _extract_brand(page, full_text)
    bsr_rank = _extract_bsr_rank(page, full_text, site.category_id)
    sold_by = _extract_sold_by(page)
    logger.debug("详情页 asin=%s 品牌=%s 销量榜排名=%s Sold by=%s", asin, brand, bsr_rank, sold_by)
    return {"brand": brand, "bsr_rank": bsr_rank, "sold_by": sold_by}


def resolve_all(asins, site, workers=8):
    """并发解析所有 asin 的品牌与销量榜排名, 返回 asin -> {brand, bsr_rank}。

    asin 属于 site 所在站点, 详情页只能用该站 dp 前缀查 (跨站 asin 不通用)。

    workers=8 是验证过稳定的并发数。曾测试过把它提到16/24/32, 但连续高并发
    压测本身就会让 IP 被亚马逊判定为可疑, 污染后续请求 (实测命中 "Continue
    shopping" 反爬拦截页, 状态仍是 200 但内容是空壳, 见 resolve_product 的
    软失败重试逻辑), 不能简单"调大数字"换取速度, 维持 8 以保证结果正确。
    """
    asins = list(asins)
    result, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(resolve_product, a, site): a for a in asins}
        for fut in as_completed(futs):
            result[futs[fut]] = fut.result()
            done += 1
            if done % 20 == 0 or done == len(asins):
                print(f"    品牌解析进度 {done}/{len(asins)}")
    _close_all_sessions()
    return result


def resolve_shop_name(brand, shops, item):
    """得到该商品应显示的店铺名。

    多店铺品牌 (见 SOLD_BY_MAP) 按详情页 "Sold by" 细分; 其余品牌沿用固定映射。
    Sold by 不是已知值时报错并返回 None, 由调用方决定如何处理该条。
    """
    mapping = SOLD_BY_MAP.get(brand)
    if not mapping:
        return " / ".join(shops)
    sold_by = item.get("sold_by")
    shop = mapping.get(sold_by)
    if shop is None:
        logger.error("[%s] 未知 Sold by=%r, 无法判定店铺, asin=%s", brand, sold_by, item.get("asin"))
        print(f"  ⚠ [{brand}] 未知 Sold by={sold_by!r}, 无法判定店铺 (asin={item.get('asin')}), 跳过此条")
    return shop


def _make_result(brand, asin, image_url, shop_str, sort_key, rank_lines, board_url, list_rank, site_order):
    """组装一条命中记录 (结构化)。图片行延后到截图阶段再生成 (见 _result_lines)。

    board_url / list_rank 用于截图回放: 确定去哪个榜单页、翻到第几页定位卡片。
    既命中销量榜又命中新品榜时只走销量榜分支, 故这里只会带销量榜的 url 与排名。
    site_order 用于输出排序: 同站内按 sort_key, 站间按 site_order (小的先发)。
    """
    return {
        "site_order": site_order,
        "sort_key": sort_key,
        "brand": brand,
        "asin": asin,
        "image_url": image_url,
        "shop_str": shop_str,
        "rank_lines": rank_lines,
        "board_url": board_url,
        "list_rank": list_rank,
    }


def _result_lines(r, shot_path):
    """把一条命中记录渲染成打印行。有截图则打印本地路径, 否则回退原图片链接。"""
    img_line = f"图片: {shot_path}" if shot_path else f"图片链接: {r['image_url']}"
    return [f"\n品牌: {r['brand']}", img_line, f"店铺：{r['shop_str']}"] + r["rank_lines"]


def segment_text(r):
    """图文投递用的文字块 (不含图片行): 品牌 / 店铺 / 排名, 合并成一行 (中文逗号分隔)。"""
    return "，".join([f"品牌：{r['brand']}", f"店铺：{r['shop_str']}"] + r["rank_lines"])


def build_site_header(site, stats):
    """单站喜报块 (自包裹: 标题 + 站喜报 + 结尾, 供按站穿插输出)。

    逐小句判断 (前50/50-100 各为 0 则省略对应小句, 某榜两小句全空则省略该榜行);
    该站两榜行皆空返回 None (调用方据此不发该站头部)。占位数字由 stats 动态填入。
    """
    def board_line(emoji, name, top50, next50):
        clauses = []
        if top50 > 0:
            clauses.append(f"前50名占{top50}席")
        if next50 > 0:
            clauses.append(f"50-100名再添{next50}席")
        return f"{emoji} {name} 榜单{'，'.join(clauses)}！" if clauses else None

    lines = [
        line for line in (
            board_line("🏆", "Best Sellers", stats["bs_top50"], stats["bs_next50"]),
            board_line("🔥", "New Releases", stats["nr_top50"], stats["nr_next50"]),
        ) if line
    ]
    if not lines:
        return None
    return "\n".join(
        ["【每日播报】🎆🧨🧨Best Sellers快讯 🧨🧨🎆", f"{site.flag} {site.name_cn}喜报："]
        + lines
        + ["继续冲榜，再创佳绩～ 🧧🚀🚀🧧"]
    )


def _page_url_for(board_url, list_rank):
    """列表排名 ≤50 在第1页, 否则第2页 (?pg=2)。"""
    return board_url if list_rank <= PAGE_TARGET else f"{board_url}?pg=2"


# 截图前把卡片里的缩略图替换成高清原图: 页面默认渲染的是 srcset 选中的低清缩略图,
# DSF=2 下放大发糊; 换成已有的高清链接 (image_url) 并移除 srcset, 再等加载完才清晰。
_UPGRADE_IMG_JS = """([asin, url]) => {
  const card = document.querySelector(`div[id^="p13n-asin-index-"]:has([data-asin="${asin}"])`);
  const img = card && card.querySelector('img');
  if (!img) return false;
  img.removeAttribute('srcset');
  img.src = url;
  return true;
}"""
_IMG_LOADED_JS = """(asin) => {
  const card = document.querySelector(`div[id^="p13n-asin-index-"]:has([data-asin="${asin}"])`);
  const img = card && card.querySelector('img');
  return !!(img && img.complete && img.naturalWidth > 0);
}"""


def screenshot_cards(results):
    """对命中结果回放截图卡片, 返回 asin -> 本地 png 路径 (截图失败的 asin 不在内)。

    单独开一个会话, 不设 disable_resources (让商品图加载, 否则卡片里图是空白),
    其余 block_ads/blocked_domains 与主抓榜一致。按页面分组, 每页只加载一次,
    滚动到位后逐个按 data-asin 定位卡片元素截图。
    """
    os.makedirs(SHOT_DIR, exist_ok=True)
    groups = {}
    for r in results:
        groups.setdefault(_page_url_for(r["board_url"], r["list_rank"]), []).append(r)

    shots = {}

    def _make_action(targets):
        def _action(page):
            scroll_to_load_all(page)
            for r in targets:
                asin = r["asin"]
                out_path = os.path.join(SHOT_DIR, f"rank_{RUN_TS}_{r['brand']}_{asin}.png")
                loc = page.locator(f'div[id^="p13n-asin-index-"]:has([data-asin="{asin}"])').first
                try:
                    loc.scroll_into_view_if_needed(timeout=5000)
                    # 换高清原图再等加载完, 否则截到的是低清缩略图 (放大发糊)
                    if r["image_url"] and page.evaluate(_UPGRADE_IMG_JS, [asin, r["image_url"]]):
                        try:
                            page.wait_for_function(_IMG_LOADED_JS, arg=asin, timeout=8000)
                        except Exception as e:
                            logger.warning("高清图加载等待超时 asin=%s: %s", asin, e)
                    loc.screenshot(path=out_path)
                    shots[asin] = out_path
                    logger.info("截图成功 asin=%s -> %s", asin, out_path)
                except Exception as e:
                    logger.warning("截图失败 asin=%s: %s", asin, e)
        return _action

    with DynamicSession(
        headless=True,
        network_idle=False,
        block_ads=True,
        blocked_domains=_BLOCKED_DOMAINS,
        wait_selector=CARD_SELECTOR,
        wait_selector_state="attached",
        timeout=40000,
    ) as session:
        for page_url, targets in groups.items():
            print(f"  截图回放: {page_url} ({len(targets)} 个)")
            logger.info("截图回放页面: %s, %d 个目标", page_url, len(targets))
            session.fetch(page_url, page_action=_make_action(targets))
            # 偶发懒加载卡死(滚动早停)导致部分卡片没出来 → 定位超时。重新导航整页是
            # 已知解药(见 scroll_to_load_all 注释), 仅对未成功的目标重试一次。
            missing = [t for t in targets if t["asin"] not in shots]
            if missing:
                logger.info("截图重试: %s 仍有 %d 个未成功, 重新导航整页", page_url, len(missing))
                session.fetch(page_url, page_action=_make_action(missing))
    return shots


def _scrape_one(url, label):
    """独立开一个浏览器会话抓单个榜单, 供两榜并行抓取调用。

    不用 network_idle (亚马逊后台埋点常年不空闲, 等它纯属浪费且时长不可控);
    改由 wait_selector 等榜单卡片出现 + page_action 滚动按"卡片增长"确定性驱动
    懒加载。disable_resources + block_ads + blocked_domains 砍掉图片/媒体/广告/
    埋点等无关请求, 页面更轻、滚动更少。
    """
    with DynamicSession(
        headless=True,
        disable_resources=True,
        network_idle=False,
        block_ads=True,
        blocked_domains=_BLOCKED_DOMAINS,
        wait_selector=CARD_SELECTOR,
        wait_selector_state="attached",
        timeout=40000,
    ) as session:
        print(f"\n[{label}]")
        return scrape_ranking(session, url, label)


def _match_brands(site, bestseller_items, new_release_items):
    """对单站两榜按 BRANDS 匹配, 返回命中记录列表 (带 site.order, 未排序)。"""
    nr_by_asin = {it["asin"]: it for it in new_release_items}
    results = []
    for brand, shops in BRANDS.items():
        bs_hits = [it for it in bestseller_items if brand_matches(brand, it.get("brand"))]
        nr_hits = [it for it in new_release_items if brand_matches(brand, it.get("brand"))]

        if bs_hits:
            # 销量榜命中: 用榜单排名, 新品榜排名按同 ASIN 关联
            for item in bs_hits:
                shop_str = resolve_shop_name(brand, shops, item)
                if shop_str is None:
                    continue
                nr_item = nr_by_asin.get(item["asin"]) or (nr_hits[0] if nr_hits else None)
                logger.info("[%s/%s] 销量榜命中 #%s 新品榜#%s asin=%s 店铺=%s",
                            site.name_cn, brand, item["rank"], nr_item["rank"] if nr_item else "-",
                            item["asin"], shop_str)
                rank_lines = [f"销量榜排名：{item['rank']}"]
                if nr_item:
                    rank_lines.append(f"新品榜排名：{nr_item['rank']}")
                # 既命中销量榜(又可能命中新品榜)→ 统一按销量榜列表页截图
                results.append(_make_result(brand, item["asin"], item["image_url"], shop_str,
                                            int(item["rank"]), rank_lines,
                                            site.bestsellers_url, int(item["rank"]), site.order))

        elif nr_hits:
            # 仅新品榜命中: 销量榜排名取详情页 BSR
            for nr_item in nr_hits:
                shop_str = resolve_shop_name(brand, shops, nr_item)
                if shop_str is None:
                    continue
                bsr = nr_item.get("bsr_rank")
                if not bsr:
                    logger.error("[%s/%s] 仅新品榜命中但详情页未取到销量榜排名 asin=%s",
                                 site.name_cn, brand, nr_item["asin"])
                    print(f"\n品牌: {brand}  ⚠ 详情页未取到销量榜排名 (asin={nr_item['asin']})")
                    continue
                logger.info("[%s/%s] 仅新品榜命中 新品榜#%s 销量榜#%s asin=%s 店铺=%s",
                            site.name_cn, brand, nr_item["rank"], bsr, nr_item["asin"], shop_str)
                # 仅新品榜命中: 排序键用 BSR, 但截图翻页要用新品榜列表排名
                results.append(_make_result(
                    brand, nr_item["asin"], nr_item["image_url"], shop_str, int(bsr),
                    [f"今日销量榜：{bsr}名", f"新品榜：{nr_item['rank']}名"],
                    site.new_releases_url, int(nr_item["rank"]), site.order))

        else:
            logger.debug("[%s/%s] 销量榜/新品榜均未上榜", site.name_cn, brand)
    return results


def _board_stats(bestseller_items, new_release_items):
    """单站头部"占X席"统计：按榜分别计、双榜各计（同一商品同时上两榜则两边各算一席）。"""
    def _matches_any(b):
        return any(brand_matches(k, b) for k in BRANDS)

    def _counts(items):
        ranks = [int(it["rank"]) for it in items if _matches_any(it.get("brand")) and it["rank"].isdigit()]
        return sum(1 for r in ranks if r <= 50), sum(1 for r in ranks if 51 <= r <= 100)

    bs_top50, bs_next50 = _counts(bestseller_items)
    nr_top50, nr_next50 = _counts(new_release_items)
    return {"bs_top50": bs_top50, "bs_next50": bs_next50,
            "nr_top50": nr_top50, "nr_next50": nr_next50}


def _collect_site(site):
    """抓单站两榜 + 详情页解析 + 按品牌匹配, 返回 (命中记录列表, stats)。"""
    print(f"\n{'=' * 70}\n{site.flag} {site.name_cn}\n{'=' * 70}")
    # 1) 两个独立浏览器会话并行抓两个榜单 (asin + 排名 + 图片)
    t_scrape0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_bs = ex.submit(_scrape_one, site.bestsellers_url, f"{site.name_cn}销量榜")
        fut_nr = ex.submit(_scrape_one, site.new_releases_url, f"{site.name_cn}新品榜")
        bestseller_items = fut_bs.result()
        new_release_items = fut_nr.result()
    t_scrape = time.time() - t_scrape0
    logger.info("[%s] 阶段耗时: 浏览器抓榜单(并行) %.1fs", site.name_cn, t_scrape)
    print(f"\n[耗时] {site.name_cn} 浏览器抓榜单(并行): {t_scrape:.1f}s")

    # 2) 并发查详情页, 得到每个商品的真实品牌 + 销量榜排名 (列表标题不含品牌)
    t_resolve0 = time.time()
    all_asins = {it["asin"] for it in bestseller_items + new_release_items if it["asin"]}
    print(f"\n[品牌解析] {site.name_cn} 共 {len(all_asins)} 个商品, 并发查询详情页...")
    logger.info("[%s] 开始品牌解析, 共 %d 个 asin", site.name_cn, len(all_asins))
    info = resolve_all(all_asins, site)
    t_resolve = time.time() - t_resolve0
    logger.info("[%s] 阶段耗时: 详情页并发解析 %.1fs", site.name_cn, t_resolve)
    print(f"[耗时] {site.name_cn} 详情页并发解析: {t_resolve:.1f}s")
    for it in bestseller_items + new_release_items:
        meta = info.get(it["asin"]) or {}
        it["brand"] = meta.get("brand")
        it["bsr_rank"] = meta.get("bsr_rank")
        it["sold_by"] = meta.get("sold_by")

    # 3) 按品牌匹配 + 统计
    results = _match_brands(site, bestseller_items, new_release_items)
    stats = _board_stats(bestseller_items, new_release_items)
    return results, stats


def collect_hits():
    """逐站抓榜 + 详情页解析 + 按品牌匹配, 返回 (命中记录列表, 各站 stats)。

    各站串行 (避免并发翻倍触发反爬); 站间 asin 不通用, 详情页解析按站独立。
    返回的 results 未截图、未排序; stats_by_site 为 {site.order: stats}, 供头部喜报用。
    """
    all_results, stats_by_site = [], {}
    for site in SITES:
        results, stats = _collect_site(site)
        all_results.extend(results)
        stats_by_site[site.order] = stats
    return all_results, stats_by_site


def run_pipeline():
    """完整流程: collect_hits + 截图, 返回 (results_sorted, shots, stats_by_site)。

    results_sorted 按 (site_order, sort_key) 升序: 站间按 site_order (美国先发完再德国),
    站内按销量榜排名数字。shots 为 asin -> 本地 png 路径 (截图失败的 asin 不在内);
    stats_by_site 为各站头部"占X席"统计。供 main() (CLI 打印) 与 runner (图文投递) 共用。
    """
    init_run_paths()  # 每次运行独立的 artifact 目录 + 日志文件
    results, stats_by_site = collect_hits()
    if not results:
        return [], {}, stats_by_site
    print("\n[截图] 回放命中商品卡片...")
    t_shot0 = time.time()
    shots = screenshot_cards(results)
    logger.info("阶段耗时: 截图回放 %.1fs", time.time() - t_shot0)
    print(f"[耗时] 截图回放: {time.time() - t_shot0:.1f}s")
    return sorted(results, key=lambda r: (r["site_order"], r["sort_key"])), shots, stats_by_site


def main():
    start = time.time()
    logger.info("========== 任务开始 ==========")
    print("=" * 70)
    print("Amazon 榜单品牌排名查询")
    print("=" * 70)

    results_sorted, shots, stats_by_site = run_pipeline()

    print("\n" + "=" * 70)
    print("查询结果")
    print("=" * 70)
    if results_sorted:
        # 按站穿插: 每站 喜报头部(自包裹) → 该站命中商品图文
        for site in SITES:
            site_results = [r for r in results_sorted if r["site_order"] == site.order]
            if not site_results:
                continue
            header = build_site_header(site, stats_by_site.get(site.order) or {})
            if header:
                print("\n" + header + "\n")
            for r in site_results:
                for line in _result_lines(r, shots.get(r["asin"])):
                    print(line)
    else:
        print("\n(本次抓取范围内无目标品牌上榜)")

    elapsed = time.time() - start
    logger.info("========== 任务结束, 耗时 %.1fs ==========", elapsed)
    print(f"\n总耗时: {elapsed:.1f}秒")
    print(f"日志: {log_filepath}")


if __name__ == "__main__":
    main()
