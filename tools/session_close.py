#!/usr/bin/env python3
"""session_close.py - what one Claude Code session leaves open, read from its own log.

    python tools/session_close.py                    the newest main log under ~/.claude/projects
    python tools/session_close.py --session a8c4b868 a session by id or id prefix
    python tools/session_close.py --selftest         planted logs: each loose end found, a clean one quiet

WHY THIS EXISTS. "Am I good to archive this session? Does it owe anything to other sessions?" is a
question worth asking at the end of every session, and answering it from memory misses things. This
reads the log instead and lists the facts. Whether the session's work is finished stays a judgement.

WHAT IT LISTS, each as N of M:
  edits      files the session changed with Edit or Write that are uncommitted in their git repo,
             and the changed files that are in no repo (scratch and /private/tmp paths left out)
  commits    commits the session's own `git commit` commands printed that a local branch still holds
             and no remote branch contains, with the branch named; or the count in a repo with no
             remote. A squash lands the work under a new sha, so a "pre-squash" branch reads as
             unpushed. A commit made with -q and nothing printing its sha is not seen
  background background commands and agents the session started with no completion notice in the log
             (a task notice, or a subagent's hand-back) and no TaskStop
  peers      questions (a "?") from other sessions (<cross-session-message>, delivered through the
             queue) with no SendMessage back to that session afterwards, addressed by its name,
             session id or uds socket; acks and FYIs need none
  drafts     drafts files the session edited: items there wait on you to send or post. Which files
             are drafts files is yours to say: --drafts NAME (repeatable) or the config key
             session_close.drafts, a list of file names or path endings such as "outbound-drafts.md".
             With neither set, this check lists nothing

Exit 0 nothing open, 1 something open, 2 the log could not be found or read.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
SCRATCH = ("/private/tmp/", "/tmp/", "/private/var/folders/")
BG_ID = re.compile(r"running in background with ID: (\w+)|agentId: (a[0-9a-f]{8,})")
DONE = re.compile(r"<task-notification>\s*<task-id>(\w+)</task-id>.*?<status>(\w+)</status>", re.S)
# A subagent can finish with only its hand-back and no task notice. The harness delivers it as the whole queued
# text, opening with the tag; a progress note from a running agent lacks the marker line.
HANDBACK = re.compile(r'<agent-message from="(a[0-9a-f]{8,})">\n\[Subagent hand-back\]')
# Two shapes: from="uds:..." from-session="local_..." from-name="..." (newer), from="local_..." name="...".
PEER = re.compile(r"<cross-session-message ([^>]*)>(.*?)(?:</cross-session-message>|$)", re.S)
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
# `git commit` prints "[branch abc1234] subject"; `git log --oneline` prints "abc1234 subject".
SHA = re.compile(r"(?:^|\[[^\]\s]+(?: \(root-commit\))? )([0-9a-f]{7,40})\b", re.M)
REPO_IN_CMD = re.compile(r"(?:\bcd|git -C)\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)")


def find_log(session, projects=PROJECTS):
    """The main log for a session id or prefix, else the newest main log."""
    logs = [p for p in glob.glob(os.path.join(projects, "*", "*.jsonl"))]
    if session:
        logs = [p for p in logs if os.path.basename(p).startswith(session)]
    return max(logs, key=os.path.getmtime) if logs else None


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


def read_log(path):
    """Everything close_report needs, in one pass over the log."""
    got = {"times": [], "edited": [], "started": [], "finished": set(), "peers": {}, "replies": [],
           "commits": [], "commit_dirs": []}
    commands, spawns = {}, set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            at = r.get("timestamp") or ""
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
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    if b.get("name") in ("Edit", "Write", "NotebookEdit") and inp.get("file_path"):
                        got["edited"].append(inp["file_path"])
                    elif b.get("name") == "SendMessage" and inp.get("to"):
                        got["replies"].append((at, inp["to"]))
                    elif b.get("name") in ("TaskStop", "KillShell", "KillBash"):   # stopped on purpose
                        got["finished"].update(str(v) for v in inp.values() if isinstance(v, str))
                    elif b.get("name") == "Bash" and re.search(r"\bgit\b[^\n]*\bcommit\b", inp.get("command") or ""):
                        commands[b.get("id")] = inp["command"]
                    if b.get("name") in ("Agent", "Task") or (b.get("name") == "Bash" and inp.get("run_in_background")):
                        spawns.add(b.get("id"))
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


def git(repo, *args):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout.strip()


def repo_of(path):
    d = os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    rc, top = git(d, "rev-parse", "--show-toplevel") if d else (1, "")
    return top if rc == 0 else None


def configured_drafts():
    """session_close.drafts from the config (kit_config.py), or () when it or the module is absent."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import kit_config
        v = kit_config.get("session_close", "drafts")
    except ImportError:
        return ()
    return tuple(v) if isinstance(v, list) else ((v,) if isinstance(v, str) and v else ())


