# RAG 结构化改造：section 切分、表格原子块与独立 MCP Server

> 2026-09 改造。动机：section-aware 切分此前是名义功能（正文块 `section` 恒为 None，
> 实际退化为跨页 2000 字符合并）；表格在清洗时结构塌缩；检索路径双口径（tool top3 /
> MCP top5）且 `search_knowledge_base` 每次调用全量重建 BM25。本次把「论文自身结构」
> 接入索引与检索全链路，并剥离出零 MySQL 依赖的独立 RAG MCP Server。

## 〇、FTS 分层检索（2026-10-09：FTS 默认基座 + embedding 可选增强）

参照 PowerContext 的"FTS 默认 + embedding 可选"分层设计，按单后端形态裁剪：

- **检索模式**（`rag_search` 的 `mode`，env `RAG_RETRIEVAL_MODE` 同名覆盖）：
  - `auto`（默认）：向量可用走 vector（依据消融 vector 0.354 > hybrid 0.278）；
    embedding 不可达（网络断/未配置/索引为空）自动降级 FTS，`note` 字段声明降级原因，
    `error_cause` 保留底层死因
  - `vector` / `hybrid`：显式模式，失败如实报错（不静默降级）
  - `fts`：纯全文检索，零 embedding 依赖
- **FTS 层**（[rag_fts.py](../app/rag_fts.py)）：SQLite FTS5 投影（默认数据目录
  `rag_fts.db`，env `RAG_FTS_PATH` 可改）；CJK 相邻对 bigram + 英文词分词
  （入库与查询同 analyzer）；BM25 排序，hit 结构与向量路径同构（带出处）
- **入库解耦**（[vector_store.py](../app/vector_store.py) 的 `_fts_synced` 子类）：
  `add_texts` 先落 FTS（vectorized=0）→ 写向量 → 成功回写 vectorized=1。
  **embedding 不可达时入库不失败**：文本进 FTS、`write_to_index` 返回
  `fts_only`、validation.metrics 记 `vectorized: false`
- **删除同步**：`rag_ingest(action=delete)` 按 paper_id/source 同时删向量与 FTS
- **存量回填**：`scripts/backfill_fts_from_vector.py`（向量库 → FTS，24307 条 68s）；
  **向量补算**：`scripts/backfill_embeddings.py`（FTS 未向量化 → embedding → 向量库，
  断点续跑）
- **已知边界**：FTS 不做跨语言（中文查询 × 英文文档需向量层）；中文分词为 bigram
  近似，未做词覆盖率准入（PowerContext 的 AdmissionFloor 可为后续增强）

实测（2026-10-09，公网断供与恢复两种真实状态）：断网时 `mode=fts` 0.43s 命中
（title/section 出处完整）；网络恢复后 `mode=auto` 自动回向量路径。
`rag_stats` 现返回 `fts_chunk_count / vectorized / not_vectorized`。

## 一、切分架构（改了什么）

| 层 | 改造前 | 改造后 |
|---|---|---|
| PDF 解析 | 表格 pipe 行拼接、追加在页尾（阅读顺序乱） | markdown 序列化 + 按 bbox top-y 插回页内文本流；figure 计数入 report |
| 清洗 | 正文块 `section=None`（跨页断链）；表格被行级规则拆碎 | section 状态跨页传递；references 跨页持续排除；表格原子块旁路行级清洗；支持 markdown `##` 标题 |
| caption | 正则识别、独立漂浮块 | Table-N caption 绑定同页最近表格，合成一个检索单元（`caption` / `artifact_id` metadata）；figure caption 保持独立 |
| 合并 | 同「section」合并（实际按 block_type） | 真 section 合并；表格/图注等原子块独立输出但**不打断**同 section 正文合并 |
| parent 切分 | `RecursiveCharacterTextSplitter(2000)` 字符重切 | `SectionBoundarySplitter`：默认原样保留，超软上限（3500）按段落边界溢出 |
| child 切分 | 400 字符裸切 | `ContextualChildSplitter`：400 字符 + 上下文头 `[论文标题 \| section]`（头仅进 child 供 embedding，parent 保持干净原文） |
| 检索入口 | 双路径：tool=hybrid+rerank top3；MCP=get_retrievers+rerank top5+截断 800 | 统一 `app/rag_service.py::rag_search`：默认**纯向量**（消融依据：等权 RRF hybrid MRR 0.278 < 纯向量 0.354），hybrid 为 `RAG_RETRIEVAL_MODE` 可配置项；进程内缓存（含 BM25 单例）；返回带出处（title/section/page/block_type/score） |

