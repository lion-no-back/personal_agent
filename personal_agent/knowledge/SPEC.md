# Knowledge Base — Technical Specification

> **Coding agent 阅读指引**：本文件是知识库模块的唯一权威设计文档。
> 实现时只读本文件 + 现有代码库，不需要其他背景信息。
> 每个任务（Task）对应一个模块，任务间依赖关系见第 9 节。

---

## 1. 定位

本模块是 PersonalAgent 的 RAG 知识库（Retrieval-Augmented Generation）。

- **写路径**：本地文件（PDF/Word/Excel/MD/图片）→ 解析 → 分块 → 可选 CoT 合成 → 向量化 → SQLite 存储
- **读路径**：用户查询 → 分解子查询 → 关键词+向量检索 → RRF 融合 → LLM rerank → 注入 prompt

Agent 通过 `knowledge_search` 工具调用读路径，无需感知实现细节。

---

## 2. 目录结构

```
personal_agent/knowledge/
├── SPEC.md                    ← 本文件（设计参考，不参与运行）
├── __init__.py                ← 导出 KnowledgeBase 单例
├── __main__.py                ← CLI：python -m personal_agent.knowledge
├── types.py                   ← 所有数据类型（Task 1）
├── store.py                   ← SQLite + sqlite-vec 存取（Task 2）
├── watcher.py                 ← 文件监听 + debounce（Task 7）
├── ingester/
│   ├── __init__.py
│   ├── pipeline.py            ← 入库主流程 + CoT 分流（Task 5）
│   ├── chunker.py             ← 分块策略（Task 3）
│   ├── cot.py                 ← 两阶段 CoT（Task 5）
│   ├── embedder.py            ← 向量化 + memo 缓存（Task 4）
│   └── parsers/
│       ├── __init__.py        ← get_parser(path) 路由函数
│       ├── types.py           ← ParsedElement（Task 1 中定义）
│       ├── pdf_parser.py      ← opendataloader-pdf + unstructured 降级（Task 3）
│       ├── office_parser.py   ← markitdown（Word/PPT/Excel）（Task 3）
│       ├── image_parser.py    ← markitdown LLM描述 + unstructured OCR（Task 3）
│       ├── text_parser.py     ← 纯文本 / Markdown（Task 3）
│       └── web_parser.py      ← markitdown HTML（Task 3）
└── retriever/
    ├── __init__.py
    ├── search.py              ← 检索入口，组合以下模块（Task 6）
    ├── keyword.py             ← 关键词评分（Task 6）
    ├── vector.py              ← 向量相似度（Task 6）
    ├── rrf.py                 ← RRF 融合（Task 6）
    ├── rerank.py              ← LLM 0/1 过滤（Task 6）
    └── query_decomposer.py    ← 查询分解（Task 6）

personal_agent/tools/
└── knowledge_tool.py          ← Agent 工具接口（Task 8）

knowledge_docs/
├── wiki/                      ← CoT 生成的结构化知识页（entities/concepts/sources/）
└── sources/                   ← 原始文档根目录，KBWatcher 监听此目录
    ├── pdf/
    ├── word/
    ├── excel/
    ├── markdown/
    └── images/
```

---

## 3. 核心数据类型（Task 1）

```python
# personal_agent/knowledge/ingester/parsers/types.py
# ParsedElement 只在 ingestion 管道内流转（parser 输出 → chunker 输入），不出 ingester/
from dataclasses import dataclass, field

@dataclass
class ParsedElement:
    type: str            # heading|paragraph|table|image_description|image_ocr|list_item|code
    content: str
    page_num: int | None
    level: int | None    # heading 级别 1-6
    bbox: list | None    # [x1,y1,x2,y2]，PDF 溯源用
    metadata: dict = field(default_factory=dict)
```

```python
# personal_agent/knowledge/types.py
# 系统级类型：store、retriever、tool 三层共用
from dataclasses import dataclass, field

@dataclass
class KBChunk:
    id: str              # sha256(source_path + ":" + str(chunk_index))
    source_hash: str     # sha256(文件二进制)，增量判断用
    source_path: str     # 相对于项目根的路径
    source_type: str     # pdf|word|excel|markdown|image|web
    wiki_page: str | None  # CoT 生成后填充，指向 wiki/ 下的页面路径
    title: str           # 块标题（检索权重放大用）
    content: str         # 原始文本
    compiled: str | None # CoT 合成内容，未走 CoT 时为 None
    chunk_index: int
    page_num: int | None
    line_start: int | None
    embedding: list[float] | None = None  # 向量化后填充；None = 尚未向量化，upsert_chunks 跳过向量写入
    tags: list[str] = field(default_factory=list)
    created_at: str = ""

@dataclass
class KBSource:
    path: str            # 原始文件路径（主键）
    hash: str
    last_synced: str
    cot_used: bool
    chunk_ids: list[str] # 血缘：该文件派生的所有 chunk id

@dataclass
class KBSearchResult:
    chunk: KBChunk
    score: float
    match_type: str      # keyword|vector|hybrid
```

