#!/usr/bin/env python3
"""session_close.py - can one Claude Code session be archived, read from its own log.

    python tools/session_close.py                    the newest main log under ~/.claude/projects
    python tools/session_close.py --session a8c4b868 a session by id or id prefix
    python tools/session_close.py --as-of 2030-01-15T18:01Z   the log read only up to that time
    python tools/session_close.py --calibrate FILE   re-run a labelled set of sessions, N of M on each side
    python tools/session_close.py --selftest         planted logs: each hard item owes, a soft one never does

WHY THIS EXISTS. "Am I good to archive this session?" is a question about facts in its log, and an
answer rebuilt from memory of the session misses some. A verdict that blocks on every line it lists
blocks nearly every session, including the ones you archive on a yes, so nobody reads it. This one
splits what blocks an archive (HARD) from what it only lists (SOFT). Whether the work itself is
finished stays a judgement.

THE VERDICT is the last line: "can archive", or "owes: <hard items>". If a hard check did not run
(a git call timed out), the line reads "not checked: ..." and never "can archive".

HARD, each one blocks an archive:
  edits      this session's own uncommitted edits: a file it changed with Edit or Write whose uncommitted
             diff adds at least half the lines one of its edits added (or, for an edit that only deleted,
             removes them), counting only lines HEAD doesn't already hold. A file dirty only with other
             sessions' later lines, or gone from disk, is soft. An edit with no line long enough to match
             counts as its own. Scratch paths (/tmp, /private/tmp) are left out
  commits    commits its own `git commit` printed that a local branch still holds and no remote has.
             Pushed means on a remote branch, or once held by a remote-tracking ref (its reflog, so a
             branch rewritten after the push is not owed), or the same patch on a remote commit (git
             patch-id, so a rebase or cherry-pick is not owed). A repo with no remote is soft. A commit
             made with -q and nothing printing its sha is not seen
  background background commands and agents it started with no completion notice in the log (a task
             notice, or a subagent's hand-back) and no TaskStop
  peers      questions (a "?") from other sessions (<cross-session-message>, delivered through the
             queue) with no SendMessage back to that session afterwards, addressed by its name,
             session id or uds socket; acks and FYIs need none
  worktrees  a worktree it added with `git worktree add` and never removed with `git worktree remove`
             in its log, whose folder is still on disk. A path held in a variable set outside the command
             is not read; a worktree the app made for it is not seen

SOFT, listed and never blocking:
  others     files it edited that are dirty only with other sessions' later changes
  drafts     drafts files it edited: items there wait on you to send or post. Which files are drafts
             files is yours to say: --drafts NAME (repeatable) or the config key session_close.drafts,
             a list of file names or path endings such as "outbound-drafts.md". With neither set, this
             check lists nothing
  promises   sentences in its last PROMISE_WINDOW replies that put work off and name no home for it
             (a commit sha, a ticket, a drafts file, the ledger, a card, a decision or a spawned task).
             A promise older than those replies is not seen
  no repo    changed files in no git repo, and commits in a repo with no remote

AS OF. --as-of reads the log up to that time, so background, peers and promises are as of then;
commits are pushed or not as of then by the remote-tracking reflogs (as of now in a repo that has
none). Edits, whether a worktree's folder exists and a patch-id match are read as of now.

CALIBRATION (--calibrate FILE). Trust the verdict once it has read known cases right. FILE is JSON:
{"cases": [{"session": <log id or prefix>, "label": "good" or "bad", "at": <ISO time>,
"owed": <what was owed, for a bad case>, "verdict": <the last measured verdict, optional>}]}.
Known-good: sessions you archived after saying yes, run as of the archive. Known-bad: sessions where
you or a later message showed work still owed after a "can archive" claim, run as of the claim. Build
it from your own logs; it prints N of M on each side, names each miss, and counts the verdicts that
differ from the saved ones.

Exit 0 can archive, 1 owes, 2 the log could not be found or read, or a hard check did not run.
--calibrate exits 0 once it has printed, 2 when FILE can't be read.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
SCRATCH = ("/private/tmp/", "/tmp/", "/private/var/folders/")
GIT_TIMEOUT = 60
BG_ID = re.compile(r"running in background with ID: (\w+)|agentId: (a[0-9a-f]{8,})")
DONE = re.compile(r"<task-notification>\s*<task-id>(\w+)</task-id>.*?<status>(\w+)</status>", re.S)
# A session's own promise: a sentence that puts work off. A sentence that names where the work went is
# accounted for.
DEFERS = re.compile(
    r"follow-?ups?\b|separate (change|commit|session|pass|fix|task)|in (a|its own|a later) (change|commit|session|pass)"
    r"|next (session|pass|round|train)|after (the |this |that |it |its |their )?\S*\s?(push|pushes|lands|landed|merges)"
    r"|\bheld\b|\bholding\b|\bpark(ed|ing)?\b|\bdefer(red)?\b|not in this (push|commit|change|pass|session)"
    r"|hand(ed|ing)? (it |them |this )?(off|over)|left (for|to) |\bTODO\b|\bowed\b|still (open|to do|owed|pending|waiting)"
    r"|not (yet )?(started|done|fixed|committed|pushed|posted|sent|run|proven|measured|verified|checked|tested)"
    r"|unfinished|\bnext,? I\b|I'll (do|fix|send|file|post|handle|come back|finish|pick|run|check|re-?run|add)"
    r"|\bremain(s|ing)? (open|owed|to)|\bwhat'?s left\b|\bleft open\b|\bstill needs?\b|\bnot covered\b|\bout of scope\b",
    re.I)
# Where put-off work can go: a drafts file (the configured ones count too), a ticket, the request ledger
# and its digest, a question card or decision, a spawned task or chip, a memory card, or a commit sha.
HOME = re.compile(r"drafts[\w-]*\.md|outbound-drafts|\bADO\b|#\d{3,}|_workitems|ledger|DIGEST|rulings\.json"
                  r"|spawn|chip|memory card|\bcard\b|\bdecision\b|open-question"
                  r"|\b[0-9a-f]{7,40}\b", re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*`\"(])|\n+")
PROMISE_WINDOW = 6
# A subagent can finish with only its hand-back and no task notice. The harness delivers it as the whole queued
# text, opening with the tag; a progress note from a running agent lacks the marker line.
HANDBACK = re.compile(r'<agent-message from="(a[0-9a-f]{8,})">\n\[Subagent hand-back\]')
# Two shapes: from="uds:..." from-session="local_..." from-name="..." (newer), from="local_..." name="...".
PEER = re.compile(r"<cross-session-message ([^>]*)>(.*?)(?:</cross-session-message>|$)", re.S)
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
# `git commit` prints "[branch abc1234] subject"; `git log --oneline` prints "abc1234 subject".
SHA = re.compile(r"(?:^|\[[^\]\s]+(?: \(root-commit\))? )([0-9a-f]{7,40})\b", re.M)
REPO_IN_CMD = re.compile(r"(?:\bcd|git -C)\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)")


class NotRun(Exception):
    """A check that could not finish: it reads as did not run, never as nothing owed."""


def find_log(session, projects=PROJECTS):
    """The main log for a session id or prefix, else the newest main log."""
    logs = [p for p in glob.glob(os.path.join(projects, "*", "*.jsonl"))]
    if session:
        logs = [p for p in logs if os.path.basename(p).startswith(session)]
    return max(logs, key=os.path.getmtime) if logs else None


def stamp(when):
    """An --as-of time as the logs write theirs (2030-01-15T18:01:18.999Z), so a string compare works."""
    t = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
    t = (t if t.tzinfo else t.astimezone()).astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.999Z")


def blocks(record):
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def text_of(block):
    if block.get("type") == "tool_result":
        c = block.get("content")
        return c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
    return block.get("text") or ""


def delivered(r):
    """Text the harness delivered to the session: the user's turns, and the queue that carries task
    notices and other sessions' messages (queue-operation enqueue, attachment queued_command)."""
    if r.get("type") == "queue-operation":
        return [r["content"]] if r.get("operation") == "enqueue" and isinstance(r.get("content"), str) else []
    if r.get("type") == "attachment":
        a = r.get("attachment") or {}
        return [a["prompt"]] if a.get("type") == "queued_command" and isinstance(a.get("prompt"), str) else []
    if (r.get("message") or {}).get("role") == "user":
        return [text_of(b) for b in blocks(r) if b.get("type") != "tool_result"]
    return []


