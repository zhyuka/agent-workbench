#!/usr/bin/env python3
"""scheduled_audits.py - run the read-only workspace audits unattended and log one line per audit.

    python tools/scheduled_audits.py              run every audit, append to the log, print one summary line
    python tools/scheduled_audits.py --log PATH   append somewhere else (a scratch file, for a trial run)
    python tools/scheduled_audits.py --selftest   planted audits: clean, findings, can't-run, crash, hang, missing
    python tools/scheduled_audits.py --watchdog   what is meant to run by itself and has stopped; exit 1 if any

WHY THIS EXISTS. An audit that runs only when someone remembers to run it drifts: its argument
parser breaks, or a check keeps failing against a structure nobody has any more, and nobody notices.
Run this daily from a scheduler (templates/launchd/ has a macOS example), so the record of what each
audit returned builds up on its own.

WHAT IT RUNS. The AUDITS list below: the workspace_audit.py checks, `skill_lint.py`,
`agent_gate_stale.py`, `skill_use.py` and `tool_time.py --audit`, each with this tools folder as its
working directory and its own timeout. Each reads and writes nothing (tool_time keeps its own flags
file). `scheduled_audits.extra` in the agent-workbench config adds your own: a list of
[name, script, args...] with the script in this folder, or [name, script, args..., true] to run it
in a login shell (for a token that only a login shell loads). For example
["figma pins", "figma_comments.py", "pins", "--new", "--lane", "batch", true] and the same with
`stale` list your open Figma threads pinned to a deleted or hidden node, or quiet 45 days, each red
once.
An audit's output is kept only when its result is not 0 (see below).

WHAT IT WRITES. One line per audit, appended to the log and fsynced as each audit finishes. The log
is `scheduled_audits.log` in config, else ~/Library/Logs/WorkspaceAudits.log on macOS and
~/.local/state/agent-workbench/WorkspaceAudits.log elsewhere:

    <run start, ISO time> TAB <audit> TAB <result>

<result> is the exit code when it is 0 (clean) or 1 (findings). Anything else is `did not run
(<why>)`: the script is missing, it timed out, it crashed (a Python traceback on stderr, which also
exits 1 and would otherwise read as findings), it exited 2, or it exited with any other code. A
check that did not run is never logged as clean. Every line of one run carries the same start time.

When an audit's result is not 0, its stdout and stderr also go to <audit>.last.txt in a folder beside
the log named after it (WorkspaceAudits/), written atomically and replaced on that audit's next
non-zero run. Spaces in the audit's name become underscores (workspace_audit_cites.last.txt). Its
first line repeats the log line, so the two can be matched. The log itself stays one line per audit.

NOT RUN. `workspace_audit.py secrets`, on purpose: an unattended run must never risk putting a
credential into a log. Run it by hand when you need it.

UPKEEP AND DIGEST. A log nobody reads fixes nothing. So the run also:
  - runs `upkeep.py cleanup` FIRST, the one step that writes (only what is merged, regenerable or
    backed up; upkeep.py says what and how it proves it), so the audits see the cleaned state;
  - rewrites today.md in the same folder: what is red with its failing lines, what the cleanup
    removed, the 15 oldest requests the request ledger (ledger.py) still holds open, then the last
    7 days of push-back counts from pushback_score.py. Read it first thing, or have a skill read it;
  - posts ONE desktop notification when an audit is red or did not run.

WATCHDOG. A job that never starts writes no failure, so nothing notices one stopping. `--watchdog`
prints what has stopped: this log older than 26 hours; the mirror's log (`mirror.log` in config)
older than 3 hours, when one is configured; on macOS, any LaunchAgent whose label starts with
`scheduled_audits.agent_prefix` that is not loaded, a KeepAlive one not running, or one listed in
`scheduled_audits.exit_matters` whose last exit was not 0. desktop_mirror.py calls it every hour and
this script after each run, so each sees the other stop; both post one notification per problem a
day (watchdog.json beside the digest). An agent paused on purpose is disabled in launchd
(`launchctl disable gui/$UID/<label>`), and the watchdog skips it. Unloading alone is not a pause:
launchd loads every plist in ~/Library/LaunchAgents again at the next login.

Exit 0 when every audit was clean; 1 when any audit had findings; 2 when none had findings but at
least one did not run, or the log could not be written.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import kit_config  # noqa: E402
from desktop_mirror import LOG_DIR, notify  # noqa: E402


def _cfg(key, default=None):
    v = kit_config.get("scheduled_audits", key)
    return default if v in (None, "", [], {}) else v


LOG = os.path.expanduser(_cfg("log", os.path.join(LOG_DIR, "WorkspaceAudits.log")))
TIMEOUT = 600

WORKSPACE_CHECKS = ("cites", "archive", "naming", "tools", "root", "xrefs", "map", "config")
AUDITS = [(f"workspace_audit {c}", ["workspace_audit.py", c]) for c in WORKSPACE_CHECKS] + [
    ("skill_lint", ["skill_lint.py"]),
    ("agent_gate_stale", ["agent_gate_stale.py"]),
    ("skill_use", ["skill_use.py"]),
    ("tool_time", ["tool_time.py", "--audit"]),
]


def configured_audits():
    """AUDITS plus scheduled_audits.extra: [name, script, args..., (true = login shell)]."""
    out = list(AUDITS)
    for row in _cfg("extra", []):
        row = list(row)
        login = bool(row and row[-1] is True)
        if login:
            row = row[:-1]
        if len(row) >= 2:
            out.append((str(row[0]), [str(x) for x in row[1:]]) + ((True,) if login else ()))
    return out


CLEANUP_JSON, DIGEST = "upkeep-cleanup.json", "today.md"


def with_upkeep(audits, folder):
    """upkeep cleanup first, then the audits."""
    return [("upkeep cleanup", ["upkeep.py", "cleanup", "--json", os.path.join(folder, CLEANUP_JSON)])] + list(audits)


def run_one(argv, cwd, timeout, login=False):
    """(result, output) for one audit. result is '0', '1', or 'did not run (<why>)'; output is the
    audit's stdout and stderr, or None when it never finished. login runs it in `zsh -lc`."""
    script = os.path.join(cwd, argv[0])
    if not os.path.isfile(script):
        return f"did not run (missing {argv[0]})", None
    cmd = [sys.executable, script] + argv[1:]
    if login:
        cmd = ["/bin/zsh", "-lc", shlex.join(cmd)]
    try:
        p = subprocess.run(cmd, cwd=cwd, timeout=timeout,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace")
    except subprocess.TimeoutExpired:
        return f"did not run (timeout after {timeout}s)", None
    except OSError as e:
        return f"did not run (could not start: {type(e).__name__})", None
    output = f"--- stdout\n{p.stdout}\n--- stderr\n{p.stderr}"
    if "Traceback (most recent call last)" in p.stderr:
        return f"did not run (crashed, exit {p.returncode})", output
    if p.returncode in (0, 1):
        return str(p.returncode), output
    return f"did not run (exit {p.returncode})", output


def output_path(log, name):
    """Where an audit's output from its last non-zero run goes: <log without .log>/<audit>.last.txt,
    or <log>.d/ when the log has no extension. A space or other character outside letters, digits
    and ._- in the audit's name becomes an underscore."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    base, ext = os.path.splitext(log)
    return os.path.join(base if ext else log + ".d", safe + ".last.txt")


def write_atomic(path, text):
    """tmp file in the same folder, fsync, os.replace: a reader never sees half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(audits, log, cwd, timeout, alert=None):
    """Run each audit, appending its line as it finishes, then write the digest and post one
    notification through alert(message). Returns (results, exit code)."""
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    started = time.time()
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        os.makedirs(os.path.dirname(output_path(log, "x")), exist_ok=True)
        f = open(log, "a", encoding="utf-8")
    except OSError as e:
        print(f"scheduled_audits: DID NOT RUN, cannot write {log}: {type(e).__name__}")
        return [], 2
    results, outputs = [], {}
    with f:
        for name, argv, *login in audits:
            result, output = run_one(argv, cwd, timeout, login=bool(login and login[0]))
            results.append((name, result))
            outputs[name] = output
            f.write(f"{stamp}\t{name}\t{result}\n")
            f.flush()
            os.fsync(f.fileno())
            if result != "0":
                out = output_path(log, name)
                try:
                    write_atomic(out, f"{stamp}\t{name}\t{result}\n"
                                 + (output if output is not None else "(no output: the audit did not finish or did not start)\n"))
                except OSError as e:
                    print(f"scheduled_audits: could not write {out}: {type(e).__name__}")
    found = sum(r == "1" for _, r in results)
    missed = [n for n, r in results if r not in ("0", "1")]
    rc = 1 if found else 2 if missed else 0
    home = os.path.expanduser("~") + os.sep
    shown = "~/" + log[len(home):] if log.startswith(home) else log
    print(f"scheduled_audits: {stamp} {len(results)} audits, {sum(r == '0' for _, r in results)} clean, "
          f"{found} with findings, {len(missed)} did not run"
          + (f" ({', '.join(missed)})" if missed else "") + f". Log {shown}")
    folder = os.path.dirname(output_path(log, "x"))
    try:
        text, message = digest(stamp, results, outputs, log, started)
        write_atomic(os.path.join(folder, DIGEST), text)
        if message and alert:
            alert(message)
    except OSError as e:
        print(f"scheduled_audits: could not write the digest: {type(e).__name__}")
    return results, rc


AGENTS = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")
AGENT_PREFIX = _cfg("agent_prefix")              # None: no LaunchAgents are watched
EXIT_MATTERS = tuple(_cfg("exit_matters", []))    # agents whose non-zero last exit is a problem
STALE_HOURS = 26
MIRROR_LOG = os.path.expanduser(kit_config.get("mirror", "log") or "") or None
MIRROR_STALE_HOURS = 3   # the mirror runs every hour, one line each run
WATCH_STATE = "watchdog.json"


def last_run(log):
    """The newest run start in the log, as a datetime, or None."""
    newest = None
    try:
        with open(log, encoding="utf-8") as f:
            for line in f:
                try:
                    t = datetime.datetime.fromisoformat(line.split("\t", 1)[0])
                except ValueError:
                    continue
                newest = t if newest is None or t > newest else newest
    except OSError:
        pass
    return newest


def agent_state(label):
    """{"running": bool, "last_exit": int or None} from `launchctl print`, or None when launchd does
    not know the label (never loaded, or booted out)."""
    if sys.platform != "darwin":
        return None
    p = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True, timeout=20)
    if p.returncode:
        return None
    code = re.search(r"^\tlast exit code = (-?\d+)", p.stdout, re.M)
    return {"running": "\tstate = running" in p.stdout, "last_exit": int(code.group(1)) if code else None}


