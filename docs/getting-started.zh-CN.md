# 从第一篇文档到跨会话记忆

本指南面向第一次使用 Paper-Context 的用户。完成后，你会在 MCP 宿主中导入文档、
检索带出处的证据，并在新会话里读到之前保存的阅读偏好。

[English](getting-started.md) · [安装与接入](../README.zh-CN.md#快速开始无需-api-key无需单独部署数据库)

## 安装检查

先完成 README 对应平台的安装命令，再运行：

```powershell
# Windows / PowerShell
.\.venv\Scripts\python.exe scripts/check_install.py
```

```bash
# macOS / Linux
./.venv/bin/python scripts/check_install.py
```

检查依次验证 Python 版本、安装依赖（包含 DOCX 解析器）、SQLite FTS5、原生库导入、
两个 MCP 服务的启动握手和工具调用。最后会重启记忆服务，用新的 session_id 读取旧偏好。
所有示例和数据库都在临时目录中，不读取 `.env`，不调用真实 embedding API，也不修改你的现有数据。
它验证基础离线安装；自定义 MySQL 和远程 embedding 的可用性需要另外验证。

成功时最后几行包含：

```text
[PASS] new server/session -> memory_get and memory_prepare recall saved preference
[PASS] offline end-to-end check complete; existing user data was not used
[PASS] installation and both MCP services are ready
```

## MCP 配置

优先使用 README 的 `scripts/configure_mcp.py` 命令。生成器直接使用当前虚拟环境的解释器，
生成完整绝对路径，支持仓库路径含空格。移动仓库或重建到其他位置后，需重新生成配置。
`.mcp.json` 和 `codex-mcp.toml` 是本机文件，不随开源代码提交。旧版本仓库自带的默认 Windows 配置会自动迁移；
同名自定义配置会保留并提示手动处理。

下面是手动配置示例，**请将仓库目录替换为实际位置**。Windows 的 JSON/TOML 中推荐写 `/`，
避免 `\` 被当成转义。客户端无需从仓库目录启动。

### Claude Code / Claude Desktop：Windows

```json
{
  "mcpServers": {
    "paper-context-rag": {
      "command": "C:/Projects/Paper-Context/.venv/Scripts/python.exe",
      "args": ["C:/Projects/Paper-Context/rag_mcp_server.py"],
      "env": {"PYTHONIOENCODING": "utf-8"}
    },
    "paper-context-memory": {
      "command": "C:/Projects/Paper-Context/.venv/Scripts/python.exe",
      "args": ["C:/Projects/Paper-Context/memory_mcp_server.py"],
      "env": {"PYTHONIOENCODING": "utf-8"}
    }
  }
}
```

### Claude Code / Claude Desktop：macOS / Linux

```json
{
  "mcpServers": {
    "paper-context-rag": {
      "command": "/home/you/Paper-Context/.venv/bin/python",
      "args": ["/home/you/Paper-Context/rag_mcp_server.py"],
      "env": {"PYTHONIOENCODING": "utf-8"}
    },
    "paper-context-memory": {
      "command": "/home/you/Paper-Context/.venv/bin/python",
      "args": ["/home/you/Paper-Context/memory_mcp_server.py"],
      "env": {"PYTHONIOENCODING": "utf-8"}
    }
  }
}
```

macOS 的用户目录通常为 `/Users/you/`，请使用真实目录。Claude Code 将配置放在项目根 `.mcp.json`；
Claude Desktop 则合并到其客户端配置中的 `mcpServers`。

### Codex：Windows

```toml
[mcp_servers.paper_context_rag]
command = "C:/Projects/Paper-Context/.venv/Scripts/python.exe"
args = ["C:/Projects/Paper-Context/rag_mcp_server.py"]
startup_timeout_sec = 300
tool_timeout_sec = 300

[mcp_servers.paper_context_memory]
command = "C:/Projects/Paper-Context/.venv/Scripts/python.exe"
args = ["C:/Projects/Paper-Context/memory_mcp_server.py"]
startup_timeout_sec = 300
tool_timeout_sec = 300
```

### Codex：macOS / Linux

```toml
[mcp_servers.paper_context_rag]
command = "/home/you/Paper-Context/.venv/bin/python"
args = ["/home/you/Paper-Context/rag_mcp_server.py"]
startup_timeout_sec = 300
tool_timeout_sec = 300

[mcp_servers.paper_context_memory]
command = "/home/you/Paper-Context/.venv/bin/python"
args = ["/home/you/Paper-Context/memory_mcp_server.py"]
startup_timeout_sec = 300
tool_timeout_sec = 300
```

把对应配置合并到 `~/.codex/config.toml`。这些字段与超时设置见
[官方 OpenAI MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。
CLI 用户可用 `codex mcp list` 查看配置，连接状态仍需在客户端确认。

## 第一次使用：导入、检索、出处、记忆

以下 JSON 是**工具参数**，在宿主中让 agent 调用对应工具，或粘贴到支持工具调试的 MCP 客户端。
不是终端命令。宿主负责生成回答，Paper-Context 返回检索证据。

### 1. 导入教学文档

仓库自带 `examples/demo-paper.txt`，是一份虚构教学文档，不是已发表论文。
把 `file_path` 替换成该文件的绝对路径，调用 `rag_ingest`：

```json
{
  "file_path": "C:/Projects/Paper-Context/examples/demo-paper.txt",
  "paper_title": "Paper-Context Teaching Example"
}
```

macOS / Linux 使用如 `/home/you/Paper-Context/examples/demo-paper.txt`。
也可直接告诉宿主：“请用 rag_ingest 导入这个绝对路径下的文件，并告诉我是否成功。”

预期 `success=true`，`validation_metrics.chunk_count > 0`，返回 `reverse` 撤销参数。
没有 API key 时 `vectorized=false` 是正常结果，全文索引已经可用。

### 2. 检索证据

调用 `rag_search`：

```json
{"query": "evidence provenance", "mode": "fts", "top_n": 3}
```

预期 `mode="fts"`，`hits` 至少一条。省略 `mode` 会使用 `auto`，没有可用向量索引时自动走 FTS，
并在 `note` 解释降级。英文文档用英文查询词；FTS 不提供跨语言语义匹配。

### 3. 检查出处，再让宿主回答

命中的证据应包含类似字段（文本和分数可能不同）：

```json
{
  "source": "demo-paper.txt",
  "title": "Paper-Context Teaching Example",
  "section": "Introduction",
  "page": "0",
  "block_type": "paragraph",
  "text": "...Evidence provenance records the title, section and source..."
}
```

对 TXT/DOCX，`page=0` 是加载器的页占位，不能当作真实页码引用。PDF 的 `page` 为从 0 开始的物理页索引；
面向读者引用时，第 0 页应写作第 1 页。section 由解析与切分得到，复杂 PDF 可能需要人工核对。
让宿主“只根据命中证据解释 provenance，并注明标题、章节和可用页码”；没有命中时应明确说未找到相关文献。

### 4. 保存阅读偏好

调用 `memory_save`：

```json
{
  "username": "demo-user",
  "key": "reading_preference",
  "value": "回答论文问题时，请注明论文标题、章节和可用页码。",
  "category": "preference",
  "scope": "global",
  "session_id": "demo-first"
}
```

预期 `status="ok"`、`scope="global"`。global 用于跨会话；session 作用域只用于指定会话。
服务只保存宿主显式写入的内容，不会自动记住所有对话。

### 5. 新会话读取

打开同一个 MCP 配置下的新聊天，保持 **同一个 username 和数据库**，使用新的 session_id。
先调用 `memory_prepare`：

```json
{"username": "demo-user", "session_id": "demo-next"}
```

预期 `status="ready"`，`content` 包含刚才的阅读偏好，`citations` 标明记忆来源。
也可调用 `memory_get` 精确确认：

```json
{"username": "demo-user", "key": "reading_preference", "session_id": "demo-next"}
```

预期返回带 `[global|preference|...]` 前缀的偏好文本。只提供 username/session_id 的 prepare 已足以
装配全局偏好；配置好向量能力后，可额外传入 `query` 做任务相关召回。

### 6. 清理演示数据（可选）

调用 `rag_ingest`，传入第 1 步返回的 `reverse` 字段，并补上必填的 `file_path=""`，例如：

```json
{"file_path": "", "action": "delete", "source": "demo-paper.txt"}
```

通过 `memory_list(username="demo-user", scope="global")` 找到演示记忆的 id，再调用
`memory_forget(username="demo-user", memory_id=该id)`。只清理对应演示项。

## 可选配置

默认无需 `.env`。需要修改配置时，可复制 `.env.example` 到 `.env`；已有文件时请编辑，不要覆盖。
环境变量优先于 `.env`。两个服务加载仓库根目录 `.env`，与宿主启动目录无关。

- **向量检索**：配置 `EMBEDDING_BASE_URL`、端点实际需要的 `EMBEDDING_API_KEY` 和匹配的 `EMBEDDING_MODEL`。
  模型名不会仅随端点自动改变。对已入库的全文数据，用虚拟环境执行 `scripts/backfill_embeddings.py` 补算。
- **MySQL**：先创建数据库，再启用示例里注释的 `SQLALCHEMY_DATABASE_URL`，执行 `scripts/init_db.py` 建表。
  连接失败会报错，不会自动换成另一个数据库。
- **数据迁移**：备份 `paper_context.db`、`chroma_db_qwen/`、`doc_store/` 和原论文文件，停掉两个服务后再搬迁。
  更换存储路径后更新 `.env` 和 MCP 配置。

## 常见报错

| 现象 | 处理方式 |
|---|---|
| `py` / `python3.11` 不存在 | 安装 Python 3.11；已有 3.10 可替换版本命令。先用 `--version` 确认，不要直接使用系统的 3.12+。 |
| PowerShell 提示 `PYTHONPATH=.` 不存在 | 使用本指南命令；初始化和测试不需要手动设置 PYTHONPATH。 |
| `No module named 'docx2txt'` | 用 MCP 配置中的同一个虚拟环境解释器重新执行 `-m pip install -r requirements.txt`，再运行安装检查。 |
| `DLL load failed` / `access violation` / 原生依赖导入失败 | 先重新安装当前 requirements（Windows 固定 ONNX Runtime 1.20.1）。如仍失败，安装/修复 [Microsoft Visual C++ x64 Runtime](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)，重建 Python 3.11 虚拟环境再检查。提供 Python/OS 版本及失败步骤，不要贴 `.env`。[ONNX 官方安装要求](https://onnxruntime.ai/docs/install/)说明 Windows 需要 Visual C++ Runtime。 |
| 连接 MySQL 失败，但只想本地使用 | 移除或注释 `.env` 中的连接串，也检查宿主环境的 `SQLALCHEMY_DATABASE_URL` / `MYSQL_*`。重启服务，重新初始化默认 SQLite。 |
| 找不到 MCP 服务 / 启动超时 | 先运行检查脚本；生成配置时使用正确虚拟环境，确认绝对路径存在，重启客户端并启用服务。冷启动较慢时保留 300 秒启动超时；Claude Code 可设置 `MCP_TIMEOUT=300000` 后启动客户端。 |
| 查询 `hits=[]` | 先确认文件入库 `success=true`，用 `rag_stats` 检查 `fts_chunk_count`；用原文关键词检索，英文文献用英文词。 |
| `vectorized=false` / `note` 提示降级 | 离线基础模式的正常结果，仍可用 FTS。需要语义检索时再配置可用 embedding 并补算。 |
| 新会话找不到偏好 | 确认保存是 `scope=global`、读取 username 相同、两个会话使用同一数据库；精确用 memory_get 检查。 |
| 生成器提示同名配置已存在 | 它保留了已有配置。手动更新这两个服务的解释器/脚本路径，或只删除这两个旧条目再重新生成。 |

升级后重新安装依赖、运行 `scripts/init_db.py` 和安装检查，再重新生成或核对 MCP 配置。

Claude Code 的启动超时环境变量见[官方 MCP 文档](https://code.claude.com/docs/en/mcp)。