---

## 4. SQLite Schema（Task 2）

```sql
-- sqlite-vec 扩展，需先 load_extension
CREATE VIRTUAL TABLE IF NOT EXISTS kb_vec USING vec0(
    chunk_id TEXT,
    embedding FLOAT[1536]
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id          TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_type TEXT NOT NULL,
    wiki_page   TEXT,
    title       TEXT,
    content     TEXT NOT NULL,
    compiled    TEXT,
    chunk_index INTEGER,
    page_num    INTEGER,
    line_start  INTEGER,
    tags        TEXT DEFAULT '[]',   -- JSON array
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS kb_sources (
    path        TEXT PRIMARY KEY,
    hash        TEXT NOT NULL,
    last_synced TEXT,
    cot_used    INTEGER DEFAULT 0,
    chunk_ids   TEXT DEFAULT '[]'    -- JSON array
);

-- embedding 复用缓存（避免相同文本重复调 API）
CREATE TABLE IF NOT EXISTS kb_embed_cache (
    content_hash TEXT PRIMARY KEY,   -- sha256(content + model_name)
    embedding    BLOB NOT NULL,
    created_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON kb_chunks(source_path);
CREATE INDEX IF NOT EXISTS idx_chunks_title  ON kb_chunks(title);
```

---

## 5. Parser 层（Task 3）

### 5.1 格式路由表

| 后缀 | Parser 函数 | 使用的库 | 降级策略 |
|---|---|---|---|
| `.pdf` | `pdf_parser.parse_pdf` | opendataloader-pdf | unstructured hi_res（扫描件/失败时） |
| `.docx` `.doc` | `office_parser.parse_office` | markitdown | — |
| `.pptx` `.ppt` | `office_parser.parse_office` | markitdown | — |
| `.xlsx` `.xls` `.csv` | `office_parser.parse_excel` | markitdown | — |
| `.jpg` `.jpeg` `.png` `.webp` `.gif` | `image_parser.parse_image` | markitdown(LLM描述) + unstructured(OCR) | 两者独立，各自失败不影响另一个 |
| `.md` `.txt` | `text_parser.parse_text` | 内置 re | — |
| `.html` `.htm` | `web_parser.parse_html` | markitdown | — |

### 5.2 各 Parser 输入/输出契约

```python
# 所有 parser 的统一签名
def parse_xxx(path: Path, **kwargs) -> list[ParsedElement]: ...

# image_parser 额外接受 llm_client（用于 markitdown LLM 描述）
def parse_image(path: Path, llm_client=None) -> list[ParsedElement]: ...

# get_parser 路由函数（parsers/__init__.py）
def get_parser(path: Path) -> Callable | None:
    return PARSER_ROUTES.get(path.suffix.lower())
```

### 5.3 关键实现说明

**pdf_parser**：
- 主路径：`opendataloader_pdf.convert(format=["json"], reading_order="xycut", table_method="default", hybrid="docling-fast")`
- 从 JSON `kids[]` 数组提取元素，字段映射：`type/content/page number/heading level/bounding box`
- 降级条件：`convert()` 抛出异常 → 调 `partition_pdf(strategy="hi_res", languages=["chi_sim","eng"])`

**office_parser**：
- `MarkItDown().convert(str(path)).text_content` 得到 Markdown 字符串
- 用 `_markdown_to_elements()` 按标题/段落/表格行拆分为 ParsedElement 列表

**image_parser**：
- 策略 1（语义理解）：`MarkItDown(llm_client=..., llm_model="claude-haiku-4-5-20251001").convert(str(path))`
- 策略 2（文字提取）：`partition_image(strategy="hi_res", languages=["chi_sim","eng"])`
- 两个策略各自 try/except，互不依赖，结果合并返回

---

## 6. 分块策略（Task 3 / chunker.py）