def close_report(log, drafts=()):
    """drafts: file names or path endings that count as drafts files."""
    g = read_log(log)
    first, last = (min(g["times"]), max(g["times"])) if g["times"] else ("", "")
    out = {"log": log, "from": first, "to": last}

    paths = sorted({p for p in g["edited"] if not p.startswith(SCRATCH)})
    dirty, loose, repos = [], [], {}
    for p in paths:
        top = repo_of(p)
        if top is None:
            if os.path.exists(p):
                loose.append(p)
            continue
        repos.setdefault(top, []).append(p)
        rc, st = git(top, "status", "--porcelain", "--", p)
        if rc == 0 and st:
            dirty.append(p)
    out["edits"] = {"changed": len(paths), "uncommitted": dirty, "not_in_git": loose}

    # Only commits this session's own `git commit` commands printed, in the repos it touched or ran
    # them in; a commit no local branch holds any more was rebased or dropped, so it is not owed.
    for d in g["commit_dirs"]:
        top = repo_of(os.path.join(d, "x")) if os.path.isdir(d) else None
        if top:
            repos.setdefault(top, [])
    commits, seen, shas = [], set(), list(dict.fromkeys(g["commits"]))
    for top in repos:
        common = os.path.normpath(os.path.join(top, git(top, "rev-parse", "--git-common-dir")[1]))
        if common in seen:
            continue
        seen.add(common)
        mine = [s for s in shas if git(top, "cat-file", "-e", f"{s}^{{commit}}")[0] == 0]
        if mine and not git(top, "remote")[1]:
            commits.append({"repo": top, "no_remote": True, "made": len(mine)})
            continue
        for s in mine:
            local = git(top, "branch", "--format=%(refname:short)", "--contains", s)[1].split()
            if local and not git(top, "branch", "-r", "--contains", s)[1]:
                # A squash lands the work under a new sha, so name the branch: "pre-squash" says it.
                commits.append({"repo": top, "sha": s[:9], "on": local[:3],
                                "subject": git(top, "log", "-1", "--format=%s", s)[1][:70]})
    out["commits"] = commits

    out["background"] = {"started": len(g["started"]),
                         "no_notice": [t for t in g["started"] if t not in g["finished"]]}

    # Owed = a question with no message back to that session after it. Acks and FYIs need none, and
    # most messages with no reply after them are acks.
    owed = []
    for p in g["peers"]:
        names = set(p["ids"])
        if p["asks"] and not any(at > p["at"] and (to in names or any(n.endswith(to) or to.endswith(n) for n in names))
                                 for at, to in g["replies"]):
            owed.append(p)
    out["peers"] = {"received": len(g["peers"]), "asked": sum(p["asks"] for p in g["peers"]), "no_reply": owed}
    ends = tuple(d for d in drafts if d)
    out["drafts"] = sorted({p for p in paths if ends and p.endswith(ends)})
    out["open"] = bool(dirty or [c for c in commits if not c.get("no_remote")] or out["background"]["no_notice"]
                       or owed or out["drafts"])
    return out