def changes(name, inp):
    """(old, new) text pairs one Edit, MultiEdit, Write or NotebookEdit call made."""
    if name == "MultiEdit":
        return [(e.get("old_string") or "", e.get("new_string") or "") for e in inp.get("edits") or [] if isinstance(e, dict)]
    if name == "Edit":
        return [(inp.get("old_string") or "", inp.get("new_string") or "")]
    return [("", inp.get("content") or inp.get("new_source") or "")]


WORKTREE = re.compile(r"\bgit\s+(?:-C\s+(\"[^\"]+\"|'[^']+'|\S+)\s+)?worktree\s+(add|remove)\s+([^;&|\n]+)")
ASSIGN = re.compile(r"(?:^|[\s;&|(])([A-Za-z_]\w*)=(\"[^\"]*\"|'[^']*'|[^\s;&|]+)")
CD = re.compile(r"\bcd\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)")


def worktree_ops(cmd, cwd):
    """[(add or remove, absolute path)] for each `git worktree add|remove` in one Bash command. A $VAR set
    in the same command is filled in; a relative path is read from -C, else the last cd before it, else cwd."""
    names = {k: v.strip("\"'") for k, v in ASSIGN.findall(cmd)}
    fill = lambda s: os.path.expanduser(re.sub(r"\$\{?(\w+)\}?", lambda m: names.get(m.group(1), m.group(0)),  # noqa: E731
                                               s.strip("\"'")))
    out = []
    for m in WORKTREE.finditer(cmd):
        try:
            words = shlex.split(m.group(3))
        except ValueError:
            words = m.group(3).split()
        path, skip = None, False
        for w in words:
            if skip:
                skip = False
            elif w in ("-b", "-B", "--reason"):
                skip = True
            elif not w.startswith("-"):
                path = fill(w)
                break
        if not path or "$" in path:     # a variable set outside this command can't be read
            continue
        cds = [fill(d) for d in CD.findall(cmd[:m.start()])]
        base = fill(m.group(1)) if m.group(1) else (os.path.join(cwd, cds[-1]) if cds else cwd)
        out.append((m.group(2), os.path.realpath(os.path.join(base, path))))
    return out


def worktrees_check(g):
    """Worktrees the session added with git, not removed in its log, whose folder is still on disk (as of now)."""
    added = list(dict.fromkeys(p for op, p in g["worktrees"] if op == "add"))
    gone = {p for op, p in g["worktrees"] if op == "remove"}
    return {"made": len(added), "left": [p for p in added if p not in gone and os.path.isdir(p)], "as_of": "now"}


def read_log(path, until=None):
    """Everything close_report needs, in one pass over the log; with until, only records up to then."""
    got = {"times": [], "edited": [], "wrote": {}, "started": [], "finished": set(), "peers": {}, "replies": [],
           "commits": [], "commit_dirs": [], "said": [], "worktrees": []}
    commands, spawns, last = {}, set(), ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            at = r.get("timestamp") or ""
            last = at or last
            # An api-request-blob has no timestamp and repeats messages written around it, so it is as old as
            # the record before it; read as undated, a later blob would put later commits into an earlier read.
            if until and last > until:
                continue
            if at:
                got["times"].append(at)
            for text in delivered(r):
                got["finished"].update(m.group(1) for m in DONE.finditer(text))
                m = HANDBACK.match(text)    # at the start only, so one quoted inside a peer's message is not it
                if m:
                    got["finished"].add(m.group(1))
                for m in PEER.finditer(text):
                    attrs = dict(ATTR.findall(m.group(1)))
                    sender = attrs.get("from-session") or attrs.get("from") or ""
                    body = re.sub(r"\s+", " ", m.group(2)).strip()
                    # A reply can address any of these: the uds socket, the session id or the name.
                    ids = sorted({attrs.get(k, "") for k in ("from", "from-session", "from-name", "name")} - {""})
                    got["peers"].setdefault((sender, body[:110]), {
                        "at": at, "from": sender, "name": attrs.get("from-name") or attrs.get("name") or "",
                        "ids": ids, "excerpt": body[:110], "asks": "?" in body})
            for b in blocks(r):
                if (b.get("type") == "text" and (r.get("message") or {}).get("role") == "assistant"
                        and not r.get("isSidechain") and b.get("text", "").strip()):
                    got["said"].append((at, b["text"]))
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    if b.get("name") in ("Edit", "Write", "NotebookEdit", "MultiEdit") and inp.get("file_path"):
                        got["edited"].append(inp["file_path"])
                        got["wrote"].setdefault(inp["file_path"], []).extend(changes(b["name"], inp))
                    elif b.get("name") == "SendMessage" and inp.get("to"):
                        got["replies"].append((at, inp["to"]))
                    elif b.get("name") in ("TaskStop", "KillShell", "KillBash"):   # stopped on purpose
                        got["finished"].update(str(v) for v in inp.values() if isinstance(v, str))
                    elif b.get("name") == "Bash" and re.search(r"\bgit\b[^\n]*\bcommit\b", inp.get("command") or ""):
                        commands[b.get("id")] = inp["command"]
                    if b.get("name") in ("Agent", "Task") or (b.get("name") == "Bash" and inp.get("run_in_background")):
                        spawns.add(b.get("id"))
                    if b.get("name") == "Bash" and "worktree" in (inp.get("command") or ""):
                        got["worktrees"] += worktree_ops(inp["command"], r.get("cwd") or "")
                elif b.get("type") == "tool_result":
                    text = text_of(b)
                    if b.get("tool_use_id") in spawns:     # not an ID quoted in some other tool's output
                        got["started"] += [m.group(1) or m.group(2) for m in BG_ID.finditer(text)]
                    if b.get("tool_use_id") in commands:
                        cmd = commands.pop(b["tool_use_id"])
                        got["commits"] += SHA.findall(text)
                        got["commit_dirs"] += [os.path.expanduser(d.strip("\"'")) for d in REPO_IN_CMD.findall(cmd)]
    got["started"] = list(dict.fromkeys(got["started"]))
    got["peers"] = sorted(got["peers"].values(), key=lambda p: p["at"])
    return got


