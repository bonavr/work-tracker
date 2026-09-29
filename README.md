# work-tracker

A Claude Code plugin that keeps the plan and progress of a piece of work (a feature, a project, a migration) in plain Markdown on your machine. Claude reads it at the start of each session and updates it as it works, so the next session continues where the last one stopped.

- Tickets with dependencies, direction decisions and a dated log.
- Hooks log your commits and pull PR state from GitHub.
- A live page in the browser shows the work under way and whose move each ticket waits on.

## Usage

Talk to Claude. It runs the `tracker` CLI for you.

1. **Create a tracker.** Ask Claude, for example: "Make a tracker for the payments rework in acme/api, with tickets for the schema, the API and the admin page." Give it the documents the work answers to (spec, issue, design): they go in the tracker's Context. Name the GitHub repo, so PR state syncs.
2. **Start a session on it.** Say "work on the payments tracker", or type `/work-tracker:tracker start payments`. Claude gets a brief: where the work is, what is under way, what it waits on.
   - A new session on a branch with an open ticket asks at your first message whether to link it. Answer yes, or "Not now" (no offer on that branch for a day).
3. **Work as usual.** Claude records steps, decisions and pauses as they happen. The hooks log each commit and keep PR state current. You write nothing yourself.
4. **See it.** Ask Claude to open the tracker, or run `tracker open`. The page updates live.
5. **Oversee many sessions (optional).** In a separate Claude session, type `/work-tracker:watch payments`. It notifies you when something needs you: a review came back, checks fail, an agent waits on you. `/work-tracker:watch stop` ends it. In a terminal: `tracker watch payments`.

`/work-tracker:tracker <command>` runs any `tracker` command, for example `/work-tracker:tracker index --active`.

### Useful commands

| Command | Does |
|---|---|
| `tracker list` | all trackers |
| `tracker index --active` | the tickets under way, whose move each waits on, the open decisions |
| `tracker ready` | what can start now, and from which branch |
| `tracker context <id>` | one ticket or decision in full, with what it builds on |
| `tracker decisions --all` | the decisions, open and settled |
| `tracker find <text>` | search a tracker (`--all`: every tracker) |
| `tracker open [id]` | the live page |
| `tracker check` | problems in the tracker's files |
| `tracker rules` | the full format: keys, statuses, sections, text limits |
| `tracker <command> --help` | the syntax of a command |

The `tracker` command is on Claude's Bash PATH while the plugin is enabled. In a terminal, run the plugin's `bin/tracker`, or put that folder on your PATH.

## Requirements

- macOS, Linux or Windows. On Windows, Git for Windows: the hooks and the CLI are `sh` scripts, and Claude Code runs them in its Git Bash.
- Python 3.9 or later, standard library only: `python3`, or on Windows `python` or `py`. The macOS system Python works.
- `git`.
- Optional: the GitHub CLI `gh`, logged in. Without it there is no PR state (in review, merged, reviews, checks) and no "whose move".

## How it works

### Data

Each tracker is a folder in `~/.claude/trackers/<slug>/` (`TRACKER_HOME` changes the root):

| File | Holds |
|---|---|
| `README.md` | the work: Context, Goal, Scope |
| `tickets/<ID>.md` | one ticket: status, branch, next action, dependencies; Plan, Carry forward (facts later tickets need), Links |
| `decisions/D-<n>.md` | one direction decision: Question, Options, Resolution |
| `log.md` | dated one-line history |
| `evidence/` | files the records cite: runs, measurements |
| `.state.json` | machine state. Do not edit |

- The folder is outside every repo, so all worktrees and sessions share one copy. Writes take a lock, so sessions that run at the same time do not overwrite each other.
- For history, run `git init` in the folder.
- Nothing leaves your machine except the `gh` calls to GitHub. The viewer listens on 127.0.0.1 only.

### Tickets, branches and sessions

- A ticket's status is `todo`, `in-progress`, `done` or `dropped`. Its PR adds `in-review` and `merged`.
- `depends_on` sets the order and the blockers. What is ready or blocked, and the branch a ticket can start from (stacked on work under way), are computed.
- The branch names the session's tickets: each ticket whose `branch` it is, a `tracker use <id>` choice, a ticket id or Issue id in the branch name, or the branch's PR.
- Each new session (after `/clear` too) starts on no tracker. Many sessions can share one tracker; `tracker start <slug> --on <id>` puts a session on one ticket of a shared branch.
- The tracker never changes git.
- **Isolation**: the tracker is private to the work. Outside it (code, commits, branch names, PRs, issues) Claude writes each fact in its own words and cites only real ids (an Issue id, a PR number, a URL), never the tracker's ids or name.

### Hooks

Each hook runs `scripts/hook.sh`, which filters the event in shell first. In a session with no tracker a hook exits in about 6 ms; Python (about 40 ms) starts only when it can add something. A hook never blocks the session.

