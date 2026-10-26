#!/usr/bin/env python3
"""weekly_metrics.py - append one row per ISO week of four process measures to a TSV.

    python tools/weekly_metrics.py                  write the last complete week; nothing if its row exists
    python tools/weekly_metrics.py --week 2030-W03  print one week's row, write nothing
    python tools/weekly_metrics.py --week 2030-W03 --as-of 2030-01-20T20:00:00Z
                                                    the same, reading log records up to that moment only
    python tools/weekly_metrics.py --selftest       planted cases: each rule caught once and passed once

WHY THIS EXISTS. A process number measured once (median time to first edit, the share of your turns
that correct the agent, the share of commits that cite a ticket) says nothing about whether a rule
you added since changed it. This measures them weekly by the same methods and appends a row to a TSV,
so a change shows up as a trend. Run it from a scheduler (cron, launchd) at login or daily.

THE WEEK. Monday 00:00 to the next Monday 00:00 UTC. A run writes the last COMPLETE week: the first run
on or after a Monday writes the week before it, and every later run that week finds the row and exits.

INPUTS. Each is an argument, or a key under `metrics` in kit_config (the argument wins):
  --logs DIR            extra folders of Claude Code project logs, beside ~/.claude/projects
                        (metrics.log_roots, a list), e.g. logs copied from another machine
  --project-folder NAME the project folder(s) under projects/ whose turns count for corrections
                        (metrics.project_folders; default: the current project's)
  --skip-folder TEXT    project folders whose name holds TEXT are not sessions (a headless batch job)
                        and are skipped (metrics.skip_folders)
  --repo DIR, --branch B  the git repo and local branch whose commits are counted (metrics.repo,
                        metrics.branch; default branch HEAD). Unset: that column is "did not run".
  --ticket-re RE        what a ticket id looks like in a commit message (metrics.ticket_re)
  --check-dir DIR       a folder whose package.json has a `check` script (metrics.check_dir).
                        Unset: the gate columns are "did not run".
  --tsv FILE            where rows go (metrics.tsv; default ~/.claude/weekly-metrics.tsv)

THE COLUMNS. A count is N of M. A source that could not be read writes "did not run", never 0.
  ttfe_median_min, ttfe_bouts  Main session logs in every project folder of the log roots, split into
      bouts at a gap over 45 min. A bout's time is its first Edit/Write/NotebookEdit/MultiEdit minus
      its first human message, and the bout belongs to the week it started in. ttfe_bouts is bouts with
      an edit of bouts with a human message. The unit is a bout, not a session. An edit before the
      bout's first human message gives a negative time; that is kept, not hidden.
  corrections  A fixed regex of correction phrasing ("why did", "that's wrong", "you missed", ...)
      over human turns in the chosen project folders only.
  ticket_commits_by_committer_date  Non-merge commits on the local branch, in the week by committer
      date, whose full message cites a ticket id. Nothing is fetched.
  gates, gates_failing  Each command of the `npm run check` script, run on its own under your login
      shell (`$SHELL -lc`, so node on the login PATH is found). `npm run check` stops at the first
      failure, so it cannot give each gate. This is the working tree at the moment of the run,
      uncommitted files and whatever branch is checked out included. It says nothing about the rest
      of the week.

RULES THAT MATTER.
  - A forked or resumed session copies records of another session, timestamp and type unchanged,
    under a new session id. A record whose timestamp and type are in several files is read only from
    the file with the most records, and a turn with the same timestamp and text is counted once.
    Without this a fork counts its parent's history twice.
  - The default ticket pattern is (?<![0-9A-Za-z])#\\d{4,5}(?![0-9A-Fa-f]). A looser #\\d{3,5} also
    matches colours such as #111113 and #111.
  - A compaction summary ("This session is being continued...", flagged isCompactSummary) is not a
    human message. Counted as one, it can be half of a week's "human" turns.

BLIND SPOTS. Logs outside the log roots (cc_logs.py reads backups and zips; this does not), which
matters only for weeks older than Claude Code's log cleanup. Commits not yet on the local branch. A
gate that hangs past 10 minutes counts as did not run.

Read-only except for the TSV, which is written atomically under a lock. Prints counts only, never log
text. Exit 0 when the row was written or already there; 1 when it could not be written; 2 when it was
written but a source did not run.
"""
import argparse
import datetime as dt
import fcntl
import glob
import io
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

