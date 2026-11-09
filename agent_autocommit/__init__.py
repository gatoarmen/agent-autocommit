"""agent-autocommit: every Hermes agent commits its own edits, once per turn, and only its own hunks.

How it works:

* ``pre_tool_call`` on ``write_file``/``patch``: the first time a session touches a file, the plugin saves the
  file's bytes (the session's snapshot). Later edits never overwrite it.
* ``pre_tool_call``/``post_tool_call`` on ``terminal``: the plugin runs a cheap ``git status`` before and after the
  command, in every eligible repo the command names. A file that was clean (== HEAD) before and is changed after
  belongs to this session. So does an untracked file that appeared. A file that was already dirty before the
  command is left alone, because nothing can tell whose change it is.
* ``post_llm_call`` (end of the turn): the plugin commits only this session's hunks, one commit per repo. The
  author is the profile and the message is ``agent(<profile>): <what was asked>``. Commits are local. The plugin
  never pushes.
* ``kanban_task_completed``: when a Kanban card that produced commits is done, each repo it changed gets one entry
  in ``WHY.md`` (why the card existed, what it delivered, which commits). That entry is committed too.

Where it commits: only git repos under one of the configured ``roots``. A repo is skipped when it has a remote
(unless ``allow_remote_repos`` is on), when it overlaps an ``exclude_paths`` entry or a path listed in the
optional ``exclude_ledger``, or when it is the Hermes install itself. A file is skipped when it is gitignored,
has a secret-looking name, is over 5 MB, or sits in ``node_modules``/``.venv``/``__pycache__``.

Off switches: env ``HERMES_NO_AUTOCOMMIT=1``, file ``<hermes root>/agent-autocommit.off`` (all profiles), file
``<profile home>/agent-autocommit.off`` (one profile). Nothing here raises into the agent.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from . import commit_engine as engine

logger = logging.getLogger(__name__)

PLUGIN_ID = "agent-autocommit"
FILE_TOOLS = {"write_file", "patch"}
MAX_BYTES = 5 * 1024 * 1024
SECRET_RE = re.compile(
    r"(^|/)(\.env(\..*)?|.*\.(pem|key|p12|pfx)|id_(rsa|ed25519|ecdsa|dsa)[^/]*|"
    r".*(secret|token|credential|cookie)s?(\.[a-z]+)?|auth\.json|\.netrc|\.pgpass)$", re.I)
WHY_FILE = "WHY.md"
CARD_RE = re.compile(r"\bt_[0-9a-f]{8}\b")

_lock = threading.Lock()
_term_before: dict[str, list[dict]] = {}  # tool_call_id -> [{"root", "status", "head", "dirty"}]


# ── hermes locations (public API only) ───────────────────────────────────────────────────────────────────────────

def _hermes_root() -> Path:
    """The shared Hermes root (``~/.hermes``), the same for every profile."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:  # noqa: BLE001
        return Path(os.path.expanduser("~")) / ".hermes"


def _profile_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001
        return _hermes_root()


def profile_from_home(home: Path) -> str:
    """``<root>/profiles/<name>`` -> ``name``; anything else (the shared root itself) -> ``default``."""
    return home.name if home.parent.name == "profiles" and home.name else "default"


def _profile() -> str:
    """Active profile name, derived from the public ``hermes_constants.get_hermes_home()``."""
    return profile_from_home(_profile_home())


def _hermes_install() -> Path | None:
    """The Hermes code tree; never a commit target, wherever the roots point."""
    try:
        import hermes_constants
        return Path(hermes_constants.__file__).resolve().parent
    except Exception:  # noqa: BLE001
        return None


# ── settings ─────────────────────────────────────────────────────────────────────────────────────────────────────
# Read from plugins.entries.agent-autocommit.settings (config.yaml) at register(); environment variables override
# (useful for tests and one-off runs). Lists in the environment are separated by os.pathsep (":" on Linux/macOS).

