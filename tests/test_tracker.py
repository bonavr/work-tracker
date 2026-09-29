"""Checks that keep the tracker reliable: its instructions, its file format, its commands and its hooks, run end to end
in temporary git repos. Run: python3 -m unittest discover tests"""

from __future__ import annotations

import atexit
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = tempfile.mkdtemp()
atexit.register(shutil.rmtree, BASE, True)
os.environ["TRACKER_HOME"] = f"{BASE}/home"  # before the import: HOME is read once
os.environ["CLAUDE_CONFIG_DIR"] = f"{BASE}/claude"  # the running Claude sessions the viewer reads: not the user's
# A Claude session running the tests must not leak in: its tracker, its id, or that it is one.
for var in ("TRACKER", "TRACKER_SESSION", "CLAUDE_ENV_FILE", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
    os.environ.pop(var, None)
sys.path.insert(0, str(ROOT / "scripts"))
from tracker import cli, git, github, hooks, markdown, model, session, viewer, views, watcher  # noqa: E402


def slug() -> str:
    return f"t{time.monotonic_ns()}"


def repo(branch: str) -> Path:
    """A new git repo on `branch` with one commit."""
    path = Path(tempfile.mkdtemp(dir=BASE))
    for args in (["init", "-q", "-b", branch], ["config", "user.email", "me@x"], ["config", "user.name", "me"]):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    commit(path, "start")
    return path


def commit(path: Path, msg: str) -> None:
    (path / f"{time.monotonic_ns()}.txt").write_text(msg)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", msg], check=True)


def run(*args: str, cwd: Path | None = None, code: int = 0) -> str:
    """One `tracker` command in this process; its output. `code` is the exit code it must give."""
    out, old = io.StringIO(), os.getcwd()
    os.chdir(cwd or old)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            cli.main(list(args))
        got = 0
    except SystemExit as exc:
        got = exc.code or 0
    finally:
        os.chdir(old)
        git.worktree.cache_clear()
    assert got == code, f"tracker {' '.join(args)} exited {got}:\n{out.getvalue()}"
    return out.getvalue()


def piped(text: str):
    """stdin for a command given `-`, as a heredoc passes it."""
    return mock.patch.object(sys, "stdin", io.StringIO(text))


class InstructionsMatchTheCli(unittest.TestCase):
    """Every command the skill, the README, the templates and the hook texts give must exist, with its flags."""

    def test_commands_and_flags(self):
        sub = cli.build_parser()[1]
        texts = [*sorted((ROOT / "skills").glob("*/SKILL.md")), ROOT / "README.md", ROOT / "hooks/hooks.json",
                 *sorted((ROOT / "scripts/tracker").glob("*.py")), *sorted((ROOT / "templates").glob("*.md"))]
        bad = []
        for path in texts:
            for span in re.findall(r"`([^`\n]+)`", path.read_text()):
                words = span.split()
                named = words[0] == "tracker"
                if named:
                    words = words[1:]
                if not words or words[0][0] in "<${-":  # a placeholder or a global flag: `tracker <command> --help`
                    continue
                cmd = sub.choices.get(words[0])
                if not cmd:
                    if named:
                        bad.append(f"{path.name}: `{span}`: no command {words[0]}")
                    continue
                known = {s for a in cmd._actions for s in a.option_strings} | {"--tracker"}
                bad += [f"{path.name}: `{span}`: {words[0]} has no {flag}"
                        for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]*", span) if flag not in known]
        self.assertEqual(bad, [])


class Format(unittest.TestCase):
    def test_frontmatter_round_trip(self):
        for value in ["plain", "a: b", "[x]", '"q"', "# h", "x #y", " pad", "ünï", "", ["a", "b,c", 'd"e', "[f]"], []]:
            with self.subTest(value=value):
                line = markdown.render_frontmatter([], {"k": value})[0] if value != "" else "k: "
                meta, problems = markdown.parse_meta([line])
                self.assertEqual((meta["k"], problems), (value, []))

    def test_links_are_safe(self):
        html = viewer.md_inline('[a](javascript:alert(1)) [b](https://x.y/?q="1") <script> [c](evidence/r.txt)')
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="javascript', html)
        self.assertIn('href="https://x.y/?q=&quot;1&quot;"', html)
        self.assertIn('href="evidence/r.txt"', html)

    def test_gh_budget(self):
        with mock.patch.object(github.subprocess, "run") as run_gh:
            github.budget(0)  # spent: no call starts, so a hook ends before its timeout
            self.assertIsNone(github.gh("pr", "list"))
            github.budget(None)
        run_gh.assert_not_called()


def ticket(root: Path, ident: str, **meta) -> None:
    meta = {"id": ident, "title": ident, "status": "todo", **meta}
    front = "\n".join(f"{k}: {markdown.format_value(v)}" for k, v in meta.items())
    (root / "tickets" / f"{ident}.md").write_text(f"---\n{front}\n---\n\n## Plan\n\n## Carry forward\n\n## Links\n")


class MachineState(unittest.TestCase):
    def setUp(self):
        self.root = model.HOME / slug()
        (self.root / "tickets").mkdir(parents=True)
        (self.root / "README.md").write_text("---\ntitle: T\nrepo: [a/x, b/y]\n---\n")
        ticket(self.root, "T-1", status="done", branch="f1", repo="a/x")
        ticket(self.root, "T-2", status="in-progress", branch="f2", repo="a/x")
        ticket(self.root, "T-3", status="in-progress", branch="f3", repo="a/x", pr_state="open", base="f2")

    def state(self, data: dict) -> dict:
        (self.root / ".state.json").write_text(json.dumps(data))
        return model.Tracker(self.root).state()

    def test_keys_per_repo(self):
        entries = {"a/x:main": 1, "b/y:main": 2, "legacy": 3}
        self.assertEqual(model.branch_entry(entries, "b/y", "main"), 2)
        self.assertEqual(model.branch_entry(entries, "A/X", "main"), 1)
        self.assertEqual(model.branch_entry(entries, "b/y", "legacy"), 3)  # a bare key stands for any repo
        self.assertIsNone(model.branch_entry(entries, "c/z", "main"))
        model.put_entry(entries, "b/y", "legacy", 4)
        self.assertEqual(entries, {"a/x:main": 1, "b/y:main": 2, "b/y:legacy": 4})

    def test_stale_entries_go(self):
        now, old = int(time.time()), int(time.time()) - 30 * 86400
        state = self.state({
            "handoff": {"a/x:f1": {"at": now}, "a/x:f2": {"at": old}, "b/y:f1": {"at": now},
                        "a/x:scratch": {"at": old}},
            "synced": {"a/x:f1": {"at": now}, "a/x:f2": {"at": old}, "a/x:main": {"at": old}, "a/x:gone": {"at": old}},
            "use": {"/w@main": ["T-1"], "/v@main": ["T-2", "T-1"]},
            "pr_match": {"a/x:f9": {"at": old}},
            "last_sync": now,
        })
        # f1's tickets are closed: its handoff goes; the same branch in another repo is not f1's ticket's.
        self.assertEqual(set(state["handoff"]), {"a/x:f2", "b/y:f1"})
        # A mark is a baseline: it goes by age alone, unless the branch has an open ticket (main: T-2, by `use`).
        self.assertEqual(set(state["synced"]), {"a/x:f1", "a/x:f2", "a/x:main"})
        self.assertEqual(state["use"], {"/v@main": ["T-2"]})
        self.assertNotIn("pr_match", state)
        self.assertEqual(state["last_sync"], now)

    def test_stacked_pr_waits_while_open(self):
        tr = model.Tracker(self.root)
        t3 = tr.lookup("T-3")
        self.assertEqual([d.ident for d in tr.deps(t3)], ["T-2"])
        self.assertEqual([t.id for t in tr.waiting_on("T-2")], ["T-3"])
        t3.save({"base": "main"})  # the PR was retargeted
        self.assertEqual(tr.deps(t3), [])


def pull(number: int, **facts) -> dict:
    """A PR as the review query answers it (github.REVIEW_FIELDS)."""
    return {"isDraft": facts.get("draft", False), "reviewDecision": facts.get("decision"),
            "mergeStateStatus": facts.get("merge", "CLEAN"),
            "reviewRequests": {"nodes": [{"requestedReviewer": {"login": x}} for x in facts.get("requested", [])]},
            "latestReviews": {"nodes": facts.get("reviews", [])},
            "reviewThreads": {"nodes": [{"isResolved": False}] * facts.get("threads", 0) + [{"isResolved": True}]},
            "commits": {"nodes": [{"commit": {"committedDate": facts.get("pushed", "2026-09-01T00:00:00Z"),
                                              "statusCheckRollup": {"state": facts.get("checks", "SUCCESS")}}}]}}


class Moves(unittest.TestCase):
    """Whose move a ticket under way waits on: read from its PR by `sync`, and from its blockers."""

    def test_review_facts(self):
        bot = {"author": {"__typename": "Bot", "login": "lint"}, "state": "CHANGES_REQUESTED",
               "submittedAt": "2026-09-02T00:00:00Z"}
        rev = {"author": {"__typename": "User", "login": "rev"}, "state": "CHANGES_REQUESTED",
               "submittedAt": "2026-09-02T00:00:00Z"}
        facts = github.review_facts(pull(1, decision="CHANGES_REQUESTED", reviews=[bot, rev, {"author": None}],
                                         threads=2, pushed="2026-09-03T00:00:00Z"))
        self.assertEqual(facts, {"decision": "changes_requested", "changes": ["rev"], "reviewed": 1788307200,
                                 "pushed": 1788393600, "checks": "success", "merge": "clean", "threads": 2})

    def test_rules(self):
        root = model.HOME / slug()
        for d in ("tickets", "decisions"):
            (root / d).mkdir(parents=True)
        (root / "README.md").write_text("---\ntitle: T\nrepo: a/x\n---\n")
        (root / "decisions" / "D-01.md").write_text("---\nid: D-01\ntitle: Store\nstatus: open\nowner: Ryan\n---\n")
        facts = {1: {"merge": "dirty", "checks": "failure"}, 2: {"checks": "failure"},
                 3: {"decision": "changes_requested", "changes": ["rev"], "reviewed": 100, "pushed": 50, "threads": 2},
                 4: {"decision": "changes_requested", "changes": ["rev"], "reviewed": 100, "pushed": 200},
                 5: {"decision": "changes_requested", "requested": ["rev"]},
                 7: {"decision": "approved", "approved": ["rev"], "threads": 1},
                 8: {"decision": "approved", "checks": "pending"}, 9: {"decision": "approved", "merge": "behind"},
                 10: {"draft": True}, 12: {"decision": "review_required"}}
        (root / ".state.json").write_text(json.dumps({"reviews": {f"a/x#{n}": f for n, f in facts.items()}}))
        for n in [*facts, 11]:
            ticket(root, f"M-{n}", status="in-progress", pr=str(n), pr_state="draft" if n == 10 else "open")
        ticket(root, "M-6", status="in-progress", depends_on=["D-01"])
        ticket(root, "M-13", status="in-progress", depends_on=["EXT-1"])
        with open(root / "tickets" / "M-13.md", "a") as f:
            f.write("\n- Blocker: [EXT-1 Legal](https://x.y) — legal must sign off\n")
        ticket(root, "M-14", status="in-progress")
        ticket(root, "M-15")
        tr = model.Tracker(root)
        moves = {t.id: (m.text() if (m := model.whose_move(tr, t)) else None) for t in tr.tickets}
        self.assertEqual(moves, {
            "M-1": "you: merge conflicts", "M-2": "you: checks failing",
            "M-3": "you: changes requested by rev (2 unresolved review threads)",
            "M-4": "you: pushed since rev requested changes; review not re-requested",
            "M-5": "rev: review requested", "M-6": "Ryan: D-01 open",
            "M-7": "you: approved by rev; 1 unresolved review thread", "M-8": "CI: checks running",
            "M-9": "you: approved; branch behind its base", "M-10": "you: draft", "M-11": None,
            "M-12": "you: no review requested", "M-13": "EXT-1: legal must sign off", "M-14": "you: no PR yet",
            "M-15": None})
        lines = views.move_lines(tr)
        self.assertEqual(lines[1:3], ["  you: merge conflicts — M-1", "  you: checks failing — M-2"])  # most urgent
        self.assertEqual(lines[-1], "  not read from GitHub yet (`tracker sync`) — M-11")

    def test_sync_reads_reviews(self):
        s = slug()
        t = ("--tracker", s)
        run("init", s, "--title", "Reviewed", "--owner", "me", "--repo", "a/x")
        for i in (1, 2):
            run(*t, "new", f"S-{i}", "--title", f"Part {i}", "--branch", f"f{i}")
            run(*t, "set", f"S-{i}", "status=in-progress")
        lists = [{"number": 10 + i, "headRefName": f"f{i}", "state": "OPEN", "isDraft": i == 2, "mergedAt": None,
                  "baseRefName": "main", "updatedAt": ""} for i in (1, 2)]
        answers = {11: pull(11, requested=["rev"]), 12: pull(12, draft=True)}
        queries = []

        def fake_gh(*args, fields=None, partial=False):
            if args[:2] == ("pr", "list"):
                return lists
            queries.append(args[3])
            return {"data": {"r0": {f"p{n}": answers[n] for n in answers if f"p{n}:" in args[3]}}}

        with mock.patch.object(github, "gh", fake_gh):
            run(*t, "sync")
            self.assertEqual(len(queries), 1)  # the PRs were new: asked for once the lists named them
            tr = model.Tracker(model.HOME / s)
            self.assertEqual(set(tr.reviews), {"a/x#11", "a/x#12"})
            index = run(*t, "index")
            self.assertIn("  you: draft — S-2\n  rev: review requested — S-1", index)
            self.assertIn("move: rev — review requested", run(*t, "context", "S-1"))
            page = viewer.main_html(tr)
            self.assertIn('<span class="chip move s-you">you: draft</span>', page)
            self.assertIn("Now · 2 · 1 your move", page)

            seen = session.watch(tr, [tr.lookup("S-1")])
            answers[11] = pull(11, decision="APPROVED", reviews=[
                {"author": {"__typename": "User", "login": "rev"}, "state": "APPROVED", "submittedAt": ""}])
            run(*t, "sync")
            self.assertEqual(len(queries), 2)  # known PRs: asked for with the lists
            tr = model.Tracker(model.HOME / s)
            self.assertEqual(session.changes_since(tr, seen, session.watch(tr, [tr.lookup("S-1")])),
                             ["S-1 move now: you — approved by rev; ready to merge"])


    def test_stale_sync_refreshes_in_the_background(self):
        s, work = slug(), repo("feat/R-1")
        run("init", s, "--title", "Fresh", "--owner", "me", "--repo", "a/x")
        run("--tracker", s, "new", "R-1", "--title", "Fresh work", "--branch", "feat/R-1")
        m = session.match_cwd(work, tracker=model.Tracker(model.HOME / s))
        with mock.patch.object(hooks, "spawn") as spawn:
            hooks.refresh(m, {}, {})  # no ticket under way: nothing to learn
            run("--tracker", s, "set", "R-1", "status=in-progress", cwd=work)
            m = session.match_cwd(work, tracker=model.Tracker(model.HOME / s))
            fields = {}
            hooks.refresh(m, {}, fields)
            hooks.refresh(m, fields, {})  # asked already: not again within the interval
        spawn.assert_called_once_with("--tracker", s, "sync")


class Starts(unittest.TestCase):
    """Where a todo ticket's work can start: the default branch, or the branch of the work under way it waits on."""

    def test_start_points(self):
        s = slug()
        root = model.HOME / s
        for d in ("tickets", "decisions"):
            (root / d).mkdir(parents=True)
        (root / "README.md").write_text("---\ntitle: T\nrepo: a/x\n---\n")
        (root / "decisions" / "D-01.md").write_text("---\nid: D-01\ntitle: Store\nstatus: open\n---\n")
        ticket(root, "A-1", status="in-progress", branch="fa")
        ticket(root, "A-2", status="in-progress", branch="fb", pr="7", pr_state="open", base="fa")  # on A-1's branch
        ticket(root, "A-3", status="in-progress", branch="fa")
        ticket(root, "A-4", status="in-progress", branch="fc")
        ticket(root, "S-1", depends_on=["A-1"])
        ticket(root, "S-2", depends_on=["A-1", "A-2"])  # A-2's branch holds A-1's work: the top of the stack
        ticket(root, "S-3", depends_on=["A-1", "A-3"])  # one branch holds both
        ticket(root, "S-4", depends_on=["A-1", "A-4"])  # separate branches: someone chooses
        ticket(root, "R-1")
        ticket(root, "W-1", depends_on=["R-1"])  # waits on work not started
        ticket(root, "W-2", depends_on=["A-1", "D-01"])  # and on a decision
        tr = model.Tracker(root)
        starts = {t.id: (x.base.id if x.base else None, [b.id for b in x.apart]) for t, x in tr.startable()}
        self.assertEqual(starts, {"S-1": ("A-1", []), "S-2": ("A-2", []), "S-3": ("A-1", []),
                                  "S-4": (None, ["A-1", "A-4"]), "R-1": (None, [])})

        ready = run("--tracker", s, "ready")
        self.assertIn("S-1  —  S-1 — stack on fa (A-1 in-progress; you: no PR yet)\n", ready)
        self.assertIn("S-2  —  S-2 — stack on fb (A-2 in-review, #7)\n", ready)  # its PR's review not read yet
        self.assertIn("S-4  —  S-4 — A-1 (fa) and A-4 (fc) are on separate branches: stack on one, or wait", ready)
        self.assertIn("R-1  —  R-1 · unblocks W-1\n", ready)
        self.assertEqual([x.split()[0] for x in ready.splitlines()], ["R-1", "S-1", "S-2", "S-3", "S-4"])
        self.assertIn("Ready to start: R-1\nCan start stacked on work under way: S-1, S-3 on A-1; S-2 on A-2; S-4 on "
                      "A-1 or A-4", run("--tracker", s, "seq"))
        index = run("--tracker", s, "index")
        self.assertIn("[stacks on A-1] ", index)
        self.assertIn("[waits R-1] ", index)
        self.assertIn("start: stack on fa (A-1 in-progress; you: no PR yet)", run("--tracker", s, "context", "S-1"))
        self.assertIn("blocked by: A-1, D-01", run("--tracker", s, "context", "W-2"))
        self.assertIn("<dt>start</dt><dd>stack on fa (A-1 in-progress; you: no PR yet)</dd>",
                      viewer.main_html(model.Tracker(root)))  # the opened ticket on the page

    def test_start_checks_the_branch(self):
        s, work = slug(), repo("main")
        t = ("--tracker", s)

        def branch(*args: str) -> None:
            subprocess.run(["git", "-C", str(work), "checkout", "-q", *args], check=True)

        run("init", s, "--title", "Stacked", "--owner", "me")
        run(*t, "new", "B-1", "--title", "Base")
        run(*t, "new", "B-2", "--title", "On main", "--depends", "B-1")
        run(*t, "new", "B-3", "--title", "On the base", "--depends", "B-1")
        branch("-b", "feat/B-1")
        commit(work, "base work")
        run(*t, "set", "B-1", "status=in-progress", cwd=work)
        self.assertIn("B-2  —  On main — stack on feat/B-1 (B-1 in-progress", run(*t, "ready"))
        branch("-b", "feat/B-2", "main")
        self.assertIn("B-2 waits on work under way that branch feat/B-2 does not contain: feat/B-1 (B-1)",
                      run(*t, "set", "B-2", "status=in-progress", cwd=work))
        branch("-b", "feat/B-3", "feat/B-1")
        self.assertNotIn("does not contain", run(*t, "set", "B-3", "status=in-progress", cwd=work))


class LiveSessions(unittest.TestCase):
    """The viewer shows the Claude sessions that run on this machine and that `tracker start` tied to the tracker."""

    def test_live_sessions(self):
        s, n = slug(), time.monotonic_ns()
        t = ("--tracker", s)
        work, spare = repo("feat/L-1"), repo("main")
        run("init", s, "--title", "Watched", "--owner", "me")
        run(*t, "new", "L-1", "--title", "Under way", "--branch", "feat/L-1")
        run(*t, "new", "L-2", "--title", "Chosen")
        run(*t, "set", "L-1", "status=in-progress", cwd=work)
        gone = subprocess.Popen(["true"])
        gone.wait()  # a process that no longer runs
        claude = session.CLAUDE_SESSIONS
        claude.mkdir(parents=True, exist_ok=True)

        since = time.mktime((2026, 9, 1, 9, 5, 0, 0, 0, -1))

        def running(name: str, pid: int, status: str = "busy", tracker: str = s, cwd: Path = work, **fields) -> None:
            sid = f"{name}{n}"
            (claude / f"{sid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": "/", "name": name,
                                                            "status": status, "statusUpdatedAt": since * 1000}))
            if tracker:
                session.save_session(sid, tracker=tracker, cwd=str(cwd), **fields)

        running("agent-a", os.getpid())
        running("agent-b", os.getpid(), "idle", cwd=spare, focus=["L-2"])
        running("agent-c", gone.pid)  # stopped without removing its file
        running("agent-d", os.getpid(), tracker="other")
        running("agent-e", os.getpid(), tracker="")  # no `tracker start`
        (claude / f"bad{n}.json").write_text("{")
        tr = model.Tracker(model.HOME / s)
        self.assertEqual([x.name for x in session.live_sessions(s)], ["agent-a", "agent-b"])

        page = viewer.main_html(tr)
        self.assertRegex(page, r'data-id="_now-L-1"[^>]*><summary><span class="agent busy" role="img" '
                               r'aria-label="agent working" title="agent-a: working since Tue 09:05"></span>')
        self.assertRegex(page, rf'data-id="_now-agent-agent-b{n}"><summary><span class="agent" role="img" '
                               r'aria-label="agent idle" title="agent-b: idle since Tue 09:05"></span><b>agent-b</b>'
                               r'<span class=meta>main</span>'
                               r'<span class=nx>on <a class=id href="#L-2">L-2</a> todo</span>')
        self.assertIn(" · 2 agents</h2>", page)
        self.assertIn(f"<p class=meta>session agent-a: working since Tue 09:05, in {work}</p>", page)
        self.assertNotRegex(page, "agent-[cde]")

        before = viewer.version(tr).split(".")[0]
        running("agent-a", os.getpid(), "idle")  # the page swaps its content when a session's status changes
        self.assertNotEqual(viewer.version(tr).split(".")[0], before)


