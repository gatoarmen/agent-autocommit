"""Repos whose own config defines content filters (clean/process, LFS) are never touched: ADR-005."""
import os
import subprocess
from pathlib import Path

from agent_autocommit import commit_engine as engine
from conftest import edit, git, make_repo


def _filtered_repo(work: Path, marker: Path, key: str = "filter.evil.clean") -> Path:
    """A committed repo whose a.txt goes through a filter that leaves `marker` behind if it ever runs."""
    repo = make_repo(work / "tool", "ab\n")
    (repo / ".gitattributes").write_text("*.txt filter=evil\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-qm", "attrs")
    git(repo, "config", key, f"touch {marker}; cat")
    return repo


def _make_racily_clean(repo: Path, rel: str, same_size: str) -> None:
    """Change `rel` without changing its size and give it the index's exact mtime: git must re-hash it."""
    path = repo / rel
    assert len(same_size) == path.stat().st_size
    path.write_text(same_size)
    index = (repo / ".git" / "index").stat()
    os.utime(path, ns=(index.st_atime_ns, index.st_mtime_ns))


def test_racily_clean_status_never_runs_repo_filter(plugin, tmp_path):
    # The race Builder reproduced: `git status` re-hashes a racily clean file through the clean filter.
    mod, work, _ = plugin
    marker = tmp_path / "PWNED"
    repo = _filtered_repo(work, marker)
    _make_racily_clean(repo, "a.txt", "cd\n")
    assert mod.eligible_root(repo / "a.txt") is None
    mod._on_pre_tool_call("terminal", {"command": "true", "workdir": str(repo)}, session_id="s1", tool_call_id="t1")
    mod._on_post_tool_call("terminal", session_id="s1", tool_call_id="t1")
    mod._on_pre_tool_call("write_file", {"path": str(repo / "a.txt")}, session_id="s1", tool_call_id="t2")
    assert mod.commit_session("s1", "turn") == []
    assert not marker.exists()
    # Control: the setup really is racy, plain git status does run the filter.
    subprocess.run(["git", "-c", "core.fsmonitor=false", "status", "--porcelain"], cwd=repo, capture_output=True,
                   check=True)
    assert marker.exists()


def test_lfs_style_process_filter_skips_repo(plugin, tmp_path):
    mod, work, _ = plugin
    marker = tmp_path / "lfs-ran"
    repo = _filtered_repo(work, marker, "filter.lfs.process")
    git(repo, "config", "filter.lfs.required", "true")
    assert mod.eligible_root(repo) is None
    edit(mod, repo / "a.txt", "changed\n", "s1")
    assert mod.commit_session("s1", "turn") == []
    assert not marker.exists()
    assert git(repo, "log", "--format=%s", "-1").strip() == "attrs"


def test_filter_from_included_config_skips_repo(plugin, tmp_path):
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    extra = tmp_path / "extra.gitconfig"
    extra.write_text('[filter "inc"]\n\tclean = cat\n')
    git(repo, "config", "include.path", str(extra))
    assert mod.eligible_root(repo) is None


def test_filter_in_worktree_config_skips_repo(plugin, tmp_path):
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "filter.wt.clean", "cat")
    assert mod.eligible_root(repo) is None


def test_smudge_only_or_global_filters_do_not_skip(plugin, tmp_path):
    # Only local clean/process keys matter: smudge does not run on status, and global config is the user's own.
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    git(repo, "config", "filter.s.smudge", "cat")
    git(repo, "config", "filter.s.required", "true")
    assert mod.eligible_root(repo) == repo.resolve()
    edit(mod, repo / "a.txt", "changed\n", "s1")
    res = mod.commit_session("s1", "turn")
    assert res and "commit" in res[0], res


def test_engine_refuses_filtered_repo_directly(tmp_path):
    repo = make_repo(tmp_path / "r", "a\n")
    git(repo, "config", "filter.evil.clean", "cat")
    reason = engine._repo_problem(repo)
    assert reason and "filter.evil.clean" in reason
    head = git(repo, "rev-parse", "HEAD")
    res = engine.commit_paths(repo, [{"path": "a.txt", "before": "a\n", "after": "b\n"}], "m")
    assert "skipped" in res and "content filters" in res["skipped"]
    res = engine.commit_session_paths(repo, None, ["a.txt"], "m")
    assert "skipped" in res and "content filters" in res["skipped"]
    assert git(repo, "rev-parse", "HEAD") == head


def test_unreadable_config_fails_closed(tmp_path):
    repo = make_repo(tmp_path / "r", "a\n")
    git(repo, "config", "include.path", str(tmp_path / "loop.gitconfig"))
    (tmp_path / "loop.gitconfig").write_text(f"[include]\n\tpath = {tmp_path / 'loop.gitconfig'}\n")
    assert engine.local_filter_problem(repo)
