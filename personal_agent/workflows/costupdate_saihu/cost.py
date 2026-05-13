import os
import sys
from pathlib import Path
import asyncio
import aiohttp
import requests
import json
import logging
import re
import html
import pandas as pd
from tqdm.asyncio import tqdm

# ==========================================
# 🚨 强制依赖检测
# ==========================================
try:
    import lxml
    from bs4 import BeautifulSoup
    import openpyxl 
except ImportError:
    print("\n❌ 严重错误：未安装必备的高速解析器 lxml, bs4 或 openpyxl！")
    sys.exit(1)

# ==========================================
# 📝 1. 日志与全局配置
# ==========================================
class FlushFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

logger = logging.getLogger("MaBangSpider")
logger.setLevel(logging.INFO)

# 禁止日志冒泡污染全局控制台
logger.propagate = False

BASE_URL = "https://private.mabangerp.com"
AMZ_BASE_URL = "https://private-amz.mabangerp.com"
CHECK_TYPE_API = f"{AMZ_BASE_URL}/index.php?mod=fbanew.listsearch"
COMBO_API_URL = f"{AMZ_BASE_URL}/index.php?mod=combosku.getCombosSkuList"
STOCK_API_URL = f"{AMZ_BASE_URL}/index.php?mod=stock.getStockList"

file_lock = asyncio.Lock()

# 提速参数
CONCURRENCY_LIMIT = 40           
MAX_RETRIES = 3                  
BATCH_SIZE_TYPE = 300            
BATCH_SIZE_STOCK = 100            
TIMEOUT_SETTING = aiohttp.ClientTimeout(total=120, sock_connect=10, sock_read=90)
WRITE_FLUSH_EVERY = 50           


# ==========================================
# 🔑 2. 获取跨域全域鉴权
# ==========================================
def get_cookie(output_dir: str):
    print("  ⏳ 正在获取马帮跨域授权...")
    logger.info("=== 启动程序，开始获取鉴权 ===")
    session = requests.Session()
    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Connection": "keep-alive"
    }
    
    try:
        session.get(f"{BASE_URL}/", headers=base_headers, timeout=10)
        login_url = f"{BASE_URL}/index.php?mod=main.doLogin&lang=undefined"
        login_headers = base_headers.copy()
        login_headers.update({
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest"
        })
        data = {"username": "15525692520", "password": "Tg15525692520&", "verifyType": "1"}
        resp = session.post(login_url, headers=login_headers, data=data, timeout=10)
        if not resp.json().get("success"):
            logger.error("主站登录失败，请检查密码")
            return None, None
    except Exception as e:
        logger.error(f"登录发生异常: {e}")
        return None, None

    cookies_dict = requests.utils.dict_from_cookiejar(session.cookies)
    memcache_key = cookies_dict.get("MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE")

    transition_url = (f"{AMZ_BASE_URL}/index.php?mod=fbanew.list&platform=amazon&version=1"
                      f"&cMKey={memcache_key}&fromTest=111&lang=cn&tz=UTC+8&DOMAIN=private.mabangerp.com")
    try:
        session.get(transition_url, headers=base_headers, timeout=10)
        final_cookies = requests.utils.dict_from_cookiejar(session.cookies)
        print("  ✅ 授权成功，引擎已就绪！\n")
        return "; ".join([f"{k}={v}" for k, v in final_cookies.items()]), memcache_key
    except Exception as e:
        logger.error(f"跨域请求失败: {e}")
        return None, None


