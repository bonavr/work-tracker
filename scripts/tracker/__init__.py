"""work-tracker: a local, file-based plan and progress tracker for a piece of work.

A tracker is a folder under $TRACKER_HOME (default ~/.claude/trackers/<slug>/):

    README.md          the work itself: frontmatter + Context, Goal, Scope
    tickets/<ID>.md    one per ticket: frontmatter (state) + Plan / Carry forward / Links
    decisions/D-<n>.md one per direction decision: status open|closed
    log.md             append-only, dated; never read by default
    evidence/          files the work's tickets and decisions cite (`tracker attach`)
    .state.json        machine state: per repo and branch, commit marks, handoffs and PR lookups; `use` choices per
                       worktree; the last `sync`. Reads drop what no longer applies (STATE_RULES)

The contract (keys and who writes them, statuses, labels, sections) is the constants in model.py; `tracker rules`
prints it.
Frontmatter is a flat YAML subset: `key: value`, `key: [a, b]`, JSON-quoted strings (markdown.py).
Modules, each importing only those before it: markdown, model, git, session, contract, views, github, watcher,
viewer, hooks, cli.
Run `bin/tracker --help` or `bin/tracker <command> --help` for usage.
"""