DEFAULTS: dict = {
    "roots": [],
    "exclude_paths": [],
    "exclude_ledger": "",
    "exclude_ledger_field": "target",
    "allow_remote_repos": False,
    "author_email_domain": "hermes.local",
    "write_why_md": True,
}
ENV = {
    "roots": "AGENT_AUTOCOMMIT_ROOTS",
    "exclude_paths": "AGENT_AUTOCOMMIT_EXCLUDE",
    "exclude_ledger": "AGENT_AUTOCOMMIT_LEDGER",
    "allow_remote_repos": "AGENT_AUTOCOMMIT_ALLOW_REMOTE",
    "author_email_domain": "AGENT_AUTOCOMMIT_EMAIL_DOMAIN",
    "write_why_md": "AGENT_AUTOCOMMIT_WHY_MD",
}
_config: dict = {}


def configure(**values) -> None:
    """Set settings programmatically (register() and tests). Unknown keys are ignored."""
    _config.update({k: v for k, v in values.items() if k in DEFAULTS})


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def setting(key: str):
    env = ENV.get(key)
    raw = os.environ.get(env) if env else None
    value = raw if raw is not None else _config.get(key, DEFAULTS[key])
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return _truthy(value)
    if isinstance(default, list):
        if isinstance(value, str):
            return [p for p in value.split(os.pathsep) if p.strip()]
        return [str(p) for p in (value or []) if str(p).strip()]
    return str(value or "")


def _state_root() -> Path:
    """Shared across profiles on purpose: two profiles editing one file must see each other's snapshots."""
    return Path(os.environ.get("AGENT_AUTOCOMMIT_STATE") or (_hermes_root() / "cache" / PLUGIN_ID))


def _resolve(path: str | Path) -> Path:
    p = Path(os.path.expanduser(str(path)))
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


def roots() -> list[Path]:
    return [_resolve(r) for r in setting("roots")]


def disabled() -> str | None:
    if os.environ.get("HERMES_NO_AUTOCOMMIT", "").strip() not in ("", "0"):
        return "HERMES_NO_AUTOCOMMIT"
    if (_hermes_root() / f"{PLUGIN_ID}.off").exists():
        return "global off-file"
    if (_profile_home() / f"{PLUGIN_ID}.off").exists():
        return "profile off-file"
    if not setting("roots"):
        return "no roots configured"
    return None


# ── which repos ──────────────────────────────────────────────────────────────────────────────────────────────────

_ledger_cache: tuple[str, str, float, list[str]] = ("", "", 0.0, [])  # (path, field, mtime, paths)


def _ledger_paths() -> list[str]:
    """Absolute paths named in the optional exclude ledger (JSON Lines; one field per row, default ``target``)."""
    global _ledger_cache
    ledger = setting("exclude_ledger")
    if not ledger:
        return []
    path = _resolve(ledger)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    field = setting("exclude_ledger_field") or "target"
    if _ledger_cache[:3] == (str(path), field, mtime):
        return _ledger_cache[3]
    found: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            target = os.path.expanduser(str(row.get(field) or "")) if isinstance(row, dict) else ""
            if os.path.isabs(target):  # blank/relative rows would resolve against the cwd: ignore them
                found.append(target)
    except OSError:
        pass
    _ledger_cache = (str(path), field, mtime, found)
    return found


def _excludes() -> list[str]:
    """Exclusion entries: absolute paths, or glob patterns matched against absolute paths."""
    out: list[str] = []
    for raw in setting("exclude_paths") + _ledger_paths():
        raw = os.path.expanduser(raw)
        if any(ch in raw for ch in "*?["):
            out.append(raw)  # a glob, matched against absolute paths (use */name/* for "anywhere")
        elif os.path.isabs(raw):
            out.append(str(_resolve(raw)))
        else:
            out += [str(r / raw) for r in roots()]  # relative entries apply under every root
    return out


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def excluded(path: Path, *, repo: bool = False) -> str | None:
    """The exclusion entry that covers ``path``. For a repo, a path entry inside it also counts (a deployed subfolder
    makes the whole repo off limits). A glob only excludes what it matches: the repo itself, a parent of it, or (via
    ``blocked_file``) the files under a matching folder; the rest of the repo still commits."""
    for entry in _excludes():
        if any(ch in entry for ch in "*?["):
            if fnmatch.fnmatch(str(path), entry) or any(fnmatch.fnmatch(str(p), entry) for p in path.parents):
                return entry
            continue
        e = Path(entry)
        if _within(path, e) or (repo and _within(e, path)):
            return entry
    return None