# ==========================================
# ⚡ 3. 后台解析器与网络请求
# ==========================================
def _parse_type_html_fast(html_content, msku_batch):
    html_content = html.unescape(html_content)
    result = {m: "判断超时" for m in msku_batch}
    found = set()
    target_skus = set(msku_batch)
    
    rows = html_content.split('<tr ')
    for row in rows[1:]:
        msku_match = re.search(r'data-msku="([^"]+)"', row)
        if not msku_match:
            msku_match = re.search(r'msku="([^"]+)"', row)
            
        if msku_match:
            row_msku = msku_match.group(1).strip()
            if row_msku in target_skus:
                is_combo = ('class="label label-success grouplabel"' in row) or ('>组<' in row) or ('grouplabel' in row and '组' in row)
                if row_msku not in found:
                    result[row_msku] = "组合SKU" if is_combo else "普通SKU"
                    found.add(row_msku)
                elif is_combo and result[row_msku] == "普通SKU":
                    result[row_msku] = "组合SKU"
                
    for m in msku_batch:
        if m not in found and result[m] == "判断超时":
            result[m] = "无此SKU"
    return result

def _parse_combo_html_sync(html_content, local_sku):
    soup = BeautifulSoup(html_content, 'lxml')
    target_data_id = None
    for tag in soup.select('a.comboskuCopy'):
        if tag.text.strip().upper() == local_sku.upper():
            parent_tr = tag.find_parent('tr')
            if parent_tr and parent_tr.has_attr('data-id'):
                target_data_id = parent_tr['data-id']
            break

    if not target_data_id: return None

    children_skus = []
    child_links = soup.select(f'tr[data-id="{target_data_id}"] a.small-edit-sku')
    for link in child_links:
        sku_text = link.get_text(strip=True)
        if sku_text: 
            clean_sku = sku_text.split('*')[0].split('×')[0].strip()
            children_skus.append(clean_sku)
    return children_skus

async def batch_check_sku_types(session, msku_batch):
    platform_sku_data = "\r\n".join(msku_batch)
    form_data = aiohttp.FormData()
    form_data.add_field('shopId', '')
    form_data.add_field('status', '1')
    form_data.add_field('ispair', '')
    form_data.add_field('isChange', '1')
    form_data.add_field('searchtexttype', 'platformSkuLike')
    form_data.add_field('atn', 'list')
    form_data.add_field('searchtype', '1')
    form_data.add_field('page', '1')
    form_data.add_field('rowsPerPage', str(max(len(msku_batch) * 2, 300)))
    form_data.add_field('selecttype', 'platformSkuIn')
    form_data.add_field('platformSkuData', platform_sku_data)

    result = {m: "判断超时" for m in msku_batch}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(CHECK_TYPE_API, data=form_data, timeout=TIMEOUT_SETTING) as resp:
                if resp.status != 200: continue
                data = await resp.json(content_type=None)
                if not data.get('success'): continue
                html_content = data.get('html', '')
                if not html_content.strip() or '暂无数据' in html_content:
                    return {m: "无此SKU" for m in msku_batch}
                result = await asyncio.to_thread(_parse_type_html_fast, html_content, msku_batch)
                return result
        except Exception:
            if attempt < MAX_RETRIES: await asyncio.sleep(2 * attempt)
    return result

def _resolve_price(item, sku_label):
    try: purchase_price = float(item.get("purchasePrice", 0.0) or 0.0)
    except: purchase_price = 0.0
    try: default_cost = float(item.get("defaultCost", 0.0) or 0.0)
    except: default_cost = 0.0
    if purchase_price > 0: return purchase_price, ""
    if default_cost > 0: return default_cost, "无采购价已取默认成本兜底"
    return 0.0, "采购价与默认成本均为0"