代码入口：`app/pdf_layout_parser.py`、`app/loader.py`、`app/ingestion/chunking.py`、
`app/retriever.py`（两个 Splitter）、`app/rag_service.py`（新）。
`graph.get_retrievers` / `rag_core.search_knowledge_base` / `mcp_data_server.execute_rag_retrieval`
全部委托统一入口；`mcp_data_server` 的 MySQL 初始化失败改为降级（不再阻塞纯 RAG 工具）。

新增环境变量见 `.env.example`「统一检索入口」段。

## 二、评测协议（在有完整栈的机器上跑）

**P0 前置（先于任何数字）**：确认 QASPER 查询文本未泄漏进索引——

```bash
python scripts/verify_index_hygiene.py
```

泄漏则先 `python scripts/rebuild_index.py --force` 重建干净索引。

**before/after（消融臂：字符切分基线 vs section 结构化切分）**：

```bash
# A. 基线（改造前代码，如 467b9ea）
git checkout <baseline-commit>
python scripts/rebuild_index.py --force
python scripts/import_qasper_to_index.py --index --limit 200
python scripts/evaluate_retrieval_ablation.py --configs vector,hybrid --limit 50 --output ablation_baseline.json

# B. 改造后（本分支）
git checkout <this-branch>
python scripts/rebuild_index.py --force
python scripts/import_qasper_to_index.py --index --limit 200
python scripts/evaluate_retrieval_ablation.py --configs vector,hybrid --limit 50 --output ablation_section.json
```

要点：
- 两臂同数据（`--limit 200` 同语料、同评测集 `eval_dataset_qasper.json`）、同口径
  （`--match-mode keyword_or_gt` 默认），唯一差异是切分链路。
- 建议条件允许时把 `--limit` 提到 100/200（现有 0.354/0.278/0.201 是 50 题）。
- rerank 臂仍被 gte-rerank 403 阻塞，不在本轮范围。
- 结果表补进本文档（见下）。

### 双臂消融结果（2026-09-27 实测：200 篇 / 616 题 / text-embedding-v4 / source 匹配）

| 臂 | MRR | Recall@1 | Recall@5 | Recall@10 | p50 (ms) | 索引形态 |
|---|---:|---:|---:|---:|---:|---|
| 基线（字符切分 2000/400） | **0.3380** | 0.2857 | **0.4058** | **0.4529** | 1268 | 4334 parent（统一 2000 字符） |
| 新切分（section 感知 + 上下文头） | 0.3290 | **0.3003** | 0.3685 | 0.3929 | 1221 | 6968 parent（平均 602 字符，100% 带 section） |

**结论（诚实负结果）**：section 感知切分在此语料上 **MRR 持平（-0.9pt，噪声内）、Recall@1 +1.5pt、Recall@5/-3.7pt、Recall@10 -6pt**。

归因分析：
1. **粒度效应主导**——本消融同时改变"结构边界"与"有效块大小"两个变量：QASPER txt 语料的 section 普遍很短，section 感知使 parent 平均从 2000 字符降到 602 字符（3.3 倍细化）。Recall@1 升、Recall@5/10 降是教科书级的"小块精度/召回交换"签名。
2. **该语料无法体现 v1 设计的真实价值**——txt full_text 已把表格/版面/caption 位置全部抹平，v1 的表格原子块、caption 绑定、阅读顺序修复在此无从发挥（对 txt 只有 markdown 标题识别一条增益路径）。
3. 待做的分解实验：(a) **尺寸匹配臂**——允许跨 section 合并补足到 ~2000 字符，隔离"结构"与"粒度"两变量；(b) **rerank 臂**——更细的候选池正是 rerank 的用武之地，可能翻转净效果；(c) **真实 PDF 语料**——v1 的价值主场。

注：本结果不改变简历表述（简历只声明机制与混合负结果，未声明切分增益）。

## 三、独立 MCP Server（RAG + Memory，多端接入）

### 3.1 RAG：`rag_mcp_server.py`

零 MySQL、零会话依赖（仅向量库 + DashScope embedding；Redis 仅作
可选 embedding 缓存，不可用自动直连）。工具面：

- `rag_search(query, top_n?, mode?)`：统一检索，返回带出处的证据
- `rag_ingest(file_path, action="ingest", paper_id?, source?, paper_title?)`：
  解析 + 入库（PDF 走 layout 解析 + section 切分）；`action="delete"` 按
  paper_id/source 撤销（即逆操作）。**统一走 `app/rag_service.rag_ingest` 编排**
  （2026-10-05 起与 harness fiber / 旧注册表同源）：入库成功自动失效检索缓存，
  结果携带 `reverse` 描述符