HOME = os.path.expanduser("~")
LOG_ROOT = os.path.join(HOME, ".claude", "projects")
TSV = os.path.join(HOME, ".claude", "weekly-metrics.tsv")
COLUMNS = ["week", "monday", "ttfe_median_min", "ttfe_bouts", "corrections", "ticket_commits_by_committer_date",
           "gates", "gates_failing", "written_utc"]
DNR = "did not run"

GAP = dt.timedelta(minutes=45)
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
GATE_TIMEOUT = 600
# The correction signal: phrasing a user types when the agent got something wrong.
SIG = re.compile(r"(wtf|\?!|why did|why do|why r|why is|why are|why even|why would|why not|why no|why the|are you sure|u sure|you sure|didn'?t you|did u |you didn'?t|u didn'?t|lmao|lmfao|\blol\b|huh|that'?s wrong|thats wrong|incorrect|not right|nope|isn'?t right|shouldn'?t you|shouldnt|but why|no need|nvm|i already|literally|you missed|u missed|again\?|still )", re.I)
TICKET_ID = re.compile(r"(?<![0-9A-Za-z])#\d{4,5}(?![0-9A-Fa-f])")


def parse_ts(s):
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def week_bounds(label):
    year, week = label.split("-W")
    start = dt.datetime.combine(dt.date.fromisocalendar(int(year), int(week), 1), dt.time(), dt.timezone.utc)
    return start, start + dt.timedelta(days=7)