def git(repo, *args, raw=None):
    """(exit code, stdout). raw=bytes runs with that input and returns bytes. A timeout raises NotRun."""
    try:
        if raw is not None:
            p = subprocess.run(["git", "-C", repo, *args], input=raw, capture_output=True, timeout=GIT_TIMEOUT)
            return p.returncode, p.stdout
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace",
                           timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise NotRun(f"git {args[0]} timed out after {GIT_TIMEOUT} s in {repo}")
    except OSError as e:
        raise NotRun(f"git could not start: {e}")
    return p.returncode, p.stdout.strip()


def repo_of(path):
    d = os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    rc, top = git(d, "rev-parse", "--show-toplevel") if d else (1, "")
    return top if rc == 0 else None


def sig(line):
    """A line long enough to say who wrote it, stripped; else None."""
    s = line.strip()
    return s if len(s) >= 4 and re.search(r"\w", s) else None


def lines_of(text):
    return {x for x in map(sig, text.splitlines()) if x}


def dirty_lines(top, p):
    """(added, removed) lines of p's uncommitted change against HEAD, or None when it is clean. Only lines
    that say who made the change count: an added line HEAD's copy already holds (a moved line, or a common
    one like `return 0`) is not one, nor a removed line the working copy still holds. Without this, a peer's
    later diff matches lines a session wrote once, and that session reads as owing."""
    rc, st = git(top, "status", "--porcelain", "--", p)
    if rc or not st:
        return None
    work = open(p, encoding="utf-8", errors="replace").read() if os.path.isfile(p) else ""
    if st.startswith("??") or git(top, "rev-parse", "-q", "--verify", "HEAD")[0]:
        return lines_of(work), set()
    _, d = git(top, "diff", "HEAD", "--no-color", "--no-ext-diff", "-U0", "--", p)
    _, rel = git(top, "ls-files", "--full-name", "--", p)
    head = lines_of(git(top, "show", f"HEAD:{rel}")[1]) if rel else set()
    rows = d.splitlines()
    return ({x for x in (sig(r[1:]) for r in rows if r.startswith("+") and not r.startswith("+++")) if x} - head,
            {x for x in (sig(r[1:]) for r in rows if r.startswith("-") and not r.startswith("---")) if x} - lines_of(work))


def edits_check(g, repos):
    """Own uncommitted edits (hard), files dirty only with others' later lines and files in no repo (soft)."""
    paths = sorted({p for p in g["edited"] if not p.startswith(SCRATCH)})
    out = {"changed": len(paths), "uncommitted": [], "others": [], "not_in_git": [], "ran": True}
    try:
        for p in paths:
            top = repo_of(p)
            if top is None:
                if os.path.exists(p):
                    out["not_in_git"].append(p)
                continue
            repos.setdefault(top, []).append(p)
            now = dirty_lines(top, p)
            if now is None:
                continue
            # Each edit on its own: one that added lines is uncommitted if at least half of those lines are; only a
            # pure deletion is judged by what it removed. An edit that replaced lines HEAD has again, which a peer
            # now deletes, or that once added a line a peer now adds back, is not uncommitted. Edit and Write can't
            # delete a file, so a file gone from disk isn't this session's.
            pairs = [(lines_of(new) - lines_of(old), lines_of(old) - lines_of(new)) for old, new in g["wrote"].get(p, [])]
            half = lambda mine, now: bool(mine & now) and 2 * len(mine & now) >= len(mine)  # noqa: E731
            mine = os.path.exists(p) and (any(half(a, now[0]) if a else half(r, now[1]) for a, r in pairs)
                                          or not any(a or r for a, r in pairs))
            out["uncommitted" if mine else "others"].append(p)
    except NotRun as e:
        out.update(ran=False, problem=str(e))
    return out


def remote_heads(top, as_of):
    """Commits remote-tracking refs pointed at, from their reflogs, up to as_of if given.
    ([shas still in the repo], whether any reflog entry was found)."""
    heads, logged = [], False
    for ref in git(top, "for-each-ref", "--format=%(refname)", "refs/remotes")[1].split():
        for row in git(top, "reflog", "show", "--date=unix", "--format=%H %gd", ref, "--")[1].splitlines():
            m = re.match(r"([0-9a-f]{40}) \S*@\{(\d+)\}", row)
            if m:
                logged = True
                if as_of is None or dt.datetime.fromtimestamp(int(m.group(2)), dt.timezone.utc) <= as_of:
                    heads.append(m.group(1))
    heads = list(dict.fromkeys(heads))
    if heads:       # an entry whose commit was pruned would fail the whole rev-list
        _, have = git(top, "cat-file", "--batch-check", raw="\n".join(heads).encode())
        heads = [row.split()[0] for row in have.decode().splitlines() if row.endswith(" commit") or " commit " in row]
    return heads, logged


def landed(top, shas, as_of):
    """({sha: how}, reflog found) for the shas a remote had by as_of (a datetime; None for any time)
    though no remote branch holds them now: a remote-tracking ref held one, or a remote commit has its patch."""
    out, (heads, logged) = {}, remote_heads(top, as_of)
    since = str(min(int(git(top, "log", "-1", "--format=%ct", s)[1] or 0) for s in shas) - 86400)
    if heads:
        _, reach = git(top, "rev-list", f"--since={since}", "--stdin", raw="\n".join(heads).encode())
        reach = set(reach.decode().split())
        for s in shas:
            if git(top, "rev-parse", s)[1] in reach:
                out[s] = "pushed by then" if as_of else "pushed once (a remote-tracking ref held it)"
    rest = [s for s in shas if s not in out]
    if rest:
        until = [] if as_of is None else [f"--until={int(as_of.timestamp())}"]
        _, patches = git(top, "log", "--remotes", "--no-merges", f"--since={since}", *until, "-p", "--format=commit %H")
        known = {}
        for row in git(top, "patch-id", "--stable", raw=(patches + "\n").encode())[1].decode().splitlines():
            pid, _, c = row.partition(" ")
            known.setdefault(pid, c)
        for s in rest:
            _, show = git(top, "show", "--format=commit %H", s)
            pid = git(top, "patch-id", "--stable", raw=(show + "\n").encode())[1].decode().split(" ")[0]
            if pid and pid in known:
                out[s] = f"the same patch is on remote commit {known[pid][:9]}"
    return out, logged


