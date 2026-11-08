"""Shared fixtures: a temporary root with real git repos, and a fresh copy of the plugin module per test."""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def make_repo(path: Path, content: str = "one\ntwo\nthree\nfour\nfive\nsix\nseven\n") -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@t")
    (path / "a.txt").write_text(content)
    (path / ".gitignore").write_text("*.log\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return path


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    """The plugin module with one root (``tmp_path/work``), private state, and a stub profile."""
    work = tmp_path / "work"
    work.mkdir()
    for var in ("AGENT_AUTOCOMMIT_EXCLUDE", "AGENT_AUTOCOMMIT_LEDGER", "AGENT_AUTOCOMMIT_ALLOW_REMOTE",
                "AGENT_AUTOCOMMIT_EMAIL_DOMAIN", "AGENT_AUTOCOMMIT_WHY_MD", "HERMES_NO_AUTOCOMMIT",
                "HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_AUTOCOMMIT_ROOTS", str(work))
    monkeypatch.setenv("AGENT_AUTOCOMMIT_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    mod = importlib.reload(importlib.import_module("agent_autocommit"))
    mod._config.clear()
    profile = {"name": "builder"}
    monkeypatch.setattr(mod, "_profile", lambda: profile["name"])
    monkeypatch.setattr(mod, "_profile_home", lambda: tmp_path / "profile-home")
    monkeypatch.setattr(mod, "_hermes_root", lambda: tmp_path / "hermes-root")
    return mod, work, profile


def edit(mod, path: Path, new: str, sid: str) -> None:
    mod._on_pre_tool_call("write_file", {"path": str(path)}, session_id=sid, tool_call_id="x")
    path.write_text(new)
