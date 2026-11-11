"""Commands a repo's own config can make git run on `log`/`status` never run from the plugin: ADR-006."""
import os
import stat
import subprocess
from pathlib import Path

from agent_autocommit import commit_engine as engine
from conftest import git, make_repo

_SIG = ("-----BEGIN PGP SIGNATURE-----\n"
        " \n"
        " iQEzBAABCAAdFiEEAAAAAAAAAAAAAAAAAAAAAAAAAAAFAmZ0ZXN0AAoJEAAAAAAA\n"
        " =AAAA\n"
        " -----END PGP SIGNATURE-----")


def _fake_signer(tmp_path: Path, marker: Path) -> Path:
    script = tmp_path / "fake-gpg"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


_SSH_SIG = ("-----BEGIN SSH SIGNATURE-----\n"
            " U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAg\n"
            " -----END SSH SIGNATURE-----")


def _signed_repo(work: Path, tmp_path: Path, marker: Path, sig: str = _SIG) -> tuple[Path, str]:
    """A repo whose HEAD carries a gpgsig header and whose local config verifies it with a command."""
    repo = make_repo(work / "tool")
    head = git(repo, "rev-parse", "HEAD").strip()
    raw = git(repo, "cat-file", "commit", head)
    header, _, body = raw.partition("\n\n")
    signed = f"{header}\ngpgsig {sig}\n\n{body}"
    sha = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo, input=signed,
                         capture_output=True, text=True, check=True).stdout.strip()
    git(repo, "update-ref", "HEAD", sha)
    git(repo, "config", "log.showSignature", "true")
    git(repo, "config", "gpg.program", str(_fake_signer(tmp_path, marker)))
    return repo, sha


def _control_runs(repo: Path, marker: Path) -> None:
    subprocess.run(["git", "log", "-1", "--format=%cI"], cwd=repo, capture_output=True)
    assert marker.exists(), "setup is wrong: plain git log should have run gpg.program"


def test_hand_commit_author_check_never_runs_gpg_program(plugin, tmp_path):
    mod, work, _ = plugin
    marker = tmp_path / "PWNED"
    repo, sha = _signed_repo(work, tmp_path, marker)
    code, emails = mod._git(repo, "log", "--format=%ae", "--no-walk", sha)  # the __init__.py hand-commit path
    assert code == 0 and emails.strip() == "t@t"
    assert not marker.exists()
    _control_runs(repo, marker)


def test_engine_git_out_log_never_runs_gpg_program(tmp_path):
    marker = tmp_path / "PWNED"
    work = tmp_path / "work"
    repo, sha = _signed_repo(work, tmp_path, marker)
    code, out = engine._git_out(repo, "log", "-1", "--format=%H")
    assert code == 0 and out.decode().strip() == sha
    code, _ = engine._git_out(repo, "show", "-s", sha)
    assert code == 0
    assert not marker.exists()
    _control_runs(repo, marker)


def test_last_commit_at_never_runs_gpg_program(tmp_path):
    marker = tmp_path / "PWNED"
    work = tmp_path / "work"
    repo, _ = _signed_repo(work, tmp_path, marker)
    assert engine.last_commit_at(repo, "a.txt") is not None
    assert not marker.exists()
    _control_runs(repo, marker)


def test_ssh_signature_program_never_runs(plugin, tmp_path):
    mod, work, _ = plugin
    marker = tmp_path / "PWNED"
    repo, sha = _signed_repo(work, tmp_path, marker, _SSH_SIG)
    git(repo, "config", "--unset", "gpg.program")
    git(repo, "config", "gpg.format", "ssh")
    git(repo, "config", "gpg.ssh.program", str(_fake_signer(tmp_path, marker)))
    (tmp_path / "allowed").write_text("")
    git(repo, "config", "gpg.ssh.allowedSignersFile", str(tmp_path / "allowed"))
    mod._git(repo, "log", "--format=%ae", "--no-walk", sha)
    engine._git_out(repo, "log", "-1", "--format=%H")
    engine.last_commit_at(repo, "a.txt")
    assert not marker.exists()
    _control_runs(repo, marker)


def test_status_does_not_recurse_into_submodule_filters(plugin, tmp_path):
    # The superproject's filter check never sees a submodule's own config, so status must not descend into it.
    mod, work, _ = plugin
    marker = tmp_path / "SUB-PWNED"
    sub_src = make_repo(tmp_path / "sub-src", "ab\n")
    (sub_src / ".gitattributes").write_text("*.txt filter=evil\n")
    git(sub_src, "add", ".gitattributes")
    git(sub_src, "commit", "-qm", "attrs")
    sup = make_repo(work / "super")
    git(sup, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_src), "sub")
    git(sup, "commit", "-qm", "add sub")
    sub = sup / "sub"
    git(sub, "config", "filter.evil.clean", f"touch {marker}; cat")
    path = sub / "a.txt"
    path.write_text("cd\n")  # same size, then the index's mtime: git must re-hash it through the filter
    index = Path(git(sub, "rev-parse", "--absolute-git-dir").strip()) / "index"
    os.utime(path, ns=(index.stat().st_atime_ns, index.stat().st_mtime_ns))
    assert mod.eligible_root(sup) is not None  # the superproject itself defines no filter
    mod._status(sup)
    assert not marker.exists()
    subprocess.run(["git", "-c", "core.fsmonitor=false", "status", "--porcelain"], cwd=sup, capture_output=True)
    assert marker.exists(), "setup is wrong: plain git status should recurse and run the submodule filter"