def _git(root: Path | str, *args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false", *args], cwd=str(root), capture_output=True,
                              text=True, timeout=30, check=False)
        return done.returncode, done.stdout if done.returncode == 0 else done.stderr
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)


def _root_for(p: Path) -> Path | None:
    for root in roots():
        if _within(p, root):
            return root
    return None


def eligible_root(path: Path | str) -> Path | None:
    """The git repo that owns ``path`` (a file or a dir) and may take commits, or None."""
    try:
        p = Path(path).expanduser()
        p = p.resolve() if p.exists() else p.parent.resolve() / p.name
    except OSError:
        return None
    base = _root_for(p)
    if base is None or excluded(p):
        return None
    probe = p if p.is_dir() else p.parent
    while not probe.exists() and probe != base:
        probe = probe.parent
    code, top = _git(probe, "rev-parse", "--show-toplevel")
    if code:
        return None
    repo = Path(top.strip()).resolve()
    if not _within(repo, base):
        return None  # a configured root nested inside a bigger repo: never commit into the outer repo
    install = _hermes_install()
    if install is not None and (_within(install, repo) or _within(repo, install)):
        return None
    if excluded(repo, repo=True):
        return None
    if not setting("allow_remote_repos"):
        code, remotes = _git(repo, "remote")
        if code or remotes.strip():
            return None  # a clone or a published repo: opt in with allow_remote_repos
    code, _ = _git(repo, "rev-parse", "--verify", "HEAD")
    return None if code else repo


def blocked_file(root: Path, rel: str) -> str | None:
    if SECRET_RE.search(rel):
        return "secret-looking name"
    full = root / rel
    try:
        if full.is_file() and full.stat().st_size > MAX_BYTES:
            return "over 5 MB"
    except OSError:
        pass
    if engine.snapshot_excluded(rel):
        return "excluded folder"
    if excluded(_resolve(full)):
        return "excluded path"
    code, _ = _git(root, "check-ignore", "-q", "--", rel)
    return "gitignored" if code == 0 else None


# ── per-session state ────────────────────────────────────────────────────────────────────────────────────────────

def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text or "nosession").strip("_") or "root"


def _session_dir(root: Path, session_id: str) -> Path:
    return _state_root() / "repos" / _safe(str(root)) / "sessions" / _safe(session_id)


def _touched_file(session_id: str) -> Path:
    return _state_root() / "touched" / f"{_safe(session_id)}.json"


def _hand_file(session_id: str) -> Path:
    return _state_root() / "touched" / f"{_safe(session_id)}.hand.json"


def _json_add(path: Path, root: Path, items: list[str]) -> None:
    with _lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        current = data.setdefault(str(root), [])
        current += [i for i in items if i not in current]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


def _json_pop(path: Path) -> dict[str, list[str]]:
    with _lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        try:
            path.unlink()
        except OSError:
            pass
    return data if isinstance(data, dict) else {}


def _add_touched(session_id: str, root: Path, rel: str) -> None:
    _json_add(_touched_file(session_id), root, [rel])


def _cwd() -> Path:
    return Path(os.environ.get("TERMINAL_CWD") or os.getcwd())


def _abs(raw: str, base: Path | None = None) -> Path:
    p = Path(os.path.expanduser(raw))
    return p if p.is_absolute() else (base or _cwd()) / p


