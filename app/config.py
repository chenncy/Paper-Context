import os
from dotenv import load_dotenv

load_dotenv()

# 基础配置
# 1. 动态获取当前文件 (config.py) 的绝对路径
# 例如: D:/Projects/RAG_Project/app/config.py
current_file_path = os.path.abspath(__file__)

# 2. 获取当前文件所在的目录 (app 目录)
# 例如: D:/Projects/RAG_Project/app
app_dir = os.path.dirname(current_file_path)

# 3. 再往上一级，获取项目根目录 (RAG_Project 目录)
# 例如: D:/Projects/RAG_Project
PROJECT_ROOT = os.path.dirname(app_dir)

# 4. 加载 .env (现在我们可以确保它一定在根目录找)
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# ================= 配置路径 =================
# 使用 os.path.join 拼接，自动适配 Windows(\) 和 Mac/Linux(/)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
# 论文/长文档统一放此目录（与 data 根目录并列加载，供索引与 RAGAS 等使用）
PAPERS_DIR = os.path.join(PROJECT_ROOT, "data", "papers")
# 向量库目录（可 env 外移，与仓库分离——数据不进 git，见 requirements-mcp.txt 同级说明）
DB_DIR = os.getenv("DB_DIR", os.path.join(PROJECT_ROOT, "chroma_db_qwen")).strip() or os.path.join(PROJECT_ROOT, "chroma_db_qwen")
VECTOR_BACKEND = os.getenv("VECTOR_BACKEND", "chroma").strip().lower() or "chroma"
MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost").strip() or "localhost"
MILVUS_PORT = int(os.getenv("MILVUS_PORT", "19530"))
MILVUS_DB_NAME = os.getenv("MILVUS_DB_NAME", "default").strip() or "default"
VECTOR_COLLECTION_NAME = os.getenv("VECTOR_COLLECTION_NAME", "split_parents").strip() or "split_parents"

# [新增] 父文档存储路径 (用于 Parent-Child Retriever)
STORE_DIR = os.getenv("STORE_DIR", os.path.join(PROJECT_ROOT, "doc_store")).strip() or os.path.join(PROJECT_ROOT, "doc_store")

# 语义路由前置漏斗

# 调试时可设 LOG_CONFIG_AT_STARTUP=true 在启动时打印配置摘要

# CORS（浏览器不允许 allow_origins=["*"] 与 allow_credentials=True 同时使用）

API_KEY = (os.getenv("DASHSCOPE_API_KEY") or "").strip()

