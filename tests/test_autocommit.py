"""Engine behaviour: one session's hunks only, roots, exclusions, remotes, off switches, secrets."""
import json

from conftest import edit, git, make_repo


def test_write_file_commits_with_profile_author(plugin):
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    f = repo / "a.txt"
    edit(mod, f, f.read_text().replace("two", "TWO"), "s1")
    res = mod.commit_session("s1", "change line two")
    assert res and "commit" in res[0], res
    assert git(repo, "log", "-1", "--format=%an <%ae>|%s").strip() == \
        "builder (Hermes) <builder@hermes.local>|agent(builder): change line two"
    assert git(repo, "status", "--porcelain") == ""


def test_email_domain_is_configurable(plugin):
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    mod.configure(author_email_domain="agents.example.org")
    edit(mod, repo / "a.txt", "x\n", "s1")
    mod.commit_session("s1", "x")
    assert git(repo, "log", "-1", "--format=%ae").strip() == "builder@agents.example.org"


def test_two_sessions_same_file_only_own_hunks(plugin):
    mod, work, profile = plugin
    repo = make_repo(work / "tool")
    f = repo / "a.txt"
    edit(mod, f, f.read_text().replace("two", "TWO"), "s1")
    profile["name"] = "designer"
    edit(mod, f, f.read_text().replace("six", "SIX"), "s2")
    # s2 edited after s1's snapshot and has not committed: s1 must refuse (its hunks cannot be told apart)
    profile["name"] = "builder"
    assert "skipped" in mod.commit_session("s1", "one")[0]
    profile["name"] = "designer"
    r2 = mod.commit_session("s2", "two")
    assert "commit" in r2[0], r2
    head = git(repo, "show", "HEAD:a.txt")
    assert "SIX" in head and "TWO" not in head  # only designer's hunk
    profile["name"] = "builder"
    mod._add_touched("s1", repo, "a.txt")
    r1 = mod.commit_session("s1", "one again")
    assert "commit" in r1[0], r1
    assert "TWO" in git(repo, "show", "HEAD:a.txt")


def test_terminal_new_and_modified_files(plugin):
    mod, work, _ = plugin
    r = make_repo(work / "tool")
    mod._on_pre_tool_call("terminal", {"command": "x", "workdir": str(r)}, session_id="s3", tool_call_id="t1")
    (r / "new.py").write_text("print(1)\n")
    (r / "a.txt").write_text("changed\n")
    (r / "out.log").write_text("ignored\n")
    (r / ".env").write_text("KEY=1\n")
    mod._on_post_tool_call("terminal", session_id="s3", tool_call_id="t1")
    res = mod.commit_session("s3", "run script")
    assert "commit" in res[0], res
    assert sorted(git(r, "show", "--name-only", "--format=", "HEAD").split()) == ["a.txt", "new.py"]


def test_terminal_leaves_other_agents_dirty_file(plugin):
    mod, work, _ = plugin
    r = make_repo(work / "tool")
    (r / "a.txt").write_text("someone else's edit\n")
    mod._on_pre_tool_call("terminal", {"command": "x", "workdir": str(r)}, session_id="s4", tool_call_id="t2")
    (r / "b.txt").write_text("mine\n")
    mod._on_post_tool_call("terminal", session_id="s4", tool_call_id="t2")
    res = mod.commit_session("s4", "x")
    assert git(r, "show", "--name-only", "--format=", "HEAD").split() == ["b.txt"], res
    assert " M a.txt" in git(r, "status", "--porcelain")


def test_terminal_from_outside_repo_with_absolute_path(plugin, tmp_path):
    mod, work, _ = plugin
    r = make_repo(work / "tool")
    target = r / "a.txt"
    mod._on_pre_tool_call("terminal", {"command": f"echo hi >> {target}", "workdir": str(tmp_path)},
                          session_id="s10", tool_call_id="t10")
    with target.open("a") as fh:
        fh.write("hi\n")
    mod._on_post_tool_call("terminal", session_id="s10", tool_call_id="t10")
    res = mod.commit_session("s10", "append")
    assert res and "commit" in res[0], res


def test_outside_every_root_is_ignored(plugin, tmp_path):
    mod, _, _ = plugin
    other = make_repo(tmp_path / "elsewhere")
    edit(mod, other / "a.txt", "x\n", "s5")
    edit(mod, tmp_path / "loose.txt", "x\n", "s5")
    assert mod.commit_session("s5", "x") == []
    assert git(other, "rev-list", "--count", "HEAD").strip() == "1"


