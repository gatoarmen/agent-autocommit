# Hermes Nervous System — Auto Commit
> **The missing operational nervous system and shared memory for autonomous multi-agent teams.**  
> *(Hermes Plugin: `agent-autocommit`)*

While modern agent harnesses and multi-agent platforms excel at individual reasoning, deploying a collaborative team of agents creates chaos: agents overwrite each other's work, pollute shared environments, hallucinate completed actions, and leave operators with opaque black-box chat logs.

**Hermes Nervous System (Auto Commit)** turns local Git into an indestructible, conflict-free operational backbone, project management ledger, and permanent team memory for multi-agent organizations.

---

### Why this is the Nervous System (Key Pillars)

#### 1. Tamper-Proof Multi-Agent Memory & Identity
Git is no longer just for software engineers—it becomes the distributed, permanent ledger of the team. Every agent turn is atomically snapshot, isolated, and committed under that specific agent's profile identity (`builder (Hermes)`, `researcher (Hermes)`, `designer (Hermes)`).

#### 2. Autonomous Project Management & Human-Readable Receipts
Built natively for goal-driven operations and Kanban workflows. When agents work on a business objective or Kanban task, every commit automatically binds to the task ID and records the underlying business rationale (`Why:`). Upon task completion, it automatically synthesizes `WHY.md`—a living executive changelog so non-technical founders and operators know exactly what changed and why, without wading through millions of reasoning tokens.

#### 3. Surgical, Conflict-Free Parallel Collaboration
Multiple autonomous agents can work simultaneously in the same workspace without collisions. By bypassing shared staging and operating on isolated hunks via direct Git plumbing (`commit-tree`, `update-ref`), agents never stomp on each other's in-flight edits. If two agents touch the same file concurrently, the engine safely isolates changes rather than corrupting work.

#### 4. Verifiable Operations for Non-Technical Founders
Bridging the gap between raw developer tools and real-world business operations. Non-technical founders running daily operations gain a deterministic audit trail: instant surgical rollbacks (`git revert <sha>`), agent accountability, and zero hallucinated progress.

---

## What it does

- **Snapshots on first touch.** The first time a session edits a file with `write_file` or `patch`, the plugin saves
  the file's bytes. For `terminal` commands it runs `git status` before and after. A file that was clean before and is
  changed after belongs to this session, and so does a new untracked file.
- **One commit per repo per turn, only your hunks.** At the end of the turn (`post_llm_call`), each file's
  `snapshot -> now` change is replayed onto HEAD. The tree is built in a private index, and HEAD moves with a
  compare-and-swap (`git update-ref <new> <old>`). The plugin never runs `git add`/`git commit`, never touches another
  session's staged work, and never makes a partial commit. If two sessions both edited the same file and neither has
  committed, the plugin refuses with a reason ("commit it by hand") rather than mixing their hunks.
- **Never pushes.** All commits are local.
- **Kanban aware.** Inside a Kanban worker (`HERMES_KANBAN_TASK`), or in a chat turn that names exactly one real card
  id, the message carries `Card: <id> (<board>) <title>` and `Why: <first line of the card's Why:>`. When the card is
  done, every repo it changed gets one entry in `WHY.md`, newest first: why, outcome, commits, and how to open the
  card. That entry is committed too. Commits you make by hand in `terminal` during the turn are adopted by the card.
- **Stays out of the way.** It skips gitignored files, secret-looking names (`.env*`, `*.pem`, `*token*`, `id_rsa*`,
  ...), files over 5 MB, `node_modules`/`.venv`/`__pycache__`, and the Hermes install itself. Errors are logged at
  debug level and never raised into the agent.

## Install

```bash
hermes plugins install https://github.com/gatoarmen/agent-autocommit#agent_autocommit --enable
```

Or, from a local checkout, link the plugin folder into a profile:

```bash
ln -s "$PWD/agent_autocommit" ~/.hermes/plugins/agent-autocommit     # default profile
hermes plugins enable agent-autocommit
```

Plugins load when a process starts, so restart the gateway, dashboard or CLI afterwards.

## Configure

The plugin does nothing until you give it at least one root. Settings live under
`plugins.entries.agent-autocommit.settings` in `config.yaml` (the Desktop Plugins tab shows them as a form):

```yaml
plugins:
  entries:
    agent-autocommit:
      settings:
        roots: ["~/projects"]              # git repos under these folders may take commits
        exclude_paths:                     # paths: skip any repo that overlaps them; globs: skip what they match
          - ~/projects/production-site     #   absolute path
          - vendor                         #   relative: applies under every root
          - "*/deploy-*"                   #   glob, matched against absolute paths
        exclude_ledger: ""                 # optional JSON Lines file whose rows name paths to exclude
        exclude_ledger_field: target       # the field of each ledger row that holds the path
        allow_remote_repos: false          # true: also commit (locally) in repos that have a remote
        author_email_domain: hermes.local  # author = <profile>@<domain>
        write_why_md: true                 # write WHY.md when a card is done
```

| Setting | Default | Meaning |
|---|---|---|
| `roots` | `[]` | Folders whose git repos may take commits. A repo must have its top level inside a root, so a root nested inside a bigger repo never commits into the outer repo. |
| `exclude_paths` | `[]` | Absolute paths, root-relative paths, or globs. A repo that contains an excluded path, or sits inside one, is skipped entirely. A glob excludes only what it matches: a repo (or a folder above it) that matches is skipped, while a glob that matches a folder *inside* a repo skips just the files under that folder. |
| `exclude_ledger` | `""` | Optional. Point it at a deploy log (one JSON object per line) and every absolute path in `exclude_ledger_field` is excluded. Blank or relative rows are ignored. A missing file excludes nothing. |
| `allow_remote_repos` | `false` | Repos with any git remote are skipped by default, since they are usually clones or published repos. Turn this on to commit in them too. The plugin still never pushes. |
| `author_email_domain` | `hermes.local` | Domain of the author/committer email. |
| `write_why_md` | `true` | Write the per-card `WHY.md` entry. `Card:`/`Why:` lines in commit messages are unaffected. |

Each setting can also be overridden with an environment variable (lists separated by `:` on Linux/macOS):
`AGENT_AUTOCOMMIT_ROOTS`, `AGENT_AUTOCOMMIT_EXCLUDE`, `AGENT_AUTOCOMMIT_LEDGER`, `AGENT_AUTOCOMMIT_ALLOW_REMOTE`,
`AGENT_AUTOCOMMIT_EMAIL_DOMAIN`, `AGENT_AUTOCOMMIT_WHY_MD`. `AGENT_AUTOCOMMIT_STATE` moves the state folder
(default `<hermes root>/cache/agent-autocommit`, shared by all profiles so they can see each other's snapshots).

### Turning it off

- `HERMES_NO_AUTOCOMMIT=1` in the environment;
- an empty file `~/.hermes/agent-autocommit.off` (all profiles);
- an empty file `<profile home>/agent-autocommit.off` (one profile);
- or `hermes plugins disable agent-autocommit`.

## Reading the history

```bash
git log --author=builder            # one agent's turns
git log --grep='Card: t_'           # everything done for cards
git revert <sha>                    # undo one turn
cat WHY.md                          # why this repo changed, one entry per finished card
```

The plugin writes a JSON Lines log of every commit or skip (with its reason) to
`<state>/commits.jsonl`.

## Development

```bash
python -m pytest -q tests
hermes plugins validate agent_autocommit
```

Design decisions are in [DECISIONS.md](DECISIONS.md).

## License

[MIT](LICENSE)