def our_agents(folder=AGENTS, prefix=None):
    """{label: keeps_alive} for every plist whose name starts with the prefix, so a new agent is watched
    without a list edit. No prefix configured: {}."""
    import plistlib
    prefix = prefix if prefix is not None else AGENT_PREFIX
    found = {}
    if not prefix:
        return found
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if name.startswith(prefix) and name.endswith(".plist"):
            try:
                with open(os.path.join(folder, name), "rb") as f:
                    found[name[:-6]] = plistlib.load(f).get("KeepAlive") is True
            except (OSError, ValueError, plistlib.InvalidFileException):
                found[name[:-6]] = False
    return found


def parse_disabled(text):
    """The labels `launchctl print-disabled` marks disabled (`"label" => disabled`, or `=> true`)."""
    return set(re.findall(r'^\s*"([^"]+)" => (?:disabled|true)\s*$', text, re.M))


def disabled_agents():
    """Labels disabled in the gui domain: paused on purpose, so not a stopped job."""
    if sys.platform != "darwin":
        return set()
    p = subprocess.run(["launchctl", "print-disabled", f"gui/{os.getuid()}"], capture_output=True, text=True, timeout=20)
    return parse_disabled(p.stdout) if p.returncode == 0 else set()


def watchdog(log, now, state=agent_state, mirror_log=None, agents=AGENTS, disabled=disabled_agents, prefix=None,
             exit_matters=None):
    """{key: problem} for the jobs that are meant to run by themselves; a job that never starts writes
    no failure anywhere. An agent disabled in launchd is paused on purpose and skipped."""
    problems = {}
    mirror_log = MIRROR_LOG if mirror_log is None else mirror_log
    prefix = AGENT_PREFIX if prefix is None else prefix
    exit_matters = EXIT_MATTERS if exit_matters is None else exit_matters
    newest = last_run(log)
    if newest is None or now - newest > datetime.timedelta(hours=STALE_HOURS):
        problems["daily-run"] = ("the daily audits have not run since "
                                 + (newest.strftime("%Y-%m-%d %H:%M") if newest else "the log began"))
    if mirror_log:
        try:
            mirrored = datetime.datetime.fromtimestamp(os.path.getmtime(mirror_log))
        except OSError:
            mirrored = None
        if mirrored is None or now - mirrored > datetime.timedelta(hours=MIRROR_STALE_HOURS):
            problems["desktop-mirror"] = ("the mirror has not run since "
                                          + (mirrored.strftime("%Y-%m-%d %H:%M") if mirrored else "its log began"))
    if not prefix:
        return problems
    found = our_agents(agents, prefix)
    if not found:
        problems["agents"] = f"no {prefix}* LaunchAgent found in {agents}"
    paused = disabled() if found else set()
    for label, keeps_alive in found.items():
        if label in paused:
            continue
        s = state(label)
        if s is None:
            problems[label] = f"{label} is not loaded"
        elif keeps_alive and not s["running"]:
            problems[label] = f"{label} is not running"
        elif label in exit_matters and s["last_exit"] not in (0, None):
            problems[label] = f"{label} last exited {s['last_exit']}"
    return problems