- `rag_stats()`：后端 / collection / chunk 数 / 父文档数

### 3.2 Memory：`memory_mcp_server.py`（入口）→ `app/memory_mcp_server.py`（实现）

依赖 MySQL（agent_memory 真源）+ Chroma（情节），工具面 = `MemoryService` 门面
（[memory_service.py](../app/memory_service.py)）的薄封装，机制层不动：

| 组 | 工具 | 说明 |
|---|---|---|
| 读 | `memory_get` / `memory_list` / `memory_search` / `memory_history` | 单条（session 优先 global 兜底）/ 列表（scope+as_of 时间旅行）/ 三路召回（索引+情节+图谱）/ 版本链 |
| 写 | `memory_save` / `memory_update` / `memory_forget` | 显式写（scope=global\|session）/ 纠错（旧值入 history）/ 遗忘（单条或按范围） |
| 上下文 | `memory_prepare` | PreparedContext 有界装配（冷启动会话注入） |
| 转交 | `handoff_prepare` / `handoff_commit` / `handoff_pending` / `handoff_acknowledge` | 跨会话/跨宿主工作转交状态机 |
| 底座 | `merge_global_fact` / `upsert_episodic_memory` + `memory://user/{id}/global_profile` resource | 兼容旧调用方保留 |

**外接模式语义**：与聊天主链路（Outbox 同事务固化）不同，MCP 写入是调用方
显式写（mem0 式），可靠性由幂等 save + 调用方重试保证。
默认用户名 = 环境变量 `MEMORY_MCP_DEFAULT_USERNAME`（默认 codex），参数可覆盖。

### 3.3 接入配置

启动命令统一用项目 `.venv` 解释器（**不要用裸 `python`**——系统 Python 缺依赖）：

**Claude Code（本项目已内置 `.mcp.json`，自动挂载，command 为项目根相对路径）**：
`rag_mcp_server.py` 与 `memory_mcp_server.py` 已登记；手动方式
`claude mcp add papercopilot-rag -- .venv/Scripts/python.exe rag_mcp_server.py`。

**Codex**：`~/.codex/config.toml`（绝对路径，cwd 无关）。注意冷启动约 2–2.5 分钟
（langchain 导入 ~80–105s + 索引 collection 加载 ~35s），`startup_timeout_sec` 需 ≥300；
频繁使用建议改 `RAG_MCP_TRANSPORT=streamable-http` 常驻：

```toml
[mcp_servers.papercopilot_rag]
command = "<repo>/.venv/Scripts/python.exe"
args = ["<repo>/rag_mcp_server.py"]
startup_timeout_sec = 300

[mcp_servers.papercopilot_memory]
command = "<repo>/.venv/Scripts/python.exe"
args = ["<repo>/memory_mcp_server.py"]
startup_timeout_sec = 300
```

**Claude Desktop**：`claude_desktop_config.json` 同理（command 用 .venv 绝对路径）。

注：`mcp_data_server.py`（语义缓存/会话上下文/数据面）保持不变，与本两 server 并存；
其 `execute_rag_retrieval` 与 `rag_search` 同源双名，外部接入优先用 `rag_search`。

## 四、已知边界（本轮不做）

- rerank：gte-rerank 403（需 key 权限或本地 bge-reranker）
- 图片：文本档（caption + 正文引用入索引）；VLM 描述、图片落盘留二期
- heading 判定仍是字号启发式（双栏小字体模板可能漏）；GROBID 未引入
- hybrid 融合调权（加权/归一化 RRF）留待消融数据指导

## 五、v2 升级层（2026-09-22，默认全关，各自独立成消融臂）

四个能力已实现、全部挂开关、默认行为与 v1 完全一致：