async def batch_fetch_stock_prices(session, sku_batch):
    stock_data_str = "\r\n".join([s.strip() for s in sku_batch if s.strip()])
    form_data = aiohttp.FormData()
    form_data.add_field("searchKey", "Stock_stockSku")
    form_data.add_field("operate", "=")
    form_data.add_field("search-content", "库存SKU")
    form_data.add_field("isBatchSearch", "1")
    form_data.add_field("selecttype", "stockSku")
    form_data.add_field("stockData", stock_data_str)

    result = {s: None for s in sku_batch} 
    upper_index = {s.strip().upper(): s for s in sku_batch}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(STOCK_API_URL, data=form_data, timeout=TIMEOUT_SETTING) as resp:
                if resp.status != 200: continue
                text = await resp.text()
                clean_text = text[text.find('{'):text.rfind('}') + 1]
                res_json = json.loads(clean_text)
                if not res_json.get("success") or not res_json.get("hasData"): return result
                stock_data = res_json.get("stockData", []) or []
                for item in stock_data:
                    sku_key = str(item.get("stockSku", "")).strip().upper()
                    if sku_key in upper_index:
                        original_sku = upper_index[sku_key]
                        if result[original_sku] is None:
                            price, reason = _resolve_price(item, original_sku)
                            result[original_sku] = (price, reason)
                return result
        except Exception:
            if attempt < MAX_RETRIES: await asyncio.sleep(2 * attempt)
    return result

async def fetch_combo_children_skus(session, local_sku):
    form_data = aiohttp.FormData()
    form_data.add_field("searchLike", "comboSku")
    form_data.add_field("operate", "Like")
    form_data.add_field("searchKeywords", local_sku)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(COMBO_API_URL, data=form_data, timeout=TIMEOUT_SETTING) as resp:
                if resp.status != 200: continue
                text = await resp.text()
                clean_text = text[text.find('{'):text.rfind('}') + 1]
                res_json = json.loads(clean_text)
                html_content = res_json.get("message", "")
                if html_content:
                    children = await asyncio.to_thread(_parse_combo_html_sync, html_content, local_sku)
                    if children is not None: 
                        logger.info(f"[阶段C1] 成功提取组合SKU {local_sku} 的子件: {children}")
                        return children
        except Exception: pass
        if attempt < MAX_RETRIES: await asyncio.sleep(2)
    logger.error(f"[阶段C1] 组合SKU {local_sku} 彻底提取失败")
    return None

# ==========================================
# 💾 4. 双流缓存写入器
# ==========================================
class CacheWriter:
    def __init__(self, data_path, error_path, flush_every=WRITE_FLUSH_EVERY):
        self.data_path = data_path
        self.error_path = error_path
        self.data_buffer = []
        self.error_buffer = []
        self.flush_every = flush_every

    async def add(self, msku, result):
        async with file_lock:
            if isinstance(result, (int, float)):
                self.data_buffer.append((msku, result))
            else:
                self.error_buffer.append((msku, result))
                
            if max(len(self.data_buffer), len(self.error_buffer)) >= self.flush_every:
                self._flush_locked()

    def _flush_locked(self):
        if self.data_buffer:
            with open(self.data_path, 'a', encoding='utf-8-sig', newline='') as f:
                for msku, cost in self.data_buffer: f.write(f"{msku},{cost}\n")
            self.data_buffer.clear()
            
        if self.error_buffer:
            with open(self.error_path, 'a', encoding='utf-8-sig', newline='') as f:
                for msku, reason in self.error_buffer: f.write(f"{msku},{reason}\n")
            self.error_buffer.clear()

    async def flush(self):
        async with file_lock: self._flush_locked()