def test_several_roots(plugin, tmp_path, monkeypatch):
    mod, work, _ = plugin
    second = tmp_path / "second-root"
    a = make_repo(work / "a")
    b = make_repo(second / "b")
    monkeypatch.setenv("AGENT_AUTOCOMMIT_ROOTS", f"{work}:{second}")
    edit(mod, a / "a.txt", "x\n", "s1")
    edit(mod, b / "a.txt", "y\n", "s1")
    res = mod.commit_session("s1", "both")
    assert len([r for r in res if "commit" in r]) == 2, res


def test_roots_from_settings_not_env(plugin, monkeypatch):
    mod, work, _ = plugin
    monkeypatch.delenv("AGENT_AUTOCOMMIT_ROOTS")
    repo = make_repo(work / "tool")
    assert mod.disabled() == "no roots configured"
    mod.configure(roots=[str(work)])
    assert mod.disabled() is None
    assert mod.eligible_root(repo / "a.txt") == repo.resolve()


def test_root_inside_a_bigger_repo_never_commits_to_it(plugin, tmp_path, monkeypatch):
    mod, _, _ = plugin
    outer = make_repo(tmp_path / "outer")
    inner = outer / "sub"
    inner.mkdir()
    monkeypatch.setenv("AGENT_AUTOCOMMIT_ROOTS", str(inner))
    edit(mod, inner / "f.txt", "x\n", "s1")
    assert mod.commit_session("s1", "x") == []


def test_repo_with_remote_is_skipped_by_default(plugin):
    mod, work, _ = plugin
    repo = make_repo(work / "cloned")
    git(repo, "remote", "add", "origin", "https://example.com/x.git")
    edit(mod, repo / "a.txt", "x\n", "s1")
    assert mod.commit_session("s1", "x") == []


def test_repo_with_remote_opt_in_commits_locally_and_never_pushes(plugin, tmp_path):
    mod, work, _ = plugin
    bare = tmp_path / "upstream.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    repo = make_repo(work / "cloned")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    before = git(bare, "rev-parse", "main").strip()
    mod.configure(allow_remote_repos=True)
    edit(mod, repo / "a.txt", "x\n", "s1")
    res = mod.commit_session("s1", "x")
    assert "commit" in res[0], res
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "2"
    assert git(bare, "rev-parse", "main").strip() == before  # nothing reached the remote


def test_exclude_paths_absolute_relative_and_glob(plugin):
    mod, work, _ = plugin
    absolute = make_repo(work / "deployed")
    relative = make_repo(work / "vendor")
    globbed = make_repo(work / "prod-api")
    kept = make_repo(work / "tool")
    mod.configure(exclude_paths=[str(absolute), "vendor", "*/prod-*"])
    for repo in (absolute, relative, globbed, kept):
        edit(mod, repo / "a.txt", "x\n", "s1")
    res = mod.commit_session("s1", "x")
    assert [r["repo"] for r in res if "commit" in r] == [str(kept.resolve())], res


def test_exclude_entry_inside_a_repo_excludes_the_whole_repo(plugin):
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    (repo / "dist").mkdir()
    mod.configure(exclude_paths=[str(repo / "dist")])
    assert mod.eligible_root(repo / "a.txt") is None


def test_ledger_is_optional_and_excludes_its_targets(plugin, tmp_path):
    mod, work, _ = plugin
    deployed = make_repo(work / "deployed-tool")
    tool = make_repo(work / "tool")
    assert mod.eligible_root(deployed / "a.txt") is not None  # no ledger configured: nothing excluded
    ledger = tmp_path / "deploys.jsonl"
    ledger.write_text(json.dumps({"target": str(deployed)}) + "\n" + json.dumps({"target": ""}) + "\n"
                      + json.dumps({"target": "tool"}) + "\nnot json\n")
    mod.configure(exclude_ledger=str(ledger))
    assert mod.eligible_root(deployed / "a.txt") is None
    assert mod.eligible_root(tool / "a.txt") == tool.resolve()  # blank/relative rows never hide a repo


def test_missing_ledger_file_never_blocks(plugin, tmp_path):
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    mod.configure(exclude_ledger=str(tmp_path / "nope.jsonl"))
    assert mod.eligible_root(repo / "a.txt") == repo.resolve()


def test_ledger_field_is_configurable(plugin, tmp_path):
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    ledger = tmp_path / "log.jsonl"
    ledger.write_text(json.dumps({"path": str(repo)}) + "\n")
    mod.configure(exclude_ledger=str(ledger), exclude_ledger_field="path")
    assert mod.eligible_root(repo / "a.txt") is None


def test_hermes_install_is_never_a_target(plugin, monkeypatch):
    mod, work, _ = plugin
    repo = make_repo(work / "hermes-agent")
    monkeypatch.setattr(mod, "_hermes_install", lambda: repo.resolve())
    assert mod.eligible_root(repo / "a.txt") is None


