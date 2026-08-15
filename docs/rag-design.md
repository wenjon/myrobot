# RAG 知识库设计（草稿 v0.1 · 2026-08-15）

> 配套 `docs/token-cost.md`：本文解决「给小柚加本地知识库」的方案选型与接入设计，不动代码。
> 状态：Draft，待评审。

## 0. 目标

让数字人「小柚」能在不联网的情况下，基于**本地文档**回答用户问题（例如「KK 怎么定义失控？」「网易跨境电商部门负责人是谁？」「2021 元宇宙产业规模多大？」）。LLM 看到 KB 检索结果后，用口语化短句转述。

## 1. 样本分析（决定切块策略）

| 文件 | 格式 | 体量 | 结构特征 | 检索粒度 |
|---|---|---|---|---|
| `KK三部曲《失控+科技想要什么+必然》凯文·凯利(美).pdf` | PDF（扫描+文本混合）| **1321 页** | 书籍，无章节标签，文字密度大 | 段落/页面 |
| `deloitte-cn-tmt-xr-zh-211202.pdf` | PDF（文本）| **40 页** | 行业报告，有目录/章节/图表 | 段落，按章节标注 |
| `2016.06-网易全员名单1.3万人-送给闻捷大哥的创业礼物.xlsx` | XLSX | **13305 行 × 58 列** | 表格化结构化数据 | **整行/单字段**，不能按字符切 |

**关键发现**：XLSX 跟两份 PDF 的处理范式完全不同——结构化数据不能简单按字符切块，必须整行转成自然语言描述（或字段级检索）。这是设计最大的分叉点。

## 2. 选型

### 2.1 硬件约束
- AMD Windows（**无 CUDA**）
- llama.cpp / Qwen3.6-35B 本地推理
- 充足内存，CPU 多核

### 2.2 组件选型

| 组件 | 选型 | 理由 |
|---|---|---|
| **Embedding 模型** | **BGE-M3**（`BAAI/bge-m3`）| 中文 SOTA，多语言，CJK 友好；支持 dense+sparse+multi-vector 三种模式 |
| **Embedding 部署** | **独立 llama.cpp 实例**（port 8082）| 不抢 Qwen3.6 的 -np 1 slot；启动 `llama-server -m bge-m3-f16.gguf --embedding` |
| **向量库** | **FAISS**（`IndexFlatIP`）| 单文件、零服务、demo 阶段最简；后期可换 IVF/HNSW |
| **持久化** | `backend/data/kb/index.faiss` + `chunks.json` + `meta.json` | 三件套，与未来 `MEMORY_DATA_DIR` 路径风格一致 |
| **PDF 解析** | `pypdf`（纯文本）→ OCR 兜底（`pytesseract`）| 1321 页 KK 可能有扫描页，遇到图像页才调 OCR |
| **XLSX 解析** | `openpyxl` + **行转自然语言**模板 | 见 §4.2 |
| **切块** | 段落优先 → 句子边界 → 固定窗口兜底 | 表格单独路径 |
| **检索** | 纯 dense（先用）→ 后续可加 BM25 混合 | 1k 文档级召回率足够 |

### 2.3 不选的方案
- ❌ **ChromaDB / Qdrant**：要常驻服务，demo 阶段 over-engineering
- ❌ **OpenAI Embedding**：违背「本地优先」原则
- ❌ **sentence-transformers + PyTorch**：AMD+Windows 下 PyTorch 装起来坑多
- ❌ **Wav2Lip 风格的「整段重嵌入」**：跟「流式秒回」目标冲突，必须保持单次 < 100ms

## 3. 架构总览

```
                    ┌──────────────────┐
   user question →  │   backend/agent  │  ←─── 现有 agent_stream 流程
                    └────────┬─────────┘
                             │ chat_with_tools 探测
                             ▼
                ┌────────────────────────┐
                │  LLM (Qwen3.6 / Ark)   │
                │  决定要不要调 kb_search │
                └────────┬───────────────┘
                         │ tool_call
                         ▼
       ┌─────────────────────────────────────┐
       │  backend/tools/knowledge/kb_search  │  ←── 新增
       │   1. encode(query) → 8082 拿向量      │
       │   2. KBStore.search(vec, top_k)      │
       │   3. 拼 [1][2].. 文本回喂 LLM        │
       └────────┬────────────────────────────┘
                │
                ▼
       ┌────────────────────────┐         ┌─────────────────┐
       │  KBStore (FAISS+JSON)  │ ←─ingest─│  scripts/ingest │
       │  index.faiss           │         │  _kb.py         │
       │  chunks.json           │         │  (手动跑一次)    │
       │  meta.json             │         └─────────────────┘
       └────────────────────────┘
                ▲
                │
       ┌────────┴─────────────────────────────┐
       │  data/kb/sources/                     │
       │   *.pdf → pypdf 解析 → chunker       │
       │   *.xlsx → openpyxl 解析 → 行转文本   │
       └──────────────────────────────────────┘
```

## 4. 切块策略（按文档类型分路径）

### 4.1 PDF（KK 三部曲 + Deloitte 报告）