# embedding 端点：任何 OpenAI 兼容端点（DashScope / Ollama / vLLM / 其他云）。
# 未配置时回退 DashScope 兼容模式 + DASHSCOPE_API_KEY（向后兼容）。
EMBEDDING_BASE_URL = (
    os.getenv("EMBEDDING_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
).strip()
EMBEDDING_API_KEY = (os.getenv("EMBEDDING_API_KEY") or API_KEY).strip()


def validate_runtime_config(*, require_api_key: bool = True) -> None:
    """在应用启动时校验关键配置，避免 import 阶段抛错。"""
    from app.db_engine import resolve_database_url

    missing: list[str] = []
    if require_api_key and not API_KEY:
        missing.append("DASHSCOPE_API_KEY")
    if not resolve_database_url():
        missing.append("SQLALCHEMY_DATABASE_URL 或 MYSQL_*")
    if missing:
        raise ValueError(f"缺少运行时配置: {', '.join(missing)}")


# 百度千帆网页搜索 API Key（可选）：用于 web_search 工具，不配置则联网搜索为占位提示
# GitHub API Token（可选）：用于提升 GitHub API 速率限制，公开仓库不填也可读
# ArXiv 结果中若识别到 GitHub 链接，是否自动补充仓库简介
# 单次 ArXiv 检索最多补充多少个 GitHub 仓库，避免调用过慢

# ================= arXiv / GitHub 外部研究 provider（http 直连 | mcp 隔离子进程）=================
def _normalize_provider(name: str, default: str = "http") -> str:
    v = (name or default).strip().lower()
    return v if v in ("http", "mcp") else default


# Graph 主链路：本地检索不足时是否启用 external research（arXiv/GitHub）
# Deep Research：证据引用模式与研究计划
# 引用模式开关（graph.generate 用：DEEP_RESEARCH_ENABLED 且本开关且 evidence_requirement/
# survey 类答案/含外部来源时启用带引用生成）。graph.py 曾引用未定义的本项导致
# generate 节点 ImportError（函数内 import 潜伏，走到分支才触发）
MILVUS_HNSW_M = int(os.getenv("MILVUS_HNSW_M", "16"))
MILVUS_HNSW_EF_CONSTRUCTION = int(os.getenv("MILVUS_HNSW_EF_CONSTRUCTION", "200"))
MILVUS_HNSW_EF_SEARCH = int(os.getenv("MILVUS_HNSW_EF_SEARCH", "64"))
# Section-aware ingestion：合并同 section block（见 app/ingestion/chunking.py）
INGESTION_SECTION_AWARE = os.getenv("INGESTION_SECTION_AWARE", "true").lower() == "true"

# ================= 统一检索入口（app/rag_service.py）=================
# 检索默认臂：vector（仓库内消融显示等权 RRF hybrid 劣于纯向量，故降为可配置项）| hybrid
RAG_RETRIEVAL_MODE = (os.getenv("RAG_RETRIEVAL_MODE", "vector") or "vector").strip().lower()
# 统一返回 Top-N（历史 tool 路径 top3 / MCP 路径 top5 不一致，统一为可配置默认值）
RAG_TOP_N = int(os.getenv("RAG_TOP_N", "5"))
# 单条证据片段最大字符数（超长截断，0 = 不截断）
RAG_HIT_MAX_CHARS = int(os.getenv("RAG_HIT_MAX_CHARS", "4000"))
# child chunk 是否注入上下文头（[论文标题 | section] 前缀，提升短块检索精度）
RAG_CHILD_CONTEXT_HEADER = os.getenv("RAG_CHILD_CONTEXT_HEADER", "true").lower() == "true"
# child chunk 目标字符数
RAG_CHILD_CHUNK_SIZE = int(os.getenv("RAG_CHILD_CHUNK_SIZE", "400"))
# parent（section）块软上限：超过时按段落边界溢出切分，而非按字符硬切
RAG_PARENT_SOFT_MAX_CHARS = int(os.getenv("RAG_PARENT_SOFT_MAX_CHARS", "3500"))

# ================= PDF Layout / OCR（阶段七）=================
# legacy | layout | grobid（grobid 失败自动回退 layout）
PDF_PARSER_MODE = os.getenv("PDF_PARSER_MODE", "layout").strip().lower()
PDF_PARSER_OCR_ENABLED = os.getenv("PDF_PARSER_OCR_ENABLED", "false").lower() == "true"

# ================= GROBID 学术 PDF 结构化解析 =================
# Docker: docker run --rm -p 8070:8070 lfoppiano/grobid:0.8.0
GROBID_URL = os.getenv("GROBID_URL", "http://localhost:8070").strip() or "http://localhost:8070"
GROBID_TIMEOUT_SECONDS = float(os.getenv("GROBID_TIMEOUT_SECONDS", "120"))

# ================= Rerank provider（统一入口见 app/rag_core.py） =================
# dashscope（默认，现状）| local（A800 本地 cross-encoder 服务）| off（消融基线）
RERANK_PROVIDER = (os.getenv("RERANK_PROVIDER", "dashscope") or "dashscope").strip().lower()
RERANK_LOCAL_URL = (os.getenv("RERANK_LOCAL_URL", "http://localhost:8081") or "http://localhost:8081").strip()
RERANK_LOCAL_TIMEOUT_SECONDS = float(os.getenv("RERANK_LOCAL_TIMEOUT_SECONDS", "10"))
# rag_search 统一入口是否在检索后追加 rerank（graph 节点路径不受此开关影响）
RAG_RERANK_ENABLED = os.getenv("RAG_RERANK_ENABLED", "false").lower() == "true"

# ================= Contextual Retrieval（入库时 LLM 为 child 生成情境说明） =================
RAG_CONTEXTUAL_ENABLED = os.getenv("RAG_CONTEXTUAL_ENABLED", "false").lower() == "true"
# 为空时复用 app.chains.llm；指定则用该模型（dashscope compatible 端点）
RAG_CONTEXTUAL_MODEL = os.getenv("RAG_CONTEXTUAL_MODEL", "").strip()
RAG_CONTEXTUAL_MAX_CHARS = int(os.getenv("RAG_CONTEXTUAL_MAX_CHARS", "300"))

# ================= VLM 图表描述（figure 的文本代理） =================
RAG_VLM_ENABLED = os.getenv("RAG_VLM_ENABLED", "false").lower() == "true"
RAG_VLM_MODEL = (os.getenv("RAG_VLM_MODEL", "qwen-vl-max") or "qwen-vl-max").strip()
FIGURES_DIR = os.path.join(DATA_DIR, "figures")

# ================= Collection Revision 灰度 =================
COLLECTION_REVISION_ENABLED = os.getenv("COLLECTION_REVISION_ENABLED", "false").lower() == "true"

# ================= Agent 可观测与鲁棒 =================
# 工具返回内容最大字符数，超出截断并注明，避免挤爆上下文
# 单次 Agent 调用（整图）超时秒数，超时返回友好提示

# ================= Reflection 自我反思与迭代 =================
# 是否在「主控给出最终答案」后做一次自检（满意 / 需补充工具 / 需重写）
# 最多反思迭代轮数，超过后不再要求补充或重写，直接结束

# Agent 在进入主控（bind_tools 推理）前是否先调用 Planner LLM 生成 task_plan 并写入系统提示

# ================= 异步记忆引擎（三合一后台任务 + 情节检索）=================
# 跨会话全局画像在 agent_memory 表中使用的虚拟 session_id
GLOBAL_PROFILE_SESSION_ID = os.getenv("GLOBAL_PROFILE_SESSION_ID", "GLOBAL_PROFILE").strip() or "GLOBAL_PROFILE"
# Chroma 中情节记忆集合名（与业务知识库 split_parents 同 persist 目录、不同 collection）
EPISODIC_MEMORY_COLLECTION = os.getenv("EPISODIC_MEMORY_COLLECTION", "episodic_memory").strip() or "episodic_memory"
EPISODIC_MEMORY_ENABLED = os.getenv("EPISODIC_MEMORY_ENABLED", "true").lower() == "true"
EPISODIC_RETRIEVAL_TOP_K = int(os.getenv("EPISODIC_RETRIEVAL_TOP_K", "2"))
# 注入主控系统提示的情节记忆最大字符数
EPISODIC_PROMPT_MAX_CHARS = int(os.getenv("EPISODIC_PROMPT_MAX_CHARS", "2500"))
# 会话摘要最大字数（异步记忆整理）
# 记忆索引注入预算（system prompt 内）
MEMORY_INDEX_MAX_ITEMS = int(os.getenv("MEMORY_INDEX_MAX_ITEMS", "20"))
MEMORY_INDEX_MAX_CHARS = int(os.getenv("MEMORY_INDEX_MAX_CHARS", "1500"))
# 记忆治理：每个 (user, session, category) 保留的最大条数（按热度+新鲜度）

# ================= 史官 / 图谱 / 图检索注入 =================
MEMORY_GRAPH_ENABLED = os.getenv("MEMORY_GRAPH_ENABLED", "true").lower() == "true"
MEMORY_GRAPH_PROMPT_MAX_CHARS = int(os.getenv("MEMORY_GRAPH_PROMPT_MAX_CHARS", "2000"))
# 为 true 时跳过「是否有新事实」意图侦测，直接跑完整固化（费算力，用于调试）
# 为 true 时：除每满 N 轮触发外，还可根据当前用户句与历史句 embedding 相似度判定「话题切换」触发固化（默认关闭，需调参）
# 会话累计 token（粗估）达阈值时触发固化；0 表示关闭

# ================= 论文项目持久化 =================
# json（默认）| db —— db 使用 MySQL paper_projects 表
# A2A TaskStore：memory（默认）| redis
# true：常驻仅短索引 + 会话摘要；episodic/graph/全文记忆靠工具拉取
MEMORY_ON_DEMAND_ENABLED = os.getenv("MEMORY_ON_DEMAND_ENABLED", "true").lower() == "true"

# ================= MCP MySQL（可选）：stdio 连接社区 MySQL Server =================
# 推荐 @berthojoris/mcp-mysql-server；需 Node/npx + pip install mcp
# 启用后 `query_mysql` 工具将执行真实只读 SQL（SELECT/WITH），用于业务统计、上传记录等
# 建议：使用只读数据库账号；生产勿把 root 密码写入 .env 并提交仓库
# 若设置整段参数（高级），将忽略下方的 DSN/分段配置；示例见 docs/mcp.md

# ================= 数据面 MCP（Agent 经 MCP 访问 Redis / MySQL / Chroma，零信任隔离）=================

# ================= 项目规范（仓库根目录 Markdown，类 Claude Code CLAUDE.md）=================
# 为 true 时：run_agent 注入系统提示；USE_DEMO_ORCHESTRATOR_STACK=true 时 run_multi_agent_stack 在 planner 前附加同一段

# ================= P0-P2 新增功能标志 =================
# P1-3: 语义路由在服务入口处分类（与 graph 层路由独立，默认开启）
# P2-5: Embedding 向量结果缓存

# 并发 / Fan-out（详见 app/concurrency_config.py、docs/langgraph_upgrade_compat.md）
from app.concurrency_config import (  # noqa: E402
    USE_FAN_OUT,
    USE_PARALLEL_RETRIEVAL,
    USE_ASYNC_RETRIEVAL,
    USE_PARALLEL_TOOLS,
    TASK_QUEUE_ENABLED,
    LLM_RATE_LIMIT_ENABLED,
    GRADE_MODE,
    REDIS_RATE_LIMIT_ENABLED,
    ASYNC_INDEX_BUILD,
    CHAT_PREFER_ASYNC_HEAVY,
    CHAT_HEAVY_MIN_CHARS,
    GRAPH_NODE_TIMING_ENABLED,
)

# ---- Tool Harness（Cordis 风格工具编排）桥接开关 ----
# 开启后，已迁移到 tool_harness 的工具优先走 harness 执行，失败自动降级回旧注册表


def log_config_summary() -> None:
    """启动时打印配置摘要（由 server lifespan 调用，不在 import 时执行）。"""
    print(f"[CONFIG] 项目根目录定位: {PROJECT_ROOT}")
    print(f"[CONFIG] 数据目录: {DATA_DIR}")
    print(f"[CONFIG] 论文目录: {PAPERS_DIR}")
    print(f"[CONFIG] 向量后端: {VECTOR_BACKEND}")
    print(f"[CONFIG] Tool Harness 桥接: {'开启' if TOOL_HARNESS_ENABLED else '关闭'}")
    print(
        f"[CONFIG] 语义路由: {'开启' if SEMANTIC_ROUTER_ENABLED else '关闭'} "
        f"(闲聊阈值={CHAT_THRESHOLD}, FAQ阈值={FAQ_THRESHOLD})"
    )
    print(
        f"[CONFIG] arXiv provider={ARXIV_PROVIDER} GitHub provider={GITHUB_PROVIDER} "
        f"external_research={'开启' if EXTERNAL_RESEARCH_ENABLED else '关闭'} "
        f"deep_research={'开启' if DEEP_RESEARCH_ENABLED else '关闭'}"
    )
    print(f"[CONFIG] Reflection 反思: {'开启' if REFLECTION_ENABLED else '关闭'} (最多 {MAX_REFLECTION_PASSES} 轮)")
    print(f"[CONFIG] Agent Planner 规划: {'开启' if AGENT_PLANNER_ENABLED else '关闭'}")
    if MCP_MYSQL_ENABLED:
        print(
            f"[CONFIG] MCP MySQL: 开启（DSN/库={MCP_MYSQL_DSN or MCP_MYSQL_DATABASE or '（见 env）'}）"
        )
    else:
        print("[CONFIG] MCP MySQL: 关闭")
    if AGENT_DATA_VIA_MCP:
        print("[CONFIG] 数据面 MCP: 开启")
    else:
        print("[CONFIG] 数据面 MCP: 关闭")
    print(
        f"[CONFIG] 项目规范文件注入: {'开启' if PROJECT_INSTRUCTIONS_ENABLED else '关闭'} "
        f"(files={PROJECT_INSTRUCTIONS_FILES!r})"
    )
    print(f"[CONFIG] 语义路由入口分类: {'开启' if SEMANTIC_ROUTER_AT_ENTRY else '关闭'}")
    print(f"[CONFIG] Embedding 缓存: {'开启' if EMBEDDING_CACHE_ENABLED else '关闭'}")
    print(
        f"[CONFIG] 并发: FAN_OUT={USE_FAN_OUT} TASK_QUEUE={TASK_QUEUE_ENABLED} "
        f"ASYNC_INDEX={ASYNC_INDEX_BUILD} CHAT_ASYNC_HEAVY={CHAT_PREFER_ASYNC_HEAVY}"
    )
    print(f"[CONFIG] CORS origins: {CORS_ALLOWED_ORIGINS}")