def _command_paths(command, base: Path) -> list[Path]:
    """Path-looking tokens of a shell command (absolute, ~, or containing a slash), resolved against ``base``."""
    if not isinstance(command, str) or not command:
        return []
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    out: list[Path] = []
    for tok in tokens:
        tok = tok.strip("'\"();|&<>")
        if "=" in tok and not tok.startswith(("/", "~")):
            tok = tok.split("=", 1)[1]
        if not tok or tok.startswith("-") or not ("/" in tok or tok.startswith("~")):
            continue
        out.append(_abs(tok, base))
        if len(out) >= 20:
            break
    return out


def _status(root: Path) -> dict[str, str]:
    code, out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if code:
        return {}
    result: dict[str, str] = {}
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        item = entries[i]
        i += 1
        if len(item) < 4:
            continue
        code2, rel = item[:2], item[3:]
        if code2[0] in "RC":
            i += 1  # skip the rename source
        result[rel] = code2
    return result


def _hash(root: Path, rel: str) -> str | None:
    code, out = _git(root, "hash-object", "--no-filters", "--", rel)
    return out.strip() if not code else None


# ── hooks ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _on_pre_tool_call(tool_name: str = "", args: dict | None = None, session_id: str = "", tool_call_id: str = "", **_kw):
    try:
        if disabled() or not isinstance(args, dict):
            return None
        if tool_name in FILE_TOOLS and isinstance(args.get("path"), str):
            target = _abs(args["path"])
            root = eligible_root(target)
            if root is None:
                return None
            rel = os.path.relpath(os.path.realpath(target) if target.exists() else target, root).replace(os.sep, "/")
            if rel.startswith("..") or blocked_file(root, rel) == "secret-looking name":
                return None
            engine.take_snapshot(_session_dir(root, session_id), rel, root / rel)
            _add_touched(session_id, root, rel)
        elif tool_name == "terminal" and tool_call_id and not args.get("background"):
            base = _abs(args["workdir"]) if isinstance(args.get("workdir"), str) and args["workdir"] else _cwd()
            found = [eligible_root(base)] + [eligible_root(c) for c in _command_paths(args.get("command"), base)]
            snaps = []
            for root in sorted({r for r in found if r}):
                status = _status(root)
                code, head = _git(root, "rev-parse", "HEAD")
                snaps.append({"root": str(root), "status": status, "head": head.strip() if code == 0 else "",
                              "dirty": {r: _hash(root, r) for r in status} if len(status) < 2000 else {}})
            if snaps:
                with _lock:
                    _term_before[tool_call_id] = snaps
    except Exception as error:  # noqa: BLE001 — bookkeeping never blocks a tool
        logger.debug("agent-autocommit pre_tool_call: %s", error)
    return None


def _on_post_tool_call(tool_name: str = "", session_id: str = "", tool_call_id: str = "", **_kw):
    if tool_name != "terminal" or not tool_call_id:
        return None
    try:
        with _lock:
            snaps = _term_before.pop(tool_call_id, None)
        if not snaps or disabled():
            return None
        for before in snaps:
            _diff_after_terminal(before, session_id)
    except Exception as error:  # noqa: BLE001
        logger.debug("agent-autocommit post_tool_call: %s", error)
    return None


def _diff_after_terminal(before: dict, session_id: str) -> None:
    root = Path(before["root"])
    _note_hand_commits(root, before.get("head", ""), session_id)
    after = _status(root)
    sdir = _session_dir(root, session_id)
    mine = engine.read_snapshot_index(sdir)
    for rel, code2 in after.items():
        prev = before["status"].get(rel)
        if prev is None:
            # clean before, dirty now: HEAD (or nothing) is this session's "before"
            if blocked_file(root, rel):
                continue
            if code2 == "??":
                engine.take_snapshot(sdir, rel, root / "__agent_autocommit_absent__" / rel)  # records "absent"
            else:
                _snapshot_from_head(sdir, root, rel)
            _add_touched(session_id, root, rel)
        elif engine.lookup(mine, rel) is not None and before["dirty"].get(rel) != _hash(root, rel):
            _add_touched(session_id, root, rel)  # already ours, changed again