def watchdog_alert(log=LOG, alert=None, now=None, state=agent_state, mirror_log=None, agents=AGENTS,
                   disabled=disabled_agents, prefix=None, exit_matters=None):
    """Post one notification for problems not alerted in the last 24 hours; a problem that clears
    is forgotten, so it alerts again if it comes back. Returns the problems."""
    now = now or datetime.datetime.now()
    problems = watchdog(log, now, state, mirror_log, agents, disabled, prefix, exit_matters)
    path = os.path.join(os.path.dirname(output_path(log, "x")), WATCH_STATE)
    try:
        with open(path, encoding="utf-8") as f:
            sent = json.load(f)
    except (OSError, ValueError):
        sent = {}
    due = [k for k in problems if now.timestamp() - sent.get(k, 0) >= 86400]
    sent = {k: (now.timestamp() if k in due else v) for k, v in sent.items() if k in problems}
    sent.update({k: now.timestamp() for k in due})
    write_atomic(path, json.dumps(sent))
    if due and alert:
        alert("; ".join(problems[k] for k in due) + f". See {os.path.dirname(log)}")
    return problems


def failing_lines(output, limit=6):
    """The lines of an audit's output that say what failed: FAIL / N of M / DIFFERS / UNINDEXED and
    the `- ` detail lines under them."""
    keep = [l.rstrip() for l in (output or "").splitlines()
            if re.search(r"\bFAIL\b|\b\d+ of \d+\b|\bDIFFERS\b|\bUNINDEXED\b|\bMISMATCH\b|^\s+- ", l)
            and not re.match(r"\s*PASS\b", l)]
    return keep[:limit] + ([f"... {len(keep) - limit} more lines"] if len(keep) > limit else [])