| 步骤 | 实现 |
|---|---|
| 提取文本 | `pypdf.PdfReader` 逐页 `extract_text()` |
| 检测空页 | 单页 < 50 字符视为扫描页，留给 OCR |
| **按段落切** | 双换行 `\n\n` 分段；单段 < 200 字往前合 |
| 段落过长 | 句子边界（`。！？`）切 + overlap 50 字 |
| meta | `{source: "KK三部曲.pdf", page: 132, chunk_idx: 17}` |

预计 KK 三部曲产出 ~6000-8000 个块（按段落平均 200-300 字算）。

### 4.2 XLSX（网易名单）

**不能按字符切**——「张三 男 网易杭州研究院 算法工程师」切成「张三男网易杭州」就没意义了。

**方案 A（行转自然语言，推荐）**：

```
1. 读表头 → 解析 58 个字段的语义
2. 过滤敏感字段（手机、邮箱、身份证、详细住址）→ 不入库
3. 每行生成模板：
   "{姓名}，{性别}，{公司} {一级部门}/{二级部门} {职位}，
    {职级}，{入职日期}入职，{工作地}工作，{学历}{专业}。"
4. 模板化的行作为 chunk，meta 带 row_idx / 部门 / 职位
5. 同部门多行可聚合：「网易杭州研究院-算法-高级工程师 共 23 人」作索引项
```

预计 13305 行 → 约 14000 个 chunk（含部分聚合）。

**方案 B（字段索引）**：
- 给每个字段建独立倒排索引（按部门、按职位、按工作地）
- 查询时按字段路由：问「跨境电商部门」→ 部门倒排表 + 1-hop 关联姓名
- 复杂但召回率更高，**留到 v2**

### 4.3 其他格式（v0 不做）

`.docx` / `.md` / `.html` / `.txt` 走通用文本路径，v0.2 再加。

## 5. 接入点

### 5.1 工具层（最小侵入）

新增 `backend/tools/knowledge/` 分类，`loader.py` 加一行：

```python
_TOOL_PACKAGES = [
    "tools.builtin",
    "tools.web",
    "tools.knowledge",   # ← 新增
]
```

`kb_search` 工具签名（read 权限，自动暴露给 LLM）：

```python
@tool(
    name="kb_search",
    description="检索本地知识库。涉及内部资料、产品说明、人事信息、报告数据等内置知识时调用。返回最相关的 k 条原文片段。",
    category=ToolCategory.DATABASE,
    permission=Permission.READ,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
    timeout_s=10.0,
)
```

`kb_ingest` 工具（write 权限，默认被闸门拦）：手动触发，**不让 LLM 误调**。

### 5.2 ResourceManager（懒加载共享资源）

`backend/tools/context.py` 加两个懒加载项：

```python
async def kb_store(self):
    if "kb_store" not in self._store:
        from tools.knowledge.store import KBStore
        self._store["kb_store"] = KBStore.load_or_empty()
    return self._store["kb_store"]

async def embedder(self):
    """本地 llama.cpp embedding 服务（port 8082）"""
    if "embedder" not in self._store:
        from tools.knowledge.embedder import LlamaCppEmbedder
        self._store["embedder"] = LlamaCppEmbedder(url="http://127.0.0.1:8082")
    return self._store["embedder"]
```

`lifespan` 关闭时 `RESOURCES.aclose()` 不用改（HTTP client 走现有 lazy http_client）。

### 5.3 system prompt（最小化追加）

在 `config.SYSTEM_PROMPT` 末尾加 1 句：

```
涉及内部资料、报告数据、名单信息等内置知识时，优先调 kb_search，不要凭记忆编造。
```

不写「每次都查」——会拖慢闲聊。

## 6. 目录与产物

```
backend/
├── data/
│   └── kb/
│       ├── sources/                 # 原始文档（gitignore）
│       │   ├── kk.pdf
│       │   ├── deloitte-xr.pdf
│       │   └── netease-roster.xlsx
│       ├── index.faiss              # FAISS 向量索引
│       ├── chunks.json              # chunk 原文（与向量一一对应）
│       └── meta.json                # {source, page/row, ...}
└── tools/
    └── knowledge/
        ├── __init__.py
        ├── store.py                 # FAISS + JSON 封装
        ├── chunker.py               # PDF/XLSX 分流
        ├── kb_search.py             # tool
        └── kb_ingest.py             # tool (write)
scripts/
└── ingest_kb.py                     # 手动 ingest 入口
```

`.gitignore` 加：
```
backend/data/kb/sources/
backend/data/kb/index.faiss
backend/data/kb/chunks.json
backend/data/kb/meta.json
```

## 7. 与 token 成本的关系

参考 `docs/token-cost.md`：

| 新增开销 | tokens | 备注 |
|---|---:|---|
| 工具 schema 永久增量 | ~150 | `kb_search` description 占大头，**可压到 ~50** |
| kb_search 单次调用回喂 | ~1500-3000 | 5 个 chunk × ~300 字 + meta，**挤进 §2 的 6000 字符工具结果预算** |
| ingest 一次性 | 不计 LLM 成本 | CPU 跑 BGE-M3，3 文档共 < 5 分钟 |