| 能力 | 开关 | 代码 | 说明 |
|---|---|---|---|
| **本地 rerank** | `RERANK_PROVIDER=local` | [rag_core.py](../app/rag_core.py) 分发 + [rerank_local.py](../app/rerank_local.py) 客户端 + [run_local_rerank_server.py](../scripts/run_local_rerank_server.py) | A800 跑 cross-encoder（bge-reranker-v2-m3），TEI/vLLM 两种 /rerank 协议自动适配；`off` 作消融基线。检索侧最大杠杆（此前 rerank 臂被 403 阻塞）。统一入口可用 `RAG_RERANK_ENABLED=true` 追加 rerank |
| **GROBID 解析** | `PDF_PARSER_MODE=grobid` | [grobid_parser.py](../app/grobid_parser.py) | PDF → TEI XML → 带结构标注的块（section 嵌套路径、表格复合块、figure caption、references 排除）。loader 对 GROBID 块走旁路不重推导；GROBID 不可达自动回退 layout。Docker 启动见 .env.example |
| **Contextual Retrieval** | `RAG_CONTEXTUAL_ENABLED=true` | [contextual_llm.py](../app/contextual_llm.py) + [ContextualChildSplitter](../app/retriever.py) | 入库时每 child 一次 LLM 调用生成情境说明（解决指代），与 `[标题\|section]` 静态头叠加；进程内缓存；失败降级静态头。Anthropic 2024 方法 |
| **VLM 图表** | `RAG_VLM_ENABLED=true` | [vlm_client.py](../app/vlm_client.py) + parser 提图 + loader 绑定 + [pipeline](../app/ingestion/pipeline.py) describe_figures 阶段 | pymupdf 提取图片块落盘（`data/figures/<论文>/`，clip 2x 渲染，过滤 <20px 装饰图）→ Figure-N caption 绑定 → Qwen-VL 生成描述作文本代理。figure 成原子块。VLM 关闭时仅 caption、无 caption 的 figure 丢弃 |

依赖注意：rerank local 服务与 VLM 各自的依赖见 `requirements-local-rerank.txt` 与 .env.example（dashscope SDK 不在主 requirements，与 gte-rerank 同现状）。

### v2 消融臂建议（在 v1 双臂基础上扩展）

```
python scripts/evaluate_retrieval_ablation.py --configs vector --limit 50 --output a1_vector.json
RAG_CONTEXTUAL_ENABLED=true 重建索引后 → 同命令 --output a2_contextual.json
RERANK_PROVIDER=local + 服务在跑 → --configs vector,hybrid --rerank --output a3_rerank.json
PDF_PARSER_MODE=grobid + 重建 → --output a4_grobid.json
```
每臂唯一变量、同数据同口径；数字回填下表：

| 臂 | MRR | Recall@5 | p50 (ms) | 备注 |
|---|---|---|---|---|
| v1 section vector（基线） | — | — | — | |
| + contextual | — | — | — | |
| + local rerank | — | — | — | |
| + GROBID 解析 | — | — | — | |

## 六、RAG ↔ tool_harness 合流（2026-09-22）

RAG 成为 harness 生态内的插件 fiber（[tools.yaml](../tool_harness/tools.yaml)），与 MCP server 并存——
同一统一入口（`app/rag_service`）的两种宿主形态：

| 形态 | 入口 | 适用 |
|---|---|---|
| 对内 fiber | `tool_harness` 12 步调用链 | 主链路 ToolExecutor（`TOOL_HARNESS_ENABLED=true` 时经 [harness_bridge](../app/tooling/harness_bridge.py) 委托） |
| 对外 MCP | `rag_mcp_server.py` stdio | Claude Code / Codex / Claude Desktop |
| 降级路径 | 旧注册表 spec（[builtin_adapter](../app/tooling/builtin_adapter.py)） | harness 关闭或失败时，同一实现直调 |

两个 fiber：

- **`rag_search`**（LOW，只读）：包装 `rag_service.rag_search`；无副作用无 reverse。
- **`rag_ingest`**（MEDIUM，可逆写效应）：
  - `apply` → `rag_service.rag_ingest`（统一编排：结构化入库 + 成功后 `refresh_retriever_cache`，
    即协效应的务实版——索引上下文变化 → 检索缓存失效）
  - **`apply_reverse`**：模块级同名 reverse 函数被 loader 自动接为 fiber 逆操作
    （`tool_harness/loader.py: getattr(module, "apply_reverse")`，零核心改动骑既有机制）。
    补偿按 `paper_id`（优先，`delete_documents_by_paper_id`）或 `source`
    （`delete_documents_by_source`）；也可显式 `action=delete` 调用
  - 结果携带机器可读 `reverse` 描述符，供 executor/agent 显式补偿
  - 注意：按 harness 冻结语义，成功业务调用不入 fiber 生命周期副作用栈
    （热替换不得撤销已完成的工作）；reverse 走失败回滚与显式补偿

委托取舍：`rag_search`/`rag_ingest` 进 `DELEGATED_TOOLS`（新旧两路径同源、行为等价）；
`search_enterprise_knowledge_base` **不**委托——intent 多路/HyDE 逻辑在旧路径，非等价。

合流后 RAG 对照 DeepSeek harness 思想的完成度：声明式注册 ✓、统一扩展点多宿主 ✓、
协效应（缓存失效）✓、**可逆副作用 ✓（ingest 带逆操作）**；运行时热插拔仍是 env+重启
（harness 本体支持，RAG provider 未接）。