def fresh_json(path, started):
    """The JSON an upkeep step wrote during THIS run, or None (missing, stale, or unreadable)."""
    try:
        if os.path.getmtime(path) < started:
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


LEDGER_DIR = None          # None: ledger.py's folder (ledger.dir); the selftest plants its own
OPEN_SHOWN = 15


def open_requests_section(short, ledger_dir=None, limit=OPEN_SHOWN):
    """The oldest open requests from the request ledger (ledger.py, run on a schedule), for today.md.
    A ledger that is missing or unreadable says so; it never reads as none open."""
    try:
        sys.path.insert(0, TOOLS)
        import ledger
        d = ledger_dir or LEDGER_DIR or ledger.LEDGER_DIR
        path = os.path.join(d, "requests.jsonl")
        if not os.path.isfile(path):
            return [f"- the request ledger did not run: there is no {short(path)} (tools/ledger.py writes it)"]
        rows = ledger.open_requests(d)
        written = int((time.time() - os.path.getmtime(path)) / 60)
    except Exception as e:       # a broken ledger is reported, and the rest of today.md still gets written
        return [f"- the request ledger could not be read ({type(e).__name__}), so open requests are unknown"]
    out = [f"- {len(rows)} requests you typed that nothing has settled yet, oldest first (ledger written {written} min "
           f"ago). {short(os.path.join(d, 'DIGEST.md'))} has them all, with your standing answers"]
    now = time.time()
    for e in rows[:limit]:
        t = ledger.parse_ts(e.get("ts") or "")
        days = f"{int((now - t.timestamp()) // 86400)}d" if t else "?"
        said = ledger.own_words(e.get("text") or "")
        said = said if len(said) <= 140 else said[:139] + "…"
        where = (e.get("title") or e["session"][:8])[:50]
        card = f" (card: {(e.get('question') or '')[:50]})" if e.get("source") == "card" else ""
        out.append(f"    {(e.get('ts') or '')[:10]} ({days}) {where}: \"{said}\"{card}")
    if len(rows) > limit:
        out.append(f"    and {len(rows) - limit} newer: python tools/ledger.py --open")
    return out


