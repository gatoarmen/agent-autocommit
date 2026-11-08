"""Every agent commit names its card and the card's Why; a done card leaves one entry in WHY.md of each repo it changed."""
import sqlite3
from pathlib import Path

import pytest

from conftest import edit, git, make_repo

BODY = "Why: voice replies took 9 s because the model reloaded on every message.\nDone when: under 2 s.\n"


def make_board(path: Path):
    c = sqlite3.connect(path)
    c.execute("create table tasks (id text, title text, body text, status text, assignee text, result text, completed_at int)")
    c.execute("create table task_runs (id integer primary key, task_id text, summary text, ended_at int)")
    c.execute("insert into tasks values (?,?,?,?,?,?,?)",
              ("t_abcd1234", "Cache voices by mtime", BODY, "running", "builder", None, 1793900000))
    c.commit()
    return c


@pytest.fixture()
def env(plugin, tmp_path, monkeypatch):
    mod, work, _ = plugin
    for name in ("voice", "other"):
        make_repo(work / name, "one\ntwo\nthree\n")
    db = tmp_path / "kanban.db"
    conn = make_board(db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abcd1234")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    return mod, work, conn


def finish(conn, summary="**Voice replies now take 1.4 s; the model stays in memory.**\n\nLong detail."):
    conn.execute("update tasks set status='done'")
    conn.execute("insert into task_runs (task_id, summary, ended_at) values ('t_abcd1234', ?, 1793900100)", (summary,))
    conn.commit()


def test_commit_names_card_and_why(env):
    mod, work, _ = env
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s1")
    res = mod.commit_session("s1", "work the card")
    assert "commit" in res[0], res
    body = git(work / "voice", "log", "-1", "--format=%B")
    assert "Card: t_abcd1234 (default) Cache voices by mtime" in body
    assert "Why: voice replies took 9 s because the model reloaded on every message." in body


def test_no_why_md_while_card_open(env):
    mod, work, _ = env
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s1")
    mod.commit_session("s1", "x")
    assert not (work / "voice" / "WHY.md").exists()


def test_done_card_writes_why_md_in_every_repo_it_changed(env):
    mod, work, conn = env
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s1")
    mod.commit_session("s1", "first turn")
    edit(mod, work / "other" / "a.txt", "one\ntwo\nTHREE\n", "s1")
    finish(conn)
    mod.commit_session("s1", "last turn")  # the turn that called kanban_complete
    for name in ("voice", "other"):
        why = (work / name / "WHY.md").read_text()
        assert "t_abcd1234 · Cache voices by mtime" in why
        assert "- **Why:** voice replies took 9 s" in why
        assert "- **Outcome:** Voice replies now take 1.4 s" in why and "Long detail" not in why
        assert why.startswith(f"# {name}: why it changed")
        assert git(work / name, "status", "--porcelain") == ""  # WHY.md is committed too
        assert "WHY.md" in git(work / name, "log", "-1", "--name-only", "--format=%s")
    voice_shas = git(work / "voice", "log", "--format=%h", "-3").split()
    assert any(s in (work / "voice" / "WHY.md").read_text() for s in voice_shas)


def test_why_md_written_once_newest_on_top(env):
    mod, work, conn = env
    (work / "voice" / "WHY.md").write_text("# voice: why it changed\n\nintro\n\n## 2026-10-01 · t_old00000 · old\n\n- **Why:** before\n")
    git(work / "voice", "add", "WHY.md")
    git(work / "voice", "commit", "-qm", "why")
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s1")
    finish(conn)
    mod.commit_session("s1", "x")
    mod.commit_session("s1", "again")  # nothing new: no second entry
    mod.on_card_completed(task_id="t_abcd1234", board="default")
    text = (work / "voice" / "WHY.md").read_text()
    assert text.count("· t_abcd1234 ·") == 1
    assert text.index("t_abcd1234") < text.index("t_old00000")


def test_why_md_can_be_turned_off(env):
    mod, work, conn = env
    mod.configure(write_why_md=False)
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s1")
    finish(conn)
    mod.commit_session("s1", "x")
    assert not (work / "voice" / "WHY.md").exists()
    assert "Card: t_abcd1234" in git(work / "voice", "log", "-1", "--format=%B")  # card lines stay


def test_completed_hook_covers_card_closed_without_edits_that_turn(env):
    mod, work, conn = env
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s1")
    mod.commit_session("s1", "x")
    finish(conn)
    mod.on_card_completed(task_id="t_abcd1234", board="default")
    assert "t_abcd1234" in (work / "voice" / "WHY.md").read_text()


def test_chat_turn_naming_one_card_gets_card_line(env, monkeypatch):
    mod, work, _ = env
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s2")
    mod.commit_session("s2", "please fix t_abcd1234")
    assert "Card: t_abcd1234" in git(work / "voice", "log", "-1", "--format=%B")


def test_no_card_no_lines(env, monkeypatch):
    mod, work, _ = env
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s3")
    mod.commit_session("s3", "loose change")
    body = git(work / "voice", "log", "-1", "--format=%B")
    assert "Card:" not in body and "Why:" not in body
    assert not (work / "voice" / "WHY.md").exists()


def test_board_unreadable_never_blocks_commit(env, monkeypatch, tmp_path):
    mod, work, _ = env
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "missing" / "kanban.db"))
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s4")
    res = mod.commit_session("s4", "x")
    assert "commit" in res[0], res
    assert "Card: t_abcd1234" in git(work / "voice", "log", "-1", "--format=%B")


