"""The commit engine on its own: append handling, all-or-nothing, compare-and-swap, nested repos."""
from pathlib import Path

from agent_autocommit import commit_engine as engine
from conftest import git, make_repo

BASE = b"".join(b"line %d\n" % n for n in range(1, 6))
ADDED = b"added by subagent\n"


def test_append_already_in_head_is_not_doubled(tmp_path: Path):
    merged, err = engine._merged_content(tmp_path, "doc.md", BASE + ADDED, BASE, BASE + ADDED)
    assert err is None
    assert merged.count(ADDED) == 1


def test_partial_append_in_head_adds_only_the_rest(tmp_path: Path):
    more = ADDED + b"second line\n"
    merged, err = engine._merged_content(tmp_path, "doc.md", BASE + ADDED, BASE, BASE + more)
    assert err is None
    assert merged.replace(b"\r\n", b"\n") == BASE + more


def test_plain_append_still_lands(tmp_path: Path):
    merged, err = engine._merged_content(tmp_path, "doc.md", BASE, BASE, BASE + ADDED)
    assert err is None and merged == BASE + ADDED


def test_commit_paths_is_all_or_nothing(tmp_path: Path):
    repo = make_repo(tmp_path / "r", "a\nb\nc\n")
    head = git(repo, "rev-parse", "HEAD").strip()
    res = engine.commit_paths(repo, [
        {"path": "new.txt", "before": None, "after": "fresh\n"},
        {"path": "a.txt", "before": "x\ny\nz\n", "after": "totally\ndifferent\n"},  # does not replay on HEAD
    ], "msg")
    assert "skipped" in res, res
    assert git(repo, "rev-parse", "HEAD").strip() == head


def test_commit_paths_uses_identity_and_leaves_worktree_alone(tmp_path: Path):
    repo = make_repo(tmp_path / "r", "a\nb\nc\n")
    (repo / "a.txt").write_text("a\nB\nc\n")
    (repo / "unrelated.txt").write_text("not ours\n")
    res = engine.commit_paths(repo, [{"path": "a.txt", "before": "a\nb\nc\n", "after": "a\nB\nc\n"}], "msg",
                              identity=("bot (Hermes)", "bot@example.org"))
    assert res.get("paths") == ["a.txt"], res
    assert git(repo, "log", "-1", "--format=%an <%ae>|%cn").strip() == "bot (Hermes) <bot@example.org>|bot (Hermes)"
    assert "?? unrelated.txt" in git(repo, "status", "--porcelain")
    assert " M a.txt" not in git(repo, "status", "--porcelain")


def test_nested_folder_never_commits_into_outer_repo(tmp_path: Path):
    repo = make_repo(tmp_path / "outer")
    (repo / "sub").mkdir()
    res = engine.commit_paths(repo / "sub", [{"path": "f.txt", "before": None, "after": "x\n"}], "msg")
    assert "not the top" in res.get("skipped", ""), res


def test_no_snapshot_for_dirty_tracked_file_refuses(tmp_path: Path):
    repo = make_repo(tmp_path / "r", "a\n")
    (repo / "a.txt").write_text("someone\n")
    res = engine.commit_session_paths(repo, tmp_path / "s" / "sessions" / "one", ["a.txt"], "msg")
    assert "no snapshot for a.txt" in res.get("skipped", ""), res