def commits_check(g, repos, as_of):
    """Only commits this session's own `git commit` commands printed, in the repos it touched or ran
    them in; a commit no local branch holds any more was rebased or dropped, so it is not owed."""
    out = {"unpushed": [], "landed": [], "no_remote": [], "ran": True, "as_of": "now" if as_of is None else "then"}
    try:
        for top in filter(None, (repo_of(os.path.join(d, "x")) for d in g["commit_dirs"] if os.path.isdir(d))):
            repos.setdefault(top, [])
        seen, shas = set(), list(dict.fromkeys(g["commits"]))
        for top in repos:
            common = os.path.normpath(os.path.join(top, git(top, "rev-parse", "--git-common-dir")[1]))
            if common in seen:
                continue
            seen.add(common)
            # One commit printed at two sha lengths is one commit.
            mine = list(dict.fromkeys(git(top, "rev-parse", s)[1][:12] for s in shas
                                      if git(top, "cat-file", "-e", f"{s}^{{commit}}")[0] == 0))
            if mine and not git(top, "remote")[1]:
                out["no_remote"].append({"repo": top, "made": len(mine)})
                continue
            held = {s: b for s in mine if (b := git(top, "branch", "--format=%(refname:short)", "--contains", s)[1].split())}
            now = as_of is None or not remote_heads(top, None)[1]
            if as_of is not None and now:
                out["as_of"] = "now (no remote-tracking reflog in " + top + ")"
            if now:     # on a remote branch now: pushed, not listed
                held = {s: b for s, b in held.items() if not git(top, "branch", "-r", "--contains", s)[1]}
            if not held:
                continue
            how = landed(top, list(held), None if now else as_of)[0]
            for s, branches in held.items():
                row = {"repo": top, "sha": s[:9], "on": branches[:3],
                       "subject": git(top, "log", "-1", "--format=%s", s)[1][:70]}
                if s in how:
                    out["landed"].append(dict(row, how=how[s]))
                else:
                    out["unpushed"].append(row)
    except NotRun as e:
        out.update(ran=False, problem=str(e))
    return out