| Event | Does |
|---|---|
| SessionStart | Gives the session its id. With a tracker: logs new commits, syncs PR state (at most every 10 min) and injects the brief. With none, on a branch with an open ticket: has Claude offer the link at your first message. |
| UserPromptSubmit | Reports what other sessions or GitHub changed since the brief. On the first message and every 5th: one state line, with work the tracker may not show (unlogged commits, uncommitted files). Starts a stale GitHub sync in the background. Handles `/work-tracker:watch`. |
| PostToolUse (Bash) | After a commit: logs it, and once per next action asks whether a step ended. After `git push` or `gh pr …`: records the branch on its ticket and syncs PR state. |
| PostToolUse (Edit, Write, MultiEdit) | Counts a hand edit of a tracker file as this session's own. |
| PostToolUse (AskUserQuestion) | Asks Claude to record the answer when it settles a direction decision. |
| SubagentStart | Tells a subagent the tracker and ticket, and that it writes nothing to them. |
| Stop | Logs the commits no other hook saw. |

### Viewer

- `tracker open [id]` starts a local server (Python `http.server`, 127.0.0.1 only) when none runs, and opens the page. The page polls every 3 s and updates in place. The server stops about 3 min after the last request.
- **Now** shows the tickets under way (your move first), each branch's handoff, and the Claude sessions on this machine that work on the tracker (a ring spins while one works).
- While a page is open, the server syncs PR state every 2 min.
- The session list reads Claude Code's `~/.claude/sessions/*.json` (`CLAUDE_CONFIG_DIR` when set). That format is not documented: if it changes, the page shows no sessions and the rest still works.

### Watch

- `tracker watch [name]` prints each change as one line; `!` marks what needs you. One watcher per tracker.
- Only the user starts it: in a terminal, or with `/work-tracker:watch <tracker>`, which makes that Claude session a read-only overseer that sends notifications.
- The CLI refuses `watch` in a session without that grant, and refuses every write in a session with it.

### Upgrading a tracker

The `schema` key in a tracker's README names its format. When `tracker check` says the format is old, run `tracker migrate --dry-run`, then `tracker migrate` (it backs up to `$TRACKER_HOME/.backups/` first).

### Settings

Environment variables. All are optional.

| Variable | Default | Does |
|---|---|---|
| `TRACKER_HOME` | `~/.claude/trackers` | where the trackers are |
| `TRACKER` | none | the tracker slug a session and its commands use when `tracker start` chose none |
| `TRACKER_NUDGE_EVERY` | 5 | messages between state lines |
| `TRACKER_VIEWER_IDLE` | 180 | seconds before an unused viewer stops |
| `TRACKER_VIEWER_SYNC` | 120 | seconds between the viewer's GitHub syncs |
| `TRACKER_WATCH_SYNC` | 120 | seconds between the watcher's GitHub syncs |
| `TRACKER_WATCH_IDLE` | 600 | seconds before an idle agent that waits on you is reported |
| `TRACKER_WATCH_BUSY` | 2700 | seconds a busy agent can record nothing before it is reported |

## Develop

- Run from a checkout: `claude --plugin-dir <path to this folder>`, then `/reload-plugins` after an edit. An open viewer restarts itself when `scripts/tracker/` changes, and an open page reloads when `viewer/` changes.
- Test: `python3 -m unittest discover tests` (about 10 s, no network). Lint: `uvx ruff check`.
- Ship: bump `version` in `.claude-plugin/plugin.json`. Installed copies are cached by version.
- The format's contract (keys and who writes each, statuses, link labels, sections) is the constants in `scripts/tracker/model.py`. `tracker rules` prints it, `tracker check` enforces it, and the hooks and templates read it.
- Each fact has one home, and every view is computed from it. `skills/tracker/SKILL.md` lists each home.

### Code

`scripts/tracker/`: stdlib only, run by `bin/tracker` and `scripts/hook.sh`. One module per concern; each imports only the modules before it:

1. `markdown`: the file format as text (frontmatter, sections, link lines)
2. `model`: contract constants, records, dependencies, body edits, the lock
3. `git`
4. `session`: the session's tracker, branch matching, commit marks and handoffs, the sessions running now
5. `contract`: `check`, `rules`, `migrate`
6. `views`: text views and the brief
7. `github`: `sync`
8. `watcher`: `tracker watch` and the user's grant
9. `viewer`: the page and its server (look: `viewer/page.html`, `style.css`, `app.js`)
10. `hooks`
11. `cli`

### Evals

`evals/` holds [`claude plugin eval`](https://code.claude.com/docs/en/plugin-evals) cases. They check that the skill, the brief and the hooks lead the model to record work through the CLI (a step with `step --next`, a hook-logged commit not logged again, one decision for a settled direction and none for an approval, a section replaced with `put`, not by hand), and to answer what can start next with the branch it stacks on. They are real model runs, billed to your plan: run them after a change to those texts, not as a routine check.

```
claude plugin eval . --scaffold --allow-tools Bash --ablation none
```

- `--scaffold`: each case's `scaffold.sh` sources `evals/lib/demo-tracker.sh`, which makes a git repo and a `demo` tracker in the run's workspace. The sandbox lets the agent write only there, so the tracker is in `.trackers/`, linked from `~/.claude/trackers`.
- `--ablation none`: with no plugin there is no tracker, so a baseline measures nothing.
- `--runs 1` while you change a case; the default is 3 runs per case.
- On macOS with only the Xcode `git` (`/usr/bin/git`), git cannot run in the sandbox, so the cases leave git to the scaffold and the hooks.
