# Paper-Context

**A paper knowledge base and long-term memory for AI agents. MCP-native and host-agnostic — thinking and orchestration stay with your agent (Codex / Claude Code / any MCP client); knowing and remembering live here.**

[中文文档](README.zh-CN.md)

```
┌─ Host layer: Codex / Claude Code / Claude Desktop (thinking, research, writing)
├─ Service layer: Paper-Context
│    ① Paper knowledge base: rag_search (evidence retrieval with provenance) / rag_ingest (reversible)
│    ② Long-term memory: memory_save / get / history / forget / prepare / handoff
└─ Data layer: SQLite FTS5 (default, zero-config) + optional vector store + MySQL (memory source of truth)
```

## Quick Start (FTS, zero-config — no API key required)

```bash
git clone https://github.com/chenncy/Paper-Context.git
cd Paper-Context
python -m venv .venv                # Python 3.10/3.11
.venv/Scripts/pip install -r requirements.txt   # or .venv/bin/pip
cp .env.example .env
```

**Initialize the database** (memory service requires MySQL; the FTS knowledge base works out of the box):

```bash
mysql -uroot -e "CREATE DATABASE paper_context CHARACTER SET utf8mb4"
echo "SQLALCHEMY_DATABASE_URL=mysql+pymysql://root:@localhost:3306/paper_context?charset=utf8mb4" >> .env
PYTHONPATH=. .venv/Scripts/python scripts/init_db.py
```

## Connect a Host

**Claude Code**: `.mcp.json` is already in the repo root — open the project and the servers are available.

**Codex** (`~/.codex/config.toml`, replace paths with your clone location):

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

Expect ~2 minutes of cold start on first tool call (index loading happens at startup; subsequent calls are fast).

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

Ingestion is decoupled from embedding: when the embedding endpoint is unreachable, ingestion still succeeds (text goes to FTS, marked unvectorized); once restored, `scripts/backfill_embeddings.py` fills the vectors in.

## Memory Mechanisms

- **Version chain & supersede**: when a fact is replaced, the old one is explicitly marked `superseded` (with timestamp) — excluded from default reads, viewable at any point in time via `as_of`
- **GDPR-style forgetting**: delete by item or by scope (all/global/session), with cascade cleanup of vector and graph projections
- **Conflict adjudication**: same-key-different-value conflicts get a single LLM adjudication (the llm is injected as a parameter — replace it with anything); preference conflicts are "contextualized" rather than overwritten
- **Handoff**: at session end, pack (objective/progress/next_steps/blockers) → the next session, or a different host, claims it — agents switch hosts without losing state

## Tests

```bash
PYTHONPATH=. .venv/Scripts/python -m pytest tests/ -q   # 71 tests, all local (SQLite / tmp files)
```

## License

MIT