# ==========================================
# 🚀 5. 主流水线 
# ==========================================
async def run_pipeline(task_list, cookie_string, memcache_key, already_done, cache_file, error_cache_file):
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": AMZ_BASE_URL, 
        "Cookie": cookie_string
    }
    connector = aiohttp.TCPConnector(limit=CONCURRENCY_LIMIT, limit_per_host=CONCURRENCY_LIMIT)
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    writer = CacheWriter(cache_file, error_cache_file)

    if not os.path.exists(cache_file):
        with open(cache_file, 'w', encoding='utf-8-sig', newline='') as f: f.write("MSKU,最终采购成本\n")
    if not os.path.exists(error_cache_file):
        with open(error_cache_file, 'w', encoding='utf-8-sig', newline='') as f: f.write("MSKU,原因\n")

    cleaned = [(m.strip(), l.strip()) for m, l in task_list]
    msku_to_local = {m: l for m, l in cleaned}
    all_mskus = [m for m, _ in cleaned]
    final_results = {}
    
    try:
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:

            # ---------- 阶段 A ----------
            print(f"  🔎 阶段 A: 批量判别 SKU 类型 (每批 {BATCH_SIZE_TYPE} 条)...")
            type_map = {}
            async def _do_type_batch(batch):
                async with semaphore: return await batch_check_sku_types(session, batch)
            type_batches = [all_mskus[i:i + BATCH_SIZE_TYPE] for i in range(0, len(all_mskus), BATCH_SIZE_TYPE)]
            if type_batches:
                for fut in tqdm(asyncio.as_completed([_do_type_batch(b) for b in type_batches]), total=len(type_batches), desc="A.类型判定"):
                    type_map.update(await fut)

            normal_mskus = [m for m, t in type_map.items() if t == "普通SKU"]
            combo_mskus = [m for m, t in type_map.items() if t == "组合SKU"]
            
            abnormal_count = 0
            for m, t in type_map.items():
                if t in ["无此SKU", "判断超时"]: 
                    final_results[m] = "库存SKU获取不到采购价"
                    abnormal_count += 1
            print(f"     ↳ 普通 {len(normal_mskus)} | 组合 {len(combo_mskus)} | 异常 {abnormal_count}")

            # ---------- 阶段 C1 ----------
            print(f"  🧩 阶段 C1: 极速解析组合 SKU 子件清单...")
            combo_local_skus = {msku_to_local[m] for m in combo_mskus}
            combo_local_to_children = {}
            tasks_to_fetch = list(combo_local_skus)

            print(f"     ↳ 需连网查询: {len(tasks_to_fetch)} 条")

            if tasks_to_fetch:
                async def _do_fetch_children(ls):
                    async with semaphore: return ls, await fetch_combo_children_skus(session, ls)
                
                children_tasks = [_do_fetch_children(ls) for ls in tasks_to_fetch]
                for fut in tqdm(asyncio.as_completed(children_tasks), total=len(children_tasks), desc="C1.网络请求提取子件"):
                    ls, children = await fut
                    if children is not None:
                        combo_local_to_children[ls] = children

            # ---------- 阶段 B ----------
            print(f"  💰 阶段 B: 批量获取底层库存采购价 (每批 {BATCH_SIZE_STOCK} 条)...")
            normal_local_skus = {msku_to_local[m] for m in normal_mskus}
            all_child_skus = set()
            for children in combo_local_to_children.values():
                if children: all_child_skus.update(children)

            all_query_skus = sorted(normal_local_skus | all_child_skus)
            price_cache = {}

            async def _do_stock_batch(batch):
                async with semaphore: return await batch_fetch_stock_prices(session, batch)

            stock_batches = [all_query_skus[i:i + BATCH_SIZE_STOCK] for i in range(0, len(all_query_skus), BATCH_SIZE_STOCK)]
            if stock_batches:
                stock_tasks = [_do_stock_batch(b) for b in stock_batches]
                for fut in tqdm(asyncio.as_completed(stock_tasks), total=len(stock_tasks), desc="B.批量库存取价"):
                    price_cache.update(await fut)

            # ---------- 阶段 C2 结算 ----------
            logger.info("========== 开始执行最后结算与异常排查 ==========")
            
            normal_success = normal_fail = 0
            for m in normal_mskus:
                ls = msku_to_local[m]
                p_data = price_cache.get(ls)
                if p_data is None:
                    final_results[m] = "库存SKU获取不到采购价"
                    normal_fail += 1
                else:
                    price, reason = p_data
                    if price is None or price == 0.0:
                        final_results[m] = "库存SKU获取不到采购价"
                        normal_fail += 1
                    else:
                        final_price = price + 1.0
                        final_results[m] = round(final_price, 2)
                        normal_success += 1
                        logger.info(f"[普通成功] {m} -> 原始: {price} | 最终(加价后): {final_results[m]}")
                        
            print(f"     ↳ 普通件：成功 {normal_success} | 失败 {normal_fail}")

            combo_success = combo_fail = 0
            for m in combo_mskus:
                ls = msku_to_local[m]
                children = combo_local_to_children.get(ls)
                
                if not children:
                    final_results[m] = "组合解析失败(未提取到子件)"
                    combo_fail += 1
                    continue

                total = 0.0
                blocked = False
                
                for child_sku in children:
                    p_data = price_cache.get(child_sku)
                    if p_data is None:
                        blocked = True
                        break
                    price, reason = p_data
                    if price is None:
                        blocked = True
                        break
                    total += price

                if blocked:
                    final_results[m] = "库存SKU获取不到采购价"
                    combo_fail += 1
                else:
                    if total == 0.0:
                        final_results[m] = "库存SKU获取不到采购价"
                        combo_fail += 1
                    else:
                        child_count = len(children)
                        final_combo_price = total + 1.0 + 1 * (child_count - 1)
                        final_results[m] = round(final_combo_price, 2)
                        combo_success += 1
                        logger.info(f"[组合成功] {m} -> 纯材料: {total} | 件数: {child_count} | 最终(加处理费后): {final_results[m]}")
                        
            print(f"     ↳ 组合件：成功 {combo_success} | 失败 {combo_fail}")

    finally:
        logger.info("======= 正在执行收尾刷盘操作，请勿关闭窗口！ =======")
        for m, _ in cleaned: 
            if m not in already_done and m in final_results:
                await writer.add(m, final_results[m])
        await writer.flush()