```
输入：
  elements:    list[ParsedElement]
  source_path: str
  source_type: str
  source_hash: str   ← pipeline 在 Step 1 已算好，直接传入；chunker 不做文件 IO
输出：list[KBChunk]（embedding=None，chunk_index 已设定）

规则：
1. 按 ParsedElement 类型处理：
   - heading：作为后续 paragraph 的标题前缀，不单独成块
   - table：单独成一个块，不拆分
   - image_description / image_ocr：单独成一个块
   - paragraph / list_item / code：累积后按大小分割

2. 目标块大小：512 token（用 len(text)//4 估算 token 数，不精确但足够）
   重叠：64 token（前一块最后 64 token 追加到下一块开头）

3. 每块 title 字段：
   - 取最近一个 heading 元素的 content
   - 无 heading 时取 source_path 文件名（去扩展名）

4. 过滤：content.strip() 为空的块丢弃
```

---

## 7. CoT 分流逻辑（Task 5 / pipeline.py）

```
条件 A（短文档）：
  len(chunks) < 5 OR sum(len(c.content) for c in chunks) // 4 < 1000
  → 跳过 CoT，chunk.compiled = None

条件 B（纯结构化数据）：
  source_type in {"excel", "csv"}
  → 跳过 CoT，chunk.compiled = None

条件 C（高重复内容）：
  对前 10 个 chunk 做向量搜索（top-1），若命中率 > 80% 且相似度 > 0.95
  → 跳过 CoT，chunk.compiled = None

以上条件满足任一 → 跳过。否则执行两阶段 CoT。
```

---

## 8. 两阶段 CoT（Task 5 / cot.py）

```
输入：
  - chunks: list[KBChunk]（content 已填，embedding 未填）
  - existing_wiki_index: str  （wiki/index.md 内容，不存在时为空串）
  - llm: LLMRouter

输出：
  - chunks（compiled 字段已填充）
  - wiki_pages: dict[str, str]  （路径 → 内容，需写入磁盘）

Stage A — Analysis（分析）：
  Prompt 结构：
    系统角色：知识提取专家
    输入：文档所有 chunk content 拼接（超 60K token 分批流式处理）
          + 现有 wiki/index.md 目录
    输出要求（JSON）：
      {
        "entities": [{"name", "type", "description", "wiki_path"}],
        "key_facts": ["..."],
        "connections_to_existing": ["..."],
        "contradictions": ["..."],
        "pages_to_create": ["wiki/entities/xxx.md", ...],
        "pages_to_update": ["wiki/concepts/yyy.md", ...]
      }

Stage B — Generation（生成）：
  对每个推荐页面：
    1. 读取现有文件内容（不存在则为空串）
    2. Prompt 结构：
       "现有页面内容：{existing}
        基于以下分析补充/修正，保留所有现有有价值信息，
        矛盾时两种说法都保留并标注来源，使用 [[wikilink]] 语法。
        分析结果：{stage_a_json}
        输出格式：---PAGE: {path}---\n{markdown content}"
    3. 解析 ---PAGE: xxx--- 块，写入 knowledge_docs/wiki/

  完成后：将 Stage A key_facts 合并作为 compiled 字段填入相关 chunks
```

---

## 9. 向量化（Task 4 / embedder.py）

```python
# 接口契约
async def embed_chunks(chunks: list[KBChunk], model: str = "text-embedding-3-small") -> list[KBChunk]:
    """填充 chunks 的 embedding 字段（通过 OpenAI embedding API）
    - 先查 kb_embed_cache，命中直接返回，不调 API
    - cache_key = sha256(chunk.content + model)
    - 批量处理：每批 200 个，控制并发
    - 返回填充了 embedding（list[float]）的 chunks
    """

# 注意：embedding 以 bytes 存入 SQLite（struct.pack），读出时 struct.unpack
# sqlite-vec 存储时用 json.dumps(embedding)
```

**模型选型**：`text-embedding-3-small`（OpenAI，1536 维，中英文双语验证充分）
⚠️ 不使用 voyage-3-lite（主要针对英文，中文召回率未验证）

---

## 10. 检索流程（Task 6）

### 10.1 查询分解（query_decomposer.py）

```
输入：query: str
输出：list[str]  最多 3 条子查询

Prompt 约束：
- 每条子查询只含 1 个概念
- 不使用布尔运算符（AND/OR/NOT）
- 保留专有名词和技术术语
- 直接用原始语言，不翻译
- 输出 JSON array
```

### 10.2 关键词评分（keyword.py）

```python
FILENAME_EXACT_BONUS = 200.0
PHRASE_IN_TITLE_BONUS = 50.0
TITLE_TOKEN_WEIGHT   = 5.0
CONTENT_TOKEN_WEIGHT = 1.0

# CJK 分词：按字拆成二元组 + 单字符
# 评分：以上四项求和，返回 (chunk_id, score) 列表
```

