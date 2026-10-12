"""并发与 Fan-out 相关 Feature Flags（默认关闭，灰度开启）。"""
import os

# Phase 2：LangGraph Send 多路检索 Fan-out（需 langgraph>=0.2）
USE_FAN_OUT = os.getenv("USE_FAN_OUT", "false").lower() == "true"

# Phase 3：检索节点内 ThreadPool / asyncio 并行（不依赖 Send，可与 USE_FAN_OUT 叠加）
USE_PARALLEL_RETRIEVAL = os.getenv("USE_PARALLEL_RETRIEVAL", "true").lower() == "true"
USE_ASYNC_RETRIEVAL = os.getenv("USE_ASYNC_RETRIEVAL", "false").lower() == "true"

# Phase 3：Agent 工具无依赖时并发执行
USE_PARALLEL_TOOLS = os.getenv("USE_PARALLEL_TOOLS", "false").lower() == "true"

# Phase 4：通用任务队列与异步 /chat
TASK_QUEUE_ENABLED = os.getenv("TASK_QUEUE_ENABLED", "false").lower() == "true"
TASK_QUEUE_KEY = os.getenv("TASK_QUEUE_KEY", "papercopilot:tasks:v1").strip() or "papercopilot:tasks:v1"
TASK_RESULT_PREFIX = os.getenv("TASK_RESULT_PREFIX", "papercopilot:task_result:").strip() or "papercopilot:task_result:"
TASK_WORKER_COUNT = int(os.getenv("TASK_WORKER_COUNT", "2"))

# Phase 4：LLM 调用并发与速率（入口 Semaphore 在 app/api/chat.py，此处为节点级）
LLM_MAX_CONCURRENT = int(os.getenv("LLM_MAX_CONCURRENT", "5"))
LLM_RATE_PER_SEC = float(os.getenv("LLM_RATE_PER_SEC", "10.0"))
LLM_RATE_LIMIT_ENABLED = os.getenv("LLM_RATE_LIMIT_ENABLED", "false").lower() == "true"

# 并行检索最大 worker（线程池）
RETRIEVE_POOL_MAX_WORKERS = int(os.getenv("RETRIEVE_POOL_MAX_WORKERS", "4"))

# ================= P0 高并发 =================
# grade：batch（默认，1 次 LLM）| parallel | per_doc | heuristic
GRADE_MODE = os.getenv("GRADE_MODE", "batch").strip().lower() or "batch"
GRADE_SNIPPET_CHARS = int(os.getenv("GRADE_SNIPPET_CHARS", "1200"))
GRADE_MAX_PARALLEL = int(os.getenv("GRADE_MAX_PARALLEL", "6"))
GRADE_HEURISTIC_ENABLED = os.getenv("GRADE_HEURISTIC_ENABLED", "true").lower() == "true"

# Redis 分布式限流（/chat 入口）
REDIS_RATE_LIMIT_ENABLED = os.getenv("REDIS_RATE_LIMIT_ENABLED", "false").lower() == "true"
REDIS_RATE_LIMIT_GLOBAL_PER_MIN = int(os.getenv("REDIS_RATE_LIMIT_GLOBAL_PER_MIN", "60"))
REDIS_RATE_LIMIT_USER_PER_MIN = int(os.getenv("REDIS_RATE_LIMIT_USER_PER_MIN", "15"))
REDIS_RATE_LIMIT_WINDOW_SEC = int(os.getenv("REDIS_RATE_LIMIT_WINDOW_SEC", "60"))

# 重查询优先走异步任务（返回 task_id，需 TASK_QUEUE_ENABLED）
CHAT_PREFER_ASYNC_HEAVY = os.getenv("CHAT_PREFER_ASYNC_HEAVY", "false").lower() == "true"
CHAT_HEAVY_MIN_CHARS = int(os.getenv("CHAT_HEAVY_MIN_CHARS", "120"))

# 上传后索引构建异步入队（与在线 /chat 隔离）
ASYNC_INDEX_BUILD = os.getenv("ASYNC_INDEX_BUILD", "false").lower() == "true"
INDEX_BUILD_QUEUE_KEY = os.getenv("INDEX_BUILD_QUEUE_KEY", "papercopilot:index:tasks:v1").strip()

# 独立 Embedding Worker（embedding + Milvus/Chroma 写入）
EMBEDDING_WORKER_ENABLED = os.getenv("EMBEDDING_WORKER_ENABLED", "false").lower() == "true"
EMBEDDING_QUEUE_KEY = os.getenv("EMBEDDING_QUEUE_KEY", "papercopilot:embedding:tasks:v1").strip()
EMBEDDING_USE_STAGING = os.getenv("EMBEDDING_USE_STAGING", "false").lower() == "true"

# Graph 节点耗时写入 state.node_timings
GRAPH_NODE_TIMING_ENABLED = os.getenv("GRAPH_NODE_TIMING_ENABLED", "true").lower() == "true"
