"""Hunk-exact git commits for several writers sharing one worktree.

Several agent sessions edit the same working tree and commit locally. Nothing here ever runs `git add` or
`git commit`, and nothing here pushes. Instead, each path's edit (`before -> after`, where `before` is the
session's first-edit snapshot) is replayed onto HEAD's copy of the file. The tree is built in a private index,
and HEAD moves with `update-ref <new> <old>`, a compare-and-swap that is retried once if another session moved
HEAD in between. If any path cannot be replayed cleanly, the whole commit is skipped and a reason is given.
There is never a partial commit, and never another session's hunks.

Nothing here raises into its caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

SNAPSHOT_DIR = "snapshots"
SNAPSHOT_INDEX = "index.json"
SNAPSHOT_MAX_BYTES = 5 * 1024 * 1024
SNAPSHOT_EXCLUDED_PARTS = frozenset({"node_modules", ".venv", ".git", "__pycache__"})

Content = bytes | str | None
"""A file's content: bytes or text, or `None` for a file that does not exist (before: new; after: deleted)."""

Identity = tuple[str, str]
"""`(name, email)` used as both author and committer of a commit."""


# ── git plumbing ─────────────────────────────────────────────────────────────────────────────────────────────────


def _git_out(root: Path, *args: str, stdin: bytes | None = None, env: dict | None = None) -> tuple[int, bytes]:
    try:
        done = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false", *args], cwd=root,
                              input=stdin, capture_output=True, timeout=60, check=False, env=env)
        return done.returncode, done.stdout if done.returncode == 0 else done.stderr
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error).encode()