**对闲聊的影响**：system prompt +1 句（~30 字符），可忽略。
**对工具调用轮的影响**：kb_search 与 web_search 互斥（同时调两个 = 多花 ~5000 tokens），可考虑加 LLM 侧引导。

## 8. 实施步骤

| # | 内容 | 估时 | 阻塞依赖 |
|---|---|---|---|
| 1 | 拉一份 BGE-M3 GGUF（bge-m3-f16.gguf 或 INT8 量化），用 llama.cpp 跑 `localhost:8082` 验证 `/embedding` 接口 | 1h | 外部资源 |
| 2 | 写 `backend/tools/knowledge/store.py`（KBStore 类，FAISS + JSON 封装 + load/save） | 2h | — |
| 3 | 写 `backend/tools/knowledge/chunker.py`（PDF 段落切 + XLSX 行转模板） | 3h | — |
| 4 | 写 `backend/tools/knowledge/embedder.py`（调 8082 单条/批量） | 1h | 1 |
| 5 | 写 `scripts/ingest_kb.py`（扫 sources/ → 切 → 嵌入 → 写库） | 2h | 2,3,4 |
| 6 | 写 `kb_search` 工具 + 接入 ResourceManager | 2h | 2,4 |
| 7 | 改 `_TOOL_PACKAGES` + `SYSTEM_PROMPT` 末尾 | 0.5h | 6 |
| 8 | 端到端验证（见 §9）| 1h | 8 |
| 9 | 文档：补 README + `docs/rag-design.md` 完成版 | 1h | 8 |

**总计**：~13h，3-4 个工作日（不算 BGE-M3 模型下载时间）。

## 9. 验证清单

跑完 §8 步骤后逐项检查：

```powershell
# 1) embedding 服务可达
curl http://127.0.0.1:8082/v1/embeddings -d '{\"input\":\"测试\",\"model\":\"bge-m3\"}'
# 应返回 {\"data\":[{\"embedding\":[0.012, ...]}]}

# 2) ingest 成功
python scripts/ingest_kb.py
# [scan]    3 个文档：kk.pdf(1321页) / deloitte-xr.pdf(40页) / netease-roster.xlsx(13305行)
# [chunk]   kk=7821 / deloitte=287 / roster=14238
# [embed]   22346 chunks, 4 分 12 秒
# [store]   index.faiss (17.4MB) + chunks.json (12.1MB) + meta.json (3.2MB)

# 3) 服务启动加载 KB
python backend\server.py
# 启动日志应包含：[工具加载] 已加载模块: [..., 'tools.knowledge.kb_search']

# 4) 前端问三个 case
# - 「凯文·凯利怎么定义失控？」        → [状态] 正在检索知识库 → 引用 KK PDF 第N段
# - 「2021 元宇宙产业规模多大？」        → 引用 Deloitte 报告某页
# - 「网易杭州研究院算法部门有谁？」     → 引用 XLSX 多行聚合
# - 「深圳今天天气」                      → 不应调 kb_search，走 web_search
# - 「你好」                            → 不应调任何工具，闲聊
```

## 10. 风险与待定

| # | 风险 | 应对 |
|---|---|---|
| 1 | BGE-M3 在 CPU 上 22k chunks 嵌入 ~4 分钟 | 接受；ingest 一次性成本 |
| 2 | KK 三部曲 1321 页有扫描页 | pypdf 抽不到字时跳过 + 标记；v0.2 加 OCR |
| 3 | XLSX 58 列有敏感字段（手机/身份证/邮箱） | ingest 时白名单过滤（见 §4.2） |
| 4 | kb_search 与 web_search 重复调 | LLM 侧 system prompt 引导「不要同时调」；或加路由判断 |
| 5 | chunks.json 12MB 全部进工作记忆 | 现阶段 OK；若到 100MB+ 改成只回 top-1 + 链接 |
| 6 | ingest 不能热更新 | v0 手动跑；v0.2 加 watchdog |
| 7 | LLM 把「检索片段」当事实复述 | system prompt 强调「引用即可，不要补全未给出的数字」 |

## 11. 未来扩展

- **v0.2**：watchdog 监听 sources/ 自动重建；chunk 摘要索引（双层）
- **v0.3**：BM25 + dense 混合检索；重排序（cross-encoder）
- **v1.0**：与 docs 第 16 章「L3 长期记忆」打通——KB 检索结果自动沉淀进 user profile

## 12. 评审要点（请重点看这几节）

- §1 样本分析（XLSX 跟 PDF 处理范式不同，是否同意分路径）
- §2.2 选型（独立 8082 跑 embedding vs 同 llama.cpp 改 -np 2）
- §4.2 XLSX 行转自然语言模板（敏感字段白名单够不够）
- §8 估时（3-4 个工作日是否合理）

## 修订记录

- v0.1 · 2026-08-15 · 初稿，基于 3 份样本文档（KK 三部曲 / Deloitte 元宇宙报告 / 网易全员名单）