### 10.3 向量检索（vector.py）

```
查询 sqlite-vec：SELECT chunk_id, distance FROM kb_vec WHERE embedding MATCH ? LIMIT 40
多块融合：blended = top_chunk_score + sum(other_scores) * 0.3
返回：(chunk_id, blended_score) 列表
```

### 10.4 RRF 融合（rrf.py）

```python
K = 60.0
# score += 1.0 / (K + rank)，对关键词和向量结果各自排名后累加
# 两种信号都命中 → 自然权重翻倍
```

### 10.5 LLM Rerank（rerank.py）

```
输入：query, chunks（top 20）
Prompt：
  "查询：{query}
   以下片段中，哪些与查询直接相关？
   返回 JSON 数字数组，仅含相关编号（0-based），如 [0,2,5]。
   [0] {title}: {content[:200]}
   ..."
模型：claude-haiku-4-5-20251001（最小成本）
超时/解析失败 → 降级，返回 RRF 原始顺序（不抛异常）
```

### 10.6 检索入口（search.py）

```python
async def search(query: str, top_k: int = 5) -> list[KBSearchResult]:
    sub_queries = await decompose_query(query)        # 最多 3 条
    kw_results  = keyword_search_all(sub_queries, n=20)
    vec_results = await vector_search_all(sub_queries, n=20)
    fused       = rrf_merge(kw_results, vec_results)   # top 20
    reranked    = await rerank(query, fused)            # 0/1 过滤
    return reranked[:top_k]
```

---

## 11. 入库主流程（Task 5 / pipeline.py）

```python
async def ingest(path: Path, force: bool = False) -> IngestResult:
    # 1. 变更检测
    current_hash = sha256_file(path)
    source = store.get_source(path)
    if not force and source and source.hash == current_hash:
        return IngestResult(skipped=True)

    # 2. 解析
    parser = get_parser(path)
    if parser is None:
        return IngestResult(skipped=True, reason="unsupported format")
    elements = parser(path, llm_client=llm_client_for_images)

    # 3. 分块
    chunks = chunker.chunk(elements, source_path=str(path), source_type=detect_type(path))

    # 4. CoT 分流判断（三条件）
    if not _should_use_cot(chunks, path):
        pass  # compiled 保持 None
    else:
        wiki_index = read_wiki_index()
        chunks, wiki_pages = await cot.run(chunks, wiki_index, llm)
        write_wiki_pages(wiki_pages)

    # 5. 向量化（含 memo 缓存）
    chunks = await embedder.embed_chunks(chunks)

    # 6. 血缘写入 & 旧 chunk 清理
    if source:
        store.delete_chunks(source.chunk_ids)
    store.upsert_chunks(chunks)
    store.upsert_source(KBSource(
        path=str(path), hash=current_hash,
        last_synced=now_iso(), cot_used=_cot_was_used,
        chunk_ids=[c.id for c in chunks],
    ))
    return IngestResult(chunks_added=len(chunks))

async def ingest_batch(paths: list[Path], force: bool = False):
    for path in paths:
        result = await ingest(path, force=force)
        print(f"[{'OK' if not result.skipped else 'SKIP'}] {path} → {result}")
```

---

## 12. 文件监听（Task 7 / watcher.py）

```python
DEBOUNCE_SECONDS = 2.0
WATCH_EXTENSIONS = {".pdf",".docx",".doc",".pptx",".ppt",
                    ".xlsx",".xls",".csv",".md",".txt",
                    ".html",".htm",".jpg",".jpeg",".png",".webp",".gif"}

# 使用 watchdog 库的 FileSystemEventHandler
# 核心：每个 path 的事件用 asyncio.TimerHandle 做 debounce
# CREATE/MODIFY → call_later(2.0, ingest)
# DELETE        → call_later(2.0, delete_source_lineage)
# 同一 path 新事件到来时取消旧的 handle（去重）
```

---

## 13. CLI 接口（Task 7 / __main__.py）

```bash
# 全量入库（首次初始化）
python -m personal_agent.knowledge ingest --all

# 单文件入库
python -m personal_agent.knowledge ingest --path knowledge_docs/sources/xxx.pdf

# 强制重新入库（忽略 hash，用于 prompt 升级后重建）
python -m personal_agent.knowledge ingest --path xxx.pdf --force

# 查看状态
python -m personal_agent.knowledge status
# 输出：来源数 / chunk 数 / 向量缓存命中率 / 最后更新时间
```

