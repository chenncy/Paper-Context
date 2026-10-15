# Paper-Context

[![tests](https://github.com/chenncy/Paper-Context/actions/workflows/tests.yml/badge.svg)](https://github.com/chenncy/Paper-Context/actions/workflows/tests.yml)

**A paper knowledge base and long-term memory for AI agents. MCP-native and host-agnostic — thinking and orchestration stay with your agent (Codex / Claude Code / any MCP client); knowing and remembering live here.**

[中文文档](README.zh-CN.md)

```
┌─ Host layer: Codex / Claude Code / Claude Desktop (thinking, research, writing)
├─ Service layer: Paper-Context
│    ① Paper knowledge base: rag_search (evidence retrieval with provenance) / rag_ingest (reversible)
│    ② Long-term memory: memory_save / get / history / forget / prepare / handoff
└─ Data layer: SQLite FTS5 + local parent documents + optional vectors; memory defaults to SQLite, with optional MySQL
```

## Quick Start (no API key or separate database server)

Use **Python 3.10 or 3.11**. Choose the commands for your platform; virtualenv activation is not required.

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

Install Python 3.11 first (or use `python3.10`), then run:

```bash
git clone https://github.com/chenncy/Paper-Context.git
cd Paper-Context
python3.11 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python scripts/init_db.py
./.venv/bin/python scripts/check_install.py
```

The final line should be `[PASS] installation and both MCP services are ready`. The check
uses temporary data to verify TXT, DOCX and PDF ingestion, evidence retrieval and memory across sessions.
It ignores the project's `.env` and does not modify existing data. The first check may take several minutes;
progress is printed along the way.

Memory defaults to `paper_context.db` in the project root. Paper indexes and parent documents live in
`chroma_db_qwen/` and `doc_store/`. No `.env` is needed; copying `.env.example` also keeps the SQLite default.
Vector search and MySQL are optional upgrades; see [configuration and troubleshooting](docs/getting-started.md#optional-configuration).

## Connect a Host

Generate configuration with the virtualenv you just installed. The generator uses absolute paths for your platform.

**Windows / PowerShell:**

```powershell
.\.venv\Scripts\python.exe scripts/configure_mcp.py --client claude
# For Codex instead:
.\.venv\Scripts\python.exe scripts/configure_mcp.py --client codex
```

**macOS / Linux:**

```bash
./.venv/bin/python scripts/configure_mcp.py --client claude
# For Codex instead:
./.venv/bin/python scripts/configure_mcp.py --client codex
```

- **Claude Code**: the generator creates `.mcp.json` in the project root. Reopen the project and enable both servers when prompted.
- **Claude Desktop**: merge the generated `mcpServers` entries into the client's MCP configuration and restart it.
- **Codex**: merge the two server sections from generated `codex-mcp.toml` into `~/.codex/config.toml` and restart Codex. Replace existing sections with the same names instead of adding duplicates.

Generated files contain no API keys and are Git-ignored. The Claude generator preserves other servers
and asks you to handle conflicting custom entries manually. Startup time varies with disk and dependency loading;
the Codex configuration allows 300 seconds for startup and tool calls.

## First Use

Follow the [complete walkthrough](docs/getting-started.md#first-use-ingest-search-provenance-memory):
ingest the included teaching document → retrieve evidence → inspect title/section/page → save a reading preference → recall it in a new session.
Each step includes tool arguments, expected results and troubleshooting. To use your own PDF, replace the file's absolute path.

## Tools (17 MCP tools)

| Service | Tool | Description |
|---|---|---|
| Knowledge base | `rag_search` | Unified retrieval: **mode=auto** (default) prefers vector search, degrades to full-text automatically when embedding is unreachable (with an explicit note); `fts` is zero-dependency. Returns evidence with title/section/page provenance — no answer generation |
| | `rag_ingest` | Structured ingestion for PDF/DOCX/TXT (section-aware chunking + table/equation/caption atomic blocks); carries a reverse descriptor, undoable via `action=delete` |
| | `rag_stats` | Index stats: chunk count / FTS count / vectorized ratio |
| Memory | `memory_save` / `memory_get` / `memory_list` / `memory_search` | Explicit writes (global cross-session / session scope) and multi-path recall |
| | `memory_history` / `memory_update` / `memory_forget` | Version chain tracing / correction (old value goes to history) / forgetting (single item or by scope) |
| | `memory_prepare` | PreparedContext: assembles a **bounded** memory view for a task (layered budget, cited, degrade-safe) |
| | `handoff_prepare` / `commit` / `pending` / `acknowledge` | Cross-session / cross-host work handoff state machine |

## Retrieval Tiers

1. **FTS (default, zero-config)**: SQLite FTS5 full-text search, CJK bigram + word tokenization, BM25 ranking — clone and use
2. **Embedding endpoint** (optional): point `EMBEDDING_BASE_URL` at any OpenAI-compatible endpoint (Ollama / vLLM / self-hosted gateway) and vector search activates automatically
3. **Cloud API** (optional): any OpenAI-compatible embedding endpoint (e.g. DashScope text-embedding-v4)

Ingestion saves parent documents, full text and complete metadata before attempting vector writes. Without an API key or with an unreachable embedding endpoint, ingestion remains available with `validation_metrics.vectorized=false`; this means the current vector write was not completed, while FTS retrieval is available. Failed local text writes return `success=false`. Re-ingesting an unchanged file reuses stable IDs.

`rag_stats` and deletion do not call embedding. If the vector backend is unavailable, stats still include FTS counts, with a `null` vector count and `vector_error`. If either side of deletion fails, the response reports `success=false` and the counts already deleted.

Once an embedding endpoint is configured, `scripts/backfill_embeddings.py` fills the vectors in. Full metadata and parent references are retained so parent-child retrieval works after backfill. For older records missing parent references, the script asks you to re-ingest the source file.

## Memory Mechanisms

- **Version chain & supersede**: when a fact is replaced, the old one is explicitly marked `superseded` (with timestamp) — excluded from default reads, viewable at any point in time via `as_of`
- **GDPR-style forgetting**: delete by item or by scope (all/global/session), with cascade cleanup of vector and graph projections
- **Conflict adjudication**: same-key-different-value conflicts get a single LLM adjudication (the llm is injected as a parameter — replace it with anything); preference conflicts are "contextualized" rather than overwritten
- **Handoff**: at session end, pack (objective/progress/next_steps/blockers) → the next session, or a different host, claims it — agents switch hosts without losing state

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

```bash
./.venv/bin/python -m pip install pytest
./.venv/bin/python -m pytest tests/ -q
```

CI runs on Windows, macOS and Linux with Python 3.10/3.11. It checks installation and real MCP startup before regression tests.

## License

MIT