def pushback_section(short, ledger_dir=None):
    """The last 7 days of push-back counts, from pushback_score.section() on the ledger. Its own lines say
    "did not run" for a missing or unreadable ledger; this catches the rest."""
    d = ledger_dir or LEDGER_DIR
    try:
        if d is None:
            import ledger
            d = ledger.LEDGER_DIR
        sys.path.insert(0, TOOLS)
        import pushback_score
        return pushback_score.section(short, ledger=os.path.join(d, "requests.jsonl"))
    except Exception as e:
        return [f"- did not run ({type(e).__name__})"]


def digest(stamp, results, outputs, log, started):
    """(today.md text, notification message or None)."""
    folder = os.path.dirname(output_path(log, "x"))
    home = os.path.expanduser("~") + os.sep
    short = lambda p: "~/" + p[len(home):] if p.startswith(home) else p
    got = dict(results)
    audits = [(n, r) for n, r in results if not n.startswith("upkeep ")]
    red = [(n, r) for n, r in results if r != "0"]
    out = [f"# Workspace audits: {stamp}", "",
           f"{len(audits)} audits: {sum(r == '0' for _, r in audits)} clean, {sum(r == '1' for _, r in audits)} "
           f"with findings, {sum(r not in ('0', '1') for _, r in audits)} did not run. "
           + "; ".join(f"{n}: {'ran' if got[n] == '0' else got[n]}" for n in got if n.startswith("upkeep ")), ""]
    out.append("## Needs a look")
    if not red:
        out.append("- nothing: every audit was clean and every upkeep step ran")
    for n, r in red:
        out.append(f"- **{n}**: {'findings' if r == '1' else r}")
        out += [f"    {l.strip()}" for l in failing_lines(outputs.get(n))]
        out.append(f"    full output: {short(output_path(log, n))}")
    cleanup = fresh_json(os.path.join(folder, CLEANUP_JSON), started)
    out += ["", "## Cleaned up"]
    if cleanup is None:
        out.append("- upkeep cleanup did not run, so nothing was cleaned")
    else:
        for st in cleanup:
            out.append(f"- {st['line']}")
            out += [f"    kept {k}" for k in st.get("kept", [])[:3]]
            if len(st.get("kept", [])) > 3:
                out.append(f"    and {len(st['kept']) - 3} more kept")
    out += ["", "## Open requests"] + open_requests_section(short)
    try:
        out += ["", "## Push-backs, last 7 days"] + pushback_section(short)
    except Exception as e:      # a broken ledger module is reported, and the rest of today.md still gets written
        out += ["", "## Push-backs, last 7 days", f"- did not run ({type(e).__name__})"]
    out += ["", f"Log: {short(log)}. Written by scheduled_audits.py; the next run replaces it.", ""]
    parts = []
    if any(r == "1" for _, r in audits):
        parts.append(f"{sum(r == '1' for _, r in audits)} audits red")
    if any(r not in ("0", "1") for _, r in red):
        parts.append(f"{sum(r not in ('0', '1') for _, r in red)} did not run")
    message = ("; ".join(parts) + f". See {short(os.path.join(folder, DIGEST))}") if parts else None
    return "\n".join(out), message