def render(r):
    home = os.path.expanduser("~")
    short = lambda p: p.replace(home, "~", 1)
    e, b, p = r["edits"], r["background"], r["peers"]
    lines = [f"session log {short(r['log'])}, {r['from'][:16]} to {r['to'][:16]} UTC",
             f"edits: {len(e['uncommitted'])} of {e['changed']} changed files uncommitted in their repo; "
             f"{len(e['not_in_git'])} in no repo (saved on disk, no history)"]
    lines += [f"    uncommitted {short(x)}" for x in e["uncommitted"]]
    lines += [f"    no repo     {short(x)}" for x in e["not_in_git"][:8]]
    if len(e["not_in_git"]) > 8:
        lines.append(f"    and {len(e['not_in_git']) - 8} more in no repo")
    unpushed = [c for c in r["commits"] if not c.get("no_remote")]
    lines.append(f"commits: {len(unpushed)} this session made that no remote branch contains"
                 + "".join(f"; {c['made']} in {short(c['repo'])}, which has no remote"
                           for c in r["commits"] if c.get("no_remote")))
    lines += [f"    {short(c['repo'])} {c['sha']} on {', '.join(c['on'])}: {c['subject']}" for c in unpushed]
    lines.append(f"background: {len(b['no_notice'])} of {b['started']} started with no completion notice")
    lines += [f"    {t}" for t in b["no_notice"]]
    lines.append(f"peers: {len(p['no_reply'])} of {p['asked']} questions from other sessions with no reply after "
                 f"({p['received']} messages in all)")
    lines += [f"    {m['at'][:16]} {m['name'] or m['from']}: {m['excerpt']}" for m in p["no_reply"]]
    lines.append(f"drafts: {len(r['drafts'])} drafts files edited" + "".join(f"; {short(d)}" for d in r["drafts"]))
    lines.append("OPEN: read the lines above before archiving" if r["open"] else
                 "nothing open in the log; whether the work itself is finished is still a judgement")
    return "\n".join(lines)


