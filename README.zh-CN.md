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

## 快速开始（无需 API key、无需单独部署数据库）

需要 **Python 3.10 或 3.11**。请按你的系统选择一组命令，不需要激活虚拟环境。

### Windows / PowerShell

```powershell
git clone https://github.com/chenncy/Paper-Context.git
cd Paper-Context
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/init_db.py
.\.venv\Scripts\python.exe scripts/check_install.py
```

### macOS / Linux

先安装 Python 3.11（也可使用 `python3.10`），然后执行：

```bash
git clone https://github.com/chenncy/Paper-Context.git
cd Paper-Context
python3.11 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python scripts/init_db.py
./.venv/bin/python scripts/check_install.py
```

检查结束应显示 `[PASS] installation and both MCP services are ready`。检查会在临时目录
验证 TXT、DOCX、PDF 入库、带出处的全文检索和跨会话记忆，不读取项目 `.env`，不修改已有数据。
首次检查可能需要几分钟，过程中会打印进度。

默认记忆数据存到项目根 `paper_context.db`，论文索引和父块分别存到 `chroma_db_qwen/`、
`doc_store/`。无需创建 `.env`；如需自定义，可复制 `.env.example`，示例也保持 SQLite 默认。
向量检索与 MySQL 是可选升级，见[配置与排错](docs/getting-started.zh-CN.md#可选配置)。

## 接入宿主

用刚安装的虚拟环境生成当前机器的绝对路径配置，无需手动拼接 Windows 或 Unix 路径。

**Windows / PowerShell：**

```powershell
.\.venv\Scripts\python.exe scripts/configure_mcp.py --client claude
# 使用 Codex 时改为：
.\.venv\Scripts\python.exe scripts/configure_mcp.py --client codex
```

**macOS / Linux：**

```bash
./.venv/bin/python scripts/configure_mcp.py --client claude
# 使用 Codex 时改为：
./.venv/bin/python scripts/configure_mcp.py --client codex
```

- **Claude Code**：生成项目根 `.mcp.json`，重新打开项目并按客户端提示启用两个服务。
- **Claude Desktop**：把生成文件里的 `mcpServers` 条目合并到客户端的 MCP 配置，重启客户端。
- **Codex**：生成 `codex-mcp.toml`，把其中两段服务配置合并到 `~/.codex/config.toml`，重启 Codex；已有同名配置时替换该段，勿重复添加。

生成的配置不含 API key，并已加入 Git 忽略。Claude 生成器保留其他服务，同名自定义配置会提示手动处理。
冷启动时间取决于磁盘和依赖加载；Codex 配置给启动与工具调用预留 300 秒。
客户端连接成功后，可查看 `rag_ingest`、`rag_search`、`memory_save` 等工具。

## 第一次使用

按[完整演示](docs/getting-started.zh-CN.md#第一次使用导入检索出处记忆)走一遍：
导入仓库自带的教学文档 → 检索证据 → 检查 title/section/page → 保存阅读偏好 → 新会话读取。
每一步都有工具参数、预期结果和排错提示；换成自己的 PDF 时只需替换文件绝对路径。

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

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

```bash
./.venv/bin/python -m pip install pytest
./.venv/bin/python -m pytest tests/ -q
```

CI 覆盖 Windows、macOS、Linux 的 Python 3.10/3.11，先做安装与真实 MCP 启动检查，再运行回归测试。

## License

MIT