class Flow(unittest.TestCase):
    """The commands a session runs, from the plan to a paused branch, in a git repo on a ticket's branch."""

    def test_plan_decide_build_pause(self):
        s, work = slug(), repo("feat/T-2-api")
        t = ("--tracker", s)
        run("init", s, "--title", "Work", "--owner", "me")
        run(*t, "new", "T-1", "--title", "Schema")
        run(*t, "new", "T-2", "--title", "API", "--depends", "T-1")
        self.assertIn("dependency cycle", run(*t, "wait", "T-1", "on", "T-2", code=2))
        self.assertIn("Critical path (longest chain of unfinished tickets): T-1 → T-2", run(*t, "seq"))

        run(*t, "add", "T-1", "carry", "Table users(id) is the contract")
        run(*t, "add", "T-1", "carry", "Wrong fact")
        run(*t, "drop", "T-1", "carry", "Wrong")
        run(*t, "add", "T-1", "plan", "Make the users table")
        run(*t, "add", "T-1", "plan", "Make the users and roles tables", "--replace", "Make the users table")
        self.assertIn("Make the users and roles tables", run(*t, "context", "T-1"))
        self.assertIn("T-1: Table users(id) is the contract", run(*t, "context", "T-2"))
        self.assertNotIn("Wrong fact", run(*t, "context", "T-2"))

        self.assertIn("D-01 opened, blocks T-2", run(*t, "decide", "Auth scheme", "--blocks", "T-2"))
        done = run(*t, "step", "T-1", "--done", "users table", cwd=work)
        self.assertIn("T-1: done; feat/T-2-api not marked: T-1 is not on it", done)  # T-2's branch keeps its mark
        self.assertNotIn("nothing blocks", done)  # T-2 still waits on D-01
        self.assertIn("nothing blocks T-2 now", run(*t, "decide", "D-1", "--resolve", "JWT", "--by", "me"))
        context = run(*t, "context", "T-2")
        self.assertIn("D-01: Auth scheme → ", context)
        self.assertNotIn("Decided D-01", context)  # the log line says it again: left out

        run(*t, "set", "T-2", "status=in-progress", cwd=work)
        self.assertEqual(model.Tracker(model.HOME / s).lookup("T-2").get("branch"), "feat/T-2-api")
        run(*t, "synced", cwd=work)  # the first mark is the baseline: only later commits are new work
        commit(work, "api routes")
        step = run(*t, "step", "T-2", "Routes built", "--next", "Auth middleware", "--carry", "GET /users is public",
                   cwd=work)
        self.assertIn("T-2: next set, 1 carry forward, logged; feat/T-2-api up to date", step)
        self.assertIn("logged 1 commit(s)", step)
        run(*t, "step", "T-2", "--done", "api", "--pause", "half", cwd=work, code=2)  # a closed branch keeps no handoff
        run(*t, "step", "T-2", "--pause", "routes done; auth half done", cwd=work)
        here = run(*t, "here", cwd=work)
        self.assertIn("Handoff", here)
        self.assertIn("routes done; auth half done", here)
        self.assertIn("T-1 done ✓", here)
        self.assertEqual(here.count("T-1: Table users(id) is the contract"), 1)  # what T-2 builds on, once
        self.assertIn("D-01: Auth scheme → ", here)
        self.assertNotIn("✗", run(*t, "check"))
        log = (model.HOME / s / "log.md").read_text()
        self.assertIn("[T-2] Routes built", log)
        self.assertIn("[T-2] Commits on feat/T-2-api:", log)
        t2 = model.Tracker(model.HOME / s).lookup("T-2")
        self.assertEqual((t2.get("next"), t2.carry_forward), ("Auth middleware", ["GET /users is public"]))
        tr = model.Tracker(model.HOME / s)  # the viewer's page, rendered without its server
        page = viewer.page_html(tr.title, viewer.main_html(tr), s, viewer.version(tr))
        self.assertIn('data-id="T-2"', page)
        self.assertFalse(viewer.code_ready())  # the code on disk is the code running: no restart
        self.assertIn("Decided D-01 Auth scheme (me): JWT", log)