def selftest():
    results = []

    def check(label, ok):
        results.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + label)

    with tempfile.TemporaryDirectory() as t:
        repo = os.path.join(t, "repo")
        os.makedirs(repo)
        env = ["-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", repo], check=True)
        for name in ("kept.txt", "dirty.txt"):
            with open(os.path.join(repo, name), "w") as f:
                f.write("a\n")
        subprocess.run(["git", "-C", repo, *env, "add", "."], check=True)
        subprocess.run(["git", "-C", repo, *env, "commit", "-qm", "base"], check=True)
        with open(os.path.join(repo, "dirty.txt"), "a") as f:
            f.write("b\n")
        loose = os.path.join(t, "notes", "plan.md")
        drafts = os.path.join(t, "work", "outbound-drafts.md")
        for p in (loose, drafts):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()

        def use(name, **inp):
            return {"type": "tool_use", "name": name, "input": inp}

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
               rec("10:01", "assistant", use("Edit", file_path=os.path.join(repo, "dirty.txt")),
                   use("Edit", file_path=os.path.join(repo, "kept.txt")), use("Write", file_path=loose),
                   use("Write", file_path=drafts), use("Write", file_path="/private/tmp/scratch/x.py")),
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
        log = os.path.join(t, "bad.jsonl")
        with open(log, "w", encoding="utf-8") as f:
            f.write("\n".join(bad) + "\n")
        r = close_report(log, ("outbound-drafts.md",))
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
        check("a repo with no remote counts only the commit the session's own git commit printed",
              [{k: v for k, v in c.items() if k != "repo"} for c in r["commits"]] == [{"no_remote": True, "made": 1}])
        check("anything open sets the exit to 1", r["open"])

        subprocess.run(["git", "-C", repo, *env, "commit", "-qam", "done"], check=True)
        # SendMessage addresses a peer by its uds socket as often as by its name.
        by_socket = ('<cross-session-message from="uds:/tmp/cc-socks/2.sock" from-session="local_peer4" '
                     'from-name="Cleanup" from-mode="prompting">Is that worktree yours?</cross-session-message>')
        good = [bad[0], rec("10:01", "assistant", use("Edit", file_path=os.path.join(repo, "dirty.txt"))),
                spawn("bg1"), rec("10:02", "user", res("Command running in background with ID: bdone1.", "bg1")),
                rec("10:03", "user", {"type": "text", "text": notice("bdone1")}),
                queued("10:04", peer, "queue"), rec("10:06", "assistant", use("SendMessage", to="Docs page", message="here")),
                queued("10:07", by_socket, "queue"),
                rec("10:08", "assistant", use("SendMessage", to="uds:/tmp/cc-socks/2.sock", message="no"))]
        with open(log, "w", encoding="utf-8") as f:
            f.write("\n".join(good) + "\n")
        r = close_report(log)
        check("a clean session (committed, notified, replied) has nothing open",
              not r["open"] and not r["edits"]["uncommitted"] and not r["peers"]["no_reply"]
              and not r["background"]["no_notice"])
        check("the report renders and ends on the judgement line", "still a judgement" in render(r))
        # A subagent can finish with only its hand-back, no task notice. A progress note from one still running, a hand-back quoted in a peer's message or echoed in a tool result
        # finish nothing.
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
        with open(log, "w", encoding="utf-8") as f:
            f.write("\n".join(hb) + "\n")
        check("a hand-back with no task notice finishes its agent; a progress note, a hand-back quoted by a peer "
              "or echoed in a tool result does not",
              close_report(log)["background"] == {"started": 4, "no_notice": ids[1:]})
        with open(log, "w", encoding="utf-8") as f:
            f.write(queued("10:04", peer, "queue") + "\n")
        check("an unanswered question alone is enough to leave the session open", close_report(log)["open"])
        check("a missing session id finds no log", find_log("zzzz-no-such", t) is None)

        # A repo with a remote: the session's unpushed commit is listed; a pushed one, another session's,
        # and one rebased away (no local branch holds it) are not.
        bare = os.path.join(t, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", bare], check=True)
        subprocess.run(["git", "-C", repo, "push", "-q", "origin", "HEAD"], check=True, capture_output=True)
        pushed = head(repo)
        subprocess.run(["git", "-C", repo, "checkout", "-qb", "gone"], check=True)
        subprocess.run(["git", "-C", repo, *env, "commit", "-q", "--allow-empty", "-m", "rebased away"], check=True)
        gone = head(repo)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-"], check=True)
        subprocess.run(["git", "-C", repo, "branch", "-qD", "gone"], check=True)
        subprocess.run(["git", "-C", repo, *env, "commit", "-q", "--allow-empty", "-m", "not pushed yet"], check=True)
        later = head(repo)
        branch = subprocess.run(["git", "-C", repo, "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
        with open(log, "w", encoding="utf-8") as f:
            f.write("\n".join(committed("tu2", repo, f"{pushed[:7]} done") + committed("tu3", repo, f"{gone[:8]} rebased away")
                              + committed("tu4", repo, f"[{branch} {later[:7]}] not pushed yet")) + "\n")
        r = close_report(log)
        check("an unpushed commit the session made is listed; pushed, rebased-away and other sessions' are not",
              [c.get("subject") for c in r["commits"]] == ["not pushed yet"] and r["open"])
        subprocess.run(["git", "-C", repo, "push", "-q", "origin", "HEAD"], check=True, capture_output=True)
        check("once pushed, it is not", close_report(log)["commits"] == [])

    print(f"selftest: {sum(results)} of {len(results)} checks passed")
    return 0 if all(results) else 1


def main():
    ap = argparse.ArgumentParser(prog="session_close.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="session id or prefix (default: the newest main log)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--drafts", action="append", default=None,
                    help="a file name or path ending that counts as a drafts file (repeatable; default: the "
                         "config key session_close.drafts)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    log = find_log(a.session)
    if not log:
        print(f"no session log{' for ' + a.session if a.session else ''} under {PROJECTS}")
        return 2
    try:
        r = close_report(log, a.drafts if a.drafts is not None else configured_drafts())
    except OSError as e:
        print(f"could not read {log}: {e}")
        return 2
    print(json.dumps(r, indent=1) if a.json else render(r))
    return 1 if r["open"] else 0


if __name__ == "__main__":
    sys.exit(main())
