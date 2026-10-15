"""Check Python, pinned dependencies, SQLite FTS5 and both MCP servers."""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=360, help="MCP smoke-test timeout in seconds")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if sys.version_info[:2] not in ((3, 10), (3, 11)):
        print("[FAIL] Use Python 3.10 or 3.11 and recreate .venv; Python 3.12+ is not supported.")
        return 1
    print(f"[PASS] Python {sys.version.split()[0]} ({sys.platform})", flush=True)
    try:
        from packaging.requirements import Requirement
        issues = []
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            try:
                version = importlib.metadata.version(requirement.name)
                if version not in requirement.specifier:
                    issues.append(f"{requirement.name}: installed {version}, expected {requirement.specifier}")
            except importlib.metadata.PackageNotFoundError:
                issues.append(f"{requirement.name}: missing")
        if issues:
            print("[FAIL] Dependencies: " + "; ".join(issues))
            print("Run this interpreter with -m pip install -r requirements.txt, then retry.")
            return 1
        print("[PASS] pinned dependencies (including docx2txt)", flush=True)
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE VIRTUAL TABLE smoke USING fts5(text)")
            db.execute("INSERT INTO smoke VALUES ('retrieval evidence')")
            if not db.execute("SELECT text FROM smoke WHERE smoke MATCH 'retrieval'").fetchone():
                raise RuntimeError("SQLite FTS5 query failed")
        print("[PASS] SQLite FTS5", flush=True)
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        return 1

    # A native dependency can terminate Python without raising an exception.
    # Probe in a child so the checker can report the failure instead of crashing.
    native_code = (
        "import os\n"
        "if os.name == 'nt':\n"
        " import ctypes; ctypes.windll.kernel32.SetErrorMode(3)\n"
        "import onnxruntime, chromadb, docx2txt, pymupdf\n"
    )
    env = dict(os.environ, PYTHONIOENCODING="utf-8", ANONYMIZED_TELEMETRY="FALSE")
    try:
        native = subprocess.run([sys.executable, "-c", native_code], env=env, capture_output=True, timeout=60)
        if native.returncode:
            print(f"[FAIL] native dependencies failed to import (exit {native.returncode}).")
            details = native.stderr.decode("utf-8", errors="replace").strip().splitlines()
            if details:
                print(details[-1])
            print("See docs/getting-started.md or docs/getting-started.zh-CN.md: native DLL / access violation.")
            return 1
        print("[PASS] native dependencies import in a separate process", flush=True)
        print("Checking MCP startup and offline use (temporary data; this can take several minutes)...", flush=True)
        smoke = subprocess.run(
            [sys.executable, str(ROOT / "scripts/smoke_mcp.py"), "--timeout", str(max(1, args.timeout - 30))],
            env=env, timeout=args.timeout,
        )
        if smoke.returncode:
            print(f"[FAIL] MCP smoke check exited with {smoke.returncode}.")
            return 1
    except subprocess.TimeoutExpired:
        print("[FAIL] check timed out; check antivirus/slow disk and retry with --timeout 600.")
        return 1
    print("[PASS] installation and both MCP services are ready", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