class Writes(unittest.TestCase):
    """What a write refuses, what it reports after itself, and the views that read records back."""

    def test_limits_check_and_show(self):
        s = slug()
        t = ("--tracker", s)
        run("init", s, "--title", "Limits", "--owner", "me")
        run(*t, "new", "L-1", "--title", "One")
        long = "word " * 50
        self.assertIn("over 200: one concrete action", run(*t, "set", "L-1", f"next={long}", code=2))
        self.assertIn("the log line is", run(*t, "log", long, code=2))
        self.assertIn("over 400", run(*t, "add", "L-1", "carry", long * 2, code=2))
        log = (model.HOME / s / "log.md").read_text()
        run(*t, "step", "L-1", "Short", "--carry", "fits", "--carry", long * 2, code=2)
        self.assertEqual((model.HOME / s / "log.md").read_text(), log)  # refused before any write

        for i in range(6):
            run(*t, "add", "L-1", "carry", f"Fact {i}")
        done = run(*t, "step", "L-1", "--done", "all of it")
        self.assertIn("added `tracker check` problems:\n⚠ L-1: done but Carry forward has 6 bullets", done)
        self.assertEqual(done.count("Carry forward has 6"), 1)
        self.assertNotIn("check", run(*t, "set", "L-1", "title=One thing"))  # the problem was there before

        shown = run(*t, "show", "L-1", "tracker", "--section", "carry,goal")
        self.assertIn("== L-1 · One thing · done\n## Carry forward\n\n- Fact 0", shown)
        self.assertIn("(no section 'goal')\n\n== README · Limits · planning\n(no section 'carry')\n\n## Goal", shown)
        whole = run(*t, "show", "l-01")
        self.assertIn("status: done", whole)
        self.assertNotIn("<!--", whole)

        run(*t, "decide", "Open one", "--refs", "L-1")
        run(*t, "decide", "Settled one", "--resolve", "yes", "--by", "me")
        listed = run(*t, "decisions")
        self.assertIn("Open one", listed)
        self.assertNotIn("Settled one", listed)
        self.assertIn("1 settled", listed)
        self.assertIn("Settled one", run(*t, "decisions", "--all"))


    def test_put_stdin_and_the_tracker_folder(self):
        s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("feat/P-1")
        t = ("--tracker", s)
        run("init", s, "--title", "Put", "--owner", "me")
        run(*t, "new", "P-1", "--title", "One", "--branch", "feat/P-1")
        run(*t, "new", "P-2", "--title", "Two")
        run(*t, "new", "P-3", "--title", "Three", "--depends", "P-1,P-2")
        self.assertIn("ready to start", run(*t, "wait", "P-3", "off", "P-1,P-2"))  # a comma list, as elsewhere

        carry = '- `api/x.py` holds the "contract"\n  - $HOME stays as it is\n- A second fact\n'
        with piped(carry):
            run(*t, "put", "P-1", "carry")
        self.assertIn(carry.strip(), run(*t, "show", "P-1", "--section", "carry"))
        self.assertNotIn("P-1", run(*t, "check"))  # its sections keep their order
        with piped(""):
            self.assertIn("put takes the section's whole new text", run(*t, "put", "P-1", "plan", code=2))
        with piped("- Nonsense: [x](https://x.y)\n"):
            self.assertIn("label 'Nonsense'", run(*t, "put", "P-1", "links", "-", code=2))

        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run("start", s, cwd=work)  # keeps the session's project directory
            with piped('Fixed the "quoted" `thing`\n'):
                run("step", "-", "--next", "Test it", cwd=work)  # no id: the session's one ticket
            with piped("Ship it"):
                run("set", "next=-", cwd=work)
            self.assertIn("only one text", run("step", "P-1", "-", "--next", "-", cwd=work, code=2))
            commit(work, "more")
            folder = model.HOME / s
            self.assertIn("feat/P-1 up to date", run("synced", cwd=folder))  # from the tracker folder: the project's
        self.assertIn("run it from the project", run(*t, "synced", cwd=folder, code=2))  # no session to ask
        tr = model.Tracker(model.HOME / s)
        self.assertEqual(tr.lookup("P-1").get("next"), "Ship it")
        self.assertIn('[P-1] Fixed the "quoted" `thing`', (tr.root / "log.md").read_text())