def test_off_switches(plugin, monkeypatch, tmp_path):
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    monkeypatch.setenv("HERMES_NO_AUTOCOMMIT", "1")
    edit(mod, repo / "a.txt", "z\n", "s7")
    assert mod.commit_session("s7", "x") == []
    monkeypatch.delenv("HERMES_NO_AUTOCOMMIT")
    (tmp_path / "profile-home").mkdir()
    (tmp_path / "profile-home" / "agent-autocommit.off").write_text("")
    edit(mod, repo / "a.txt", "w\n", "s8")
    assert mod.commit_session("s8", "x") == []
    (tmp_path / "profile-home" / "agent-autocommit.off").unlink()
    (tmp_path / "hermes-root").mkdir()
    (tmp_path / "hermes-root" / "agent-autocommit.off").write_text("")
    assert mod.disabled() == "global off-file"


def test_secret_never_committed_via_write_file(plugin):
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    edit(mod, repo / "api_token.txt", "sk-123\n", "s9")
    assert mod.commit_session("s9", "x") == []


def test_subject_strips_agent_prefix_and_brackets(plugin):
    mod, _, _ = plugin
    assert mod._subject("Message from 🤖 Builder (@builder): fix the test") == "fix the test"
    assert mod._subject("[context note] do it") == "do it"
    assert mod._subject("") == "edits"


def test_register_reads_settings_from_context(plugin, tmp_path, monkeypatch):
    mod, work, _ = plugin
    monkeypatch.delenv("AGENT_AUTOCOMMIT_ROOTS")

    class Ctx:
        profile_name = "builder"
        hooks: list = []

        def get_config(self, key, default=None):
            return {"roots": [str(work)], "allow_remote_repos": True}.get(key, default)

        def register_hook(self, name, cb):
            self.hooks.append(name)

    ctx = Ctx()
    mod.register(ctx)
    assert mod.roots() == [work.resolve()]
    assert mod.setting("allow_remote_repos") is True
    assert sorted(ctx.hooks) == ["kanban_task_completed", "post_llm_call", "post_tool_call", "pre_tool_call"]


def test_glob_matching_a_folder_inside_a_repo_skips_only_those_files(plugin):
    mod, work, _ = plugin
    repo = make_repo(work / "dash")
    (repo / "releases").mkdir()
    mod.configure(exclude_paths=["*/releases"])
    assert mod.eligible_root(repo / "a.txt") == repo.resolve()
    edit(mod, repo / "releases" / "r.txt", "build\n", "s1")
    edit(mod, repo / "a.txt", "x\n", "s1")
    mod.commit_session("s1", "x")
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["a.txt"]


def test_changing_ledger_field_rereads_the_ledger(plugin, tmp_path):
    mod, _, _ = plugin
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"first": "/srv/one", "second": "/srv/two"}) + "\n")
    mod.configure(exclude_ledger=str(ledger), exclude_ledger_field="first")
    assert mod._ledger_paths() == ["/srv/one"]
    mod.configure(exclude_ledger_field="second")
    assert mod._ledger_paths() == ["/srv/two"]


def test_profile_from_hermes_home_both_cases(tmp_path):
    import agent_autocommit as mod
    assert mod.profile_from_home(tmp_path / ".hermes" / "profiles" / "designer") == "designer"
    assert mod.profile_from_home(tmp_path / ".hermes") == "default"


def test_profile_reads_get_hermes_home(tmp_path, monkeypatch):
    import agent_autocommit as mod
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "writer"))
    assert mod._profile() == "writer"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert mod._profile() == "default"


def test_repo_config_fsmonitor_never_runs(plugin, tmp_path):
    # A repo's own .git/config can name a command as core.fsmonitor; git status would run it on the host.
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    marker = tmp_path / "fsmonitor-ran"
    git(repo, "config", "core.fsmonitor", f"touch {marker}; false")
    (repo / "a.txt").write_text("changed\n")
    mod._git(repo, "status", "--porcelain=v1")
    assert not marker.exists()


def test_repo_config_clean_filter_never_runs_when_hashing(plugin, tmp_path):
    # A clean filter from the repo's config would run during the comparison-only hash-object.
    mod, work, _ = plugin
    repo = make_repo(work / "tool")
    marker = tmp_path / "filter-ran"
    git(repo, "config", "filter.evil.clean", f"touch {marker}; cat")
    (repo / ".gitattributes").write_text("*.txt filter=evil\n")
    (repo / "a.txt").write_text("changed\n")
    assert mod._hash(repo, "a.txt")
    assert not marker.exists()