# ==========================================
# ✂️ 6. 拆分逻辑 (已升级为：原生模板克隆法)
# ==========================================
def auto_split_to_excel(csv_path, chunk_size=9900):
    if not os.path.exists(csv_path):
        print(f"  ⚠️ 找不到总表 {csv_path}，跳过拆分。")
        return

    output_dir = os.path.dirname(csv_path)
    template_path = str(Path(__file__).parent / "成本导入模板.xlsx")
    
    print(f"\n  ✂️ 开始将报表拆分为直接可上传的 Excel 格式 (每份不超过 {chunk_size} 条)...")
    
    if not os.path.exists(template_path):
         print(f"  ❌ 找不到模板文件 {template_path}，无法同步表头格式，跳过拆分。")
         return

    try:
        # 1. 读取我们生成的完整CSV数据 (Pandas仅作纯数据读取，不参与生成Excel)
        try:
            full_df = pd.read_csv(csv_path, encoding='utf-8-sig')
        except UnicodeDecodeError:
            full_df = pd.read_csv(csv_path, encoding='gbk')
            
        if len(full_df.columns) < 2:
             print(f"  ❌ CSV文件 {csv_path} 数据异常，不足两列。")
             return
             
        # 2. 提取前两列数据转换为 Python 原生列表，大幅提升后续逐行写入速度
        data_records = full_df.iloc[:, :2].values.tolist()
        total_rows = len(data_records)
        
        # 3. 按 chunk_size 分块处理
        chunks = [data_records[i:i + chunk_size] for i in range(0, total_rows, chunk_size)]
        
        for i, chunk in enumerate(chunks):
            target_file = os.path.join(output_dir, f"上传文件_第{i+1}部分.xlsx")
            
            # 💡 核心改变：每次分块都用 openpyxl 直接加载原始模板，保留 100% 的格式/公式/样式
            wb = openpyxl.load_workbook(template_path)
            ws = wb.active
            
            # 4. 从第 2 行开始逐行灌入数据 (假设第 1 行是表头)
            start_row = 2
            for row_idx, row_data in enumerate(chunk):
                # 写入第一列 (MSKU) 到 Column A
                ws.cell(row=start_row + row_idx, column=1, value=row_data[0])
                # 写入第二列 (最终采购成本) 到 Column B
                ws.cell(row=start_row + row_idx, column=2, value=row_data[1])
                
            # 5. “另存为”分块文件
            wb.save(target_file)
            wb.close() # 释放内存
            
            print(f"    -> 已生成分片: {os.path.basename(target_file)} (包含 {len(chunk)} 条)，格式已完美克隆模板！")
            
    except Exception as e:
        print(f"  ❌ Excel 拆分与模板克隆失败: {e}")