def _note_hand_commits(root: Path, head_before: str, session_id: str) -> None:
    """A terminal command that made commits itself (``git commit`` by hand): remember them for this turn's card."""
    if not head_before:
        return
    code, head = _git(root, "rev-parse", "HEAD")
    if code or head.strip() == head_before:
        return
    code, out = _git(root, "rev-list", "--reverse", f"{head_before}..HEAD")
    if not code:
        _json_add(_hand_file(session_id), root, out.split())


def _snapshot_from_head(sdir: Path, root: Path, rel: str) -> None:
    """Snapshot = HEAD's copy (the file was clean before the command)."""
    if engine.lookup(engine.read_snapshot_index(sdir), rel) is not None:
        return
    code, out = engine._git_out(root, "show", f"HEAD:{rel}")
    tmp = _state_root() / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    if code:
        engine.take_snapshot(sdir, rel, tmp / "__absent__" / rel)
        return
    holder = tmp / f"head-{os.getpid()}-{threading.get_ident()}"
    holder.write_bytes(out)
    try:
        engine.take_snapshot(sdir, rel, holder)
    finally:
        try:
            holder.unlink()
        except OSError:
            pass


def _subject(user_message) -> str:
    text = user_message if isinstance(user_message, str) else ""
    text = re.sub(r"\s+", " ", re.sub(r"\[[^\]]{0,200}\]", "", text)).strip()
    text = re.sub(r"^Message from \S+ \S+ \(@[\w-]+\):\s*", "", text)  # Hermes agent-to-agent prefix
    return (text[:68] + "…") if len(text) > 69 else (text or "edits")


def _identity(profile: str) -> engine.Identity:
    name = f"{profile} (Hermes)"
    return name, f"{profile}@{setting('author_email_domain') or 'hermes.local'}"


# ── the card behind the change (why) ─────────────────────────────────────────────────────────────────────────────
# git log says WHAT changed; the Kanban card says WHY. A commit made for a card carries `Card:` and `Why:` lines,
# and when the card is done each repo it changed gets one entry in WHY.md.

def _kanban_home() -> Path:
    return Path(os.environ.get("HERMES_KANBAN_HOME") or _hermes_root())


def _board_dbs() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    pin = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pin:
        out.append((os.environ.get("HERMES_KANBAN_BOARD", "").strip() or "default", Path(pin).expanduser()))
    home = _kanban_home()
    out.append(("default", home / "kanban.db"))
    for p in sorted((home / "kanban" / "boards").glob("*/kanban.db")):
        out.append((p.parent.name, p))
    seen: set[str] = set()
    uniq = []
    for board, path in out:
        if str(path) not in seen:
            seen.add(str(path))
            uniq.append((board, path))
    return uniq


def _first_line(text: str, limit: int) -> str:
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*[*_]+|[*_]+\s*$", "", line.strip()).strip()
        if line:
            return (line[: limit - 1] + "…") if len(line) > limit else line
    return ""