class Upgrade(unittest.TestCase):
    def test_migrate_brings_an_older_format_up_to_date(self):
        s = slug()
        run("init", s, "--title", "Old", "--owner", "me")
        readme = model.HOME / s / "README.md"
        readme.write_text(readme.read_text().replace(f"schema: {model.SCHEMA}\n", ""))
        self.assertIn("run `tracker migrate`", run("--tracker", s, "check"))
        (model.HOME / s / ".DS_Store").write_text("finder")
        run("--tracker", s, "migrate")
        self.assertNotIn("migrate", run("--tracker", s, "check"))
        backup = next((model.HOME / ".backups").glob(f"{s}-*"))
        self.assertTrue((backup / "README.md").exists())
        self.assertFalse((backup / ".DS_Store").exists())  # the backup holds the tracker, not Finder's files


def hook(event: str, sid: str, cwd: Path, env: dict | None = None, **extra) -> dict | None:
    """Run scripts/hook.sh for one event; its JSON output, or None when it adds nothing."""
    data = json.dumps({"session_id": sid, "cwd": str(cwd), "hook_event_name": event, **extra})
    out = subprocess.run([str(ROOT / "scripts/hook.sh"), event], input=data, capture_output=True, text=True,
                         check=True, env={**os.environ, **(env or {})}).stdout
    return json.loads(out) if out.strip() else None