def configured_drafts():
    """session_close.drafts from the config (kit_config.py), or () when it or the module is absent."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import kit_config
        v = kit_config.get("session_close", "drafts")
    except ImportError:
        return ()
    return tuple(v) if isinstance(v, list) else ((v,) if isinstance(v, str) and v else ())


def promises(said, window=PROMISE_WINDOW, homes=()):
    """Sentences in the last `window` replies that put work off and name no home for it. homes: more
    names that count as a home (the configured drafts files)."""
    out, seen = [], set()
    homes = [os.path.basename(h).lower() for h in homes if h]
    for at, text in said[-window:]:
        for s in SENTENCE.split(text):
            s = s.strip(" -*>|`")
            key = re.sub(r"\W+", " ", s.lower())[:100]
            named = HOME.search(s) or any(h in s.lower() for h in homes)
            if 20 <= len(s) <= 600 and DEFERS.search(s) and not named and key not in seen:
                seen.add(key)
                out.append({"at": at, "text": s})
    return out


def unanswered(g):
    """Owed = a question with no message back to that session after it. Acks and FYIs need none, and
    most messages with no reply after them are acks."""
    owed = []
    for p in g["peers"]:
        names = set(p["ids"])
        if p["asks"] and not any(at > p["at"] and (to in names or any(n.endswith(to) or to.endswith(n) for n in names))
                                 for at, to in g["replies"]):
            owed.append(p)
    return {"received": len(g["peers"]), "asked": sum(p["asks"] for p in g["peers"]), "no_reply": owed}


def verdict(r):
    """(hard items owed, hard checks that did not run, soft items). Only the first two decide."""
    e, c, b, p = r["edits"], r["commits"], r["background"], r["peers"]

    def n(items, what):
        """'1 unpushed commit', '2 unpushed commits': the s goes on the word before any '|'."""
        word, _, rest = what.partition("|")
        return [f"{len(items)} {word}{'s' * (len(items) != 1)}{rest}"] if items else []
    hard = (n(e["uncommitted"], "uncommitted own edit") + n(c["unpushed"], "unpushed commit")
            + n(b["no_notice"], "background job| with no completion notice")
            + n(p["no_reply"], "unanswered peer question")
            + [f"worktree {w.replace(os.path.expanduser('~'), '~', 1)} left in place" for w in r["worktrees"]["left"]])
    not_run = ([f"edits ({e['problem']})"] if not e["ran"] else []) + ([f"commits ({c['problem']})"] if not c["ran"] else [])
    soft = (n(e["others"], "file| dirty with others' later edits") + n(r["drafts"], "drafts file| edited")
            + n(r["promises"], "promise line") + n(e["not_in_git"], "changed file| in no repo"))
    return hard, not_run, soft


def close_report(log, drafts=(), as_of=None):
    """drafts: file names or path endings that count as drafts files.
    as_of: a time; the log is read up to then and commits are judged as of then."""
    until = stamp(as_of) if as_of else None
    then = dt.datetime.fromisoformat(until.replace("Z", "+00:00")) if until else None
    g = read_log(log, until)
    out = {"log": log, "from": min(g["times"], default=""), "to": max(g["times"], default=""), "as_of": until}
    repos = {}
    out["edits"] = edits_check(g, repos)
    out["commits"] = commits_check(g, repos, then)
    out["background"] = {"started": len(g["started"]), "no_notice": [t for t in g["started"] if t not in g["finished"]]}
    out["peers"] = unanswered(g)
    out["worktrees"] = worktrees_check(g)
    ends = tuple(d for d in drafts if d)
    out["drafts"] = sorted({p for p in g["edited"] if ends and p.endswith(ends) and not p.startswith(SCRATCH)})
    out["promises"] = promises(g["said"], homes=ends)
    out["hard"], out["not_run"], out["soft"] = verdict(out)
    out["owes"] = bool(out["hard"])
    out["verdict"] = ("owes: " + "; ".join(out["hard"]) if out["hard"] else
                      "not checked: " + "; ".join(out["not_run"]) if out["not_run"] else "can archive")
    if out["hard"] and out["not_run"]:
        out["verdict"] += "; not checked: " + "; ".join(out["not_run"])
    return out


def render(r):
    home = os.path.expanduser("~")
    short = lambda p: p.replace(home, "~", 1)  # noqa: E731
    e, c, b, p = r["edits"], r["commits"], r["background"], r["peers"]
    lines = [f"session log {short(r['log'])}, {r['from'][:16]} to {r['to'][:16]} UTC"
             + (f", read up to {r['as_of'][:16]}" if r.get("as_of") else ""), "HARD, each blocks an archive:"]
    lines.append(f"  edits: {len(e['uncommitted'])} of {e['changed']} changed files hold this session's uncommitted lines"
                 if e["ran"] else f"  edits: DID NOT RUN ({e['problem']})")
    lines += [f"      {short(x)}" for x in e["uncommitted"]]
    if c["ran"]:
        lines.append(f"  commits: {len(c['unpushed'])} this session made that no remote has (as of {c['as_of']})"
                     + (f"; {len(c['landed'])} landed another way" if c["landed"] else ""))
        lines += [f"      {short(x['repo'])} {x['sha']} on {', '.join(x['on'])}: {x['subject']}" for x in c["unpushed"]]
        lines += [f"      landed: {x['sha']} {x['how']}" for x in c["landed"][:5]]
    else:
        lines.append(f"  commits: DID NOT RUN ({c['problem']})")
    lines.append(f"  background: {len(b['no_notice'])} of {b['started']} started with no completion notice")
    lines += [f"      {t}" for t in b["no_notice"]]
    lines.append(f"  peers: {len(p['no_reply'])} of {p['asked']} questions from other sessions with no reply after "
                 f"({p['received']} messages in all)")
    lines += [f"      {m['at'][:16]} {m['name'] or m['from']}: {m['excerpt']}" for m in p["no_reply"]]
    w = r["worktrees"]
    lines.append(f"  worktrees: {len(w['left'])} of {w['made']} it added with git are still on disk (read as of now)")
    lines += [f"      {short(x)}" for x in w["left"]]
    lines.append("SOFT, listed and never blocking:")
    lines.append(f"  others: {len(e['others'])} edited files dirty only with other sessions' later lines"
                 + "".join(f"; {short(x)}" for x in e["others"][:4]))
    lines.append(f"  drafts: {len(r['drafts'])} drafts files edited" + "".join(f"; {short(d)}" for d in r["drafts"]))
    n = len(r["promises"])
    lines.append(f"  promises: {n} sentence{'' if n == 1 else 's'} in the last {PROMISE_WINDOW} replies put work off and "
                 "name no home (a commit, ticket, drafts file, ledger entry, card or spawned task)")
    lines += [f"      {x['at'][:16]} {x['text'] if len(x['text']) <= 160 else x['text'][:159] + '…'}" for x in r["promises"]]
    lines.append(f"  no repo: {len(e['not_in_git'])} changed files in no repo"
                 + "".join(f"; {x['made']} commits in {short(x['repo'])}, which has no remote" for x in c.get("no_remote", [])))
    lines += [f"      {short(x)}" for x in e["not_in_git"][:8]]
    lines.append(r["verdict"])
    return "\n".join(lines)


def calibrate(path, out=print, drafts=(), projects=PROJECTS):
    """Re-run the verdict on the labelled set, each case as of its time, and print N of M on each side."""
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        out(f"did not run: {path} could not be read ({e})")
        return 2
    sides = {"good": [], "bad": []}
    for case in d["cases"]:
        log = find_log(case["session"], projects)
        sides[case["label"]].append((case, close_report(log, drafts, as_of=case["at"]) if log else None))
    drift = 0
    for label, want, words in (("good", False, "known-good (archived on your yes), read \"can archive\""),
                               ("bad", True, "known-bad (work owed after a can-archive claim), read \"owes\"")):
        rows = sides[label]
        miss = [(c, r) for c, r in rows if r is None or r["owes"] != want or (not want and r["not_run"])]
        out(f"{words}: {len(rows) - len(miss)} of {len(rows)}")
        for c, r in miss:
            out(f"    miss {c['session'][:8]} at {c['at'][:16]}: {'no log' if r is None else r['verdict']}"
                + (f" (owed: {c['owed']})" if label == "bad" else ""))
        drift += sum(1 for c, r in rows if r is not None and c.get("verdict") and c["verdict"] != r["verdict"])
    out("as of: the log and the commits as of each case's time; edits, worktree folders and a patch-id match as of now. "
        f"{drift} verdicts differ from the ones saved in {os.path.basename(path)}")
    return 0


def selftest():
    results = []

    def check(label, ok):
        results.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + label)

    global GIT_TIMEOUT
    DR = ("outbound-drafts.md",)
    with tempfile.TemporaryDirectory() as t:
        repo = os.path.join(t, "repo")
        os.makedirs(repo)
        env = ["-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", repo], check=True)
        for name in ("kept.txt", "dirty.txt", "shared.md"):
            with open(os.path.join(repo, name), "w") as f:
                f.write("a\n")
        subprocess.run(["git", "-C", repo, *env, "add", "."], check=True)
        subprocess.run(["git", "-C", repo, *env, "commit", "-qm", "base"], check=True)
        with open(os.path.join(repo, "dirty.txt"), "a") as f:
            f.write("the session's own line\n")
        loose = os.path.join(t, "notes", "plan.md")
        drafts = os.path.join(t, "work", "outbound-drafts.md")
        for p in (loose, drafts):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()

        def use(name, **inp):
            return {"type": "tool_use", "name": name, "input": inp}

        def edit(path, new, old="a"):
            return use("Edit", file_path=path, old_string=old, new_string=new)

        def res(text, tu=None):
            return {"type": "tool_result", "content": text, "tool_use_id": tu}

        def spawn(*ids):
            return rec("10:02", "assistant", *[{"type": "tool_use", "id": i, "name": "Agent" if i.startswith("ag") else
                                                "Read" if i.startswith("rd") else "Bash",
                                                "input": {"command": "sleep 1", "run_in_background": True}} for i in ids])

        def rec(ts, role, *bl):
            return json.dumps({"timestamp": f"2026-10-20T{ts}:00.000Z", "message": {"role": role, "content": list(bl)}})

        def queued(ts, text, kind):
            if kind == "queue":
                return json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": f"2026-10-20T{ts}:00.000Z",
                                   "content": text})
            return json.dumps({"type": "attachment", "timestamp": f"2026-10-20T{ts}:00.000Z",
                               "attachment": {"type": "queued_command", "prompt": text}})

        def notice(task):
            return f"<task-notification>\n<task-id>{task}</task-id>\n<status>completed</status>\n</task-notification>"

        def head(repo_dir):
            return subprocess.run(["git", "-C", repo_dir, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

        def committed(tu, repo_dir, printed):
            return [rec("10:01", "assistant", {"type": "tool_use", "id": tu, "name": "Bash", "input": {
                        "command": f"cd {repo_dir} && git commit -qm x && git log --oneline -1"}}),
                    rec("10:01", "user", {"type": "tool_result", "tool_use_id": tu, "content": printed})]

        def write(lines, name="s.jsonl"):
            p = os.path.join(t, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            return p

        with open(os.path.join(repo, "kept.txt"), "a") as f:
            f.write("session\n")
        subprocess.run(["git", "-C", repo, *env, "commit", "-qm", "session work", "--", "kept.txt"], check=True)
        mine = head(repo)
        with open(os.path.join(repo, "kept.txt"), "a") as f:
            f.write("other\n")
        subprocess.run(["git", "-C", repo, *env, "commit", "-qm", "another session's work", "--", "kept.txt"], check=True)

        peer = ('<cross-session-message from="uds:/tmp/cc-socks/1.sock" from-session="local_peer1" '
                'from-name="Docs page">Can you send me the commit you pushed?</cross-session-message>')
        fyi = '<cross-session-message from="local_peer2" name="Review dashboard">FYI only</cross-session-message>'
        bad = [rec("10:00", "user", {"type": "text", "text": "do the thing"}),
               rec("10:00", "assistant", use("SendMessage", to="Docs page", message="an earlier note, not a reply")),
               rec("10:01", "assistant", edit(os.path.join(repo, "dirty.txt"), "a\nthe session's own line"),
                   edit(os.path.join(repo, "kept.txt"), "a\nsession"), use("Write", file_path=loose, content=""),
                   use("Write", file_path=drafts, content=""), use("Write", file_path="/private/tmp/scratch/x.py")),
               *committed("tu1", repo, f"{mine[:7]} session work"),
               spawn("bg1", "bg2", "bg3", "bg4", "ag1", "rd1"),
               rec("10:02", "user", res("Command running in background with ID: bdone1. Output ...", "bg1"),
                   res("Command running in background with ID: bhang2. Output ...", "bg2"),
                   res("Command running in background with ID: bqueue3. Output ...", "bg3"),
                   res("Command running in background with ID: bserve4. Output ...", "bg4"),
                   res("Async agent launched. agentId: a1234567890abcdef (internal ID)", "ag1"),
                   res("a file that quotes 'running in background with ID: becho9'", "rd1")),
               rec("10:03", "user", {"type": "text", "text": notice("bdone1")}),
               queued("10:03", notice("bqueue3"), "queue"),
               queued("10:03", notice("a1234567890abcdef"), "attachment"),
               queued("10:04", "Another Claude session sent a message:\n" + peer, "queue"),
               queued("10:04", peer, "attachment"),
               rec("10:05", "user", {"type": "text", "text": fyi}),
               queued("10:05", '<cross-session-message from-session="local_peer3" from-name="AI system">Pushed 1a2b3c4. '
                               'No action needed.</cross-session-message>', "queue"),
               rec("10:06", "assistant", use("SendMessage", to="Review dashboard", message="thanks"),
                   use("SendMessage", to="uds:/tmp/cc-socks/3.sock", message="another session's socket"),
                   use("TaskStop", task_id="bserve4")),
               rec("10:07", "user", res("echo of an old one: " + peer.replace("peer1", "peer9") + notice("bhang2")))]
        log = write(bad)
        r = close_report(log, DR)
        check("an Edit left uncommitted in its repo is listed; a committed one is not",
              r["edits"]["uncommitted"] == [os.path.join(repo, "dirty.txt")] and r["edits"]["changed"] == 4)
        check("a changed file in no repo is listed; a /private/tmp scratch file is left out",
              set(r["edits"]["not_in_git"]) == {loose, drafts})
        check("a notice in a user turn, the queue or an attachment, or a TaskStop, counts; an echoed notice does not",
              r["background"] == {"started": 5, "no_notice": ["bhang2"]})
        check("a peer question (either shape, queued twice) with no reply after it is listed once; one replied to, "
              "an FYI and an echo in a tool result are not",
              [m["from"] for m in r["peers"]["no_reply"]] == ["local_peer1"] and r["peers"]["received"] == 3
              and r["peers"]["asked"] == 1 and r["peers"]["no_reply"][0]["name"] == "Docs page")
        check("an edited drafts file is listed", r["drafts"] == [drafts])
        check("with no drafts files configured, none is listed", close_report(log)["drafts"] == [])
        check("a repo with no remote counts only the commit the session's own git commit printed, and is soft",
              [{k: v for k, v in x.items() if k != "repo"} for x in r["commits"]["no_remote"]] == [{"made": 1}]
              and not r["commits"]["unpushed"])
        check("the hard items owe, each named in the verdict", r["owes"] and r["verdict"].startswith("owes: ")
              and all(w in r["verdict"] for w in ("uncommitted own edit", "background job", "unanswered peer question")))

        # Planted, one hard item each: every one alone reads "owes".
        with open(os.path.join(repo, "dirty.txt"), "w") as f:
            f.write("a\n")
        own = write([rec("10:01", "assistant", edit(os.path.join(repo, "shared.md"), "a\nmy unsaved paragraph line"))])
        with open(os.path.join(repo, "shared.md"), "a") as f:
            f.write("my unsaved paragraph line\n")
        r = close_report(own)
        check("planted: an uncommitted own edit alone reads owes",
              r["verdict"] == "owes: 1 uncommitted own edit" and "owes:" in render(r).splitlines()[-1])
        subprocess.run(["git", "-C", repo, *env, "commit", "-qam", "own edit committed"], check=True)
        with open(os.path.join(repo, "shared.md"), "a") as f:
            f.write("a peer's later line\n")
        r = close_report(own)
        check("a file dirty only with another session's later line is soft: can archive, listed under others",
              r["verdict"] == "can archive" and r["edits"]["others"] == [os.path.join(repo, "shared.md")]
              and "others' later edits" in " ".join(r["soft"]))
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--", "shared.md"], check=True)
        whole = os.path.join(repo, "tool.py")
        body = "def main():\n    print('the session wrote this tool')\n    return 0\n"
        with open(whole, "w") as f:
            f.write(body)
        subprocess.run(["git", "-C", repo, *env, "add", "tool.py"], check=True)
        subprocess.run(["git", "-C", repo, *env, "commit", "-qm", "the tool"], check=True)
        with open(whole, "a") as f:
            f.write("\n\ndef other():\n    print('a peer added this later')\n    return 0\n")
        r = close_report(write([rec("10:01", "assistant", use("Write", file_path=whole, content=body))]))
        check("a peer's later lines that repeat a common line the session wrote (return 0) are not the session's",
              r["verdict"] == "can archive" and r["edits"]["others"] == [whole])
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--", "tool.py"], check=True)
        with open(os.path.join(repo, "shared.md"), "w") as f:
            f.write("a\na replacement paragraph line\nmy unsaved paragraph line\n")
        subprocess.run(["git", "-C", repo, *env, "commit", "-qam", "the session's edit, then the old line back"], check=True)
        with open(os.path.join(repo, "shared.md"), "w") as f:
            f.write("a\na replacement paragraph line\n")
        r = close_report(write([rec("10:01", "assistant", edit(os.path.join(repo, "shared.md"), "a replacement paragraph line",
                                                               old="my unsaved paragraph line"))]))
        check("a peer deleting a line the session once replaced, which HEAD has again, is not the session's edit",
              r["verdict"] == "can archive" and r["edits"]["others"] == [os.path.join(repo, "shared.md")])
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--", "shared.md"], check=True)
        mod = os.path.join(repo, "mod.py")
        body = "".join(f"    step_{i} = plan.take({i})\n" for i in range(4)) + "    return todo\n"
        with open(mod, "w") as f:
            f.write(body)
        subprocess.run(["git", "-C", repo, *env, "add", "mod.py"], check=True)
        subprocess.run(["git", "-C", repo, *env, "commit", "-qm", "the session's file"], check=True)
        with open(mod, "w") as f:
            f.write(body.replace("    return todo\n", ""))
        subprocess.run(["git", "-C", repo, *env, "commit", "-qam", "a later commit drops one line"], check=True)
        with open(mod, "a") as f:
            f.write("    return todo\n")
        r = close_report(write([rec("10:01", "assistant", use("Write", file_path=mod, content=body))], "mod.jsonl"))
        check("a peer adding back one line of five the session once wrote is not the session's edit",
              r["verdict"] == "can archive" and r["edits"]["others"] == [mod])
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--", "mod.py"], check=True)
        cut = edit(mod, "    step_0 = plan.take(0)\n", "    step_0 = plan.take(0)\n    step_1 = plan.take(1)\n")
        os.remove(mod)
        r = close_report(write([rec("10:01", "assistant", cut)], "rm.jsonl"))
        check("a file a peer deleted, holding a line the session once deleted, is not the session's edit",
              r["verdict"] == "can archive" and r["edits"]["others"] == [mod])
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--", "mod.py"], check=True)
        agent = write([spawn("ag9"), rec("10:02", "user", res("Async agent launched. agentId: a9999999999999999 (x)", "ag9"))])
        check("planted: a running agent alone reads owes",
              close_report(agent)["verdict"] == "owes: 1 background job with no completion notice")
        talk = lambda ts, text: json.dumps({"timestamp": f"2026-10-20T{ts}:00.000Z",  # noqa: E731
                                            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
        only = write([talk("10:00", "The phone layout is not yet proven, so it waits for the next pass."),
                      rec("10:01", "assistant", use("Write", file_path=drafts, content="a draft for the user\n"))])
        r = close_report(only, DR)
        check("planted: a session with only promise lines and a drafts edit reads can archive, the lines listed",
              r["verdict"] == "can archive" and len(r["promises"]) == 1 and r["drafts"] == [drafts]
              and render(r).splitlines()[-1] == "can archive")

        # SendMessage addresses a peer by its uds socket as often as by its name.
        by_socket = ('<cross-session-message from="uds:/tmp/cc-socks/2.sock" from-session="local_peer4" '
                     'from-name="Cleanup" from-mode="prompting">Is that worktree yours?</cross-session-message>')
        good = [bad[0], rec("10:01", "assistant", edit(os.path.join(repo, "dirty.txt"), "a")),
                spawn("bg1"), rec("10:02", "user", res("Command running in background with ID: bdone1.", "bg1")),
                rec("10:03", "user", {"type": "text", "text": notice("bdone1")}),
                queued("10:04", peer, "queue"), rec("10:06", "assistant", use("SendMessage", to="Docs page", message="here")),
                queued("10:07", by_socket, "queue"),
                rec("10:08", "assistant", use("SendMessage", to="uds:/tmp/cc-socks/2.sock", message="no"))]
        log = write(good)
        r = close_report(log)
        check("a clean session (committed, notified, replied) can archive",
              r["verdict"] == "can archive" and not r["edits"]["uncommitted"] and not r["peers"]["no_reply"]
              and not r["background"]["no_notice"])
        check("the report renders and ends on the verdict line", render(r).splitlines()[-1] == "can archive")
        check("as of a time before the reply, the peer question is unanswered; the log after it is not read",
              close_report(log, as_of="2026-10-20T10:05Z")["verdict"] == "owes: 1 unanswered peer question")
        blob = write([bad[0], rec("11:00", "user", {"type": "text", "text": "later"}),
                      json.dumps({"type": "api-request-blob", "message": {"role": "user", "content": peer}})])
        check("a record with no timestamp is as old as the one before it: after the as-of time, not read",
              close_report(blob, as_of="2026-10-20T10:30Z")["verdict"] == "can archive"
              and close_report(blob)["verdict"] == "owes: 1 unanswered peer question")
        # A subagent can finish with only its hand-back, no task notice. A progress note from one still running, a
        # hand-back quoted in a peer's message or echoed in a tool result finish nothing.
        def handback(aid, body="[Subagent hand-back] The text below is the final report."):
            return f'<agent-message from="{aid}">\n{body}\n</agent-message>'
        ids = ["a2222222222222222", "a3333333333333333", "a4444444444444444", "a5555555555555555"]
        hb = [spawn("ag2", "ag3", "ag4", "ag5"),
              rec("10:02", "user", *[res(f"Async agent launched. agentId: {a} (internal ID)", t)
                                     for a, t in zip(ids, ("ag2", "ag3", "ag4", "ag5"))]),
              queued("10:03", handback(ids[0]), "queue"), queued("10:03", handback(ids[0]), "attachment"),
              queued("10:03", handback(ids[1], "Row 3 is running; I'll report when it ends."), "queue"),
              queued("10:04", '<cross-session-message from="local_peer5" name="Notes">It said:\n'
                              + handback(ids[2]) + "</cross-session-message>", "queue"),
              rec("10:05", "user", res(handback(ids[3])))]
        check("a hand-back with no task notice finishes its agent; a progress note, a hand-back quoted by a peer "
              "or echoed in a tool result does not",
              close_report(write(hb))["background"] == {"started": 4, "no_notice": ids[1:]})
        check("an unanswered question alone owes", close_report(write([queued("10:04", peer, "queue")]))["owes"])

        # The session's own promises: a late reply that puts work off with no home for it is listed, once; one
        # older than the window, one that names its home, a subagent's and one quoted in a tool result are not.
        def talk2(ts, text, side=False):
            return json.dumps({"timestamp": f"2026-10-20T{ts}:00.000Z", "isSidechain": side,
                               "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
        kept = "The phone layout is not yet proven, so it waits for the next pass."
        said = ([talk2("09:00", "The stale README is still owed; I'll fix it in a separate commit.")]
                + [talk2(f"09:1{i}", f"Step {i} is finished and checked.") for i in range(PROMISE_WINDOW - 3)]
                + [talk2("10:00", kept), talk2("10:01", "The label question is held in drafts.md for your go."),
                   talk2("10:02", "Still owed: the gate rewrite, after the push lands.", side=True),
                   rec("10:03", "user", res("notes: the README is still owed")), talk2("10:04", kept)])
        r = close_report(write(said))
        check("a late reply that puts work off with no home is listed once, and does not block; an older one, "
              "one naming drafts.md, a subagent's and a tool result's are not listed",
              [x["text"] for x in r["promises"]] == [kept] and not r["owes"] and kept in render(r))
        parked = [("10:00", "The reply is parked in outbox.md for your go.")]
        check("a configured drafts file counts as a promise's home",
              len(promises(parked)) == 1 and promises(parked, homes=("work/outbox.md",)) == [])
        check("blind spot, named in the docstring: a promise older than the last replies is not seen",
              close_report(write(said[:1] + [talk2(f"10:0{i}", "Fixed, committed and checked.")
                                             for i in range(PROMISE_WINDOW)]))["promises"] == [])
        check("a missing session id finds no log", find_log("zzzz-no-such", t) is None)

        # A repo with a remote: the session's unpushed commit owes; a pushed one, another session's, one rebased
        # away (no local branch holds it), one pushed and then rewritten off the remote (the reflog had it) and one
        # cherry-picked onto the remote (same patch) do not.
        bare = os.path.join(t, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", bare], check=True)
        branch = subprocess.run(["git", "-C", repo, "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", repo, "push", "-q", "origin", "HEAD"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "fetch", "-q", "origin"], check=True, capture_output=True)
        pushed = head(repo)
        subprocess.run(["git", "-C", repo, "checkout", "-qb", "gone"], check=True)
        subprocess.run(["git", "-C", repo, *env, "commit", "-q", "--allow-empty", "-m", "rebased away"], check=True)
        gone = head(repo)
        subprocess.run(["git", "-C", repo, "checkout", "-q", branch], check=True)
        subprocess.run(["git", "-C", repo, "branch", "-qD", "gone"], check=True)
        subprocess.run(["git", "-C", repo, "checkout", "-qb", "pre-squash"], check=True)
        with open(os.path.join(repo, "kept.txt"), "a") as f:
            f.write("pushed once, then the remote branch was rewritten\n")
        subprocess.run(["git", "-C", repo, *env, "commit", "-qam", "pushed once"], check=True)
        once = head(repo)
        subprocess.run(["git", "-C", repo, "push", "-q", "origin", "pre-squash"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "push", "-q", "-f", "origin", f"{pushed}:refs/heads/pre-squash"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", branch], check=True)
        subprocess.run(["git", "-C", repo, "checkout", "-qb", "land"], check=True)
        with open(os.path.join(repo, "dirty.txt"), "a") as f:
            f.write("cherry-picked line\n")
        subprocess.run(["git", "-C", repo, *env, "commit", "-qam", "picked"], check=True)
        picked = head(repo)
        subprocess.run(["git", "-C", repo, "checkout", "-q", branch], check=True)
        subprocess.run(["git", "-C", repo, *env, "commit", "-q", "--allow-empty", "-m", "a step first"], check=True)
        subprocess.run(["git", "-C", repo, *env, "cherry-pick", picked], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "push", "-q", "origin", "HEAD"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, *env, "commit", "-q", "--allow-empty", "-m", "not pushed yet"], check=True)
        later = head(repo)
        log = write(committed("tu2", repo, f"{pushed[:7]} done") + committed("tu3", repo, f"{gone[:8]} rebased away")
                    + committed("tu5", repo, f"[pre-squash {once[:7]}] pushed once")
                    + committed("tu6", repo, f"[land {picked[:7]}] picked")
                    + committed("tu4", repo, f"[{branch} {later[:7]}] not pushed yet")
                    + committed("tu7", repo, f"{later[:10]} the same commit, printed longer"))
        r = close_report(log)
        check("planted: an unpushed commit owes, once at two sha lengths; pushed, rebased-away, pushed-then-rewritten "
              "and cherry-picked ones do not",
              [x["subject"] for x in r["commits"]["unpushed"]] == ["not pushed yet"] and r["owes"]
              and sorted(x["subject"] for x in r["commits"]["landed"]) == ["picked", "pushed once"])
        check("the commit pushed and then rewritten off its branch counts as pushed by the reflog",
              any(x["subject"] == "pushed once" and x["how"].startswith("pushed once") for x in r["commits"]["landed"]))
        subprocess.run(["git", "-C", repo, "push", "-q", "origin", "HEAD"], check=True, capture_output=True)
        check("once pushed, it does not owe", close_report(log)["commits"]["unpushed"] == [])
        # The planted log is dated 2026-10-20 and the pushes happened just now: as of 10/20 12:00 none had.
        then = close_report(log, as_of="2026-10-20T12:00Z")["commits"]
        check("as of a time after the commit and before the push, it owes again (the remote-tracking reflog)",
              then["as_of"] == "then" and [x["subject"] for x in then["unpushed"]][-1:] == ["not pushed yet"])

        # Worktrees, read as of now: added and still on disk owes; added and removed, or added and gone, does not.
        left, done, gone = (os.path.join(t, n) for n in ("wt-left", "wt done", "wt-gone"))
        for w in (left, done, gone):
            subprocess.run(["git", "-C", repo, "worktree", "add", "-q", "--detach", w], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "worktree", "remove", gone], check=True)
        bash = lambda cmd: rec("10:01", "assistant", use("Bash", command=cmd))  # noqa: E731
        r = close_report(write([bash(f"cd /tmp && git -C {repo} worktree add --detach ../wt-left HEAD"),
                                bash(f'W="{done}"; git -C {repo} worktree add -q --detach "$W" HEAD && ls "$W"'),
                                bash(f'W="{done}"; git -C {repo} worktree remove --force "$W"')], "wt.jsonl"))
        check("planted: a worktree it added and left on disk owes, named; one its log removed does not",
              r["verdict"] == f"owes: worktree {os.path.realpath(left).replace(os.path.expanduser('~'), '~', 1)} left in place"
              and r["worktrees"]["made"] == 2 and "(read as of now)" in render(r))
        check("a worktree it added whose folder is gone now does not owe, though its log never removed it",
              close_report(write([bash(f"cd {repo} && git worktree add {gone}")], "wt2.jsonl"))["verdict"] == "can archive")
        for w in (left, done):
            subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", w], check=True)

        # A git call that times out is a check that did not run, not a crash.
        keep, GIT_TIMEOUT = GIT_TIMEOUT, 0.0001
        try:
            r = close_report(log)
            ok = (not r["edits"]["ran"] or not r["commits"]["ran"]) and r["verdict"].startswith("not checked:")
        except Exception as e:
            ok = False
            print(f"    crashed: {type(e).__name__}: {e}")
        finally:
            GIT_TIMEOUT = keep
        check("a git timeout reads not checked, never a crash and never can archive", ok)

        # Calibration: a labelled set re-run as of each case's time, N of M on each side, misses named.
        projects = os.path.join(t, "projects", "-home-user-project")
        os.makedirs(projects)
        for sid, lines in (("c0ffee01", good), ("c0ffee02", [queued("10:04", peer, "queue")])):
            with open(os.path.join(projects, sid + ".jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        cal = os.path.join(t, "calibration.json")
        with open(cal, "w", encoding="utf-8") as f:
            json.dump({"cases": [{"session": "c0ffee01", "label": "good", "at": "2026-10-20T10:30Z"},
                                 {"session": "c0ffee02", "label": "bad", "at": "2026-10-20T10:30Z", "owed": "a reply"},
                                 {"session": "c0ffee01", "label": "bad", "at": "2026-10-20T10:30Z", "owed": "planted miss"}]}, f)
        said_cal = []
        rc = calibrate(cal, said_cal.append, projects=os.path.join(t, "projects"))
        check("calibrate prints N of M on each side and names the miss",
              rc == 0 and said_cal[0].endswith(": 1 of 1") and said_cal[1].endswith(": 1 of 2")
              and said_cal[2].startswith("    miss c0ffee01") and "(owed: planted miss)" in said_cal[2])
        check("a calibration file that can't be read does not run",
              calibrate(os.path.join(t, "nope.json"), said_cal.append) == 2)

    print(f"selftest: {sum(results)} of {len(results)} checks passed")
    return 0 if all(results) else 1


def main():
    ap = argparse.ArgumentParser(prog="session_close.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="session id or prefix (default: the newest main log)")
    ap.add_argument("--as-of", help="read the log up to this time (ISO, e.g. 2030-01-15T18:01Z)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--drafts", action="append", default=None,
                    help="a file name or path ending that counts as a drafts file (repeatable; default: the "
                         "config key session_close.drafts)")
    ap.add_argument("--calibrate", metavar="FILE", help="re-run a labelled set of sessions (see CALIBRATION); N of M on each side")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    drafts = a.drafts if a.drafts is not None else configured_drafts()
    if a.calibrate:
        return calibrate(a.calibrate, drafts=drafts)
    log = find_log(a.session)
    if not log:
        print(f"no session log{' for ' + a.session if a.session else ''} under {PROJECTS}")
        return 2
    try:
        r = close_report(log, drafts, a.as_of)
    except OSError as e:
        print(f"could not read {log}: {e}")
        return 2
    print(json.dumps(r, indent=1) if a.json else render(r))
    return 1 if r["owes"] else 2 if r["not_run"] else 0


if __name__ == "__main__":
    sys.exit(main())