def _same_dir(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _repo_problem(root: Path) -> str | None:
    """Why `root` cannot take a commit: no repository, no HEAD, or a repository whose top is somewhere else.

    The last case matters: a folder nested inside another repository must never commit into that repository."""
    code, top = _git_out(root, "rev-parse", "--show-toplevel")
    if code:
        return "no HEAD: not a git repository with a commit"
    top_text = top.decode(errors="replace").strip()
    if not _same_dir(top_text, root):
        return f"no HEAD: {root} is not the top of its git repository ({top_text})"
    code, _ = _git_out(root, "rev-parse", "--verify", "HEAD")
    if code:
        return "no HEAD: not a git repository with a commit"
    return None


def _bytes(value: Content) -> bytes | None:
    if value is None:
        return None
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


def _lf(value: bytes) -> bytes:
    return value.replace(b"\r\n", b"\n")


def _merged_content(root: Path, rel: str, at_head: bytes, before: bytes, after: bytes) -> tuple[bytes | None, str | None]:
    """HEAD's copy with only `before -> after` applied, or `(None, reason)` when it does not apply cleanly."""
    head_n, before_n, after_n = _lf(at_head), _lf(before), _lf(after)
    if head_n == before_n:
        return after, None
    if head_n == after_n:
        # HEAD already carries this change (a parallel save of the same edit won, a retry after losing the
        # compare-and-swap, or a save killed after moving HEAD): keep HEAD's bytes, nothing to commit.
        return at_head, None
    if after_n.startswith(before_n) and before_n:
        added = after_n[len(before_n):]
        if head_n.startswith(before_n) and added.startswith(head_n[len(before_n):]):
            # HEAD already holds the start of this append: only the rest goes in, never a second copy.
            return after_n, None
        # A pure append (a log line, a new entry at the end) goes at the end of HEAD's copy. merge-file would
        # call it a conflict whenever another session left uncommitted lines at the same end.
        base = head_n if not head_n or head_n.endswith(b"\n") else head_n + b"\n"
        return base + after_n[len(before_n):], None
    with tempfile.TemporaryDirectory() as scratch:
        parts = {name: Path(scratch) / name for name in ("head", "before", "after")}
        for name, data in (("head", head_n), ("before", before_n), ("after", after_n)):
            parts[name].write_bytes(data)
        code, merged = _git_out(root, "merge-file", "-p", str(parts["head"]), str(parts["before"]), str(parts["after"]))
    if code:
        return None, f"this edit does not apply cleanly to HEAD:{rel}; commit it by hand"
    return merged, None


def _identity_env(identity: Identity | None) -> dict | None:
    if not identity:
        return None
    name, email = identity
    return {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def commit_paths(root: Path, changes: Sequence[Mapping[str, object]], message: str, *,
                 identity: Identity | None = None) -> dict:
    """Commit every `{"path", "before", "after"}` of `changes` in ONE commit on top of HEAD, and nothing else.

    `before`/`after` are bytes or text, `None` meaning the file is absent. Returns `{"commit", "paths"}` (plus
    `warnings`) or `{"skipped": reason}`. Never raises. All or nothing: if one path does not replay cleanly, the
    commit is skipped and HEAD does not move."""
    try:
        return _commit_paths(Path(root), changes, message, identity)
    except Exception as error:  # noqa: BLE001 — bookkeeping never fails the write it follows
        return {"skipped": f"commit failed: {type(error).__name__}: {error}"}


def _commit_paths(root: Path, changes: Sequence[Mapping[str, object]], message: str, identity: Identity | None) -> dict:
    if not changes:
        return {"skipped": "nothing to commit: no path changed"}
    problem = _repo_problem(root)
    if problem:
        return {"skipped": problem}
    items = [(str(item["path"]).replace("\\", "/"), _bytes(item.get("before")), _bytes(item.get("after")))  # type: ignore[arg-type]
             for item in changes]
    for attempt in (1, 2):
        code, head = _git_out(root, "rev-parse", "--verify", "HEAD")
        if code:
            return {"skipped": "no HEAD: not a git repository with a commit"}
        head = head.decode().strip()
        planned: list[tuple[str, str | None, str, str | None]] = []  # rel, old_blob, mode, new_blob (None = remove)
        for rel, before, after in items:
            code, out = _git_out(root, "ls-tree", head, "--", rel)
            fields = out.split() if not code and out.strip() else []
            old_blob, mode = (fields[2].decode(), fields[0].decode()) if len(fields) >= 3 else (None, "100644")
            if after is None:
                if old_blob is None:
                    continue  # absent at HEAD and absent now: nothing of ours
                planned.append((rel, old_blob, mode, None))
                continue
            if old_blob is None:
                content = after  # a file git has never seen: the whole file is this change
            else:
                code, raw = _git_out(root, "cat-file", "blob", old_blob)
                if code:
                    return {"skipped": f"cannot read HEAD:{rel}"}
                content, reason = _merged_content(root, rel, raw, before or b"", after)
                if content is None:
                    return {"skipped": reason}
            code, blob = _git_out(root, "hash-object", "-w", f"--path={rel}", "--stdin", stdin=content)
            if code:
                return {"skipped": f"hash-object failed: {blob.decode(errors='replace').strip()}"}
            blob_id = blob.decode().strip()
            if blob_id != old_blob:
                planned.append((rel, old_blob, mode, blob_id))
        if not planned:
            return {"skipped": "nothing to commit: HEAD already carries this change"}
        with tempfile.TemporaryDirectory() as scratch:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
            code, out = _git_out(root, "read-tree", head, env=env)
            if code:
                return {"skipped": f"git read-tree failed: {out.decode(errors='replace').strip()}"}
            for rel, _old, mode, blob_id in planned:
                step = (("update-index", "--force-remove", "--", rel) if blob_id is None
                        else ("update-index", "--add", "--cacheinfo", f"{mode},{blob_id},{rel}"))
                code, out = _git_out(root, *step, env=env)
                if code:
                    return {"skipped": f"git update-index failed: {out.decode(errors='replace').strip()}"}
            code, tree = _git_out(root, "write-tree", env=env)
        if code:
            return {"skipped": "write-tree failed"}
        code, commit = _git_out(root, "commit-tree", tree.decode().strip(), "-p", head, stdin=message.encode("utf-8"),
                                env=_identity_env(identity))
        if code:
            return {"skipped": f"commit-tree failed: {commit.decode(errors='replace').strip()}"}
        commit = commit.decode().strip()
        reflog = f"agent-autocommit: {', '.join(rel for rel, *_ in planned)}"[:200]
        code, out = _git_out(root, "update-ref", "-m", reflog, "HEAD", commit, head)
        if code == 0:
            break
        if attempt == 2:
            return {"skipped": "HEAD kept moving under the commit; commit it by hand"}
    result: dict = {"commit": commit[:12], "paths": [rel for rel, *_ in planned], "attempts": attempt}
    warnings: list[str] = []
    for rel, old_blob, mode, blob_id in planned:
        code, staged = _git_out(root, "ls-files", "-s", "--", rel)
        staged_blob = staged.split()[1].decode() if not code and staged.strip() else None
        if staged_blob == old_blob:  # the shared index held HEAD's copy (or nothing): point it at ours
            step = (("update-index", "--force-remove", "--", rel) if blob_id is None
                    else ("update-index", "--add", "--cacheinfo", f"{mode},{blob_id},{rel}"))
            code, out = _git_out(root, *step)
            if code:
                warnings.append(f"index not updated ({out.decode(errors='replace').strip()}); `git status` shows "
                                f"{rel} as a staged revert until `git reset -q -- {rel}`")
        else:
            warnings.append(f"{rel} was staged by someone else; their staged copy was left alone")
    if warnings:
        result["warnings"] = warnings
    return result


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


# ── first-edit snapshots ─────────────────────────────────────────────────────────────────────────────────────────


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _older_than(stamp: str, cutoff: datetime) -> bool:
    """True when the ISO `stamp` is before `cutoff`; an unreadable stamp is never old (it keeps counting)."""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when < cutoff


def last_commit_at(root: Path, rel: str) -> datetime | None:
    """Committer time (UTC) of the newest commit that touched `rel`, or None (untracked, no git, any error)."""
    try:
        done = subprocess.run(["git", "-C", str(root), "-c", "core.fsmonitor=false", "log", "-1", "--format=%cI", "--", rel],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (done.stdout or "").strip() if done.returncode == 0 else ""
    try:
        return datetime.fromisoformat(text).astimezone(UTC) if text else None
    except ValueError:
        return None


def _saved_in_history(committed: datetime | None, last: str) -> bool:
    """True when a commit of the path landed in a later second than the sibling's last edit, so that edit is in history.

    git keeps whole seconds, so a commit in the same second as the edit could be on either side of it. That case
    still counts as unsaved, which is the safe side: a refusal, never a mix."""
    if committed is None:
        return False
    try:
        edited = datetime.fromisoformat(last).astimezone(UTC).replace(microsecond=0)
    except ValueError:
        return False
    return committed > edited


def other_sessions_editing(session_dir: Path, rel: str, since: str, *, root: Path | None = None,
                           stale_after_hours: float | None = None) -> list[str]:
    """Sibling sessions whose snapshot index saw an edit of `rel` after `since` that they have not committed since.

    Their hunks landed after this session's snapshot, so `snapshot -> worktree` would carry them too, and the
    caller must refuse rather than mix work. This reads only the sibling snapshot indexes (one small JSON per
    session).

    A sibling whose last edit of `rel` is older than the newest commit of `rel` in `root` does not count: the edit
    is in history, whoever saved it (a commit by hand, another tool). Known gap: a commit made by hand that leaves
    a sibling's hunks out clears that sibling too. A commit through this engine cannot, since it refuses first.
    With `stale_after_hours`, a sibling whose last edit is older than that no longer counts, so a dead session
    cannot lock a file forever. `None` keeps every sibling."""
    sessions = Path(session_dir).parent
    cutoff = datetime.now(UTC) - timedelta(hours=stale_after_hours) if stale_after_hours is not None else None
    history: list[datetime | None] = []  # looked up once, only when a sibling would count
    names: list[str] = []
    for index_path in sessions.glob(f"*/{SNAPSHOT_DIR}/{SNAPSHOT_INDEX}"):
        other = index_path.parent.parent
        if _same_dir(other, session_dir):
            continue
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found = _lookup(index, rel) if isinstance(index, dict) else None
        if found is None:
            continue
        last = str(found[1].get("last_edit_at") or found[1].get("taken_at") or "")
        committed = str(found[1].get("committed_at") or "")
        if cutoff is not None and _older_than(last, cutoff):
            continue
        if last > since and (not committed or committed < last):
            if root is not None:
                if not history:
                    history.append(last_commit_at(root, rel))
                if _saved_in_history(history[0], last):
                    continue
            names.append(other.name)
    return sorted(names)


def _snapshot_dir(session_dir: Path) -> Path:
    return Path(session_dir) / SNAPSHOT_DIR


def _key(rel: str) -> str:
    return rel.replace("\\", "/").strip("/")


def read_snapshot_index(session_dir: Path) -> dict[str, dict]:
    try:
        data = json.loads((_snapshot_dir(session_dir) / SNAPSHOT_INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_index(session_dir: Path, index: Mapping[str, object]) -> None:
    directory = _snapshot_dir(session_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / SNAPSHOT_INDEX
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8", newline="\n")
    os.replace(tmp, target)


def _lookup(index: Mapping[str, dict], rel: str) -> tuple[str, dict] | None:
    key = _key(rel)
    if key in index:
        return key, index[key]
    lowered = key.lower()
    for name, entry in index.items():
        if name.lower() == lowered:
            return name, entry
    return None


def lookup(index: Mapping[str, dict], rel: str) -> tuple[str, dict] | None:
    """Public form of the case-insensitive snapshot-index lookup."""
    return _lookup(index, rel)


def snapshot_excluded(rel: str) -> str | None:
    parts = {part.lower() for part in _key(rel).split("/")}
    hit = parts & SNAPSHOT_EXCLUDED_PARTS
    return f"excluded folder {sorted(hit)[0]}" if hit else None


def take_snapshot(session_dir: Path, rel: str, absolute: Path) -> str:
    """Save the file's bytes (or `absent`) the first time this session edits it. Later edits never overwrite it.

    Returns what happened: `saved` · `absent` · `exists` · `skipped:<why>`."""
    key = _key(rel)
    why = snapshot_excluded(key)
    if why:
        return f"skipped:{why}"
    index = read_snapshot_index(session_dir)
    found = _lookup(index, key)
    if found is not None:
        found[1]["last_edit_at"] = _stamp()  # the content stays; only the last-touched time moves
        _write_index(session_dir, index)
        return "exists"
    path = Path(absolute)
    entry: dict[str, object] = {"taken_at": _stamp(), "last_edit_at": _stamp()}
    if not path.exists():
        entry.update({"absent": True, "file": None, "sha256": None})
    else:
        if not path.is_file():
            return "skipped:not a file"
        size = path.stat().st_size
        if size > SNAPSHOT_MAX_BYTES:
            return f"skipped:{size} bytes is over {SNAPSHOT_MAX_BYTES}"
        data = path.read_bytes()
        name = hashlib.sha256(key.lower().encode("utf-8")).hexdigest()[:32] + ".snap"
        directory = _snapshot_dir(session_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
        entry.update({"absent": False, "file": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    index = read_snapshot_index(session_dir)  # re-read: a parallel edit in the same session may have landed
    if _lookup(index, key) is not None:
        return "exists"
    index[key] = entry
    _write_index(session_dir, index)
    return "absent" if entry["absent"] else "saved"


def load_snapshot(session_dir: Path, rel: str) -> tuple[bool, bytes | None]:
    """`(found, content)`; content None means the file was absent at the first edit."""
    found = _lookup(read_snapshot_index(session_dir), rel)
    if found is None:
        return False, None
    _name, entry = found
    if entry.get("absent"):
        return True, None
    try:
        data = (_snapshot_dir(session_dir) / str(entry.get("file"))).read_bytes()
    except OSError:
        return False, None
    if entry.get("sha256") and hashlib.sha256(data).hexdigest() != entry["sha256"]:
        return False, None
    return True, data


def advance_snapshot(session_dir: Path, rel: str, content: bytes | None) -> None:
    """After this session committed `rel`, its next hunks are measured from what it just committed."""
    index = read_snapshot_index(session_dir)
    found = _lookup(index, rel)
    key = found[0] if found else _key(rel)
    now = _stamp()
    entry: dict[str, object] = {"taken_at": now, "committed_at": now,
                                "last_edit_at": (found[1].get("last_edit_at") if found else None) or now, "advanced": True}
    if content is None:
        entry.update({"absent": True, "file": None, "sha256": None})
    else:
        name = hashlib.sha256(key.lower().encode("utf-8")).hexdigest()[:32] + ".snap"
        directory = _snapshot_dir(session_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(content)
        entry.update({"absent": False, "file": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
    index[key] = entry
    _write_index(session_dir, index)


# ── one session's hunks ──────────────────────────────────────────────────────────────────────────────────────────


def _relative(root: Path, path: str) -> str | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(root) / candidate
    try:
        return Path(os.path.realpath(candidate)).relative_to(Path(os.path.realpath(root))).as_posix()
    except ValueError:
        return None


def commit_session_paths(root: Path, session_dir: Path | None, paths: Sequence[str], message: str, *,
                         identity: Identity | None = None, stale_after_hours: float | None = None) -> dict:
    """Commit this session's hunks in `paths`, measured from its first-edit snapshot, in one commit.

    Without a snapshot, a path goes in only when git has never seen it (untracked and new: the whole file is
    ours). A tracked file that differs from HEAD with no snapshot cannot be told apart from another session's
    work, so the whole commit is refused with `no snapshot for <path>; commit it by hand`. Never raises."""
    try:
        root = Path(root)
        problem = _repo_problem(root)
        if problem:
            return {"skipped": problem}
        changes: list[dict[str, object]] = []
        notes: list[str] = []
        for raw in paths:
            rel = _relative(root, raw)
            if rel is None:
                return {"skipped": f"{raw} is outside the repository"}
            after = _read(root / rel)
            found, before = load_snapshot(session_dir, rel) if session_dir is not None else (False, None)
            if not found:
                code, head = _git_out(root, "rev-parse", "--verify", "HEAD")
                code, out = _git_out(root, "ls-tree", head.decode().strip(), "--", rel)
                in_head = not code and bool(out.strip())
                if not in_head:
                    code, tracked = _git_out(root, "ls-files", "--", rel)
                    if tracked.strip():
                        return {"skipped": f"no snapshot for {rel}: it is staged but not in HEAD; commit it by hand"}
                    if after is None:
                        notes.append(f"{rel}: absent, nothing to commit")
                        continue
                    before = None  # untracked and new: the whole file is this session's
                else:
                    blob = out.split()[2].decode()
                    code, at_head = _git_out(root, "cat-file", "blob", blob)
                    if not code and after is not None and _lf(at_head) == _lf(after):
                        notes.append(f"{rel}: already at HEAD")
                        continue
                    return {"skipped": f"no snapshot for {rel}; commit it by hand"}
            if after == before:
                notes.append(f"{rel}: unchanged since this session's snapshot")
                continue
            if found and session_dir is not None:
                mine = _lookup(read_snapshot_index(session_dir), rel)
                since = str(mine[1].get("taken_at") or "") if mine else ""
                others = other_sessions_editing(session_dir, rel, since, root=root, stale_after_hours=stale_after_hours)
                if others:
                    return {"skipped": f"{', '.join(others)} also edited {rel} after this session's snapshot and has "
                                       "not committed it; its hunks cannot be told from ours; commit it by hand"}
            changes.append({"path": rel, "before": before, "after": after})
        if not changes:
            return {"skipped": "nothing to commit: " + ("; ".join(notes) if notes else "no path changed")}
        result = commit_paths(root, changes, message, identity=identity)
        if "commit" in result and session_dir is not None:
            for change in changes:
                try:
                    advance_snapshot(session_dir, str(change["path"]), change["after"])  # type: ignore[arg-type]
                except OSError as error:
                    result.setdefault("warnings", []).append(f"snapshot not advanced for {change['path']}: {error}")
        if notes:
            result["notes"] = notes
        return result
    except Exception as error:  # noqa: BLE001
        return {"skipped": f"commit failed: {type(error).__name__}: {error}"}