def said(out: dict | None) -> str:
    return json.dumps(out or {})


class Hooks(unittest.TestCase):
    """scripts/hook.sh end to end: its shell filters, and what each event tells the model."""

    def test_session(self):
        s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("feat/H-1")
        run("init", s, "--title", "Hooked", "--owner", "me")
        run("--tracker", s, "new", "H-1", "--title", "Hook work")
        run("--tracker", s, "set", "H-1", "status=in-progress", cwd=work)

        # No tracker in the session: on a branch with an open ticket the model offers the link; nothing else runs.
        env_file = Path(BASE) / f"{sid}.env"  # what the session's Bash commands get: its id, and the CLI on PATH
        path = f"{Path(sys.executable).parent}:/usr/bin:/bin"
        offer = hook("session-start", sid, work, env={"CLAUDE_ENV_FILE": str(env_file), "PATH": path}, source="startup")
        self.assertIn("AskUserQuestion", said(offer))
        self.assertEqual(env_file.read_text(), f'export TRACKER_SESSION={sid}\nexport PATH="$PATH:{ROOT}/bin"\n')
        self.assertIsNone(hook("prompt", sid, work, prompt="hi"))
        self.assertIsNone(hook("stop", sid, work))
        run("start", "--decline", cwd=work)  # "Not now": no offer on this branch in the next sessions
        self.assertIsNone(hook("session-start", f"{sid}b", work, source="startup"))

        env = {**os.environ, "TRACKER_SESSION": sid}
        linked = subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True,
                                text=True, check=True).stdout
        self.assertIn("H-1", linked)
        brief = said(hook("session-start", sid, work, source="startup"))
        self.assertIn(f"[work-tracker] {s}", brief)
        self.assertIn("Tracker protocol", brief)
        self.assertIn(model.ISOLATION_RULE, brief)  # stated once, carried into every session with a tracker
        self.assertIn(model.ISOLATION_RULE, run("rules"))
        self.assertIn("H-1 in-progress", said(hook("prompt", sid, work, prompt="go")))
        self.assertIsNone(hook("post-bash", sid, work, tool_input={"command": "ls"}))
        go = {"questions": [{"question": "Push?", "options": [{"label": "Yes, push"}, {"label": "Not now"}]}]}
        self.assertIsNone(hook("answered", sid, work, tool_input=go))  # approval, not direction
        pick = {"questions": [{"question": "Store?", "options": [{"label": "Postgres"}, {"label": "Yes, SQLite"}]}]}
        self.assertIn("direction decision", said(hook("answered", sid, work, tool_input=pick)))

        helper = said(hook("subagent-start", sid, work, agent_id="a1", agent_type="Explore"))
        self.assertIn("do not write it", helper)
        self.assertIn(model.ISOLATION_RULE, helper)  # a subagent writes code and commits too

        # The hooks log every commit; the model is asked only whether `next` still holds, once per `next`.
        committed = {"tool_input": {"command": "git commit -m x"}}
        commit(work, "hook work")
        self.assertIsNone(hook("post-bash", sid, work, agent_id="a1", **committed))  # a subagent's: logged, no ask
        ask = said(hook("prompt", sid, work, prompt="done?"))  # the session is asked at its next message
        self.assertIn("1 commit(s) on feat/H-1 since `next` last changed", ask)
        self.assertIsNone(hook("prompt", sid, work, prompt="again"))
        commit(work, "more work")
        self.assertIsNone(hook("post-bash", sid, work, **committed))  # logged; `next` is the one already asked about
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            self.assertIn("H-1: next set; feat/H-1 up to date", run("step", "--next", "Ship it", cwd=work))
        commit(work, "last work")  # as in the terminal: no post-bash hook sees it
        self.assertIsNone(hook("stop", sid, work))  # logs it, and never blocks
        self.assertIn("1 logged commit(s) since `next` last changed", run("--tracker", s, "here", cwd=work))
        self.assertIn("H-1 next: Ship it", said(hook("prompt", sid, work, prompt="and now?")))  # a new `next`
        log = (model.HOME / s / "log.md").read_text()
        self.assertEqual([x.split(": ", 1)[1].split(" ", 1)[1] for x in log.splitlines() if "Commits on" in x],
                         ["hook work", "more work", "last work"])  # each once

    def test_commits_wait_for_a_ticket(self):
        s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("feat/W-1")
        run("init", s, "--title", "Waits", "--owner", "me")
        run("--tracker", s, "new", "W-1", "--title", "Wait work", "--branch", "feat/W-1")
        env = {**os.environ, "TRACKER_SESSION": sid}
        subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True, check=True)
        commit(work, "early work")
        self.assertIsNone(hook("post-bash", sid, work, tool_input={"command": "git commit"}))  # no ticket under way
        self.assertIn("No ticket in progress on feat/W-1; 1 commit(s) not logged",
                      said(hook("prompt", sid, work, prompt="go")))
        run("--tracker", s, "set", "W-1", "status=in-progress", cwd=work)
        self.assertIsNone(hook("stop", sid, work))
        self.assertIn("[W-1] Commits on feat/W-1: ", (model.HOME / s / "log.md").read_text())  # logged for it now

    def test_work_git_shows(self):
        s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("feat/G-1")
        run("init", s, "--title", "Git", "--owner", "me")
        run("--tracker", s, "new", "G-1", "--title", "Git work", "--branch", "feat/G-1")
        (work / "before.txt").write_text("uncommitted before the mark")
        env = {**os.environ, "TRACKER_SESSION": sid}
        subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True, check=True)
        self.assertIn("No ticket in progress", said(hook("prompt", sid, work, prompt="go")))  # the first message
        self.assertIsNone(hook("prompt", sid, work, prompt="more"))
        (work / "edited.txt").write_text("written by a Bash command: no hook saw it")
        told = said(hook("prompt", sid, work, prompt="again"))
        self.assertIn("No ticket in progress on feat/G-1; 1 uncommitted file(s) since", told)
        self.assertIsNone(hook("prompt", sid, work, prompt="and again"))  # once per mark
        self.assertIsNone(hook("edit", sid, work, tool_input={"file_path": str(work / "edited.txt")}))

        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):  # this session's own change: not reported
            run("set", "G-1", "status=in-progress", cwd=work)
        self.assertIsNone(hook("prompt", sid, work, prompt="4"))
        self.assertIn("G-1 in-progress; 1 uncommitted file(s)", said(hook("prompt", sid, work, prompt="5")))
        self.assertIn("The tracker may lag the work: 1 uncommitted file(s)", run("--tracker", s, "here", cwd=work))
        run("--tracker", s, "synced", cwd=work)
        self.assertNotIn("may lag", run("--tracker", s, "here", cwd=work))

    def test_one_ticket_of_a_busy_branch(self):
        s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("feat/busy")
        run("init", s, "--title", "Busy", "--owner", "me")
        for i in (1, 2, 3):
            run("--tracker", s, "new", f"B-{i}", "--title", f"Part {i}", "--branch", "feat/busy")
        for i in (1, 2):
            run("--tracker", s, "set", f"B-{i}", "status=in-progress", cwd=work)
        run("--tracker", s, "set", "B-3", "status=dropped", "summary=not needed")
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            self.assertIn("More than one ticket is under way here", run("start", s, cwd=work))
            self.assertIn("this session works on B-1, B-2", run("step", "Built a part", cwd=work, code=2))
            self.assertIn("--on takes open tickets: B-3 is dropped", run("start", s, "--on", "B-3", cwd=work, code=2))
            chosen = run("start", s, "--on", "B-2", cwd=work)
            self.assertIn("B-2 in-progress; also B-1 in-progress", chosen)
            self.assertNotIn("More than one ticket", chosen)
            run("step", "Built part 2", "--next", "Test it", cwd=work)
            run("set", "next=Ship it", cwd=work)
        self.assertIn("B-2 in-progress (next: Ship it)", said(hook("prompt", sid, work, prompt="go")))
        tr = model.Tracker(model.HOME / s)
        self.assertEqual((tr.lookup("B-1").get("next"), tr.lookup("B-2").get("next")), ("", "Ship it"))
        self.assertIn("[B-2] Built part 2", (tr.root / "log.md").read_text())


