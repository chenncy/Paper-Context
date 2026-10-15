# From Your First Document to Memory Across Sessions

This walkthrough is for a first-time Paper-Context user. You will ingest a document through an MCP host,
retrieve evidence with provenance, save a reading preference and recall it in a new session.

[中文](getting-started.zh-CN.md) · [Installation and connection](../README.md#quick-start-no-api-key-or-separate-database-server)

## Installation Check

After following the platform-specific installation commands in the README, run:

```powershell
# Windows / PowerShell
.\.venv\Scripts\python.exe scripts/check_install.py
```

```bash
# macOS / Linux
./.venv/bin/python scripts/check_install.py
```

The checker verifies Python, pinned dependencies (including the DOCX parser), SQLite FTS5, native imports,
MCP handshakes and tool calls. It restarts the memory server and reads a saved preference with a new session ID.
All files and databases are temporary. It ignores the project's `.env`, does not call a real embedding API
and does not modify existing data. Custom MySQL and remote embedding connections require separate checks.

The final lines should include:

```text
[PASS] new server/session -> memory_get and memory_prepare recall saved preference
[PASS] offline end-to-end check complete; existing user data was not used
[PASS] installation and both MCP services are ready
```

## MCP Configuration

Use `scripts/configure_mcp.py` as shown in the README. It generates absolute paths using the current
virtualenv's interpreter, including paths with spaces. Regenerate configuration after moving the repository
or virtualenv. Generated `.mcp.json` and `codex-mcp.toml` files are local and Git-ignored. The old repository's
default Windows entries are migrated automatically; custom entries with the same names are preserved for manual handling.

The following manual examples require replacing the repository location. Forward slashes work in Windows
JSON/TOML paths and avoid backslash escaping. The client does not need to start in the repository directory.

### Claude Code / Claude Desktop: Windows

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

### Claude Code / Claude Desktop: macOS / Linux

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

On macOS, the home directory is usually under `/Users/you/`; use your actual path.
Claude Code reads project-root `.mcp.json`. For Claude Desktop, merge `mcpServers` entries into its client configuration.

### Codex: Windows

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

### Codex: macOS / Linux

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

Merge the appropriate sections into `~/.codex/config.toml`. See the
[official OpenAI MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) for these fields and timeouts.
CLI users can run `codex mcp list` to inspect configuration; verify connection status in the client.

## First Use: Ingest, Search, Provenance, Memory

The JSON below contains **tool arguments**, not shell commands. Ask your host agent to call the named tool,
or paste the arguments into an MCP client with a tool inspector. The host generates answers; Paper-Context returns evidence.

### 1. Ingest the Teaching Document

`examples/demo-paper.txt` is a fictional teaching document, not a published paper. Replace `file_path`
with its absolute path and call `rag_ingest`:

```json
{
  "file_path": "C:/Projects/Paper-Context/examples/demo-paper.txt",
  "paper_title": "Paper-Context Teaching Example"
}
```

On macOS/Linux, use a path such as `/home/you/Paper-Context/examples/demo-paper.txt`.
Alternatively, ask the host: "Use rag_ingest to import this absolute file path and report whether it succeeded."

Expect `success=true`, `validation_metrics.chunk_count > 0` and a `reverse` descriptor.
With no API key, `vectorized=false` is expected: full text is already indexed.

### 2. Retrieve Evidence

Call `rag_search`:

```json
{"query": "evidence provenance", "mode": "fts", "top_n": 3}
```

Expect `mode="fts"` and at least one hit. Omitting `mode` selects `auto`, which falls back to FTS when
the vector index is unavailable and explains this in `note`. Query English documents with English words;
FTS does not perform cross-language semantic matching.

### 3. Inspect Provenance and Ask the Host to Answer

A hit should contain fields like these (text and scores may vary):

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

For TXT/DOCX, `page=0` is a loader placeholder, not an actual citation page. PDF `page` is a zero-based
physical page index; display page 0 as page 1 to a reader. Sections come from parsing/chunking and may
need verification for complex PDFs. Ask the host to explain provenance using only the retrieved evidence,
citing title, section and available pages. If there are no hits, report that no relevant documents were found.

### 4. Save a Reading Preference

Call `memory_save`:

```json
{
  "username": "demo-user",
  "key": "reading_preference",
  "value": "Always include the paper title, section and available page when citing evidence.",
  "category": "preference",
  "scope": "global",
  "session_id": "demo-first"
}
```

Expect `status="ok"` and `scope="global"`. Global scope persists across sessions; session scope is specific
to the given session. Memory stores what the host explicitly writes; it does not automatically remember all conversation.

### 5. Read It in a New Session

Open a new chat using the same MCP configuration. Keep the **same username and database**, with a new session ID.
Call `memory_prepare` first:

```json
{"username": "demo-user", "session_id": "demo-next"}
```

Expect `status="ready"`, the preference in `content`, and memory sources in `citations`.
For an exact read, call `memory_get`:

```json
{"username": "demo-user", "key": "reading_preference", "session_id": "demo-next"}
```

Expect the preference text with a `[global|preference|...]` prefix. A prepare call with username/session ID
is sufficient to assemble global preferences. Once vector capabilities are configured, add a `query` for task-related recall.

### 6. Remove Demo Data (Optional)

Call `rag_ingest` with the `reverse` descriptor from ingestion, plus the required `file_path=""`:

```json
{"file_path": "", "action": "delete", "source": "demo-paper.txt"}
```

Use `memory_list(username="demo-user", scope="global")` to find the demo item's ID, then call
`memory_forget(username="demo-user", memory_id=that_id)`. Delete only the demo item.

## Optional Configuration

No `.env` is required. Copy `.env.example` only for custom settings; edit an existing `.env` instead of overwriting it.
Environment variables take precedence. Both services load the repository-root `.env` regardless of the host's working directory.

- **Vector search**: set `EMBEDDING_BASE_URL`, the endpoint's `EMBEDDING_API_KEY` and matching `EMBEDDING_MODEL`.
  Changing the URL does not change the model name. Run `scripts/backfill_embeddings.py` with the virtualenv to vectorize existing full text.
- **MySQL**: create the database, enable the commented `SQLALCHEMY_DATABASE_URL` example, then run `scripts/init_db.py`.
  Connection failures are reported and do not silently switch databases.
- **Data migration**: stop both services before backing up/moving `paper_context.db`, `chroma_db_qwen/`, `doc_store/`
  and original documents. Update `.env` and MCP paths after moving storage.

## Troubleshooting

| Symptom | Action |
|---|---|
| `py` / `python3.11` is missing | Install Python 3.11, or substitute 3.10. Check `--version`; a system Python 3.12+ is unsupported. |
| PowerShell cannot find `PYTHONPATH=.` | Use this guide's commands; initialization and tests do not require PYTHONPATH. |
| `No module named 'docx2txt'` | Run `-m pip install -r requirements.txt` with the same virtualenv interpreter configured in MCP, then rerun the checker. |
| `DLL load failed` / `access violation` / native import failure | Reinstall current requirements (Windows pins ONNX Runtime 1.20.1). If it still fails, install/repair the [Microsoft Visual C++ x64 Runtime](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist), recreate a Python 3.11 virtualenv and retry. Report Python/OS versions and the failed step; do not share `.env`. The [ONNX installation guide](https://onnxruntime.ai/docs/install/) lists the Windows Visual C++ Runtime requirement. |
| MySQL connection fails, but you want local storage | Remove/comment the `.env` connection string and check host `SQLALCHEMY_DATABASE_URL` / `MYSQL_*` variables. Restart and initialize default SQLite. |
| MCP server missing or startup timeout | Run the checker; generate config with the correct virtualenv, verify absolute paths, restart and enable the servers. Allow 300 seconds for slow startup; Claude Code can be started with `MCP_TIMEOUT=300000`. |
| `hits=[]` | Confirm ingestion returned `success=true`, inspect `rag_stats.fts_chunk_count`, and query source keywords. Use English words for English documents. |
| `vectorized=false` / fallback `note` | Expected in offline mode: FTS is available. Configure embeddings and backfill only when you need semantic search. |
| New session cannot find a preference | Verify global scope, identical username and database, then check the exact key with memory_get. |
| Generator reports an existing same-name server | Existing configuration is preserved. Update the two paths manually or remove only those two old entries before regenerating. |

After upgrading, reinstall requirements, run initialization and the checker, then regenerate or verify MCP configuration.

See the [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp) for its startup timeout environment variable.
