"""Generate host configuration using the current virtualenv and absolute paths."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def claude_config(root: Path, python: Path) -> dict:
    return {"mcpServers": {
        f"paper-context-{service}": {
            "command": python.as_posix(),
            "args": [(root / f"{service}_mcp_server.py").as_posix()],
            "env": {"PYTHONIOENCODING": "utf-8"},
        }
        for service in ("rag", "memory")
    }}


def codex_config(root: Path, python: Path) -> str:
    # JSON strings/arrays are also valid TOML basic strings/arrays.
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    blocks = []
    for service in ("rag", "memory"):
        blocks.append(
            f"[mcp_servers.paper_context_{service}]\n"
            f"command = {quote(python.as_posix())}\n"
            f"args = {quote([(root / f'{service}_mcp_server.py').as_posix()])}\n"
            "startup_timeout_sec = 300\n"
            "tool_timeout_sec = 300\n"
            f"[mcp_servers.paper_context_{service}.env]\n"
            'PYTHONIOENCODING = "utf-8"\n'
        )
    return "\n".join(blocks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("claude", "codex"), default="claude")
    args = parser.parse_args()
    if sys.prefix == sys.base_prefix:
        parser.error("Run with .venv/Scripts/python.exe (Windows) or .venv/bin/python (macOS/Linux).")
    # Do not resolve the Unix virtualenv symlink: its path selects the virtualenv.
    python = Path(sys.executable).absolute()
    if args.client == "claude":
        target = ROOT / ".mcp.json"
        config = json.loads(target.read_text(encoding="utf-8-sig")) if target.exists() else {}
        servers = config.setdefault("mcpServers", {})
        generated = claude_config(ROOT, python)["mcpServers"]
        # Keep other servers; refuse to silently replace a user's custom server.
        for name, settings in generated.items():
            service = name.removeprefix("paper-context-")
            legacy = {"command": ".venv/Scripts/python.exe", "args": [f"{service}_mcp_server.py"]}
            if name in servers and servers[name] != settings and servers[name] != legacy:
                parser.error(f"{name} already exists in .mcp.json; remove that entry or edit its paths manually.")
        servers.update(generated)
        target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Generated .mcp.json. Reopen the project in Claude Code and enable both servers.")
    else:
        target = ROOT / "codex-mcp.toml"
        target.write_text(codex_config(ROOT, python), encoding="utf-8")
        print("Generated codex-mcp.toml. Merge its two server sections into ~/.codex/config.toml, then restart Codex.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