---

## 14. Agent 工具接口（Task 8）

```python
# personal_agent/tools/knowledge_tool.py

class KnowledgeSearchTool(BaseTool):
    name = "knowledge_search"
    description = (
        "检索内部知识库中的文档、运营规则、产品数据。"
        "适用于：已导入的 PDF/Word/Excel/文档内容、内部规则、历史分析报告。"
        "不适用于：实时网络信息、当前时间查询。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题，用自然语言描述"},
            "top_k": {"type": "integer", "default": 5, "description": "返回结果数"},
        },
        "required": ["query"],
    }

    async def run(self, query: str, top_k: int = 5) -> str:
        results = await kb.retriever.search(query, top_k=top_k)
        if not results:
            return "知识库中未找到相关内容。"
        parts = []
        for i, r in enumerate(results, 1):
            src = r.chunk.source_path
            page = f"，第{r.chunk.page_num}页" if r.chunk.page_num else ""
            body = r.chunk.compiled or r.chunk.content[:400]
            parts.append(f"**{i}. {r.chunk.title}** [来源: {src}{page}]\n{body}")
        return "\n\n---\n\n".join(parts)

# 注册到 tools/registry.py：在现有工具列表末尾追加 KnowledgeSearchTool()
```

---

## 15. 与现有系统集成点（Task 8）

```python
# main.py 中追加（在 SchedulerDaemon.start() 之后）：

from personal_agent.knowledge import KnowledgeBase

kb = KnowledgeBase(
    db_path=Path("knowledge_docs/kb.sqlite"),
    wiki_dir=Path("knowledge_docs/wiki"),
    sources_dir=Path("knowledge_docs/sources"),
    llm=llm_router,                      # 复用现有 LLMRouter
    embed_model="text-embedding-3-small",
)
kb.start_watcher()  # 启动后台文件监听线程
```

`KnowledgeBase` 单例在 `__init__.py` 中通过模块级变量暴露，`knowledge_tool.py` 直接 import。

---

## 16. 依赖（Task 0 — 环境准备）

```
# 新增到 requirements.txt / pyproject.toml
opendataloader-pdf          # PDF 精准解析
markitdown[all]             # Office/图片/HTML 万能转 Markdown
unstructured[pdf,image]     # PDF OCR 降级 + 图片 OCR
sqlite-vec                  # SQLite 向量扩展
watchdog                    # 文件系统监听
openai                      # text-embedding-3-small（embedding API）
# python-docx 已被 markitdown[all] 包含，无需单独安装
```

---

## 17. 任务依赖顺序

```
Task 0: 环境准备（安装依赖，验证 sqlite-vec 可加载）
  ↓
Task 1: types.py（KBChunk / KBSource / ParsedElement / KBSearchResult）
  ↓
Task 2: store.py（SQLite schema + CRUD：upsert_chunks / get_source / delete_chunks 等）
  ↓
Task 3: parsers/（所有 parser + chunker.py，依赖 Task 1）
  ↓
Task 4: embedder.py（依赖 Task 2 的 cache 表，Task 1 的 KBChunk）
  ↓
Task 5: pipeline.py + cot.py（依赖 Task 2/3/4）
  ↓
Task 6: retriever/（依赖 Task 2/4）
  ↓
Task 7: watcher.py + __main__.py（依赖 Task 5）
  ↓
Task 8: knowledge_tool.py + main.py 集成（依赖 Task 6/7）
```

---

## 18. 每个 Task 的验收标准

| Task | 验收方式 |
|---|---|
| 0 | `python -c "import sqlite_vec; import markitdown; import opendataloader_pdf"` 无报错 |
| 1 | 所有 dataclass 可实例化，字段类型正确 |
| 2 | 能创建 DB，能写入和读出 KBChunk，向量 MATCH 查询返回结果 |
| 3 | 对样本 PDF/docx/xlsx/png 分别调用 parser，返回非空 ParsedElement 列表 |
| 4 | 对同一文本调用两次 embed_chunks，第二次不发起 API 请求（缓存命中） |
| 5 | `ingest(path)` 正常完成，DB 中有对应 chunks，kb_sources 有血缘记录 |
| 6 | `search("测试查询")` 返回非空 KBSearchResult 列表，score 字段有值 |
| 7 | 放一个文件到 sources/ 目录，2 秒后 DB 中出现对应 chunks |
| 8 | 在飞书/钉钉发送"查一下XXX"，Agent 调用 knowledge_search 工具并返回结果 |