def selftest():
    global LEDGER_DIR
    ok = True

    def check(label, cond):
        nonlocal ok
        ok = ok and cond
        print(("PASS " if cond else "FAIL ") + label)

    plants = {
        "clean.py": "import sys; sys.exit(0)\n",
        "finds.py": "import sys; print('1 of 2'); sys.exit(1)\n",
        "cant.py": "import sys; print('nothing to audit', file=sys.stderr); sys.exit(2)\n",
        "crash.py": "raise RuntimeError('boom')\n",
        "hang.py": "import time; time.sleep(30)\n",
        "odd.py": "import sys; sys.exit(3)\n",
    }
    with tempfile.TemporaryDirectory() as t:
        for name, src in plants.items():
            with open(os.path.join(t, name), "w", encoding="utf-8") as f:
                f.write(src)
        log = os.path.join(t, "logs", "audits.log")
        audits = [(n[:-3], [n]) for n in plants] + [("gone", ["gone.py"])]
        results, rc = run(audits, log, t, timeout=2)
        got = dict(results)
        check("exit 0 is logged as 0", got["clean"] == "0")
        check("exit 1 is logged as 1", got["finds"] == "1")
        check("exit 2 is logged as did not run", got["cant"] == "did not run (exit 2)")
        check("a traceback is did not run, not findings", got["crash"] == "did not run (crashed, exit 1)")
        check("a hang is cut off and logged as did not run", got["hang"] == "did not run (timeout after 2s)")
        check("an unknown exit code is did not run", got["odd"] == "did not run (exit 3)")
        check("a missing script is did not run", got["gone"] == "did not run (missing gone.py)")
        check("findings outrank did-not-run in the exit code", rc == 1)
        with open(log, encoding="utf-8") as f:
            lines = f.read().splitlines()
        check("one tab-separated line per audit, one start time",
              len(lines) == 7 and all(len(l.split("\t")) == 3 for l in lines)
              and len({l.split("\t")[0] for l in lines}) == 1)
        kept = output_path(log, "finds")
        text = open(kept, encoding="utf-8").read() if os.path.isfile(kept) else ""
        check("a non-zero audit's output is kept beside the log", "1 of 2" in text)
        check("a clean audit's output is not kept", not os.path.exists(output_path(log, "clean")))

        _, rc = run([("clean", ["clean.py"]), ("cant", ["cant.py"])], log, t, timeout=2)
        check("no findings plus a did-not-run exits 2", rc == 2)
        _, rc = run([("clean", ["clean.py"])], log, t, timeout=2)
        check("all clean exits 0", rc == 0)
        with open(log, encoding="utf-8") as f:
            check("later runs append, earlier lines stay", len(f.read().splitlines()) == 10)

        blocked = os.path.join(t, "clean.py", "audits.log")
        _, rc = run([("clean", ["clean.py"])], blocked, t, timeout=2)
        check("an unwritable log exits 2", rc == 2)

        # The digest and the notification, with a planted upkeep step that writes its JSON.
        plants2 = {"up_clean.py": "import sys, json; json.dump([{'line': 'worktree-prune: removed 1 of 2', "
                                  "'kept': ['wt-a (uncommitted changes)']}], open(sys.argv[2], 'w'))\n",
                   "up_none.py": "import sys; sys.exit(0)\n",
                   # Exits 3 unless a login shell read the planted .zprofile below.
                   "up_login.py": "import os, sys\nsys.exit(0 if os.environ.get('PLANTED_LOGIN') == '1' else 3)\n"}
        for name, src in plants2.items():
            with open(os.path.join(t, name), "w", encoding="utf-8") as f:
                f.write(src)
        log2 = os.path.join(t, "logs2", "audits.log")
        folder = os.path.dirname(output_path(log2, "x"))
        sent = []
        led = os.path.join(t, "ledger")
        os.makedirs(led)
        planted = [{"id": f"q{i}", "ts": f"2026-10-{1 + i:02d}T09:00:00Z", "session": f"s{i}", "title": f"Planted {i}",
                    "source": "typed", "kind": "request", "state": "open", "text": f"planted open ask {i}."}
                   for i in range(17)]
        planted.append({"id": "qd", "ts": "2026-10-01T08:00:00Z", "session": "sd", "source": "typed",
                        "kind": "request", "state": "done", "ref": "1234abc", "text": "planted done ask"})
        with open(os.path.join(led, "requests.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(x) for x in planted) + "\n")
        LEDGER_DIR = led
        login_ok = None
        if os.path.exists("/bin/zsh"):
            with open(os.path.join(t, ".zprofile"), "w", encoding="utf-8") as f:
                f.write("export PLANTED_LOGIN=1\n")
            zdotdir = os.environ.get("ZDOTDIR")
            os.environ["ZDOTDIR"] = t          # zsh -l reads this .zprofile, not yours
            try:
                login_ok = dict(run([("login", ["up_login.py"], True)], os.path.join(t, "logs3", "a.log"), t,
                                    timeout=20)[0]).get("login") == "0"
            finally:
                if zdotdir is None:
                    os.environ.pop("ZDOTDIR", None)
                else:
                    os.environ["ZDOTDIR"] = zdotdir
            check("a login-shell step runs in zsh -l (it read the planted .zprofile)", login_ok)
        results, _ = run([("upkeep cleanup", ["up_clean.py", "--json", os.path.join(folder, CLEANUP_JSON)]),
                          ("finds", ["finds.py"]), ("cant", ["cant.py"])],
                         log2, t, timeout=20, alert=sent.append)
        md = open(os.path.join(folder, DIGEST), encoding="utf-8").read() if os.path.isfile(os.path.join(folder, DIGEST)) else ""
        check("the digest names a red audit with its failing line", "**finds**: findings\n    1 of 2" in md)
        check("the digest names an audit that did not run", "**cant**: did not run (exit 2)" in md)
        check("the digest carries the cleanup lines and what was kept",
              "worktree-prune: removed 1 of 2" in md and "kept wt-a (uncommitted changes)" in md)
        check("one notification names red and did-not-run",
              len(sent) == 1 and "1 audits red" in sent[0] and "1 did not run" in sent[0])
        check("the digest lists the 15 oldest open requests with your words, counts the rest, skips a done one",
              "## Open requests\n- 17 requests you typed" in md
              and 'Planted 0: "planted open ask 0."' in md and 'Planted 14: "planted open ask 14."' in md
              and "planted open ask 15" not in md and "and 2 newer" in md and "planted done ask" not in md
              and md.index("## Open requests") < md.index("Log: "))
        check("the push-back section follows Open requests",
              "## Push-backs, last 7 days\n" in md
              and md.index("## Open requests") < md.index("## Push-backs") < md.index("Log: "))
        LEDGER_DIR = os.path.join(t, "no-ledger")
        sent.clear()
        run([("upkeep cleanup", ["up_none.py"]), ("clean", ["clean.py"])], log2, t, timeout=20, alert=sent.append)
        md = open(os.path.join(folder, DIGEST), encoding="utf-8").read()
        check("a step that wrote no JSON this run shows as did not run, not last run's numbers",
              "upkeep cleanup did not run" in md)
        check("a missing request ledger reads as did not run, not as no open requests or no push-backs",
              "## Open requests\n- the request ledger did not run" in md
              and "## Push-backs, last 7 days\n- did not run" in md)
        LEDGER_DIR = None
        check("all clean posts no notification", not sent)

        # The watchdog: a stale log, a stale mirror or a stopped keep-alive agent alerts once a day.
        wlog = os.path.join(t, "wd", "audits.log")
        os.makedirs(os.path.dirname(wlog))
        with open(wlog, "w", encoding="utf-8") as f:
            f.write("2026-10-18T08:00:05\tskill_lint\t0\n")
        mlog = os.path.join(t, "wd", "mirror.out.log")
        open(mlog, "w").close()
        import plistlib
        plists = os.path.join(t, "wd", "LaunchAgents")
        os.makedirs(plists)
        P = "local.me."
        for label, extra in ((P + "serve", {"KeepAlive": True}), (P + "planted-exit", {"StartInterval": 300}),
                             (P + "daily", {"StartCalendarInterval": {"Hour": 8}}), ("com.other.thing", {"KeepAlive": True})):
            with open(os.path.join(plists, label + ".plist"), "wb") as f:
                plistlib.dump({"Label": label, **extra}, f)

        def at(when, mirrored_minutes_ago=10):
            stamp = (when - datetime.timedelta(minutes=mirrored_minutes_ago)).timestamp()
            os.utime(mlog, (stamp, stamp))
            return when

        def states(**over):
            base = {"serve": {"running": True, "last_exit": None},
                    "planted_exit": {"running": False, "last_exit": 0},
                    "daily": {"running": False, "last_exit": 1}}
            base.update(over)
            return lambda label: base.get(label.replace(P, "").replace("-", "_"))
        up = states()
        got = []

        def wd(when, state=up, minutes=10):
            return watchdog_alert(wlog, got.append, at(when, minutes), state, mlog, plists, set, P,
                                  (P + "planted-exit",))
        p = wd(datetime.datetime(2026, 10, 20, 9, 0))
        check("watchdog: a log 49 hours old alerts",
              set(p) == {"daily-run"} and len(got) == 1 and "2026-10-18 08:00" in got[0])
        wd(datetime.datetime(2026, 10, 20, 11, 0))
        check("watchdog: the same problem does not alert again within a day", len(got) == 1)
        wd(datetime.datetime(2026, 10, 21, 9, 30))
        check("watchdog: it alerts again after a day", len(got) == 2)
        with open(wlog, "a", encoding="utf-8") as f:
            f.write("2026-10-21T08:00:05\tskill_lint\t0\n")
        p = wd(datetime.datetime(2026, 10, 21, 10, 0))
        check("watchdog: a fresh log, a recent mirror, every agent loaded and a daily job that exited 1 is quiet",
              not p and len(got) == 2)
        p = wd(datetime.datetime(2026, 10, 21, 10, 0),
               states(serve={"running": False, "last_exit": -9}, daily=None, planted_exit={"running": False, "last_exit": 1}))
        check("watchdog: a stopped keep-alive agent, an unloaded agent and an exit_matters agent exiting 1 each alert",
              set(p) == {P + "serve", P + "daily", P + "planted-exit"} and len(got) == 3
              and "serve is not running" in got[2] and "daily is not loaded" in got[2] and "last exited 1" in got[2])
        check("watchdog: only plists with the configured prefix are watched, and KeepAlive is read from each",
              our_agents(plists, P) == {P + "daily": False, P + "planted-exit": False, P + "serve": True})
        p = wd(datetime.datetime(2026, 10, 21, 10, 0), minutes=210)
        check("watchdog: a mirror log 210 minutes old alerts",
              set(p) == {"desktop-mirror"} and len(got) == 4 and "mirror has not run since 2026-10-21 06:30" in got[3])
        p = watchdog(wlog, datetime.datetime(2026, 10, 21, 10, 0), up, mlog, os.path.join(t, "wd", "none"), set, P)
        check("watchdog: no LaunchAgents folder is a problem, not a clean bill", "agents" in p)
        p = watchdog(wlog, datetime.datetime(2026, 10, 21, 10, 0), up, "", plists, set, "")
        check("watchdog: with no mirror log and no agent prefix configured, only the daily log is watched",
              not p)
        unloaded = states(daily=None)
        check("watchdog: an unloaded agent disabled in launchd is paused, not a problem",
              not watchdog(wlog, at(datetime.datetime(2026, 10, 21, 10, 0)), unloaded, mlog, plists,
                           lambda: {P + "daily"}, P))
        check("watchdog: an unloaded agent alerts when only another one is disabled",
              set(watchdog(wlog, at(datetime.datetime(2026, 10, 21, 10, 0)), unloaded, mlog, plists,
                           lambda: {P + "serve"}, P)) == {P + "daily"})
        check("watchdog: print-disabled is read for disabled and true, never enabled or false",
              parse_disabled('\tdisabled services = {\n\t\t"local.me.a" => disabled\n\t\t"local.me.b" => enabled\n'
                             '\t\t"local.me.c" => true\n\t\t"local.me.d" => false\n\t}\n')
              == {"local.me.a", "local.me.c"})

    check("secrets is never in the scheduled list", not any("secrets" in a for _, argv, *_ in AUDITS for a in argv))
    plan = with_upkeep([("an audit", ["a.py"])], "")
    check("the daily plan is cleanup, then the audits", [s[0] for s in plan] == ["upkeep cleanup", "an audit"])
    check("every scheduled script exists here",
          all(os.path.isfile(os.path.join(TOOLS, argv[0])) for _, argv, *_ in with_upkeep(AUDITS, "")))
    print("selftest", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="scheduled_audits.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=LOG, help=f"file to append to (default {LOG})")
    ap.add_argument("--timeout", type=int, default=TIMEOUT, help=f"seconds per audit (default {TIMEOUT})")
    ap.add_argument("--selftest", action="store_true",
                    help="planted audits: clean, findings, can't-run, crash, hang, missing")
    ap.add_argument("--watchdog", action="store_true",
                    help="print what is meant to run by itself and has stopped; exit 1 if anything has")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.watchdog:
        found = watchdog(a.log, datetime.datetime.now())
        agents = our_agents()
        paused = sorted(set(agents) & disabled_agents())
        print("\n".join(found.values())
              or "watchdog: the daily run is current"
                 + (" and so is the mirror" if MIRROR_LOG else "")
                 + (f"; {len(agents) - len(paused)} agents loaded, each keep-alive one running" if AGENT_PREFIX else "")
                 + (f"; paused on purpose: {', '.join(paused)}" if paused else ""))
        sys.exit(1 if found else 0)
    rc = run(with_upkeep(configured_audits(), os.path.dirname(output_path(a.log, "x"))), a.log, TOOLS, a.timeout,
             alert=lambda m: notify(m, "Workspace audits"))[1]
    # After the run, so its own fresh line is in the log: this call watches the mirror and the agents.
    try:
        for problem in watchdog_alert(a.log, alert=lambda m: notify(m, "Workspace watchdog")).values():
            print(f"watchdog: {problem}")
    except Exception as e:
        print(f"watchdog: DID NOT RUN ({type(e).__name__}: {e})")
    sys.exit(rc)