def add_card(conn, task_id, assignee, title="Another", why="another reason"):
    conn.execute("insert into tasks values (?,?,?,?,?,?,?)", (task_id, title, f"Why: {why}\n", "running", assignee, None, None))
    conn.commit()


def test_meeting_turn_naming_several_cards_picks_the_one_assigned_to_me(env, monkeypatch):
    mod, work, conn = env
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    add_card(conn, "t_11112222", "default", "Meeting handoff")
    add_card(conn, "t_33334444", "librarian", "Librarian task")
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s5")
    mod.commit_session("s5", "agenda: @builder t_abcd1234, @librarian t_33334444, handoff t_11112222")
    assert "Card: t_abcd1234" in git(work / "voice", "log", "-1", "--format=%B")


def test_meeting_turn_two_cards_mine_says_nothing(env, monkeypatch):
    mod, work, conn = env
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    add_card(conn, "t_55556666", "builder")
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s6")
    mod.commit_session("s6", "t_abcd1234 and t_55556666")
    assert "Card:" not in git(work / "voice", "log", "-1", "--format=%B")


def test_hand_commit_in_terminal_is_adopted_by_the_card_and_reaches_why_md(env):
    mod, work, conn = env
    r = work / "voice"
    mod._on_pre_tool_call("terminal", {"command": "git commit", "workdir": str(r)}, session_id="s7", tool_call_id="h1")
    (r / "a.txt").write_text("one\nTWO\nthree\n")
    git(r, "commit", "-qam", "by hand")
    sha = git(r, "rev-parse", "--short=9", "HEAD").strip()
    mod._on_post_tool_call("terminal", session_id="s7", tool_call_id="h1")
    res = mod.commit_session("s7", "x")
    assert any(row.get("adopted") == [sha] for row in res), res
    assert any("author" in row.get("warning", "") for row in res), res  # t@t is not builder@hermes.local
    finish(conn)
    mod.on_card_completed(task_id="t_abcd1234", board="default")
    why = (r / "WHY.md").read_text()
    assert "t_abcd1234" in why and sha in why


def test_hand_commit_without_card_is_not_adopted(env, monkeypatch):
    mod, work, _ = env
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    r = work / "voice"
    mod._on_pre_tool_call("terminal", {"command": "git commit", "workdir": str(r)}, session_id="s8", tool_call_id="h2")
    (r / "a.txt").write_text("one\nTWO\nthree\n")
    git(r, "commit", "-qam", "by hand")
    mod._on_post_tool_call("terminal", session_id="s8", tool_call_id="h2")
    assert mod.commit_session("s8", "loose") == []
    cards = mod._state_root() / "cards"
    assert not cards.exists() or not list(cards.glob("*.json"))


def test_why_md_committed_in_second_repo_that_already_has_one(env):
    # one WHY.md snapshot per card used to be shared by every repo: the second repo's merge then conflicted
    mod, work, conn = env
    (work / "other" / "WHY.md").write_text("# other: why it changed\n\n## 2026-10-01 · t_old00000 · old\n")
    git(work / "other", "add", "WHY.md")
    git(work / "other", "commit", "-qm", "why")
    edit(mod, work / "voice" / "a.txt", "one\nTWO\nthree\n", "s1")
    edit(mod, work / "other" / "a.txt", "one\ntwo\nTHREE\n", "s1")
    finish(conn)
    mod.commit_session("s1", "x")
    for name in ("voice", "other"):
        assert git(work / name, "status", "--porcelain") == "", name
        assert "· t_abcd1234 ·" in git(work / name, "show", "HEAD:WHY.md"), name