def last_complete_week(now):
    """The week holding the moment seven days ago is always the last one that has ended."""
    year, week, _ = (now - dt.timedelta(days=7)).date().isocalendar()
    return f"{year}-W{week:02d}"


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join((b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _has_tool_result(content):
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def read_logs(roots, as_of=None, summaries_are_human=False, corr_folders=(), skip_folders=()):
    """Every main session log, read once. Returns (bouts, turns, files, unreadable), or None when no
    log root exists. A record after as_of is ignored, which is how a past moment is re-run.
    corr_folders: the project folder names whose human turns count for corrections. skip_folders:
    text that marks a folder as not a session. summaries_are_human=True counts compaction summaries
    as human turns (the naive reading, kept for comparison)."""
    if not any(os.path.isdir(r) for r in roots):
        return None
    events, turns, files, unreadable, size = [], [], 0, 0, {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for folder in sorted(os.listdir(root)):
            ttfe_on = not any(s in folder for s in skip_folders)
            corr_on = folder in corr_folders
            if not (ttfe_on or corr_on):
                continue
            for path in sorted(glob.glob(os.path.join(root, folder, "*.jsonl"))):
                stem = os.path.basename(path)[:-6]
                try:
                    f = io.open(path, encoding="utf-8", errors="replace")
                except OSError:
                    unreadable += 1
                    continue
                files += 1
                with f:
                    for ln, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            o = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(o, dict):
                            continue
                        raw = o.get("timestamp") or ""
                        t = parse_ts(raw)
                        if t is None or (as_of and t > as_of):
                            continue
                        kind = o.get("type")
                        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
                        content = msg.get("content")
                        summary = bool(o.get("isCompactSummary")) and not summaries_are_human
                        if ttfe_on:
                            human = edit = False
                            if kind == "assistant":
                                edit = any(isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS
                                           for b in (content if isinstance(content, list) else []))
                            elif kind == "user":
                                human = (not _has_tool_result(content) and not o.get("isMeta")
                                         and not summary and bool(_text(content)[:4000].strip()))
                            events.append((o.get("sessionId") or stem, ln, t, human, edit, (raw, kind), path))
                            size[path] = size.get(path, 0) + 1
                        if corr_on and kind == "user" and not o.get("isMeta") and not summary and not _has_tool_result(content):
                            txt = _text(content).strip()
                            if txt and not txt.startswith("<") and "system-reminder" not in txt[:200]:
                                turns.append((raw, t, txt))
    # A record copied into several files (a fork, a resumed copy) belongs to the file holding the
    # most records, so the shared history is cut into bouts once, where it is longest.
    owner = {}
    for e in events:
        cur = owner.get(e[5])
        if cur is None or (size[e[6]], e[6]) > (size[cur], cur):
            owner[e[5]] = e[6]
    return _bouts([e[:5] for e in events if owner[e[5]] == e[6]]), turns, files, unreadable


def _bouts(events):
    """[t0, first human message, first edit] per bout: a session's records in line order, cut at a gap
    over 45 min. Every record with a timestamp counts towards the gap."""
    events.sort(key=lambda e: (e[0], e[1]))
    out, cur, prev, prev_sid = [], None, None, None
    for sid, _, t, human, edit in events:
        if sid != prev_sid or t - prev > GAP:
            if cur:
                out.append(cur)
            cur = [t, None, None]
        if edit and cur[2] is None:
            cur[2] = t
        if human and cur[1] is None:
            cur[1] = t
        prev, prev_sid = t, sid
    if cur:
        out.append(cur)
    return out


def ttfe_week(bouts, start, end):
    """Minutes to first edit for each bout that edited, and the count of bouts with a human message,
    in [start, end). end None means every week."""
    vals, total = [], 0
    for t0, usr, edit in bouts:
        if usr is None or not (start <= t0 and (end is None or t0 < end)):
            continue
        total += 1
        if edit:
            vals.append((edit - usr).total_seconds() / 60)
    return vals, total


def corrections_week(turns, start, end):
    seen, n, c = set(), 0, 0
    for raw, t, txt in turns:
        if not start <= t < end or (raw, txt) in seen:
            continue
        seen.add((raw, txt))
        n += 1
        c += bool(SIG.search(txt)) and len(txt) < 4000
    return c, n


def ticket_count(repo, rev, start=None, end=None, pattern=TICKET_ID):
    """(matching, all) non-merge commits in rev, by committer date when a window is given, or None."""
    # -z ends each commit with NUL, which a message cannot hold; a message can hold a 0x1e.
    if not repo:
        return None
    try:
        r = subprocess.run(["git", "-C", repo, "log", "-z", rev, "--no-merges", "--format=%cI%n%B"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode:
        return None
    m = n = 0
    for rec in r.stdout.split("\x00"):
        if not rec.strip():
            continue
        when, _, body = rec.partition("\n")
        t = parse_ts(when)
        if start is not None and not (t and start <= t < end):
            continue
        n += 1
        m += bool(pattern.search(body))
    return m, n


def gate_name(cmd):
    m = re.search(r"scripts/([\w.-]+)\.mjs(.*)", cmd)
    return (m.group(1) + m.group(2)).strip() if m else cmd


def run_gates(site, commands=None):
    """[(name, exit code or None)] for each command of `npm run check`. None means it did not run:
    127 from the shell (command not found), a timeout, or a shell that would not start."""
    if commands is None:
        if not site:
            return None
        try:
            with open(os.path.join(site, "package.json"), encoding="utf-8") as f:
                check = json.load(f)["scripts"]["check"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        commands = [c.strip() for c in check.split("&&") if c.strip()]
    out = []
    for cmd in commands:
        try:
            rc = subprocess.run([os.environ.get("SHELL") or "/bin/sh", "-lc", cmd], cwd=site, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=GATE_TIMEOUT).returncode
        except (OSError, subprocess.TimeoutExpired):
            rc = None
        out.append((gate_name(cmd), None if rc == 127 else rc))
    return out


def gate_columns(results):
    if not results:
        return DNR, DNR
    ran = [r for r in results if r[1] is not None]
    if not ran:
        return DNR, "; ".join(f"{n} (did not run)" for n, _ in results)
    gates = f"{sum(1 for _, rc in ran if rc == 0)} of {len(ran)} pass"
    if len(ran) < len(results):
        gates += f"; {len(results) - len(ran)} did not run"
    bad = [f"{n} (exit {rc})" for n, rc in ran if rc] + [f"{n} (did not run)" for n, rc in results if rc is None]
    return gates, "; ".join(bad) or "none"


def measure(label, roots, as_of=None, repo=None, branch="HEAD", site=None, gate_commands=None,
            corr_folders=(), skip_folders=(), pattern=TICKET_ID):
    start, end = week_bounds(label)
    row = {"week": label, "monday": start.date().isoformat()}
    logs = read_logs(roots, as_of, corr_folders=corr_folders, skip_folders=skip_folders)
    if logs is None or not logs[2]:
        row["ttfe_median_min"] = row["ttfe_bouts"] = row["corrections"] = DNR
    else:
        bouts, turns, _, _ = logs
        vals, total = ttfe_week(bouts, start, end)
        row["ttfe_median_min"] = f"{statistics.median(vals):.1f}" if vals else ("no edits" if total else "no bouts")
        row["ttfe_bouts"] = f"{len(vals)} of {total}"
        c, n = corrections_week(turns, start, end)
        row["corrections"] = f"{c} of {n}"
    a = ticket_count(repo, branch, start, end, pattern)
    row["ticket_commits_by_committer_date"] = DNR if a is None else f"{a[0]} of {a[1]}"
    row["gates"], row["gates_failing"] = gate_columns(run_gates(site, gate_commands))
    row["written_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    unreadable = logs[3] if logs else 0
    return row, unreadable


def _weeks_in(path):
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    return lines[0] if lines else "", {ln.split("\t", 1)[0] for ln in lines[1:] if ln.strip()}


def write_row(path, row):
    """'written', 'present' or 'header differs'. Re-checks under the lock so two runs write once."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    header = "\t".join(COLUMNS)
    lock = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = header + "\n"
        if os.path.exists(path):
            first, weeks = _weeks_in(path)
            if first != header:
                return "header differs"
            if row["week"] in weeks:
                return "present"
            with open(path, encoding="utf-8") as f:
                old = f.read()
            if not old.endswith("\n"):
                old += "\n"
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".weekly-metrics.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(old + "\t".join(row[c] for c in COLUMNS) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return "written"
    finally:
        os.close(lock)


def selftest():
    """Planted logs, repo, gates and TSV. Each case is a rule that must hold; the run prints N of M."""
    checks = []

    def check(name, got, want):
        checks.append((got == want, name, got, want))

    def rec(kind, ts, sid, content=None, tool=None, **extra):
        o = {"type": kind, "timestamp": ts, "sessionId": sid, **extra}
        if kind == "assistant":
            o["message"] = {"role": "assistant", "content": [{"type": "tool_use", "name": tool, "input": {}}] if tool else []}
        else:
            o["message"] = {"role": "user", "content": content}
        return json.dumps(o)

    with tempfile.TemporaryDirectory() as td:
        root = os.path.join(td, "projects")
        desk = os.path.join(root, "-home-user-project")
        tcc = os.path.join(root, "-tmp-batch-clean-x1")
        os.makedirs(desk)
        os.makedirs(tcc)
        s = [
            rec("system", "2026-09-28T09:59:58Z", "S"),
            rec("user", "2026-09-28T10:00:00Z", "S", "please fix the header"),
            rec("user", "2026-09-28T10:01:00Z", "S", "<command-name>/go</command-name>"),
            rec("user", "2026-09-28T10:02:00Z", "S", "why did you skip the footer", isMeta=True),
            rec("assistant", "2026-09-28T10:05:00Z", "S", tool="Edit"),
            rec("user", "2026-09-28T10:06:00Z", "S", [{"type": "tool_result", "content": "ok"}]),
            rec("user", "2026-09-28T10:10:00Z", "S", "why did you skip the footer"),
            rec("user", "2026-09-28T11:30:00Z", "S", "thanks, looks good"),
            rec("user", "2026-09-28T13:00:00Z", "S", "This session is being continued. Still open: why did you",
                isCompactSummary=True),
        ]
        with open(os.path.join(desk, "S.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(s) + "\n")
        # The fork leaves out the parent's first record, so its copy of the bout starts 2 s later.
        with open(os.path.join(desk, "F.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(x.replace('"sessionId": "S"', '"sessionId": "F"') for x in s[1:]) + "\n")
        with open(os.path.join(tcc, "T.jsonl"), "w", encoding="utf-8") as f:
            f.write(rec("user", "2026-09-28T09:00:00Z", "T", "You are cleaning a transcript") + "\n"
                    + rec("assistant", "2026-09-28T09:00:30Z", "T", tool="Write") + "\n")
        opts = dict(corr_folders={"-home-user-project"}, skip_folders=("batch-clean",))
        bouts, turns, files, _ = read_logs([root], **opts)
        start, end = week_bounds("2026-W40")
        vals, total = ttfe_week(bouts, start, end)
        check("ttfe: 5 min; fork copy, skipped batch folder and summary-only bout not counted", ([round(v, 1) for v in vals], total), ([5.0], 2))
        check("corrections: '<...>', isMeta, tool_result and summary turns skipped, fork turns once", corrections_week(turns, start, end), (1, 3))
        old = read_logs([root], summaries_are_human=True, **opts)
        check("naive rules: the summary is a human turn and a bout", (corrections_week(old[1], start, end), ttfe_week(old[0], start, end)[1]), ((2, 4), 3))
        check("corrections: another week is empty", corrections_week(turns, *week_bounds("2026-W39")), (0, 0))
        check("logs: no root is did not run", read_logs([os.path.join(td, "absent")]), None)
        cut = read_logs([root], parse_ts("2026-09-28T10:04:00Z"), **opts)
        check("as-of: an edit after the cut is not seen", ttfe_week(cut[0], start, end), ([], 1))

        check("ticket: id matches", bool(TICKET_ID.search("Fix the nav. Ticket #1380.")), True)
        check("ticket: hex colour #111113 does not", bool(TICKET_ID.search("background #111113 stays")), False)
        check("ticket: hex colour #111 does not", bool(TICKET_ID.search("identical to the old #111; the CTA")), False)
        check("ticket: hex colour #1234ab does not", bool(TICKET_ID.search("border #1234ab")), False)
        check("ticket: two digits do not", bool(TICKET_ID.search("item #12")), False)
        repo = os.path.join(td, "repo")
        git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", repo], check=True)
        for day, m in [(28, "Nav: fix it\n\nTicket #1380."), (29, "Colour: keep #111113 and #111"),
                       (30, "Paste with a \x1e byte\n\nTicket #1400")]:
            env = dict(os.environ, GIT_COMMITTER_DATE=f"2026-09-{day}T12:00:00Z", GIT_AUTHOR_DATE=f"2026-09-{day}T12:00:00Z")
            subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", m], check=True, env=env)
        check("ticket: N of M in the week, a 0x1e in a message is one commit", ticket_count(repo, "HEAD", start, end), (2, 3))
        check("ticket: a bad repo is did not run", ticket_count(os.path.join(td, "nope"), "HEAD"), None)
        check("ticket: no repo set is did not run", ticket_count(None, "HEAD"), None)

        res = run_gates(td, ["true", "sh -c 'exit 3'", "no-such-command-weekly-metrics"])
        check("gates: pass, fail and did-not-run told apart", gate_columns(res),
              ("1 of 2 pass; 1 did not run", "sh -c 'exit 3' (exit 3); no-such-command-weekly-metrics (did not run)"))
        check("gates: no package.json is did not run", gate_columns(run_gates(td)), (DNR, DNR))
        check("gates: no check folder set is did not run", gate_columns(run_gates(None)), (DNR, DNR))

        check("week: Monday 12:30 writes the week before", last_complete_week(dt.datetime(2026, 10, 5, 12, 30, tzinfo=dt.timezone.utc)), "2026-W40")
        check("week: Sunday 23:00 still the week before", last_complete_week(dt.datetime(2026, 10, 11, 23, 0, tzinfo=dt.timezone.utc)), "2026-W40")
        check("week: the next Monday moves on", last_complete_week(dt.datetime(2026, 10, 12, 0, 30, tzinfo=dt.timezone.utc)), "2026-W41")

        tsv = os.path.join(td, "m", "weekly-metrics.tsv")
        row = {c: "x" for c in COLUMNS}
        row["week"] = "2026-W40"
        first = write_row(tsv, row)
        second = write_row(tsv, row)
        with open(tsv, encoding="utf-8") as f:
            n_lines = len(f.read().splitlines())
        check("tsv: written once, then present, header once", (first, second, n_lines), ("written", "present", 2))
        with open(tsv, "w", encoding="utf-8") as f:
            f.write("week\tsomething else\n")
        check("tsv: a changed header refuses", write_row(tsv, row), "header differs")

    for ok, name, got, want in checks:
        if not ok:
            print(f"FAIL  {name}: got {got!r}, want {want!r}")
    hits = sum(c[0] for c in checks)
    print(f"selftest: {hits} of {len(checks)} planted cases behaved")
    return 0 if hits == len(checks) else 1


def config(*keys):
    try:
        import kit_config
        return kit_config.get("metrics", *keys)
    except ImportError:
        return None


def as_list(v):
    return [] if v is None else [v] if isinstance(v, str) else list(v)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--week", help="print this week's row (YYYY-Www), write nothing")
    ap.add_argument("--as-of", help="with --week: ignore log records after this UTC moment")
    ap.add_argument("--logs", action="append", default=[], help="an extra folder of project logs (repeatable)")
    ap.add_argument("--project-folder", action="append", default=[],
                    help="a project folder name whose turns count for corrections (repeatable)")
    ap.add_argument("--skip-folder", action="append", default=[], help="skip project folders holding this text")
    ap.add_argument("--repo", help="git repo whose commits are counted")
    ap.add_argument("--branch", help="local branch in --repo (default HEAD)")
    ap.add_argument("--ticket-re", help="regex for a ticket id in a commit message")
    ap.add_argument("--check-dir", help="folder whose package.json has a `check` script")
    ap.add_argument("--tsv", help="the TSV rows go to (default ~/.claude/weekly-metrics.tsv)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    roots = [LOG_ROOT] + [os.path.expanduser(d) for d in (a.logs or as_list(config("log_roots")))]
    folders = set(a.project_folder or as_list(config("project_folders")))
    if not folders:
        import cc_logs
        folders = {os.path.basename(cc_logs.project_dir())}
    pattern = re.compile(a.ticket_re or config("ticket_re")) if (a.ticket_re or config("ticket_re")) else TICKET_ID
    repo, check_dir = a.repo or config("repo"), a.check_dir or config("check_dir")
    kw = dict(repo=os.path.expanduser(repo) if repo else None, branch=a.branch or config("branch") or "HEAD",
              site=os.path.expanduser(check_dir) if check_dir else None, corr_folders=folders,
              skip_folders=tuple(a.skip_folder or as_list(config("skip_folders"))), pattern=pattern)
    tsv = os.path.expanduser(a.tsv or config("tsv") or TSV)
    if a.week:
        row, unreadable = measure(a.week, roots, as_of=parse_ts(a.as_of) if a.as_of else None, **kw)
        print("\t".join(COLUMNS))
        print("\t".join(row[c] for c in COLUMNS))
        return 2 if unreadable or any(DNR in v for v in row.values()) else 0
    label = last_complete_week(dt.datetime.now(dt.timezone.utc))
    if os.path.exists(tsv) and label in _weeks_in(tsv)[1]:
        print(f"weekly_metrics {label}: already in {tsv}")
        return 0
    row, unreadable = measure(label, roots, **kw)
    status = write_row(tsv, row)
    print(f"weekly_metrics {label}: {status}; ttfe {row['ttfe_median_min']} min ({row['ttfe_bouts']} bouts), "
          f"corrections {row['corrections']}, tickets {row['ticket_commits_by_committer_date']}, gates {row['gates']}"
          + (f"; {unreadable} logs unreadable" if unreadable else ""))
    if status not in ("written", "present"):
        return 1
    return 2 if unreadable or any(DNR in v for v in row.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
