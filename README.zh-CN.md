# Paper-Context

[![tests](https://github.com/chenncy/Paper-Context/actions/workflows/tests.yml/badge.svg)](https://github.com/chenncy/Paper-Context/actions/workflows/tests.yml)

**给 AI Agent 的论文知识库与长期记忆。MCP-native，宿主无关——思考和编排交给你的 agent（Codex / Claude Code / 任何 MCP 客户端），"知道"和"记住"交给这里。**

[English](README.md)

```
┌─ 宿主层：Codex / Claude Code / Claude Desktop（思考、研究、写作）
├─ 服务层：Paper-Context
│    ① 论文知识库：rag_search（带出处的证据检索）/ rag_ingest（可撤销入库）
│    ② 长期记忆：memory_save / get / history / forget / prepare / handoff
└─ 数据层：SQLite FTS5 + 本地父块存储 + 可选向量库；记忆默认 SQLite，可选 MySQL
```

## 快速开始（零配置——无需 API key、无需数据库）

```bash
git clone https://github.com/chenncy/Paper-Context.git
cd Paper-Context
python -m venv .venv                # Python 3.10/3.11
.venv/Scripts/pip install -r requirements.txt   # 或 .venv/bin/pip
PYTHONPATH=. .venv/Scripts/python scripts/init_db.py
```

到此即可用——FTS 全文检索零 API key，记忆服务默认本地 SQLite 文件
（`paper_context.db`）。两个可选升级：

- **向量检索**：`EMBEDDING_BASE_URL` 指向任意 OpenAI 兼容端点
- **MySQL**（多进程/持久部署）：

```bash
mysql -uroot -e "CREATE DATABASE paper_context CHARACTER SET utf8mb4"
echo "SQLALCHEMY_DATABASE_URL=mysql+pymysql://root:@localhost:3306/paper_context?charset=utf8mb4" >> .env
```

## 接入宿主

**Claude Code**：项目根 `.mcp.json` 已登记，在项目目录打开即用。

**Codex**（`~/.codex/config.toml`，路径替换为你的仓库位置）：

```toml
[mcp_servers.paper_context_rag]
command = "<repo>/.venv/Scripts/python.exe"
args = ["<repo>/rag_mcp_server.py"]
startup_timeout_sec = 300

[mcp_servers.paper_context_memory]
command = "<repo>/.venv/Scripts/python.exe"
args = ["<repo>/memory_mcp_server.py"]
startup_timeout_sec = 300
```

首次调用前有约 2 分钟冷启动（索引加载，启动期预载完成，之后工具调用为热路径）。

## 工具面（17 个 MCP 工具）

| 服务 | 工具 | 说明 |
|---|---|---|
| 知识库 | `rag_search` | 统一检索：**mode=auto**（默认）向量优先、embedding 不可达自动降级全文检索并在 note 声明；`fts` 零依赖；返回带 title/section/page 出处的证据，不生成答案 |
| | `rag_ingest` | PDF/DOCX/TXT 结构化入库（section 感知切分 + 表格/公式/caption 原子块）；带 reverse 描述符，`action=delete` 可撤销 |
| | `rag_stats` | 索引统计：chunk 数 / FTS 数 / 向量化比例 |
| 记忆 | `memory_save` / `memory_get` / `memory_list` / `memory_search` | 显式写入（global 跨会话 / session 会话内）与多路召回 |
| | `memory_history` / `memory_update` / `memory_forget` | 版本链追溯 / 纠错（旧值入史）/ 遗忘（单条或按范围） |
| | `memory_prepare` | PreparedContext：为一次任务装配**有界**记忆上下文（分层预算、带引用、失败降级不阻塞） |
| | `handoff_prepare` / `commit` / `pending` / `acknowledge` | 跨会话/跨宿主工作转交状态机 |

## 检索的三级档位

1. **FTS（默认，零配置）**：SQLite FTS5 全文检索，CJK bigram + 英文词分词，BM25 排序——克隆即用
2. **embedding 端点**（可选）：`EMBEDDING_BASE_URL` 指向任何 OpenAI 兼容端点（Ollama / vLLM / 自建网关）后向量检索自动启用
3. **云 API**（可选）：任何 OpenAI 兼容 embedding 端点（如 DashScope text-embedding-v4）

入库先保存父块、全文文本与完整元数据，再尝试写入向量库。无 API key 或向量端点不可达时仍可入库，`validation_metrics.vectorized=false` 表示本次未完成向量写入，可使用全文检索；本地文本写入失败会返回 `success=false`。同一文件内容不变时，重复导入复用稳定 ID。

`rag_stats` 和删除不调用 embedding；向量后端不可用时统计仍返回全文条数，向量条数为 `null` 并附 `vector_error`。删除两侧任一失败会返回 `success=false`，同时说明已删除的条数。

配置好可用的 embedding 端点后，可执行 `scripts/backfill_embeddings.py` 补算；完整元数据和父块引用一并保留，补算后可继续使用父子块检索。旧数据缺少父块引用时，脚本会提示重新导入源文件。

## 记忆系统的机制特性

- **版本链与取代**：旧事实被新事实取代时显式 `superseded`（记录失效时刻），默认读取排除、`as_of` 时间旅行回看任意时刻
- **GDPR 式遗忘**：按条或按范围（all/global/session）删除，级联清理向量与图谱投影
- **冲突裁决**：同 key 不同 value 经 LLM 单次裁决（llm 参数注入，可替换）；preference 类冲突"条件化"而非覆盖
- **Handoff 转交**：会话结束打包（objective/progress/next_steps/blockers）→ 下一次会话或另一个宿主认领——agent 换宿主不断档

## 测试

```bash
PYTHONPATH=. .venv/Scripts/python -m pytest tests/ -q   # 89 个测试，全部本地 SQLite/临时文件
```

## License

MIT