def _read_card(task_id: str, board_hint: str = "") -> dict:
    """``{id, board, title, why, status, summary, completed_at, assignee}``, read-only; partial when unreadable."""
    card = {"id": task_id, "board": board_hint or os.environ.get("HERMES_KANBAN_BOARD", "").strip() or "default"}
    for board, path in _board_dbs():
        if board_hint and board != board_hint and str(path) != os.environ.get("HERMES_KANBAN_DB", ""):
            continue
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            try:
                row = conn.execute("select title, body, status, result, completed_at, assignee from tasks where id=?",
                                   (task_id,)).fetchone()
                if not row:
                    continue
                run = conn.execute("select summary from task_runs where task_id=? and coalesce(summary,'')!='' "
                                   "order by id desc limit 1", (task_id,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        title, body, status, result, completed_at, assignee = row
        m = re.search(r"^\s*\**\s*Why\s*:?\**\s*:?\s*(.+)$", body or "", re.M | re.I)
        card.update(board=board, title=(title or "").strip(), status=status,
                    why=_first_line(m.group(1) if m else "", 220),
                    summary=_first_line((run[0] if run else "") or result or "", 260), completed_at=completed_at,
                    assignee=(assignee or "").strip())
        return card
    return card


def _card_for_turn(user_message) -> dict | None:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if task_id:
        return _read_card(task_id)
    ids = set(CARD_RE.findall(user_message if isinstance(user_message, str) else ""))
    cards = [c for c in (_read_card(i) for i in sorted(ids)) if c.get("title")]  # real cards only
    if len(cards) == 1:
        return cards[0]
    mine = [c for c in cards if c.get("assignee") == _profile()]  # a meeting agenda names everyone's cards
    return mine[0] if len(mine) == 1 else None


def _card_lines(card: dict | None) -> str:
    if not card:
        return ""
    lines = f"Card: {card['id']} ({card['board']}) {card.get('title', '')}".rstrip() + "\n"
    if card.get("why"):
        lines += f"Why: {card['why']}\n"
    return lines + "\n"


def _card_state(task_id: str) -> Path:
    return _state_root() / "cards" / f"{_safe(task_id)}.json"


def _remember_commit(card: dict, root: Path, sha: str) -> None:
    path = _card_state(card["id"])
    with _lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {"board": card["board"], "repos": {}, "written": []}
        shas = data["repos"].setdefault(str(root), [])
        if sha and sha[:9] not in shas:
            shas.append(sha[:9])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


def _why_entry(card: dict, shas: list[str]) -> str:
    stamp = card["completed_at"] if card.get("completed_at") else None
    when = time.strftime("%Y-%m-%d", time.localtime(stamp) if stamp else time.localtime())
    out = f"## {when} · {card['id']} · {card.get('title') or '(untitled)'}\n\n"
    if card.get("why"):
        out += f"- **Why:** {card['why']}\n"
    if card.get("summary"):
        out += f"- **Outcome:** {card['summary']}\n"
    if shas:
        out += f"- **Commits:** {', '.join(shas)}\n"
    out += f"- **Card:** `hermes kanban --board {card['board']} show {card['id']}`\n\n"
    return out


def _insert_entry(text: str, repo_name: str, entry: str) -> str:
    if not text.strip():
        text = (f"# {repo_name}: why it changed\n\nOne entry per finished Kanban card that changed this repo, newest "
                "first. Written by agent-autocommit when the card closes; the full detail lives on the card.\n\n")
    idx = text.find("\n## ")
    if idx < 0:
        return text + ("" if text.endswith("\n") else "\n") + entry
    return text[: idx + 1] + entry + text[idx + 1:]


def _log(row: dict) -> None:
    try:
        path = _state_root() / "commits.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def write_why(task_id: str, board: str = "") -> list[dict]:
    """When the card is done: one committed WHY.md entry in every repo this card's commits touched. Idempotent."""
    if not setting("write_why_md"):
        return []
    path = _card_state(task_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    card = _read_card(task_id, board_hint=board or data.get("board", ""))
    if card.get("status") != "done":
        return []
    profile = _profile()
    results = []
    for root_s, shas in data.get("repos", {}).items():
        if root_s in data.get("written", []):
            continue
        root = Path(root_s)
        if eligible_root(root) is None:
            continue
        sdir = _state_root() / "why" / _safe(task_id) / _safe(root_s)  # one snapshot per repo: WHY.md differs per repo
        why = root / WHY_FILE
        engine.take_snapshot(sdir, WHY_FILE, why)
        try:
            old = why.read_text(encoding="utf-8") if why.exists() else ""
        except OSError:
            continue
        if f"· {task_id} ·" in old:
            data.setdefault("written", []).append(root_s)
            continue
        why.write_text(_insert_entry(old, root.name, _why_entry(card, shas)), encoding="utf-8")
        msg = (f"agent({profile}): why: {card['id']} {card.get('title', '')}".rstrip()[:90] + "\n\n" + _card_lines(card)
               + f"files: {WHY_FILE}\n\nCommitted by agent-autocommit.\n")
        result = engine.commit_session_paths(root, sdir, [WHY_FILE], msg, identity=_identity(profile))
        row = {"ts": _now(), "profile": profile, "card": task_id, "repo": root_s, **result}
        results.append(row)
        _log(row)
        if result.get("commit"):
            data.setdefault("written", []).append(root_s)
    with _lock:
        path.write_text(json.dumps(data), encoding="utf-8")
    return results


def on_card_completed(task_id: str = "", board: str | None = None, **_kw):
    """``kanban_task_completed``: a card may close in a turn with no edits; WHY.md is still written."""
    try:
        if task_id and not disabled():
            write_why(task_id, board or "")
    except Exception as error:  # noqa: BLE001
        logger.debug("agent-autocommit kanban_task_completed: %s", error)
    return None


def commit_session(session_id: str, user_message: str = "", platform: str = "") -> list[dict]:
    off = disabled()
    touched = _json_pop(_touched_file(session_id))
    hand = _json_pop(_hand_file(session_id))
    if off or not (touched or hand):
        return []
    profile = _profile()
    identity = _identity(profile)
    try:
        card = _card_for_turn(user_message)
    except Exception:  # noqa: BLE001 — the why is a bonus; the commit never waits on the board
        card = None
    results = []
    for root_s, shas in hand.items():  # commits the agent made itself this turn: adopt them for the card
        if not card or not shas:
            continue
        root = Path(root_s)
        for sha in shas:
            _remember_commit(card, root, sha)
        code, emails = _git(root, "log", "--format=%ae", "--no-walk", *shas)
        strangers = sorted({e for e in emails.split() if e != identity[1]}) if code == 0 else []
        row = {"ts": _now(), "profile": profile, "session": session_id, "repo": root_s,
               "card": card["id"], "adopted": [s[:9] for s in shas]}
        if strangers:
            row["warning"] = f"hand commit author {', '.join(strangers)} is not {identity[1]}"
        results.append(row)
        _log(row)
    for root_s, rels in touched.items():
        root = Path(root_s)
        keep = [r for r in rels if not blocked_file(root, r)]
        if not keep:
            continue
        msg = (f"agent({profile}): {_subject(user_message)}\n\n{_card_lines(card)}session: {session_id}\n"
               f"platform: {platform or '-'}\nfiles: {', '.join(keep)}\n\nCommitted by agent-autocommit.\n")
        result = engine.commit_session_paths(root, _session_dir(root, session_id), keep, msg, identity=identity)
        row = {"ts": _now(), "profile": profile, "session": session_id,
               "repo": str(root), **({"card": card["id"]} if card else {}), **result}
        results.append(row)
        _log(row)
        if card and result.get("commit"):
            _remember_commit(card, root, str(result["commit"]))
    if card:
        try:
            write_why(card["id"], card["board"])
        except Exception as error:  # noqa: BLE001
            logger.debug("agent-autocommit write_why: %s", error)
    return results


def _on_post_llm_call(session_id: str = "", user_message="", platform: str = "", **_kw):
    try:
        commit_session(session_id, user_message, platform)
    except Exception as error:  # noqa: BLE001
        logger.debug("agent-autocommit post_llm_call: %s", error)
    return None


def register(ctx) -> None:
    try:
        configure(**{key: ctx.get_config(key, default) for key, default in DEFAULTS.items()})
    except Exception as error:  # noqa: BLE001 — a bad setting never stops the plugin from loading
        logger.warning("agent-autocommit: could not read settings: %s", error)
    if not setting("roots"):
        logger.info("agent-autocommit: no roots configured; set plugins.entries.%s.settings.roots", PLUGIN_ID)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("kanban_task_completed", on_card_completed)


__all__ = ["register", "configure", "commit_session", "eligible_root", "write_why", "on_card_completed"]
