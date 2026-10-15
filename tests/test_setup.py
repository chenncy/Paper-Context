"""Installation regressions: local config preservation and isolated data paths."""
import json
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from scripts import configure_mcp, smoke_mcp


def run_generator(monkeypatch, directory, client="claude"):
    monkeypatch.setattr(configure_mcp, "ROOT", directory)
    monkeypatch.setattr(sys, "prefix", str(directory / ".venv"))
    monkeypatch.setattr(sys, "executable", str(directory / ".venv" / "python"))
    monkeypatch.setattr(sys, "argv", ["configure_mcp.py", "--client", client])
    return configure_mcp.main()


def test_claude_generator_preserves_other_servers_and_migrates_legacy(tmp_path, monkeypatch):
    directory = tmp_path / "Paper Context 空格"
    directory.mkdir()
    target = directory / ".mcp.json"
    other = {"command": "other-client", "args": ["--token-from-env"]}
    target.write_text(json.dumps({"mcpServers": {
        "other": other,
        "paper-context-rag": {"command": ".venv/Scripts/python.exe", "args": ["rag_mcp_server.py"]},
    }}), encoding="utf-8")
    assert run_generator(monkeypatch, directory) == 0
    configuration = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
    assert configuration["other"] == other
    assert configuration["paper-context-rag"]["args"] == [(directory / "rag_mcp_server.py").as_posix()]
    assert configuration["paper-context-memory"]["command"] == (directory / ".venv/python").as_posix()
    # Re-running with the same interpreter does not create duplicates or conflict.
    assert configure_mcp.main() == 0


def test_claude_generator_keeps_custom_configuration_on_conflict(tmp_path, monkeypatch):
    target = tmp_path / ".mcp.json"
    original = '{"mcpServers":{"paper-context-memory":{"command":"my-custom-python"}}}'
    target.write_text(original, encoding="utf-8")
    with pytest.raises(SystemExit):
        run_generator(monkeypatch, tmp_path)
    assert target.read_text(encoding="utf-8") == original


def test_codex_windows_and_unix_configs_parse_without_path_escaping():
    tomllib = pytest.importorskip("tomllib", reason="TOML parser is built into Python 3.11+")
    for root, python in (
        (PureWindowsPath("C:/Paper Context/论文"), PureWindowsPath("C:/Paper Context/论文/.venv/Scripts/python.exe")),
        (PurePosixPath("/home/user/Paper Context/论文"), PurePosixPath("/home/user/Paper Context/论文/.venv/bin/python")),
    ):
        config = tomllib.loads(configure_mcp.codex_config(root, python))["mcp_servers"]
        assert config["paper_context_rag"]["command"] == python.as_posix()
        assert config["paper_context_memory"]["args"] == [(root / "memory_mcp_server.py").as_posix()]
        assert config["paper_context_rag"]["startup_timeout_sec"] == 300


def test_offline_smoke_blocks_real_credentials_and_redis(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("SQLALCHEMY_DATABASE_URL", "mysql+pymysql://test:test@localhost/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/9")
    environment = smoke_mcp.isolated_env(tmp_path)
    assert environment["PAPER_CONTEXT_SKIP_DOTENV"] == "1"
    assert "EMBEDDING_API_KEY" not in environment and "OPENAI_API_KEY" not in environment
    assert environment["REDIS_URL"] == ""
    assert environment["SQLALCHEMY_DATABASE_URL"] == f"sqlite:///{(tmp_path / 'memory.db').as_posix()}"
    for setting in ("DB_DIR", "STORE_DIR", "PAPERS_DIR", "RAG_FTS_PATH"):
        assert Path(environment[setting]).is_relative_to(tmp_path)
