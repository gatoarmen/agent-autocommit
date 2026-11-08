# Decisions

## ADR-001: Fork the commit engine instead of vendoring it

**Status:** accepted (2026-11-09)

**Context.** The plugin started inside a private setup. Its commit engine was a verbatim copy of a larger internal
"record and commit" module, which also handled scoped publishing, locked files, workspace-root discovery from session
metadata, a process-wide `NO_COMMIT` switch, and Windows paths. Most of that is dead weight here. Some of it
(session-metadata lookups, internal decision references in docstrings, an internal off switch) only makes sense
inside that setup. Vendoring the file "unchanged, refresh from upstream" kept the two in sync, but it also tied a
public plugin to a private tree that nobody else can read or refresh from.

**Decision.** `agent_autocommit/commit_engine.py` is a clean fork that keeps only what this plugin calls:

- `commit_paths`: replay `before -> after` per path onto HEAD in a private index, all or nothing, compare-and-swap on
  HEAD with one retry, and keep the shared index consistent afterwards.
- `commit_session_paths` (was `commit_hand_record`): measure one session's change from its first-edit snapshot,
  refuse when a sibling session has uncommitted edits to the same file, and refuse a dirty tracked file with no
  snapshot.
- the snapshot store (`take_snapshot`, `load_snapshot`, `advance_snapshot`, `other_sessions_editing`).

Changes against the original:

- Removed: `publish_scoped`, `commit_record`, `commit_captured`, `capture`, file locking, the `strict_base` path, the
  module-wide "no commit" environment switch (the plugin has its own off switches), and the `init.json` workspace-root lookup.
  `other_sessions_editing` now needs `root` passed explicitly, and the plugin always passes it.
- Added: an explicit `identity=(name, email)` argument that is passed to `git commit-tree` through a per-call
  environment. The old plugin set `GIT_AUTHOR_*` on `os.environ` under a global lock. That leaked to concurrent
  threads and serialized every commit in the process.
- Reflog lines say `agent-autocommit:` instead of the internal tool name. Messages use plain English, with no
  references to internal decisions.

**Consequences.** Fixes to the merge logic no longer arrive by copying a file. Any future fix has to be ported by
hand. The engine is small (about 450 lines) and covered by `tests/test_commit_engine.py` plus the end-to-end tests,
so the cost is acceptable. The snapshot file format is unchanged, but the plugin now keys its state folders by
absolute repo path, so snapshots left by the earlier plugin are not picked up. Commit or discard pending edits
before switching.

## ADR-002: Configurable roots and `exclude_paths` replace the hard-coded deploy ledger

**Status:** accepted (2026-11-09)

**Context.** The original plugin committed only under one hard-coded folder. It treated every top-level folder named
in a specific deploy log (`<root>/<ledger dir>/deploys.jsonl`) as deployed, and so off limits. Nobody else has that
folder or that log.

**Decision.** `roots` (a list) says where commits may happen, and it is empty by default, so a fresh install does
nothing until configured. `exclude_paths` (absolute, root-relative, or glob) says where they may not. A repo that
contains an excluded path, or is inside one, is skipped entirely. Globs are matched only against the repo, its
parents, and each file, never against every folder inside the repo, which would mean walking the tree on every edit.
The deploy log survives as an optional
`exclude_ledger` (any JSON Lines file, configurable field) whose absolute paths are added to the exclusions.

**Consequences.** Existing setups map one to one: `roots: [<old root>]`, `exclude_ledger: <old log>`. A missing
ledger excludes nothing instead of failing.

## ADR-003: Repos with a remote are opt-in, and the plugin never pushes

**Status:** accepted (2026-11-09)

**Context.** The original skipped every repo with a remote: a third-party clone or a published repo was "not ours".
Many users keep all their work in repos with a remote, so the plugin would do nothing for them.

**Decision.** Keep skipping by default, and add `allow_remote_repos: true` to opt in. Even then the plugin only makes
local commits. No code path runs `git push`, `fetch` or `pull`, and a test checks that the remote's ref is unchanged
after a commit.

**Consequences.** Users decide when to push, typically after squashing or reviewing the agents' commits.

## ADR-004: Profile name from the public Hermes home

**Status:** accepted (2026-11-09)

**Context.** `hermes plugins validate` warned (rule 9, private imports) about
`hermes_cli.profiles.get_active_profile_name`, which the original called to learn the active profile.

**Decision.** Remove the import. The profile is derived from the public `hermes_constants.get_hermes_home()`:
a home of the form `<root>/profiles/<name>` gives `<name>`, and anything else (the shared root) gives `default`.
`hermes_constants` is on the public plugin API list. The Hermes root comes from
`hermes_constants.get_default_hermes_root()` instead of `~/.hermes`. No justification or allow-list entry is needed.

**Consequences.** Validation passes with no private-import warning.