# ==========================================
# 🏁 7. 供主控调用的入口函数
# ==========================================
def run_cost(input_csv: str, output_dir: str):
    """暴露给主控调用的主函数"""
    
    # 动态配置日志路径
    log_file = os.path.join(output_dir, "spider_debug.log")
    if logger.hasHandlers():
        logger.handlers.clear()
    file_handler = FlushFileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(file_handler)

    output_csv = os.path.join(output_dir, "最终采购成本表.csv")
    error_csv = os.path.join(output_dir, "不更新MSKU表.csv")        
    cache_file = os.path.join(output_dir, "task_cache.csv")
    error_cache_file = os.path.join(output_dir, "error_cache.csv")  

    print(f"  📦 正在加载输入表 {input_csv}...")
    try: df = pd.read_csv(input_csv, encoding='utf-8')
    except UnicodeDecodeError: df = pd.read_csv(input_csv, encoding='gb18030')

    col_msku, col_status, col_local = df.columns[0], df.columns[1], df.columns[2]
    out_of_stock_df = df[df[col_status] == '断货'].copy()
    valid_df = df[(df[col_status] != '断货')].dropna(subset=[col_msku, col_local]).copy()
    task_tuples = list(zip(valid_df[col_msku].astype(str), valid_df[col_local].astype(str)))

    already_done = set()
    if os.path.exists(cache_file):
        try:
            cache_df = pd.read_csv(cache_file, header=0, dtype={'MSKU': str})
            already_done = set(cache_df['MSKU'].tolist())
        except Exception: pass
        
    if os.path.exists(error_cache_file):
        try:
            error_df = pd.read_csv(error_cache_file, header=0, dtype={'MSKU': str})
            already_done.update(error_df['MSKU'].tolist())
        except Exception: pass

    if not out_of_stock_df.empty:
        if not os.path.exists(error_cache_file):
            with open(error_cache_file, 'w', encoding='utf-8-sig', newline='') as f: f.write("MSKU,原因\n")
        with open(error_cache_file, 'a', encoding='utf-8-sig', newline='') as f:
            for m in out_of_stock_df[col_msku].astype(str).tolist():
                if m not in already_done:
                    f.write(f"{m},断货\n")
                    already_done.add(m)

    pending_tasks = [(m, l) for m, l in task_tuples if m.strip() not in already_done]
    print(f"  📊 待处理在售 SKU 数量: {len(pending_tasks)} 条\n")

    if pending_tasks:
        cookie_str, cMKey = get_cookie(output_dir)
        if not cookie_str: return
        try: 
            asyncio.run(run_pipeline(pending_tasks, cookie_str, cMKey, already_done, cache_file, error_cache_file))
        except KeyboardInterrupt: 
            print("\n  ⚠️ 任务被手动中断，所有进度已安全刷入缓存！")
        except Exception as e: 
            logger.error(f"全局异常: {e}", exc_info=True)

    print("\n  📝 正在生成最终报表...")
    if os.path.exists(cache_file):
        pd.read_csv(cache_file, header=0).to_csv(output_csv, index=False, encoding='utf-8-sig')
    if os.path.exists(error_cache_file):
        pd.read_csv(error_cache_file, header=0).to_csv(error_csv, index=False, encoding='utf-8-sig')
        
    print(f"  🎉 成本计算任务完毕！")
    
    # 🎯 触发模板克隆拆分逻辑
    auto_split_to_excel(output_csv, chunk_size=5100)