class Watch(unittest.TestCase):
    """`tracker watch`: what it reports, that a restart misses nothing and repeats nothing, and that only the user
    starts it."""

    def setUp(self):
        self.s = slug()
        self.t = ("--tracker", self.s)
        self.work = repo("feat/W-1")
        run("init", self.s, "--title", "Watched", "--owner", "me")
        for i in (1, 2, 3):
            run(*self.t, "new", f"W-{i}", "--title", f"Part {i}", *(["--depends", "W-1"] if i == 3 else []))
        self.root = model.HOME / self.s

    def watcher(self, sid: str = "") -> watcher.Watcher:
        w = watcher.Watcher(model.Tracker(self.root), sid)
        w.settle = 0  # a batch at once, not after the change settles
        return w

    def test_reports_what_changed(self):
        w, now = self.watcher(), time.time()
        first = w.claim(now)
        self.assertIn(f"watching {self.s}: 0 under way, 0 your move, 0 agent(s), 0 open decision(s)", first[0])
        self.assertIn("can start: W-1, W-2", first[-1])
        self.assertEqual(w.poll(now), [])

        run(*self.t, "set", "W-1", "status=in-progress", cwd=self.work)
        run(*self.t, "step", "W-1", "Built the parser", "--next", "Test it", cwd=self.work)
        model.append_log(model.Tracker(self.root), "Commits on feat/W-1: a1 one; b2 two; and 3 more", ["W-1"])
        run(*self.t, "decide", "Pick a store", "--refs", "W-2")
        lines = w.poll(now)
        w1 = next(x for x in lines if " W-1: " in x)
        for fact in ("todo → in-progress", "move: you: no PR yet", "Built the parser", "next: Test it", "+5 commits"):
            self.assertIn(fact, w1)
        self.assertNotIn("!", "".join(lines))  # the agent's own work: nothing needs the user
        self.assertIn("W-3: can start now: stack on feat/W-1", "".join(lines))
        self.assertIn("D-01: Opened D-01 Pick a store", "".join(lines))

        # A review comes back: the move is the user's.
        tr = model.Tracker(self.root)
        tr.lookup("W-1").save({"pr": "12", "pr_state": "open"})
        tr.save_state({**tr.raw_state(), "reviews": {"#12": {"decision": "changes_requested", "changes": ["rev"]}}})
        lines = w.poll(now)
        self.assertRegex(lines[0], r"^\d\d:\d\d ! W-1: in-progress → in-review · move: you: changes requested by rev$")
        self.assertEqual(w.poll(now), [])  # said once
        ticket(self.root, "W-9", depends_on=["W-99"])
        self.assertIn("! check: ", "".join(w.poll(now)))

    def test_restart(self):
        """A restart misses nothing and repeats nothing; one watcher per tracker."""
        w, now = self.watcher(), time.time()
        w.claim(now)
        w.release(now)
        run(*self.t, "set", "W-2", "next=Plan it")  # while no watcher runs
        again = self.watcher()
        caught = again.claim(now)
        self.assertEqual(len(caught), 1)
        self.assertIn("W-2: next: Plan it", caught[0])
        again.release(now)
        self.assertEqual(self.watcher().claim(now), [])  # nothing new
        self.assertIn("watching", self.watcher("other-session").claim(now)[0])  # another watcher starts afresh

        (self.root / "log.md").write_text("")  # rewritten by hand: nothing counts as new
        w = self.watcher("other-session")
        w.claim(now)
        self.assertEqual(w.poll(now), [])
        run(*self.t, "log", "A note", "--ref", "W-2")
        self.assertIn("W-2: A note", "".join(w.poll(now)))

        other = subprocess.Popen(["sleep", "30"])
        try:
            watcher.state_path(self.s).write_text(json.dumps({"pid": other.pid, "who": "terminal"}))
            with self.assertRaises(SystemExit) as refused, contextlib.redirect_stderr(io.StringIO()):
                self.watcher().claim(now)
            self.assertEqual(refused.exception.code, watcher.REFUSED)  # one watcher per tracker
        finally:
            other.kill()
            other.wait()

    def test_agents(self):
        run(*self.t, "set", "W-1", "status=in-progress", cwd=self.work)
        n, now = time.monotonic_ns(), time.time()
        claude = session.CLAUDE_SESSIONS
        claude.mkdir(parents=True, exist_ok=True)

        def running(name: str, status: str, since: float) -> Path:
            sid = f"{name}{n}"
            f = claude / f"{sid}.json"
            f.write_text(json.dumps({"pid": os.getpid(), "sessionId": sid, "name": name, "status": status,
                                     "statusUpdatedAt": since * 1000}))
            session.save_session(sid, tracker=self.s, cwd=str(self.work))
            return f

        running("api", "busy", now - 10)
        running("ui", "idle", now - watcher.IDLE_S - 60)
        w = self.watcher()
        first = "\n".join(w.claim(now))
        self.assertIn("! W-1 in-progress · move: you: no PR yet · agent api busy 0 min · agent ui idle 11 min", first)
        self.assertEqual(w.poll(now), [])  # the summary said it: the agents on W-1, and that ui waits

        late = now + watcher.BUSY_S + 1
        self.assertIn("agent api: busy 45 min; nothing recorded for W-1 in 45 min", "".join(w.poll(late)))
        self.assertEqual(w.poll(late + 1), [])  # once per spell
        extra = running("web", "busy", late)
        told = "\n".join(w.poll(late))
        self.assertIn("agent web: started on W-1", told)
        self.assertIn("W-1: 3 agents on it: api, ui, web", told)
        extra.unlink()
        self.assertIn("agent web: ended on W-1", "".join(w.poll(late)))

    def test_github(self):
        s, now = slug(), time.time()
        run("init", s, "--title", "Synced", "--owner", "me", "--repo", "a/x")
        w = watcher.Watcher(model.Tracker(model.HOME / s))
        failed = ["sync skipped: gh failed or timed out for a/x"]
        with mock.patch.object(watcher, "sync", return_value=failed):
            w.claim(now)
            self.assertIn("! github: sync skipped: gh failed", "".join(w.poll(now)))
            self.assertEqual(w.poll(now + watcher.SYNC_S), [])  # said once
        with mock.patch.object(watcher, "sync", return_value=[]):
            self.assertEqual(w.poll(now + 2 * watcher.SYNC_S), [])  # skipped for its interval: not proof it works
            tr = model.Tracker(model.HOME / s)
            tr.save_state({"last_sync": now + 1})
            self.assertIn("github: sync works again", "".join(w.poll(now + 3 * watcher.SYNC_S)))

    def test_only_the_user_starts_it(self):
        sid, cwd = f"sid{time.monotonic_ns()}", self.work
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            self.assertIn("starts only when the user asks", run("watch", self.s, "--once", code=4))
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):  # a Claude session whose hooks did not run
            run("watch", self.s, "--once", code=4)
        self.assertIsNone(hook("prompt", sid, cwd, prompt="/watchlist"))
        self.assertFalse(watcher.granted(sid))

        self.assertIsNone(hook("prompt", sid, cwd, prompt=f"/work-tracker:watch {self.s}"))  # only the user types it
        self.assertTrue(watcher.granted(sid))
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            out = run("watch", self.s, "--once")
            self.assertIn(f"watching {self.s}: 0 under way", out)
            self.assertIn(f"Next: `tracker watch {self.s} --once` again", out)
            run(*self.t, "log", "a note", code=4)  # it watches only
            run("start", self.s, code=4)
        page = viewer.main_html(model.Tracker(self.root))
        self.assertIn("Watched by session sid", page)  # between two runs of `--once` it still watches

        self.assertIsNone(hook("prompt", sid, cwd, prompt="/work-tracker:watch stop"))
        self.assertFalse(watcher.granted(sid))
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run(*self.t, "log", "a note")
        st = watcher.load_state(self.s)
        watcher.state_path(self.s).write_text(json.dumps({**st, "ended": time.time() - watcher.REARM_S - 1}))
        self.assertRegex(viewer.main_html(model.Tracker(self.root)), r'class="watch ended">Watch by session sid')

        worker = f"sid{time.monotonic_ns()}"
        subprocess.run([str(ROOT / "bin/tracker"), "start", self.s], cwd=cwd, env={**os.environ,
                       "TRACKER_SESSION": worker}, capture_output=True, check=True)
        self.assertIn("works on tracker", said(hook("prompt", worker, cwd, prompt="/work-tracker:watch")))
        self.assertFalse(watcher.granted(worker))  # a session on a tracker does the work


if __name__ == "__main__":
    unittest.main()
