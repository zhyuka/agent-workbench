#!/usr/bin/env python3
"""ledger.py - the request ledger: every message the user typed and every card answer, each with a state.

    python tools/ledger.py                    read the new lines of every main session log, re-link, write
    python tools/ledger.py --rebuild          forget the offsets and read every log from the start
    python tools/ledger.py --open [--session ID]   print the open requests (one session, or all), oldest first,
                                              each with the id a card names it by
    python tools/ledger.py --questions       print the open questions put off on a card, with their ids
    python tools/ledger.py --calibrate LABELS score kind and state against a file of hand labels
    python tools/ledger.py --selftest         planted logs: each source, each state, offsets, exclusions

WHY THIS EXISTS. A request or a decision the user typed lives only in the session's log unless an
agent chooses to save it, and a compaction summary drops it within two or three compactions. A user
can answer a thousand cards in a fortnight while the durable stores gain a dozen rulings, and a
proposal answered only in chat is lost that way. This keeps one record of what was asked for and
decided, with a state that says where each one landed, so a session close, the daily digest and
DIGEST.md can show what has no home.

WHAT IT READS. Every main log, ~/.claude/projects/*/<uuid>.jsonl. Subagent logs (<uuid>/subagents/)
are not read. A session is skipped when its entrypoint is sdk-* (headless `claude -p`), its working
folder is a scratch folder (/private/tmp, /tmp, /private/var/folders) or one listed in
`ledger.skip_cwd`, or its first message starts with a prefix in `ledger.skip_first` (a scripted
prompt some tool sends). Backup copies of logs are not read.

WHERE IT WRITES. `ledger.dir` in the agent-workbench config, default ~/.local/share/agent-workbench/ledger:
requests.jsonl (the entries), state.json (offsets), .cache/ (git scans). --dir overrides it.

WHAT IT RECORDS, one JSON line per entry in requests.jsonl:
  - every message the user typed: a user turn whose origin is human (or, in older logs with no
    origin, one that is not a harness line), and a message sent mid-turn (an attachment
    queued_command with humanTurn). Not a turn whose turnOrigin names another sender, such as "sdk"
    for a prompt a script sent into a desktop session. Not tool results, compaction summaries,
    messages from other sessions, task notifications, skill bodies, system reminders or built-in slash commands. A typed
    slash command keeps its words as "/name args". A "Do this task here" click and a session's first
    message that is a spawn_task prompt are kept with source chip / spawned, since the words are an
    agent's.
  - every AskUserQuestion question with the answer, verbatim: card_id, q_index, header, question,
    options, recommended, recommended_source, answer, answer_note, plus answer_kind (recommended,
    other option, typed) and differs.
  Text is kept verbatim up to 6,000 characters (cut marked) after credential shapes are redacted.

KIND. request (asks for work), decision (states a choice, a rule or a fact the user holds), answer (a
card pick, or a short reply to the agent's question), question (a card question asked TO the user
that was put off: "Later", "Not yet", "Not now", "Defer", "Hold..." or "Wait...", with a "Now"
option beside it or not, or a lone "Park..." that names no home, such as "Park until the name is
set"), other (chat, a question about status, a paste). Plain patterns, scored on hand labels by
--calibrate. A free-text card answer is classified like a typed message, since "figure it out" on a
card is a request, unless the words lead with a put-off ("not yet. we're still in call", "hold for
the pool owner"): a question too.

STATE, with the evidence it rests on in `ref` and the rule that set it in `why`:
  done      a commit (sha) the session made after it, before its next request, or files it edited; a
            reply that leads with a result, answers a check or a question, or says there is nothing
            to do; a reply that handed the next step to the user, when the user's next message took
            it from there; a task chip offered for work sent to another session; or a commit message
            elsewhere that quotes its words
  filed     a work item (id) the session created after it with ado.py, or one whose text quotes it
  ruled     a rulings file entry (id) the session wrote after it, or one whose source quotes it
  card      a memory card (file) the session wrote after it, or one that quotes its words
  dropped   the user's own later words in that session withdrawing it (quoted)
  answered  a card pick, or a short reply to the agent's question; for a question put off, the
            answer on a later card that names its id or asks it again; for an open request, the
            pick on a later card that names its id
  open     none of the above: no home outside this log
The thread of a request is the request and the answers and short replies that follow it in the same
session, up to the next request or decision; its evidence is the union over the thread.
  - Research or a lasting fix ("research", "/ambition", "from root", "figure it out") is settled only
    by an edit, a commit, a store entry or a reply that leads with a result. One answered in chat
    that ends on a proposal or "needs you" stays open: that is how proposals get lost.
  - Any other request stays open when the session ends on a step only the user can take.
  - Asked for later in the user's own words ("remind me", "verify it when it lands", "own session"),
    or handed on by a card pick that sends the work to another session, a queue or a work tracker
    ("Separate session", "Park it in ADO"), is settled only by an artifact or a reply about it, or a
    chip offered for it. Only these two may close on a reply that leads with a result, since the
    reply is the work asked for or shows the hand-off happened; for a card pick it must share two of
    the card's words, not one. A card pick that was put off never closes this way: it is a question,
    below.
  - A question put off (kind question) gets a stable id, q- and six hex digits of its entry id, and
    stays open in DIGEST.md until a later card names that id (a follow-up, or a triage pass) or
    asks the same question, and it is answered there: then it is answered, with that card in `ref`.
    Skipped there, it stays open; put off again there, the later card carries it (repeated_as) and
    it is listed once. Nothing else closes it: not a reply about it, a commit, an edit or a quote.
    A put-off question that names a work item or a draft is held where it lives, so it stays an
    answer.
  - An open request goes by the first 8 hex digits of its entry id, which --open and DIGEST.md print;
    a card pick's entry id (card:<tool id>:<n>) has none, so it goes by the first 8 of that id's sha1.
    It is answered when a later card, in any session, names that id and the pick on the newest such
    card does not leave it open ("Later", "Leave...", "Keep...", "No", "Skip"...).
  - Typed again word for word: listed once, at the latest copy (repeated_as). Added to before any
    reply: takes the state of the request that followed, which points back (follow_up_of). Neither
    holds for a card pick: the same pick on two cards is two requests.
  - A ruled entry carries scope (the ruling's match block and the tools its source names) and
    supersedes (an earlier ledger entry ruled under the same id).
Decisions take the same states, with open meaning "only in the log".

THE STORES it links against, each optional (agent-workbench config, key `ledger`):
  repos      git repos whose commits (any branch) can quote a request
  memory     memory folders (default every ~/.claude/projects/*/memory)
  rulings    a JSON rulings file: {"rulings": [{"id", "topic", "ruling", "source", "date", "match",
             "reversed", "supersedes"}]}; an Edit or Write to a file of that name records a ruling
  ado_cache  the work-item cache ado_rest.py writes (default: its path for ado.org/ado.project)
  role       the label each entry carries in decided_by_role (default "user")
  user_names names that mark a diff line as possibly quoting the user (with quote marks)
A configured store that cannot be read is named and the run exits 2; an unconfigured one is skipped.

NO MODEL PASS. Plain patterns only. A model reading one log cannot see what landed in another
session or went moot either, which is where most wrong opens come from.

INCREMENTAL. state.json keeps, per log file, the byte offset of the last whole line read (with size
and inode, so a rewritten file is read again from the start), and per session the entry new evidence
attaches to and the cards still waiting on an answer. A run reads only new lines. Entries already
done, filed, ruled, carded, dropped or answered keep their state; the open ones are linked again
every run.

Exit 0 written; 2 a log or a store could not be read (named), the ledger is still written.
"""
import argparse
import bisect
import contextlib
import datetime as dt
import fcntl
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import kit_config  # noqa: E402

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
# The Claude desktop app's record of each Code session (title, app id), read for titles and links.
CCD_SESSIONS = os.path.join(HOME, "Library", "Application Support", "Claude", "claude-code-sessions")
SCRATCH = ("/private/tmp/", "/tmp/", "/private/var/folders/")
MAX_TEXT = 6000


def _cfg(key, default=None):
    v = kit_config.get("ledger", key)
    return default if v in (None, "", [], {}) else v


def _x(p):
    return os.path.expanduser(p) if isinstance(p, str) else p


LEDGER_DIR = _x(_cfg("dir", os.path.join(HOME, ".local", "share", "agent-workbench", "ledger")))
SKIP_CWD = SCRATCH + tuple(_x(p).rstrip("/") + "/" for p in _cfg("skip_cwd", []))
SKIP_FIRST = tuple(_cfg("skip_first", []))
ROLE = _cfg("role", "user")
RULINGS = _x(_cfg("rulings"))          # None: no rulings file
RULINGS_NAME = os.path.basename(RULINGS) if RULINGS else "rulings.json"
REPOS = [_x(p) for p in _cfg("repos", [])]
USER_NAMES = list(_cfg("user_names", []))
GIT_EXCLUDE = [":(exclude)" + p for p in _cfg("git_exclude", [])]


def default_memory():
    m = _cfg("memory")
    if m:
        return [_x(p) for p in (m if isinstance(m, list) else [m])]
    return sorted(glob.glob(os.path.join(PROJECTS, "*", "memory")))


def default_ado_cache():
    """ledger.ado_cache, else the cache ado_rest.py writes for the configured org and project."""
    p = _cfg("ado_cache")
    if p:
        return _x(p)
    org, proj = kit_config.ado()
    if not (org and proj):
        return None
    slug = lambda s: re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "unset"
    return os.path.join(HOME, ".cache", kit_config.NAME, "ado", f"{slug(org.rsplit('/', 1)[-1])}--{slug(proj)}.json")


SETTLED = ("done", "filed", "ruled", "card", "dropped", "answered")

# ---------------------------------------------------------------- redaction
_SECRET_RXS = [
    re.compile(r'figd_[A-Za-z0-9_-]{10,}'),
    re.compile(r'glpat-[A-Za-z0-9_-]{10,}'),
    re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),
    re.compile(r'sk-(?:ant-)?[A-Za-z0-9_-]{16,}'),
    re.compile(r'xox[abprs]-[A-Za-z0-9-]{10,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'),
    re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}'),
    re.compile(r'(?i)\b(token|pat|password|passwd|secret|api[\s_-]?key|client[\s_-]?secret|access[\s_-]?key)\b'
               r'(\s*(?:[:=]|is)\s*)["\']?[^\s"\',;]{8,}'),
    re.compile(r'(?i)\b(instrumentationkey|accountkey|sharedaccesskey|sig)=[^;&\s]+'),
    re.compile(r'://[^/\s:@]+:[^/\s@]+@'),
]
_LONG_RX = re.compile(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])')


def redact(s):
    """Credential shapes out of a string. Pure hex (a sha) and slugs of short words are left alone."""
    if not isinstance(s, str) or not s:
        return s
    for rx in _SECRET_RXS:
        s = rx.sub("[REDACTED]", s)

    def long_sub(m):
        t = m.group(0)
        if re.fullmatch(r"[0-9a-fA-F]+", t):
            return t
        segs = re.split(r"[-_/.]", t)
        if len(segs) >= 3 and all(len(x) <= 16 and (x.isalpha() or x.isdigit() or x.islower() or not x) for x in segs):
            return t
        if re.search(r"[A-Za-z]", t) and re.search(r"[0-9]", t):
            return "[REDACTED]"
        return t
    return _LONG_RX.sub(long_sub, s)


def red_any(v):
    if isinstance(v, list):
        return [red_any(x) for x in v]
    return redact(v) if isinstance(v, str) else v


def cap(s, n=MAX_TEXT):
    s = s or ""
    return s if len(s) <= n else s[:n] + f" [... {len(s) - n} more characters cut]"


# ---------------------------------------------------------------- what counts as typed
HARNESS = re.compile(
    r"^\s*(\[Request interrupted|<local-command-stdout>|<local-command-stderr>|Caveat: The messages below|"
    r"<system-reminder>|API Error|This session is being continued from a previous conversation|"
    r"<task-notification>|<cross-session-message|Another Claude session sent a message|"
    r"\[Cross-session idle notice\]|Base directory for this skill|<agent-message|"
    r"Your claude\.ai usage limit has reset|\[Image: original|<bash-input>|<bash-stdout>|<bash-stderr>|"
    r"<user-memory-input>|Unknown skill:|Continue from where you left off|<scheduled-task)", re.I)
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
COMMAND = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
BUILTIN = {"clear", "compact", "model", "config", "cost", "status", "login", "logout", "resume", "effort", "fast",
           "mcp", "permissions", "memory", "help", "doctor", "exit", "rename", "context", "usage", "hooks",
           "agents", "plugin", "plugins", "add-dir", "ide", "theme", "vim", "terminal-setup", "export", "release-notes",
           "upgrade", "bug", "feedback", "privacy-settings", "statusline", "output-style", "keybindings", "todos",
           "rewind", "tasks", "sandbox", "skills", "extra-usage", "review", "pr-comments", "install-github-app"}


def clean_text(raw):
    """The words a person typed, from a user turn's text, or None for a harness line or a built-in command."""
    t = REMINDER.sub("", raw or "").strip()
    if not t:
        return None
    m = COMMAND.search(t)
    if m:
        name = m.group(1).strip()
        a = COMMAND_ARGS.search(t)
        args = (a.group(1) if a else "").strip()
        if name in BUILTIN:
            return None
        return f"/{name} {args}".strip()
    if HARNESS.match(t):
        return None
    return t


def blocks(r):
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c or [] if isinstance(b, dict)]


def typed_of(r):
    """(source, text, images) for a record the user typed, else None."""
    if r.get("type") == "attachment":
        a = r.get("attachment") or {}
        if a.get("type") != "queued_command" or a.get("humanTurn") is not True:
            return None
        p = a.get("prompt")
        parts = ([b.get("text") or "" for b in p if isinstance(b, dict) and b.get("type") == "text"]
                 if isinstance(p, list) else [p or ""])
        imgs = sum(1 for b in p if isinstance(b, dict) and b.get("type") == "image") if isinstance(p, list) else 0
        t = clean_text("\n".join(parts))
        return ("mid-turn", t, imgs) if t else None
    if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary") or r.get("isSidechain"):
        return None
    origin = r.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind and kind != "human":
        return None
    # a prompt a script sent into a desktop session (a brief, a probe, a bench run) carries no origin and
    # turnOrigin "sdk"; the user's own turns there carry turnOrigin "human"
    if r.get("turnOrigin") not in (None, "human"):
        return None
    bl = blocks(r)
    if any(b.get("type") == "tool_result" for b in bl):
        return None
    t = clean_text("\n".join(b.get("text") or "" for b in bl if b.get("type") == "text"))
    imgs = sum(1 for b in bl if b.get("type") == "image")
    return ("typed", t, imgs) if t else None


# ---------------------------------------------------------------- evidence patterns
GIT_COMMIT = re.compile(r"\bgit\b[^\n|;&]*\bcommit\b")
GIT_PUSH = re.compile(r"\bgit\b[^\n|;&]*\bpush\b")
ADO_CREATE = re.compile(r"\bado(?:_write|_rest)?\.py\b[^\n]*\b(create|new|add|file)\b")
SHA_LINE = re.compile(r"^\[([^\]\s]+)(?: \(root-commit\))? ([0-9a-f]{7,40})\] (.*)$", re.M)
PUSHED = re.compile(r"^\s*([0-9a-f]{7,40})\.\.([0-9a-f]{7,40})\s+\S+\s+->\s+(\S+)", re.M)
ADO_MADE = re.compile(r"\bcreated #(\d{3,6})\b|\"id\":\s*(\d{3,6}),\s*\"rev\":\s*1\b")
RULING_ID = re.compile(r'"id"\s*:\s*"([a-z0-9][a-z0-9-]{2,80})"')
REPO_IN_CMD = re.compile(r"(?:\bcd|git -C)\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)")
SPAWN_TOOLS = ("mcp__ccd_session__spawn_task",)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def head_tail(s, n=700):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n // 2] + " [...] " + s[-n // 2:]


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9#/:. ]+", " ", (s or "").lower())).strip()


def recommended_of(labels):
    """The labels marked (Recommended); if none, the first label."""
    rec = [l for l in labels if re.search(r"\(recommended\)", l, re.I)]
    if rec:
        return rec, "labelled"
    return (labels[:1], "first") if labels else ([], "first")


# ---------------------------------------------------------------- the reader
class Reader:
    """Reads new lines of main logs into entries, attaching later evidence to the latest entry of each
    session. Everything it needs between runs is in self.state."""

    def __init__(self, state, entries):
        self.state = state
        self.entries = entries          # id -> entry
        self.state.setdefault("files", {})
        self.state.setdefault("sessions", {})
        self.state.setdefault("spawns", {})
        self.problems = []

    # --- per session
    def sess(self, sid):
        return self.state["sessions"].setdefault(sid, {"first": None, "last": None, "entry": None, "cards": {},
                                                       "cmds": {}, "skip": None, "cwd": None, "branch": None,
                                                       "entrypoint": None, "title": None, "n": 0})

    def ev(self, s):
        e = self.entries.get(s["entry"]) if s.get("entry") else None
        if e is None:
            return None
        return e.setdefault("ev", {})

    def add_entry(self, s, sid, e):
        if e["id"] in self.entries:
            s["entry"] = e["id"]      # a copy of a record already read (a resumed or forked log)
            return
        e["session"] = sid
        e["decided_by_role"] = ROLE
        e["visibility"] = "personal"
        if s.get("title"):
            e["title"] = s["title"]
        refs = {}
        if s.get("cwd"):
            refs["worktree"] = s["cwd"]
        if s.get("branch"):
            refs["branch"] = s["branch"]
        if refs:
            e["state_refs"] = refs
        self.entries[e["id"]] = e
        s["entry"] = e["id"]
        s["n"] += 1

    # --- per file
    def scan(self, paths):
        for path in sorted(paths, key=lambda p: (os.path.getmtime(p) if os.path.exists(p) else 0, p)):
            try:
                self.read_file(path)
            except OSError as e:
                self.problems.append(f"could not read {path}: {type(e).__name__}: {e}")

    def read_file(self, path):
        st = os.stat(path)
        f = self.state["files"].get(path) or {}
        off = f.get("off", 0)
        if f.get("ino") != st.st_ino or st.st_size < off:
            off = 0
        sid_file = os.path.basename(path)[:-len(".jsonl")]
        if st.st_size == off:
            return
        with open(path, "rb") as fh:
            fh.seek(off)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break                     # a line still being written: read it next run
                off += len(raw)
                self.line(sid_file, raw)
        self.state["files"][path] = {"off": off, "size": st.st_size, "ino": st.st_ino, "session": sid_file}

    def line(self, sid, raw):
        s = self.sess(sid)
        if s.get("skip"):
            return
        # Most bytes are tool results nobody waits on and attachments that are not a queued message.
        if b'"tool_result"' in raw and b'"isCompactSummary"' not in raw and not any(
                k.encode() in raw for k in list(s["cards"]) + list(s["cmds"])):
            m = re.search(rb'"timestamp":"([^"]+)"', raw)
            if m:
                s["last"] = m.group(1).decode()
            return
        if b'"type":"attachment"' in raw and b'queued_command' not in raw:
            return
        try:
            r = json.loads(raw)
        except ValueError:
            return
        if not isinstance(r, dict):
            return
        t = r.get("type")
        ts = r.get("timestamp")
        if ts:
            s["first"] = s["first"] or ts
            s["last"] = ts
        if r.get("entrypoint") and not s["entrypoint"]:
            s["entrypoint"] = r["entrypoint"]
            if str(r["entrypoint"]).startswith("sdk"):
                s["skip"] = f"entrypoint {r['entrypoint']}"
                return
        if r.get("cwd"):
            if not s["cwd"]:
                s["cwd"] = r["cwd"]
                if (r["cwd"].rstrip("/") + "/").startswith(SKIP_CWD):
                    s["skip"] = f"cwd {r['cwd']}"
                    return
        if r.get("gitBranch"):
            s["branch"] = r["gitBranch"]
        if t == "custom-title" and r.get("customTitle"):
            s["title"], s["custom"] = r["customTitle"], True
            return
        if t == "ai-title" and r.get("aiTitle"):
            if not s.get("custom"):
                s["title"] = r["aiTitle"]
            return
        if r.get("isCompactSummary"):
            ev = self.ev(s)
            if ev is not None:
                ev["compacted"] = ev.get("compacted", 0) + 1
            return
        got = typed_of(r)
        if got:
            source, text, imgs = got
            if s["n"] == 0 and SKIP_FIRST and text.startswith(SKIP_FIRST):
                s["skip"] = "a scripted first prompt (ledger.skip_first)"
                return
            e = {"id": r.get("uuid") or hashlib.sha1(raw).hexdigest()[:20], "ts": ts, "source": source,
                 "text": cap(redact(text))}
            if imgs:
                e["images"] = imgs
            if s["n"] == 0:
                e["first_in_session"] = True
            self.add_entry(s, sid, e)
            return
        if t == "assistant":
            self.assistant(s, sid, r)
        elif t == "user":
            self.results(s, sid, r)

    def assistant(self, s, sid, r):
        ev = self.ev(s)
        for b in blocks(r):
            bt = b.get("type")
            if bt == "text" and (b.get("text") or "").strip():
                if ev is not None:
                    ev["reply"] = head_tail(redact(b["text"]), 900)
                    ev["replies"] = ev.get("replies", 0) + 1
            elif bt == "tool_use":
                name, inp, tid = b.get("name"), b.get("input") or {}, b.get("id")
                if name == "AskUserQuestion" and tid:
                    qs = [q for q in inp.get("questions") or [] if isinstance(q, dict)]
                    s["cards"][tid] = {"ts": r.get("timestamp"), "qs": [
                        {"question": q.get("question") or "", "header": q.get("header") or "",
                         "multi": bool(q.get("multiSelect")),
                         "options": [{"label": o.get("label") or "", "description": o.get("description") or ""}
                                     for o in q.get("options") or [] if isinstance(o, dict)]} for q in qs]}
                    if ev is not None:
                        ev["asked"] = ev.get("asked", 0) + 1
                elif name == "Bash" and tid:
                    cmd = inp.get("command") or ""
                    if GIT_COMMIT.search(cmd):
                        s["cmds"][tid] = {"kind": "commit", "cmd": cmd[:400]}
                    elif GIT_PUSH.search(cmd):
                        s["cmds"][tid] = {"kind": "push", "cmd": cmd[:400]}
                    elif ADO_CREATE.search(cmd):
                        s["cmds"][tid] = {"kind": "ado", "cmd": cmd[:400]}
                elif name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
                    p = inp.get("file_path") or inp.get("notebook_path") or ""
                    if ev is None or not p:
                        continue
                    body = (inp.get("new_string") or inp.get("content") or "") + " ".join(
                        x.get("new_string") or "" for x in inp.get("edits") or [] if isinstance(x, dict))
                    if os.path.basename(p) == RULINGS_NAME:
                        ev.setdefault("rulings", [])
                        for rid in RULING_ID.findall(body):
                            if rid not in ev["rulings"]:
                                ev["rulings"].append(rid)
                    elif "/memory/" in p and p.endswith(".md") and not p.endswith("MEMORY.md"):
                        ev.setdefault("cards", [])
                        if os.path.basename(p) not in ev["cards"]:
                            ev["cards"].append(os.path.basename(p))
                    elif not p.startswith(SCRATCH):
                        ev.setdefault("edits", [])
                        if p not in ev["edits"] and len(ev["edits"]) < 30:
                            ev["edits"].append(p)
                elif name in SPAWN_TOOLS:
                    title, prompt = (inp.get("title") or "").strip(), (inp.get("prompt") or "").strip()
                    if title or prompt:
                        self.state["spawns"][hashlib.sha1(norm(prompt)[:300].encode()).hexdigest()[:16]] = title
                        self.state["spawns"]["title:" + norm(title)] = title
                    if ev is not None:
                        ev.setdefault("chips", [])
                        if title and title not in ev["chips"]:
                            ev["chips"].append(title)

    def results(self, s, sid, r):
        for b in blocks(r):
            if b.get("type") != "tool_result":
                continue
            tid = b.get("tool_use_id")
            content = b.get("content")
            text = content if isinstance(content, str) else " ".join(
                x.get("text", "") for x in content or [] if isinstance(x, dict))
            if tid in s["cards"]:
                self.card(s, sid, r, tid, s["cards"].pop(tid), b, text)
            elif tid in s["cmds"]:
                c = s["cmds"].pop(tid)
                ev = self.ev(s)
                if ev is None:
                    continue
                if c["kind"] == "commit":
                    repo = [d.strip("\"'") for d in REPO_IN_CMD.findall(c["cmd"])]
                    found = [(m.group(2), m.group(3)) for m in SHA_LINE.finditer(text)]
                    for sha, subj in found:
                        ev.setdefault("commits", [])
                        if sha not in [x[0] for x in ev["commits"]]:
                            ev["commits"].append([sha, redact(subj[:120]), repo[0] if repo else ""])
                elif c["kind"] == "push":
                    for m in PUSHED.finditer(text):
                        ev.setdefault("pushes", [])
                        if m.group(2) not in [x[0] for x in ev["pushes"]]:
                            ev["pushes"].append([m.group(2), m.group(3)])
                elif c["kind"] == "ado":
                    for m in ADO_MADE.finditer(text):
                        i = m.group(1) or m.group(2)
                        ev.setdefault("ado", [])
                        if i not in ev["ado"]:
                            ev["ado"].append(i)

    def card(self, s, sid, r, tid, card, b, text):
        tur = r.get("toolUseResult") if isinstance(r.get("toolUseResult"), dict) else {}
        answers = tur.get("answers") if isinstance(tur.get("answers"), dict) else None
        notes = tur.get("annotations") if isinstance(tur.get("annotations"), dict) else {}
        qs = card["qs"]
        for i, q in enumerate(qs):
            labels = [o["label"] for o in q["options"]]
            rec, rec_src = recommended_of(labels)
            ans = None
            status = "answered"
            if answers is not None:
                ans = answers.get(q["question"])
                if ans is None and len(qs) == 1 and len(answers) == 1:
                    ans = next(iter(answers.values()))
            else:
                m = re.search(r"the user (?:said|wrote|responded)[^:]*:\s*(.+)", text or "", re.I | re.S)
                if m:
                    ans = m.group(1).strip()
                    status = "declined"
                elif b.get("is_error"):
                    status = "declined"
                else:
                    status = "error"
            if ans is None and status == "answered":
                status = "answered_unparsed"
            if isinstance(ans, str) and q["multi"] and ", " in ans and all(p in labels for p in ans.split(", ")):
                picked = ans.split(", ")
            else:
                picked = ans if isinstance(ans, list) else [ans] if ans is not None else []
            if not picked:
                kind = None
            elif set(picked) == set(rec):
                kind = "recommended"
            elif all(p in labels for p in picked):
                kind = "other option"
            else:
                kind = "typed"
            note = (notes.get(q["question"]) or {}).get("notes") if isinstance(notes.get(q["question"]), dict) else None
            text_out = ans if isinstance(ans, str) else ", ".join(str(x) for x in ans) if isinstance(ans, list) else ""
            if note:
                text_out = f"{text_out} (note: {note})" if text_out else note
            e = {"id": f"card:{tid}:{i}", "ts": r.get("timestamp"), "source": "card", "card_id": tid, "q_index": i,
                 "n_questions": len(qs), "asked_at": card.get("ts"), "status": status,
                 "header": redact(q["header"]), "question": cap(redact(q["question"]), 1500),
                 "multi_select": q["multi"], "options": [{"label": redact(o["label"]),
                                                          "description": redact(o["description"])[:300]}
                                                         for o in q["options"]],
                 "recommended": red_any(rec), "recommended_source": rec_src, "answer": red_any(ans),
                 "text": cap(redact(text_out))}
            if note:
                e["answer_note"] = redact(note)
            if kind:
                e["answer_kind"] = kind
                e["differs"] = kind != "recommended"
            self.add_entry(s, sid, e)


# ---------------------------------------------------------------- store
def load_jsonl(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    e = json.loads(line)
                    out[e["id"]] = e
    except FileNotFoundError:
        pass
    return out


def write_atomic(path, text):
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


def main_logs(projects=PROJECTS):
    return [p for p in glob.glob(os.path.join(projects, "*", "*.jsonl"))]


# ---------------------------------------------------------------- kind
URL = re.compile(r"https?://\S+")
PASTE = re.compile(r"<pasted_content[^>]*>.*?(</pasted_content>|$)", re.S)
OTHER_RX = re.compile(
    r"\b(am i good to archive|good to archive|archive (this|the) session|i'?m archiving|ok,? archive|"
    r"has everything (from this session )?been addressed|does anything need to be told|"
    r"i hit my usage limit|please continue from where you left off)\b|^(ok,? )?archive( it| this| this session| the session)?\W*$")
CONTINUE_RX = re.compile(r"^(ok[,.]?\s*)?(continue|go on|resume|proceed|try again|carry on|next|progress|status|"
                         r"any update|update\??|where are we|what'?s next|what next|eta)\b[\s.!?,]*(askuserquestion)?[\s.!?]*$")
ACK_RX = re.compile(r"^(thanks|thank you|ty|cool|nice|great|perfect|awesome|ok|okay|k|kk|got it|sounds good|lgtm|"
                    r"done|sent|posted|signed in|logged in)\b[\s.!]*$")
ANSWER_START = re.compile(
    r"^(yes|yep|yeah|ya|yup|y|sure|ok|okay|go|go ahead|do it|approve[d]?|confirm(ed)?|correct|right|no|nope|nah|"
    r"neither|both|all|none|option|\d+|[a-d]|first|second|third|later|drop|rewrite|skip|hold|leave it|keep|"
    r"post it|push it|ship it|merge it|fast-forward|ff|archive|close it|trash it|delete it|send it|commit it|"
    r"agree|sounds good|fine|that works|works)\b")
REQUEST_STRONG = re.compile(
    r"\b(can (u|you)|could (u|you)|would (u|you)|will (u|you)|pls|plz|please|make sure|i want|i need|need (u|you) to|"
    r"let'?s|u should|you should|remind me|figure (it |this |that )?out|from root|root fix|do the research|research|"
    r"lmk|let me know|tell me|show me|give me|send me|"
    r"look into|dig into|investigate|think bigger|higher|keep iterating|keep going|did (u|you) even|"
    r"why (r|are|did|didn'?t|isn'?t|is|was|wasn'?t|do|does|doesn'?t|would|wouldn'?t) |why (u|you|not)\b|how come|"
    r"doesn'?t (look|seem|work)|does not|still (broken|off|wrong|not|missing|there|showing)|not fixed|broke again|"
    r"it broke|is (wrong|off|broken|missing)|should (be|have|not|n'?t))")
VERBS = ("fix add remove build make create update change push commit post file check verify research look find "
         "investigate review test run rerun re-run open close delete trash archive move rename copy send draft write "
         "clean refactor sweep audit implement ship deploy merge rebase restart kill stop start set setup install show "
         "explain summarize summarise compare measure prove think plan design recall remind track park apply redo retry "
         "reanchor re-anchor redraw regenerate restore revert reply edit include exclude drop keep tell message ping "
         "list give get put use do try help mark undo convert match align center centre swap replace split merge load "
         "upload download attach capture screenshot sync bump pin unpin tag label rewrite cut shorten expand "
         "remember note record save log wait").split()
LEAD = re.compile(r"^((ok|okay|so|also|and|then|now|ya|yes|but|well|hm+|oh|great|cool|alright|right|actually|"
                  r"btw|pls|please|just|go ahead and|can u|can you|maybe|perhaps|i think|i guess|lets|let's)[\s,.!]+)+")
DECISION_RX = re.compile(
    r"^(go with|use |keep (?!going|reading|iterating|at it|it up|trying|pushing|working|digging)|leave |stick with|let'?s go with|we'?ll go|we will go|don'?t |do not |never |always |"
    r"no need|stop |not needed|no,? |ship (it )?as|as per figma|follow figma)|"
    r"\b(from now on|going forward|for next time|next time|always|never|should always|should never|should stay|"
    r"ship as drawn|as per figma|is my (product |)\w+|is (the|our) (lead|counterpart|owner|pm|po|designer)|"
    r"(he|she|they)'?(s| is| are) (the|my|our|a) |stop (asking|doing|using|posting|sending|making))")
RULE_QUESTION = re.compile(r"\b(what should the rule|should the rule|which way should every|should every|"
                           r"every (page|faq|card|band|desktop|phone)|on every page|across (all|every) pages|all pages|"
                           r"everywhere|scope of|gate scope|site rule|site-wide|sitewide|standing rule|from now on)\b",
                           re.I)
QUOTED = re.compile(r"[\"“][^\"”]{12,}[\"”]")
RESEARCH_RX = re.compile(r"(/ambition|\bresearch\b|figure (it |this |that )?out|(?<!whichever is )(?<!whatever is )smartest|"
                         r"best practice|from root|"
                         r"root fix|think bigger|look into|dig into|investigate|what should happen|"
                         r"what (else )?(have|are) (i|we|u|you) (not )?(missing|considered))")
DEFER_RX = re.compile(r"\b(remind me|later\b|tomorrow|next week|next time|for next time|"
                      r"once [^.?!]{0,60}\b(done|finish\w*|committed|ready|lands|merged|back|pushed)|"
                      r"when [^.?!]{0,60}\b(done|finish\w*|ready|back|replies|replied|lands)|"
                      r"own session|separate session|their own session|"
                      r"park (it|this|that)|keep (it|this) in mind)")
FOLLOW_RX = re.compile(r"\b(did (u|you) even|still|again|not fixed|doesn'?t (seem|look|work)|why (did|didn'?t|is|isn'?t|"
                       r"was|wasn'?t|r|are) (u|you|it|this|that)|i (already )?told (u|you)|already told|broke again|"
                       r"so (u|you) (fixed|checked|verified)|r u sure|are you sure)\b")
DROP_RX = re.compile(r"\b(nvm|never ?mind|forget (it|that|about (it|that))|scratch that|ignore (that|it|my last)|"
                     r"cancel (that|it)|don'?t (do|bother with) (it|that)|no need (to do|for) (it|that)|not needed anymore|"
                     r"don'?t (file|post|send|push|make|build|commit) (it|that|this|them))\b")
# An option that sends the work to another session, a queue or a work tracker. A "Later" or a lone "Park..."
# puts the question off (puts_off_question) before this is read, and "Another session has them" says who has it.
PARK_OPTION = re.compile(r"\b(separate task|own session|their own session|separate session|new session|next session|"
                         r"park|queue)\b", re.I)
# A question that asks which work gets its own session ("Which other moves should get their own session now?"),
# not one that says so in passing ("...stays local for its own session. Push it?") or "get its own value".
OWN_SESSION = re.compile(r"\bown sessions?\b", re.I)
USER_LATER = re.compile(r"\b(me|i'?ll|i will|will) (\w+ )?(later|lyk|let (u|you) know|ping (u|you)|tell (u|you))\b[^.?!]*",
                      re.I)
WORK_VERBS = re.compile(r"\b(fix|build|make|add|update|remove|file|push|commit|post|draft|write|create|change|redo|apply|"
                        r"do|run|re-?run|send|delete|move|rename|include|mock\w*|edit|implement|ship|merge|rebase|install|"
                        r"set ?up|copy|reply|revert|clean|refactor|replace|restore|rebuild|prove|reprove|keep going|"
                        r"keep iterating|think (much )?bigger|have (them|it|that session)|frame)\b")
# A turn that ended on a usage limit or an API error has no answer in it.
NO_ANSWER = re.compile(r"^\W*(you'?ve hit your|you have hit your|api error|claude usage limit|.{0,80}limit resets)", re.I)
# A reply that only says what it will do next ("Now the second full pass.", "First, checking your tab:")
PROGRESS = re.compile(r"^\W*(now|next|first|then|checking|looking|let me|i'?ll|i will|i'?m (now )?(checking|looking|"
                      r"reading|running|pulling|fetching|starting)|starting|pulling|reading|fetching|running|searching|"
                      r"going|on it|before|one moment)\b|:\s*$", re.I)
NOTHING_TO_DO = re.compile(r"\b(nothing (for me )?to (commit|do|push|fix|change)|nothing (left|more) to (do|commit)|"
                           r"no need to|doesn'?t need (redoing|to be redone|a redo|changing))\b", re.I)
PICK_NO_WORK = re.compile(r"^(skip|leave|keep|hold|no\b|don'?t|not now|close|defer|nothing|let|drop|wait|i'?ll|"
                          r"the (browser|other)|they|already)", re.I)
# A pick that puts the card's question off rather than answering it, and one that skips it.
DEFER_PICK = re.compile(r"^(later|not yet|not now|defer|hold|wait)\b", re.I)
PUT_OFF = re.compile(r"^\W*(later|not yet|not now|defer|hold|wait|skip)\b", re.I)
# Typed words that put it off; a bare "wait" or "hold on" is more often "wait, why...?" to the agent.
TYPED_PUT_OFF = re.compile(r"^\W*(later|not yet|not now|defer\w*|hold (for|until|till|off)|wait (for|until|till|on))\b",
                           re.I)
# The only pick, leading with "Park" ("Park until the name is set"), also puts it off unless it names a home
# ("Park it in ADO"). A park beside other work ("Design first, park the rest", one of a multi-select's picks) is
# a request: such parks go to a register or a work tracker.
PARK_PICK = re.compile(r"^\W*park\b", re.I)
HANDS_ON = re.compile(r"\b(ado|session|separate task|queue|register)\b", re.I)
# A put-off question that already lives somewhere: a work item, or a draft in a drafts file
# ("Draft on 12:34 (...): what should happen to it?", "The vendor message: what should happen to it?").
HELD_ELSEWHERE = re.compile(r"(?:#|_workitems/edit/)\d{3,5}\b|\b(draft|message)\b[^?\n]{0,100}:\s*what\b", re.I)
QID = re.compile(r"\bq-[0-9a-f]{6}\b", re.I)
REQ_ID = re.compile(r"\b[0-9a-f]{8}\b", re.I)
# A pick on a card that names an open request and leaves it open ("No longer needed" closes it).
KEEPS_OPEN = re.compile(r"^\W*(later|not yet|not now|defer|hold|wait|skip|leave|keep|don'?t|no\b(?! longer))", re.I)


def picks_of(e):
    """The option labels the user picked on a card, when they picked rather than typed."""
    if e.get("source") != "card" or e.get("status") != "answered" or e.get("answer_kind") not in (
            "recommended", "other option"):
        return []
    labels = [o["label"] for o in e.get("options") or []]
    a = e.get("answer")
    picked = a if isinstance(a, list) else [a or ""]
    if isinstance(a, str) and e.get("multi_select") and ", " in a and all(p in labels for p in a.split(", ")):
        picked = a.split(", ")
    return [p for p in picked if p in labels]


def puts_off_question(e):
    """The pick that puts off a question asked TO the user ("Later", "Defer"..., "Now" beside it or not, or
    a lone "Park..."), the typed words when they lead that way ("not yet. we're still in call"), or None. One
    that hands the work on is a request (classify); a question naming a work item or a draft is held where it
    lives."""
    if HELD_ELSEWHERE.search((e.get("question") or "") + " " + (e.get("header") or "")):
        return None
    if e.get("answer_kind") == "typed":
        t = e.get("text") or ""
        return own_words(t)[:60] if e.get("status") == "answered" and TYPED_PUT_OFF.match(t) else None
    picks = picks_of(e)
    return next((p for p in picks if DEFER_PICK.match(p) or (PARK_PICK.match(p) and len(picks) == 1
                                                              and not HANDS_ON.search(p))), None)


def qid_of(e):
    """The id a put-off question is shown and named by: q- and six hex digits of its entry id."""
    return "q-" + hashlib.sha1(e["id"].encode()).hexdigest()[:6]


def rid_of(e):
    """The id an open request is shown and named by: the first 8 hex digits of its entry id, or of that id's
    sha1 for a card pick (card:<tool id>:<n>), or None."""
    rid = (hashlib.sha1(e["id"].encode()).hexdigest() if e["id"].startswith("card:") else e["id"])[:8].lower()
    return rid if re.fullmatch(r"[0-9a-f]{8}", rid) else None


def own_words(text):
    """The user's words for display, verbatim except: pasted blocks, an HTML comment marker (<!-- reply N -->)
    and agent text quoted with "> " are left out, and whitespace is folded to one line."""
    s = PASTE.sub(" [pasted text] ", text or "")
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = "\n".join(l for l in s.splitlines() if not l.lstrip().startswith(">"))
    return re.sub(r"\s+", " ", s).strip()


def pending_sentence(text, rx):
    """The sentence of a reply where it hands the next step to the user, with the one after it when it ends
    on a colon ("I've stopped where I need you:" says nothing alone)."""
    sents = [p.strip() for p in re.split(r"(?<=[.!?:])\s+", re.sub(r"[*_`#>]+", "", text or "")) if p.strip()]
    for i, p in enumerate(sents):
        if rx.search(p):
            if p.endswith(":") and i + 1 < len(sents):
                p = p + " " + sents[i + 1]
            return re.sub(r"\s+", " ", p).strip(" |-")
    return ""


def words_of(text):
    t = PASTE.sub(" ", text or "")
    t = URL.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def classify_text(text, asked_before):
    """request / decision / answer / other for typed words (a typed message or a free-text card answer)."""
    raw = words_of(text)
    t = raw.lower()
    if not t:
        return "request" if URL.search(text or "") else "other"
    if OTHER_RX.search(t) or CONTINUE_RX.match(t):
        return "other"
    if ACK_RX.match(t):
        return "answer" if asked_before else "other"
    if t.startswith("/"):
        return "request"
    short = len(t) <= 120
    rest = LEAD.sub("", re.sub(r"^(no|nope|nah)\b[\s,.!]*", "", t))
    if re.match(r"^(no|nope|nah)\b", t) and rest.split(" ", 1)[0].strip(",.!?") in VERBS:
        return "request"            # "no, update 1447 as if ..." corrects the proposal with its own instruction
    if asked_before and short and ANSWER_START.match(t) and not (REQUEST_STRONG.search(t) or RESEARCH_RX.search(t)
                                                                 or DEFER_RX.search(t)):
        return "answer"
    if REQUEST_STRONG.search(t) or RESEARCH_RX.search(t):
        if DECISION_RX.match(t) and not re.search(r"\b(can (u|you)|pls|please|fix|research|remind)\b", t):
            return "decision"
        return "request"
    if DECISION_RX.search(t):
        return "decision"
    first = LEAD.sub("", t).split(" ", 1)[0].strip(",.!?:;") if t else ""
    if first in VERBS:
        return "request"
    if URL.search(text or "") and len(t) > 8:
        return "request"
    if t.endswith("?"):
        return "other"
    if asked_before and short:
        return "answer"
    return "other"


def classify(e, asked_before):
    """Kind of one entry. A card pick is an answer, unless the option defers work elsewhere; a free-text card
    answer is read like a typed message."""
    if e["source"] in ("chip", "spawned"):
        return "request"
    if e["source"] == "card":
        if e.get("status") not in ("answered", "declined"):
            return "other"
        opts = {o["label"]: o.get("description") or "" for o in e.get("options") or []}
        q = (e.get("question") or "") + " " + (e.get("header") or "")
        text = e.get("text") or ""
        picked = e.get("answer") if isinstance(e.get("answer"), list) else [e.get("answer") or ""]
        if e.get("answer_kind") == "typed":
            # A typed answer can carry option labels ("..., Queryable record (Recommended), One CLI per service").
            picked = [l for l in opts if l and re.sub(r"\s*\(recommended\)", "", l, flags=re.I) in text]
        if puts_off_question(e):
            return "question"           # asked TO the user and put off: open until a later card answers it
        asks_own = any(OWN_SESSION.search(x) for x in re.findall(r"[^.?!]*\?", q))
        for p in picked:
            if p in opts and not (PICK_NO_WORK.match(p) or USER_LATER.search(p)) and (PARK_OPTION.search(p) or asks_own):
                e["defers"] = True      # the work it picks goes to another session, a queue or a work tracker
                return "request"
        if e.get("answer_kind") == "typed":
            words = re.sub(r"^\s*/[\w:-]+\s*", "", QUOTED.sub(" ", text))
            for l in picked:
                words = words.replace(l, " ").replace(re.sub(r"\s*\(recommended\)", "", l, flags=re.I), " ")
            k = classify_text(words, True)
            if k in ("other", "answer"):
                return "decision" if RULE_QUESTION.search(q) and picked else "answer"
            return k
        if RULE_QUESTION.search(q):
            return "decision"
        return "answer"
    return classify_text(e.get("text") or "", asked_before)


# ---------------------------------------------------------------- state
DONE_LEAD = re.compile(
    r"\b(done|fixed|pushed|committed|posted|sent|updated|added|removed|deleted|installed|created|filed|copied|"
    r"drafted|applied|rebuilt|re-?proved|regenerated|written|wrote|rewrote|moved|archived|cleaned|merged|replaced|"
    r"restored|renamed|recorded|saved|logged|marked|stopped|built|shipped|landed|verified|confirmed|in place|is live|"
    r"are live|on your clipboard|gone|passes|passed|is green|resolved|closed|edited|answered|reverted|undone|"
    r"tagged|attached|switched|set up|re-?ran|ran it|i ran|checked|fast-forwarded|found|replied|finished|works|"
    r"already (on|in|done|pushed|there|fixed|up to date|ran|run|finished|complete)|up to date|here (is|are)|"
    r"(is|are) (now )?on #?\w+|now (reads|shows|says|matches|has|have|carries|carry|extracts?|lists|holds|passes))\b",
    re.I)
NEG_LEAD = re.compile(r"^\W*(nothing|not yet|no[,.]? |i (can'?t|couldn'?t|haven'?t|didn'?t)|can'?t|couldn'?t|unable|blocked|"
                      r"i'?ve stopped where|stopped where|before (i|editing)|first,? |one question|two (things|calls|questions)|"
                      r"three (things|calls|questions))", re.I)
PENDING = re.compile(
    r"\b(say go|your go|needs? (a |your |one |a one-time )?(go|yes|call|decision|sign-in|login|setup|one-time|answer)|"
    r"only you can|your call|want me to|should i\b|shall i\b|do you want|would you like|if you want|waiting on you|"
    r"waits? (on|for) you|needs you|need you|from you\b|neither blocks|when you'?re ready|i'?ll ask you|"
    r"before i (start|edit|change|push|commit)|one question|which (one|way|do you)|ok to (copy|send|push|post|commit)|"
    r"go ahead with these|tell me (when|which|if))", re.I)
USER_PENDING = re.compile(
    r"\b((setup|sign-in|login|go|decision|call|approval|answer|input|yes) from you|needs? you\b|"
    r"need your (go|call|yes|approval|decision)|only you can|your call\b|neither blocks|one-time setup|"
    r"when you('re| are) ready|say go\b|needs your go|waits? on you|waiting on you|stopped where i need you)", re.I)
STOP = set("the a an and or but to of in on at for with from by is are was were be been it its this that these those "
           "i u you we they he she me my our your their them us so too also just then than as if not no yes ok okay "
           "pls please can could would should will do does did done have has had get got make made go going like "
           "what why how when where which who all any some more most much very really even still again now here there "
           "about into over out up down off only own same other others such each every both few many lot lots thing "
           "things stuff etc e g ie im ive dont didnt isnt cant wont thats whats theres youre ur r u y ya yea ".split())


def salient(text):
    """Content words, lightly stemmed, for matching a request to a commit or an artifact."""
    t = re.sub(r"[^a-z0-9#]+", " ", words_of(text).lower().replace("re-anchor", "reanchor").replace("re-prove", "reprove"))
    out = set()
    for w in t.split():
        if not (len(w) >= 4 or re.fullmatch(r"#?\d{3,6}", w) or w in ("os", "ado", "faq", "qa")) or w in STOP:
            continue
        if w.endswith("ied"):
            w = w[:-3] + "y"
        elif len(w) > 5:
            w = re.sub(r"(ing|ed|es|s)$", "", w)
        elif len(w) == 5 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]          # "lands" and "landed" meet at "land"
        out.add(w)
    return out


def shingles(text, n=7):
    w = norm(words_of(text)).split()
    return {" ".join(w[i:i + n]) for i in range(0, max(0, len(w) - n + 1))}


def first_sentence(s):
    s = re.sub(r"[*_`#>]+", "", (s or "").strip())
    m = re.match(r"(.{0,260}?[.!?:])(\s|$)", s, re.S)
    return (m.group(1) if m else s[:260]).strip()


def last_sentence(s):
    """The sentence a passage ends on, whole, for quoting where a thread stopped."""
    s = re.sub(r"[*_`#>]+", "", (s or "").strip())
    parts = [p.strip() for p in re.split(r"(?<=[.!?:])\s+", s) if p.strip()]
    return parts[-1][:260] if parts else ""


class Stores:
    """The durable places a request or decision can land, read once per run. Every read is read-only.
    A store left unconfigured (None) is skipped; one configured and unreadable is named in problems."""

    def __init__(self, since=None, problems=None, cache_dir=None, git_repos=None, memory=None, rulings="default",
                 ado="default", ccd=CCD_SESSIONS):
        self.problems = problems if problems is not None else []
        self.rulings = self._rulings(RULINGS if rulings == "default" else rulings)
        self.cards = self._cards(default_memory() if memory is None else memory)
        self.ado, self.ado_pulled = self._ado(default_ado_cache() if ado == "default" else ado)
        self.ccd = self._ccd(ccd)
        self.cache_dir = cache_dir
        self.git = self._git(git_repos if git_repos is not None else REPOS, since)
        self._quote_index = None

    def _load(self, path, what):
        if not path:
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            self.problems.append(f"{what}: {path} not found")
        except (OSError, ValueError) as e:
            self.problems.append(f"{what}: could not read {path}: {type(e).__name__}")
        return None

    def _rulings(self, path):
        j = self._load(path, "rulings") or {}
        return [r for r in j.get("rulings") or [] if isinstance(r, dict)]

    def _cards(self, folders):
        out = []
        if isinstance(folders, str):
            folders = [folders]
        for folder in folders:
            for p in sorted(glob.glob(os.path.join(folder, "*.md"))):
                if p.endswith("MEMORY.md"):
                    continue
                try:
                    text = open(p, encoding="utf-8").read()
                except OSError as e:
                    self.problems.append(f"memory: could not read {p}: {type(e).__name__}")
                    continue
                fm, body = {}, text
                m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
                if m:
                    body = m.group(2)
                    for line in m.group(1).splitlines():
                        k = re.match(r"\s*(name|description|type|modified|originSessionId|index):\s*(.*)$", line)
                        if k:
                            fm[k.group(1)] = k.group(2).strip().strip('"')
                out.append({"file": os.path.basename(p), "fm": fm, "body": body})
        return out

    def _ado(self, path):
        j = self._load(path, "work-item cache") or {}
        items = {}
        comments = j.get("comments") or {}
        for k, i in (j.get("items") or {}).items():
            f = i.get("fields") or {}
            text = " ".join(str(f.get(x) or "") for x in ("System.Title", "System.Description",
                                                          "Microsoft.VSTS.Common.AcceptanceCriteria",
                                                          "Microsoft.VSTS.TCM.ReproSteps"))
            text += " " + " ".join(str(c.get("text") or "") for c in comments.get(str(k)) or [] if isinstance(c, dict))
            items[str(i.get("id") or k)] = {"rev": i.get("rev"), "title": f.get("System.Title") or "",
                                            "created": f.get("System.CreatedDate") or "",
                                            "changed": f.get("System.ChangedDate") or "",
                                            "state": f.get("System.State") or "",
                                            "tags": f.get("System.Tags") or "",
                                            "text": re.sub(r"<[^>]+>", " ", text)}
        return items, j.get("pulled")

    def _ccd(self, folder):
        out = {}
        for p in glob.glob(os.path.join(folder, "*", "*", "local_*.json")):
            try:
                d = json.load(open(p, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if d.get("cliSessionId"):
                out[d["cliSessionId"]] = {"ccd": d.get("sessionId"), "title": d.get("title"),
                                          "archived": d.get("isArchived")}
        return out

    def _git(self, repos, since):
        """{sha: {repo, ts, msg, add}} for commits on any branch since `since`. `add` is the added diff lines
        that carry a quote mark or name the user, normalized: where a quote of the user's words would sit.
        Cached per repo in cache_dir/git-<name>.json; a run reads only commits from two days before the last
        scan."""
        out = {}
        mark = re.compile("[\"\u201c\u201d'\u2018\u2019\u00ab\u00bb]|\\bthe user\\b"
                          + "".join("|\\b" + re.escape(n) + "\\b" for n in USER_NAMES))
        for repo in repos:
            if not os.path.isdir(repo):
                self.problems.append(f"git: {repo} is not a folder")
                continue
            name = os.path.basename(repo.rstrip("/"))
            cache = os.path.join(self.cache_dir, f"git-{name}.json") if self.cache_dir else None
            known, scanned = {}, None
            if cache and os.path.exists(cache):
                try:
                    j = json.load(open(cache, encoding="utf-8"))
                    known, scanned = j.get("commits") or {}, j.get("scanned")
                except (OSError, ValueError):
                    known, scanned = {}, None
            start = since or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)).date().isoformat()
            if scanned and known:
                back = (parse_ts(scanned) - dt.timedelta(days=2)).isoformat()
                start = max(start, back[:19])
            began = now_iso()
            try:
                p = subprocess.run(["git", "-C", repo, "log", "--all", "--no-color", f"--since={start}",
                                    "--format=%H%x00%cI%x00%B%x01"], capture_output=True, text=True, timeout=300,
                                   errors="replace")
                q = subprocess.run(["git", "-C", repo, "log", "--all", "-p", "--no-color", f"--since={start}",
                                    "--format=@@C%x00%H", "--", ".", ":(exclude)*lock*", ":(exclude)*.svg",
                                    ":(exclude)*.html", *GIT_EXCLUDE],
                                   capture_output=True, text=True, timeout=600, errors="replace")
            except (OSError, subprocess.TimeoutExpired) as e:
                self.problems.append(f"git {name}: {type(e).__name__}, so commits after {start} were not searched")
                p = q = None
            if p is not None and p.returncode == 0:
                for rec in p.stdout.split("\x01"):
                    parts = rec.strip("\n").split("\x00")
                    if len(parts) >= 3 and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                        known.setdefault(parts[0], {"ts": parts[1], "msg": parts[2].strip()[:4000], "add": ""})
                for block in q.stdout.split("@@C\x00")[1:]:
                    sha, _, diff = block.partition("\n")
                    added = [l[1:] for l in diff.splitlines()
                             if l.startswith("+") and not l.startswith("+++") and len(l) < 600 and mark.search(l)]
                    if sha in known and added:
                        known[sha]["add"] = norm(" ".join(added))[:20000]
                if cache:
                    try:
                        write_atomic(cache, json.dumps({"scanned": began, "commits": known}))
                    except OSError as e:
                        self.problems.append(f"git cache {cache}: {type(e).__name__}")
            elif p is not None:
                self.problems.append(f"git {name}: git log exited {p.returncode}")
            for sha, v in known.items():
                out[sha] = dict(v, repo=repo)
        return out

    def commit(self, sha):
        if not sha:
            return None
        if sha in self.git:
            return self.git[sha]
        for k, v in self.git.items():
            if k.startswith(sha):
                return v
        return None

    def quote_index(self):
        """[(where, ts, ref, normalized text)] for every store a quote can land in."""
        if self._quote_index is None:
            idx = []
            for sha, c in self.git.items():
                idx.append(("commit", c["ts"], sha[:9], norm(c["msg"]) + " " + c["add"]))
            for c in self.cards:
                idx.append(("card", c["fm"].get("modified") or "", c["file"], norm(c["body"] + " " + c["fm"].get(
                    "description", ""))))
            for r in self.rulings:
                idx.append(("ruled", r.get("date") or "", r.get("id"), norm((r.get("ruling") or "") + " " +
                                                                              (r.get("source") or ""))))
            for i, a in self.ado.items():
                idx.append(("filed", a["created"], i, norm(a["text"])))
            self._quote_index = idx
        return self._quote_index


def link(entries, state, stores, relink=False):
    """Set kind, state, ref and why on every entry; open ones are linked again each run."""
    by_session = {}
    for e in entries.values():
        by_session.setdefault(e["session"], []).append(e)
    spawns = state.get("spawns") or {}
    for sid, es in by_session.items():
        es.sort(key=lambda e: (e.get("ts") or "", e["id"]))
        meta = stores.ccd.get(sid) or {}
        title = meta.get("title") or (state.get("sessions", {}).get(sid) or {}).get("title")
        asked_before = False
        for i, e in enumerate(es):
            if title:
                e["title"] = title
            if meta.get("ccd"):
                e["ccd"] = meta["ccd"]
            # chips and spawned prompts: words an agent wrote
            if e["source"] in ("typed", "mid-turn"):
                t = e.get("text") or ""
                if t.startswith("Do this task here"):
                    e["source"] = "chip"
                elif e.get("first_in_session") and (hashlib.sha1(norm(t)[:300].encode()).hexdigest()[:16] in spawns):
                    e["source"] = "spawned"
            prev = es[i - 1] if i else None
            pev = (prev.get("ev") or {}) if prev else {}
            asked_before = bool(prev and (pev.get("asked") or "?" in (pev.get("reply") or "")
                                          or PENDING.search(pev.get("reply") or "")))
            if relink or "kind" not in e:
                e["kind"] = classify(e, asked_before)
            elif e["kind"] in ("answer", "request", "decision") and e["source"] == "card" and puts_off_question(e):
                e["kind"] = "question"      # a card read before the put-off-question rules
            if e["kind"] == "question":
                e["qid"] = qid_of(e)
                e.pop("defers", None)       # read as a request for later before those rules
            else:
                e.pop("qid", None)
        # segments: a new one starts at each typed, chip or spawned request or decision
        seg, segs = [], []
        for e in es:
            if seg and e["source"] in ("typed", "chip", "spawned") and e["kind"] in ("request", "decision"):
                segs.append(seg)
                seg = []
            seg.append(e)
        if seg:
            segs.append(seg)
        later_commits = []      # (ts, sha) every commit or push this session made, for the keyword link
        for e in es:
            ev = e.get("ev") or {}
            for c in ev.get("commits") or []:
                later_commits.append((e.get("ts") or "", c[0]))
            for p in ev.get("pushes") or []:
                later_commits.append((e.get("ts") or "", p[0]))
        for s_i, seg in enumerate(segs):
            for j, e in enumerate(seg):
                if not relink and e.get("state") in SETTLED and e["kind"] != "question":
                    continue
                state_of(e, seg[j:], es, later_commits, stores)
        carry(es)
        # follow_up_of: a correction points at the request before it in the same session
        last_req = None
        for e in es:
            if e["kind"] in ("request", "other") and e is not None and last_req and FOLLOW_RX.search(
                    words_of(e.get("text") or "").lower()):
                e["follow_up_of"] = last_req["id"]
            if e["kind"] == "request":
                last_req = e
    close_questions(entries)
    close_requests(entries)
    cross_link(entries, stores)
    add_scope(entries, stores)
    add_state_refs(entries, state, stores)


def add_scope(entries, stores):
    """For a ruled entry: what the ruling covers (its match block, and the tools its source names) and the
    earlier ruled entry or ruling id it replaces."""
    by_id = {r.get("id"): r for r in stores.rulings}
    seen = {}
    for e in sorted(entries.values(), key=lambda x: x.get("ts") or ""):
        if e.get("state") != "ruled":
            continue
        r = by_id.get(e.get("ref"))
        if not r:
            continue
        scope = {k: v for k, v in (r.get("match") or {}).items() if v not in (None, "", [])}
        tools = sorted(set(re.findall(r"\btools/[\w.-]+\.(?:py|mjs|js)\b", (r.get("source") or "") + " " +
                                      (r.get("ruling") or ""))))
        if tools:
            scope["tools"] = tools
        if r.get("reversed"):
            scope["reversed"] = r["reversed"][:300]
        if scope:
            e["scope"] = scope
        prior = seen.get(r["id"])
        sup = r.get("supersedes") or r.get("replaces")
        if prior and prior != e["id"]:
            e["supersedes"] = prior
        elif sup:
            e["supersedes"] = sup
        seen[r["id"]] = e["id"]


def minutes_between(a, b):
    ta, tb = parse_ts(a.get("ts") or ""), parse_ts(b.get("ts") or "")
    return (tb - ta).total_seconds() / 60 if ta and tb else 1e9


def carry(es):
    """An open request typed again word for word, or added to before any reply came, is carried by the
    later request: it takes that one's state, and the later one points back at it. Not a card pick: two
    cards that get the same pick are two requests, and only a card that names its id carries one."""
    reqs = [e for e in es if e["kind"] in ("request", "decision")]
    for a_i in range(len(reqs) - 1, -1, -1):
        a = reqs[a_i]
        if a.get("state") != "open" or a["source"] == "card":
            continue
        later = reqs[a_i + 1:]
        words = norm(words_of(a.get("text") or ""))
        same = next((b for b in later if words and norm(words_of(b.get("text") or "")) == words), None)
        if same:
            a["repeated_as"] = same["id"]
            same.setdefault("follow_up_of", a["id"])
            if same.get("state") != "open":
                set_state(a, same["state"], same.get("ref"), "typed again later (" + same["id"] + "); " + same["why"])
            continue
        nxt = later[0] if later else None
        if (nxt and not (a.get("ev") or {}).get("reply") and nxt["source"] in ("typed", "mid-turn", "card")
                and minutes_between(a, nxt) <= 60 and nxt.get("state") != "open"):
            nxt.setdefault("follow_up_of", a["id"])
            set_state(a, nxt["state"], nxt.get("ref"), "added to before any reply; the next request carries it ("
                      + nxt["id"] + "): " + nxt["why"])


def evidence(seg):
    out = {k: [] for k in ("commits", "pushes", "rulings", "cards", "ado", "edits", "chips")}
    replies = []
    for e in seg:
        ev = e.get("ev") or {}
        for k in out:
            for x in ev.get(k) or []:
                if x not in out[k]:
                    out[k].append(x)
        if ev.get("reply"):
            replies.append(ev["reply"])
    return out, replies


def set_state(e, st, ref, why):
    e["state"], e["why"] = st, why
    if ref:
        e["ref"] = ref
    else:
        e.pop("ref", None)


def state_of(e, rest, session_entries, later_commits, stores):
    k = e["kind"]
    if k == "question":
        # Only close_questions settles it, from an answer on a later card; nothing in this thread does.
        set_state(e, "open", None, f"a question to the user, put off on a card (\"{puts_off_question(e)}\"); "
                  f"open until a later card or a triage pass names {e.get('qid') or qid_of(e)}, or asks it again, "
                  "and it is answered there")
        return
    if k == "answer":
        set_state(e, "answered", e.get("card_id"), "a card pick or a short reply to the agent's question")
        return
    if k == "other":
        replied = bool((e.get("ev") or {}).get("replies"))
        set_state(e, "answered" if replied else "open", None,
                  "chat, status or an archive check" + (", replied to" if replied else ", no reply yet"))
        return
    text = words_of(e.get("text") or "")
    low = text.lower()
    agent_words = e["source"] in ("chip", "spawned")    # an agent wrote these words, so their phrasing says nothing
    idx = session_entries.index(e)
    after = [x for x in session_entries[idx + 1:] if x["source"] in ("typed", "mid-turn", "card")]
    # dropped: one of the next three things the user typed withdraws it (the very next one, or one that names it)
    for n, x in enumerate([x for x in after if x["source"] in ("typed", "mid-turn")][:3]):
        xt = words_of(x.get("text") or "")
        if DROP_RX.search(xt.lower()) and len(xt.split()) <= 200 and (n == 0 or salient(xt) & salient(text)):
            set_state(e, "dropped", cap(x["text"], 200), "a message the user typed next withdraws it")
            return
    got, replies = evidence(rest)
    replies = [r for r in replies if not NO_ANSWER.search(r[:200])]
    own = USER_LATER.sub(" ", low)     # "will lyk later" is the user's own step, not work asked for later
    deferred = bool(e.get("defers")) or (not agent_words and bool(DEFER_RX.search(own))
                                         and (len(own.split()) <= 80 or "remind me" in own))
    research = not agent_words and bool(RESEARCH_RX.search(low))
    work = WORK_VERBS.search(low)
    question = not agent_words and "?" in text and not work
    checking = not agent_words and bool(re.match(r"(check|verify|confirm|see|look|review|recall|is|are|was|did|does)\b",
                                                 LEAD.sub("", low))) and not work
    wants_file = bool(re.search(r"\b(ado|ticket|work item|file (a|an|one|it)|bug|task)\b", low))
    wants_card = bool(re.search(r"\b(memory|card|remember)\b", low))
    wants_rule = bool(re.search(r"\b(rule|ruling)\b", low))
    ts = e.get("ts") or ""
    mine = salient(text if e["source"] != "card" else text + " " + (e.get("question") or "")[:300])
    if deferred:
        # Asked for later: only an artifact about it counts, not whatever else the thread made.
        for kind_, items, label in (("filed", got["ado"], lambda x: (stores.ado.get(x) or {}).get("title", "")),
                                    ("ruled", got["rulings"], lambda x: next((r.get("ruling", "") + " " + r.get("topic", "")
                                                                              for r in stores.rulings if r.get("id") == x), x)),
                                    ("card", got["cards"], lambda x: next((c["fm"].get("description", "") + " " + c["body"][:2000]
                                                                           for c in stores.cards if c["file"] == x), x)),
                                    ("done", [c[0] for c in got["commits"]] + [p[0] for p in got["pushes"]],
                                     lambda x: (stores.commit(x) or {}).get("msg", "")),
                                    ("done", got["chips"], lambda x: x)):
            for x in items:
                shared = mine & salient(label(x))
                if len(shared) >= 2 and len(shared) >= 0.25 * max(1, len(mine)):
                    chip = x in got["chips"]
                    set_state(e, kind_, ("chip: " + x[:80]) if chip else x[:9] if kind_ == "done" else x,
                              ("asked for another session; a task chip for it was offered: " if chip else
                               "asked for later; an artifact this session made after it shares its words: ")
                              + ", ".join(sorted(shared)[:4]))
                    return
        got = {k_: [] for k_ in got}
    if got["ado"] and (wants_file or not (got["commits"] or got["pushes"])):
        set_state(e, "filed", got["ado"][0], "a work item this session created after it")
        return
    if got["rulings"] and (wants_rule or k == "decision" or not got["commits"]):
        set_state(e, "ruled", got["rulings"][0], "a rulings file entry this session wrote after it")
        return
    if got["cards"] and (wants_card or k == "decision" or not (got["commits"] or got["pushes"] or got["edits"])):
        set_state(e, "card", got["cards"][0], "a memory card this session wrote after it")
        return
    if got["commits"] or got["pushes"]:
        sha = (got["commits"] or got["pushes"])[0][0]
        set_state(e, "done", sha, "a commit this session made after it, before its next request")
        return
    if got["ado"]:
        set_state(e, "filed", got["ado"][0], "a work item this session created after it")
        return
    # a later commit in the same session whose words match it
    best = None
    for cts, sha in later_commits:
        if cts < ts:
            continue
        c = stores.commit(sha)
        if not c:
            continue
        shared = mine & salient(c["msg"])
        if len(shared) >= 2 and len(shared) >= 0.25 * max(1, len(mine)):
            best = (sha, shared)
            break
    if best:
        set_state(e, "done", best[0][:9], "a later commit in this session shares its words: " + ", ".join(sorted(best[1])[:4]))
        return
    final = replies[-1] if replies else ""
    results = [r for r in replies if DONE_LEAD.search(first_sentence(r)) and not NEG_LEAD.search(first_sentence(r))]
    waits = [r for r in replies if USER_PENDING.search(r)]

    def related(r):
        shared = mine & salient(r[:700])
        return len(shared) >= 2 and len(shared) >= 0.15 * max(1, len(mine))
    own_reply = (e.get("ev") or {}).get("reply") or ""
    if NO_ANSWER.search(own_reply[:200]):
        own_reply = ""
    if own_reply and PROGRESS.search(first_sentence(own_reply)):
        # the reply opens on what it does next; the answer, if any, is the thread's last reply
        own_reply = final if final and final is not own_reply and not PROGRESS.search(first_sentence(final)) else ""
    first_is_result = bool(own_reply and DONE_LEAD.search(first_sentence(own_reply))
                           and not NEG_LEAD.search(first_sentence(own_reply)))
    nothing = next((r for r in replies if NOTHING_TO_DO.search(r[:400])), None)
    if deferred:
        r = next((r for r in results if related(r)), None)
        if r:
            set_state(e, "done", "reported", "asked for later; a reply about it leads with a result: " + first_sentence(r)[:110])
        elif first_is_result and e["source"] != "card" and (len(mine) <= 1 or mine & salient(own_reply[:700])):
            set_state(e, "done", "reported", "asked for later; the reply to it leads with the result: "
                      + first_sentence(own_reply)[:110])
        elif (question or nothing) and own_reply:
            set_state(e, "done", "reported", "the reply answers it: " + first_sentence(nothing or own_reply)[:110])
        else:
            set_state(e, "open", None, "asked for later and nothing durable holds it (no commit, work item, ruling or card)")
        return
    if (checking or question) and not research and own_reply and not USER_PENDING.search(first_sentence(own_reply)):
        set_state(e, "done", "reported", "a check or a question, and the reply answers it: " + first_sentence(own_reply)[:110])
        return
    if nothing and not research:
        set_state(e, "done", "reported", "the thread says there is nothing to do: " + first_sentence(nothing)[:110])
        return
    if research and not got["edits"]:
        if waits:
            set_state(e, "open", None, "research or a lasting fix that ended on a step only the user can take: "
                      + pending_sentence(waits[-1], USER_PENDING)[:200])
        elif results:
            set_state(e, "done", "reported", "the thread reports a result: " + first_sentence(results[0])[:120])
        else:
            set_state(e, "open", None, "research or a lasting fix answered only in chat: nothing durable holds the result")
        return
    if got["edits"]:
        set_state(e, "done", "edited " + os.path.basename(got["edits"][0]), "files edited after it, before its next request")
        return
    if not replies:
        set_state(e, "open", None, "no reply yet")
        return
    if results:
        set_state(e, "done", "reported", "the thread reports a result: " + first_sentence(results[0])[:120])
        return
    if final.rstrip().endswith("?") or USER_PENDING.search(final) or (k == "request" and PENDING.search(first_sentence(final))):
        where = (pending_sentence(final, USER_PENDING) or pending_sentence(final, PENDING)
                 or re.sub(r"\s+", " ", last_sentence(final)))
        in_seg = {id(y) for y in rest}
        nxt_user = next((x for x in after if id(x) not in in_seg), None)     # the user's first message after the thread
        if nxt_user:
            set_state(e, "done", "reported", "the reply reported back and left the next step to the user (" + where[:90]
                      + "); the next message took it from there: " + words_of(nxt_user.get("text") or "")[:80])
        else:
            set_state(e, "open", None, "the session ends waiting on the user: " + where[:120])
        return
    if k == "decision":
        set_state(e, "open", None, "a decision kept only in the log")
        return
    set_state(e, "done", "reported", "replied to, and nothing after says it is still waiting")


def close_questions(entries):
    """A question put off is answered when a later card, in any session, names its id (a follow-up or a
    triage pass) or asks the same question, and it is answered there rather than put off or skipped. Put off again on such a card, that card carries it (repeated_as), so it is listed once and closes
    when the later one does. Re-run in full every run, from the entries alone."""
    cards = answered_cards(entries)
    qs = [e for e in cards if e["kind"] == "question"]
    for q in reversed(qs):          # newest first, so a later question that carries one is settled first
        q.pop("repeated_as", None)
        qid, same = q.get("qid") or qid_of(q), norm(q.get("question") or "")
        for c in cards:
            if (c.get("ts") or "") <= (q.get("ts") or ""):
                continue
            named = qid in {m.lower() for m in QID.findall(card_words(c))}
            if not (named or (same and norm(c.get("question") or "") == same)):
                continue
            how = f"names {qid}" if named else "asks the same question"
            if c["kind"] == "question":
                q["repeated_as"] = c["id"]
                if c.get("state") == "answered":
                    set_state(q, "answered", c.get("ref"), f"put off again on a later card that {how} ({c['qid']}), "
                              f"which was answered: {c['why']}")
                    break
                continue
            if PUT_OFF.match(c.get("text") or ""):
                continue                # put off or skipped there: still open
            set_state(q, "answered", c["id"], f"answered on a later card that {how}: "
                      + cap(own_words(c.get("text") or ""), 120))
            break


def answered_cards(entries):
    """Every card the user answered, oldest first."""
    return sorted((e for e in entries.values() if e["source"] == "card" and e.get("status") == "answered"),
                  key=lambda e: (e.get("ts") or "", e["id"]))


def card_words(c):
    """A card's question, header and options: where a later card names an id."""
    return " ".join([c.get("question") or "", c.get("header") or ""]
                    + [o.get("label", "") + " " + o.get("description", "") for o in c.get("options") or []])


def close_requests(entries):
    """An open request is answered when a later card, in any session, names its id (rid_of, which --open
    prints) and the pick on the newest such card does not leave it open (KEEPS_OPEN). Re-run in full every
    run, from the entries alone."""
    cards = [(c, {m.lower() for m in REQ_ID.findall(card_words(c))}) for c in answered_cards(entries)]
    for e in entries.values():
        rid = e["kind"] == "request" and e.get("state") == "open" and rid_of(e)
        named = [c for c, ids in cards if rid and rid in ids and (c.get("ts") or "") > (e.get("ts") or "")]
        if named and not KEEPS_OPEN.match(named[-1].get("text") or ""):
            set_state(e, "answered", named[-1]["id"], f"answered on a later card that names {rid}: "
                      + cap(own_words(named[-1].get("text") or ""), 120))


def cross_link(entries, stores):
    """An open request or decision whose words appear verbatim (7 words in a row) in a commit, a card, a
    ruling or a work item dated after it."""
    idx = None
    for e in entries.values():
        if e.get("state") != "open" or e["kind"] not in ("request", "decision"):
            continue
        sh = shingles(e.get("text") or "")
        if not sh:
            continue
        idx = idx or stores.quote_index()
        ts = (e.get("ts") or "")[:19]
        for where, when, ref, text in idx:
            if when and when[:19] < ts[:len(when[:19])]:
                continue
            if any(s in text for s in sh):
                set_state(e, {"commit": "done"}.get(where, where), ref, f"its words are quoted in a {where} dated after it")
                break


def add_state_refs(entries, state, stores):
    spans = [(s.get("first") or "", s.get("last") or "", sid) for sid, s in (state.get("sessions") or {}).items()
             if not s.get("skip") and s.get("first")]
    tips = {}
    for e in entries.values():
        refs = e.setdefault("state_refs", {})
        ts = e.get("ts") or ""
        if "live" not in refs and ts:
            live = sorted(sid[:8] for a, b, sid in spans if a <= ts <= b and sid != e["session"])
            if live:
                refs["live"] = live[:40]
        if "ado" not in refs:
            ids = sorted(set(re.findall(r"(?:#|_workitems/edit/)(\d{3,5})\b", (e.get("text") or "") + " " +
                                        (e.get("question") or ""))))
            revs = {i: stores.ado[i]["rev"] for i in ids if i in stores.ado}
            if revs:
                refs["ado"] = revs
                refs["ado_as_of"] = stores.ado_pulled
        wt, br = refs.get("worktree"), refs.get("branch")
        if "commit" not in refs and wt and br and os.path.isdir(wt) and br != "HEAD":
            key = (wt, br)
            if key not in tips:
                try:
                    p = subprocess.run(["git", "-C", wt, "log", "--format=%cI %h", br], capture_output=True, text=True,
                                       timeout=60)
                    tips[key] = sorted((l.split(" ")[0], l.split(" ")[1]) for l in p.stdout.splitlines() if " " in l) \
                        if p.returncode == 0 else []
                except (OSError, subprocess.TimeoutExpired):
                    tips[key] = []
            at = [sha for when, sha in tips[key] if parse_ts(when) and parse_ts(ts) and parse_ts(when) <= parse_ts(ts)]
            if at:
                refs["commit"] = at[-1]
        if not refs:
            e.pop("state_refs", None)


# ---------------------------------------------------------------- run
ORDER = ("id", "ts", "session", "ccd", "title", "source", "kind", "qid", "state", "ref", "why", "text", "follow_up_of",
         "card_id", "q_index", "n_questions", "asked_at", "status", "header", "question", "multi_select", "options",
         "recommended", "recommended_source", "answer", "answer_kind", "differs", "answer_note", "images",
         "first_in_session", "repeated_as", "scope", "supersedes", "decided_by_role", "visibility", "state_refs",
         "ev")


def ordered(e):
    return {k: e[k] for k in ORDER if k in e} | {k: v for k, v in e.items() if k not in ORDER}


@contextlib.contextmanager
def locked(ledger_dir, wait=180):
    """One run at a time: a scheduled run and a run from a session read and write the same files."""
    os.makedirs(ledger_dir, exist_ok=True)
    f = open(os.path.join(ledger_dir, ".lock"), "a")
    deadline = time.time() + wait
    try:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise TimeoutError(f"another ledger run held {ledger_dir}/.lock for {wait}s")
                time.sleep(1)
        yield
    finally:
        f.close()           # closing the file releases the lock


def run(ledger_dir=None, projects=PROJECTS, rebuild=False, relink=False, stores_kw=None, quiet=False):
    """Read new log lines, link, write. Returns (entries, problems)."""
    ledger_dir = ledger_dir or LEDGER_DIR
    with locked(ledger_dir):
        return _run(ledger_dir, projects, rebuild, relink, stores_kw, quiet)


def _run(ledger_dir, projects, rebuild, relink, stores_kw, quiet):
    path, state_path = os.path.join(ledger_dir, "requests.jsonl"), os.path.join(ledger_dir, "state.json")
    entries = {} if rebuild else load_jsonl(path)
    state = {}
    if not rebuild:
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except FileNotFoundError:
            entries = {}
        except ValueError:
            state, entries = {}, {}
    R = Reader(state, entries)
    R.scan(main_logs(projects))
    problems = list(R.problems)
    since = min((e.get("ts") or "" for e in entries.values() if e.get("state") in (None, "open")), default=None)
    kw = dict(stores_kw or {})
    kw.setdefault("cache_dir", os.path.join(ledger_dir, ".cache"))
    stores = Stores(since=(since or "")[:10] or None, problems=problems, **kw)
    link(entries, state, stores, relink=relink or rebuild)
    rows = sorted(entries.values(), key=lambda e: (e.get("ts") or "", e["id"]))
    write_atomic(path, "".join(json.dumps(ordered(e), ensure_ascii=False) + "\n" for e in rows))
    write_atomic(state_path, json.dumps(state))
    if not quiet:
        c = {}
        for e in rows:
            c[(e["kind"], e["state"])] = c.get((e["kind"], e["state"]), 0) + 1
        opened = sum(1 for e in rows if e["kind"] == "request" and e["state"] == "open")
        asked = sum(1 for e in rows if e["kind"] == "question" and e["state"] == "open" and not e.get("repeated_as"))
        print(f"ledger: {len(rows)} entries from {len([s for s in state['sessions'].values() if not s.get('skip')])} "
              f"sessions ({sum(1 for s in state['sessions'].values() if s.get('skip'))} skipped); "
              f"{opened} open requests, {asked} open questions. Written {path}")
        print("  " + "; ".join(f"{k} {s}: {n}" for (k, s), n in sorted(c.items())))
        for p in problems:
            print(f"  DID NOT READ {p}")
    return entries, problems


def open_requests(ledger_dir=None, session=None, kind="request"):
    rows = load_jsonl(os.path.join(ledger_dir or LEDGER_DIR, "requests.jsonl")).values()
    # a request typed again word for word is listed once, at its latest copy
    return sorted((e for e in rows if e.get("kind") == kind and e.get("state") == "open"
                   and not e.get("repeated_as")
                   and (not session or e["session"].startswith(session))), key=lambda e: e.get("ts") or "")


def open_questions(ledger_dir=None):
    """The questions put off on a card that no later card has answered, oldest first; one carried by a
    later card is listed at that card."""
    return open_requests(ledger_dir, kind="question")


def show_open(rows):
    for e in rows:
        where = e.get("title") or e["session"][:8]
        words = (f"{e['qid']}  \"{cap(own_words(e.get('question') or ''), 200)}\" -> " if e.get("qid")
                 else f"{rid_of(e)}  " if rid_of(e) else "") + cap(own_words(e.get("text") or ""), 220)
        print(f"{(e.get('ts') or '')[:16]}  {where[:50]}  [{e['source']}]  {words}")
        print(f"    {e.get('why', '')}")


# ---------------------------------------------------------------- calibration
def calibrate(ledger_dir, labels):
    """Kind precision and recall, and state accuracy, against hand labels. Prints N of M with misses.

    LABELS is a JSON file you write by reading a sample of your own ledger:
      {"sample": [{"n": 1, "id": "<entry id>", "kind": "request", "state": "open", "text": "..."}, ...],
       "named":  [{"id": "<entry id>", "ts": "...", "session": "...", "text": "...", "expect": "open"}, ...]}
    "named" lists requests you know were lost, so their state must stay open (or `expect`)."""
    try:
        lab = json.load(open(labels, encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"calibrate: could not read {labels}: {type(e).__name__}; nothing was scored")
        return 2
    rows = load_jsonl(os.path.join(ledger_dir or LEDGER_DIR, "requests.jsonl"))
    sample = [x for x in lab["sample"]]
    missing = [x for x in sample + lab["named"] if x["id"] not in rows]
    print(f"calibration: {len(sample)} hand-labelled entries, {len(lab['named'])} named lost requests; "
          f"{len(missing)} not in the ledger")
    got = [(x, rows[x["id"]]) for x in sample if x["id"] in rows]
    kinds = ("request", "decision", "answer", "other")
    print("kind        precision        recall")
    for k in kinds:
        tp = sum(1 for x, e in got if x["kind"] == k and e["kind"] == k)
        pp = sum(1 for x, e in got if e["kind"] == k)
        ap = sum(1 for x, e in got if x["kind"] == k)
        print(f"  {k:9} {tp:3} of {pp:3} = {tp / pp if pp else 0:5.0%}   {tp:3} of {ap:3} = {tp / ap if ap else 0:5.0%}")
    ok = sum(1 for x, e in got if x["kind"] == e["kind"])
    print(f"  kind agrees on {ok} of {len(got)}")
    scored = [(x, e) for x, e in got if x["kind"] != "other"]
    sok = sum(1 for x, e in scored if x["state"] == e["state"])
    print(f"state agrees on {sok} of {len(scored)} (labelled request, decision or answer)")
    req = [(x, e) for x, e in scored if x["kind"] == "request"]
    print(f"  requests: {sum(1 for x, e in req if x['state'] == e['state'])} of {len(req)}; "
          f"open/not-open agrees on {sum(1 for x, e in req if (x['state'] == 'open') == (e['state'] == 'open'))} "
          f"of {len(req)}")
    named = [(x, rows.get(x["id"])) for x in lab["named"]]
    hit = sum(1 for x, e in named if e and e.get("state") == x.get("expect", "open"))
    print(f"named lost requests in their expected state: {hit} of {len(named)}")
    for x, e in named:
        print(f"    {'ok  ' if e and e.get('state') == x.get('expect', 'open') else 'MISS'} {x['ts'][:16]} {x['session']} "
              f"expect {x.get('expect', 'open')}, got {(e or {}).get('state')} {(e or {}).get('ref') or ''} :: {x['text'][:80]}")
    print("misses:")
    for x, e in got:
        if x["kind"] != e["kind"] or (x["kind"] != "other" and x["state"] != e["state"]):
            print(f"  #{x['n']:<3} label {x['kind']}/{x['state']}  got {e['kind']}/{e['state']}  "
                  f"({e.get('why', '')[:70]}) :: {x['text'][:80]!r}")
    return 0


# ---------------------------------------------------------------- selftest
def selftest():
    results = []

    def check(label, ok):
        results.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + label)

    with tempfile.TemporaryDirectory() as t:
        projects = os.path.join(t, "projects")
        proj = os.path.join(projects, "-Users-x-Desktop")
        os.makedirs(proj)
        mem = os.path.join(t, "memory")
        os.makedirs(mem)
        with open(os.path.join(mem, "feedback_planted.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: feedback_planted\ndescription: planted\nmodified: 2026-10-20T12:00:00Z\n---\n"
                    "The user said: \"always put the planted gutter at twenty four pixels on every phone band\".\n")
        rul = os.path.join(t, "rulings.json")
        json.dump({"rulings": [{"id": "planted-old", "topic": "x", "ruling": "x", "source": "x", "date": "2026-09-01",
                                "status": "ruled"},
                               {"id": "planted-chips-as-drawn", "topic": "Chips", "ruling": "x", "date": "2026-10-20",
                                "source": "user ruling; checked by tools/qa_audit.py", "status": "ruled",
                                "match": {"name_re": "chip", "slug_re": "home"}}]}, open(rul, "w"))
        ado = os.path.join(t, "ado.json")
        json.dump({"pulled": 0, "items": {}, "comments": {}}, open(ado, "w"))
        kw = {"git_repos": [], "memory": [mem], "rulings": rul, "ado": ado,
              "ccd": os.path.join(t, "none"), "cache_dir": os.path.join(t, "cache")}
        global SKIP_CWD, SKIP_FIRST, ROLE, RULINGS_NAME
        saved = (SKIP_CWD, SKIP_FIRST, ROLE, RULINGS_NAME)
        SKIP_CWD = SCRATCH + (os.path.join(t, "experiments") + "/",)
        SKIP_FIRST = ("Scripted helper prompt",)
        ROLE, RULINGS_NAME = "user", "rulings.json"
        n = [0]

        def rec(kind, ts, **k):
            n[0] += 1
            r = {"type": kind, "uuid": f"u{n[0]:04d}", "timestamp": f"2026-10-20T{ts}:00.000Z",
                 "sessionId": "s1", "entrypoint": "claude-desktop", "cwd": "/Users/x/Desktop"}
            r.update(k)
            return json.dumps(r)

        def typed(ts, text):     # the desktop app's shape for the user's own turn: promptSource sdk, turnOrigin human
            return rec("user", ts, message={"role": "user", "content": text}, origin={"kind": "human"},
                       promptSource="sdk", turnOrigin="human")

        def say(ts, text):
            return rec("assistant", ts, message={"role": "assistant", "content": [{"type": "text", "text": text}]})

        def tool(ts, tid, name, **inp):
            return rec("assistant", ts, message={"role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": name, "input": inp}]})

        def result(ts, tid, text, tur=None):
            k = {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}
            if tur:
                k["toolUseResult"] = tur
            return rec("user", ts, **k)

        card_q = {"question": "Park the planted bridge, or build it now?", "header": "Bridge",
                  "options": [{"label": "Park it as an ADO task (Recommended)", "description": "file it"},
                              {"label": "Build it now", "description": "now"}]}
        pick_q = {"question": "Push 1234abc?", "header": "Push",
                  "options": [{"label": "Push it (Recommended)", "description": ""}, {"label": "Hold", "description": ""}]}
        lines = [
            typed("10:00", "fix the planted widget crash in the gateway script"),
            tool("10:01", "c1", "Bash", command="cd /tmp/r && git commit -qm x"),
            result("10:01", "c1", "[main 1234abc] Fix the planted widget crash"),
            say("10:02", "Fixed it and committed 1234abc. Nothing is pushed."),
            typed("10:03", "/ambition research the smartest way past the planted rate limit. figure it out"),
            say("10:04", "REST isn't the problem; the budget is shared. The durable fix is a desktop bridge. "
                         "Both need a one-time setup from you. Neither blocks today's work."),
            tool("10:05", "q1", "AskUserQuestion", questions=[card_q]),
            result("10:06", "q1", "answered", {"answers": {card_q["question"]: "y even need this at all tho? figure it out"}}),
            say("10:06", "Understood. Moving on to the push question."),
            tool("10:07", "q2", "AskUserQuestion", questions=[pick_q]),
            result("10:07", "q2", "answered", {"answers": {pick_q["question"]: "Push it (Recommended)"}}),
            rec("user", "10:08", message={"role": "user", "content": "This session is being continued from a previous "
                                          "conversation. Summary: ..."}, isCompactSummary=True),
            rec("user", "10:08", message={"role": "user", "content": "<task-notification><task-id>b1</task-id>"},
                origin={"kind": "task-notification"}),
            rec("user", "10:08", message={"role": "user", "content": [{"type": "text", "text": "Base directory for this skill"}]},
                isMeta=True),
            typed("10:09", "Another Claude session sent a message:\n<cross-session-message from=\"x\">hi?</cross-session-message>"),
            rec("user", "10:09", message={"role": "user", "content": "# Pre-review: the planted scripted brief"},
                promptSource="sdk", turnOrigin="sdk"),
            typed("10:09", "<command-name>/compact</command-name>"),
            typed("10:10", "<command-message>ambition</command-message><command-name>/ambition</command-name>"
                           "<command-args>think bigger about the planted deck</command-args>"),
            tool("10:10", "w1", "Write", file_path="/Users/x/Desktop/shareouts/deck.md", content="x"),
            say("10:11", "Rewrote the deck around the planted framing."),
            rec("attachment", "10:12", attachment={"type": "queued_command", "prompt": "also add the planted footer note",
                                                   "humanTurn": True}),
            rec("attachment", "10:12", attachment={"type": "queued_command", "prompt": "<task-notification>x"}),
            typed("10:13", "file an ado item for the planted footer"),
            tool("10:13", "a1", "Bash", command="python tools/ado.py create --title x"),
            result("10:14", "a1", "created #2999: Task // planted footer"),
            typed("10:15", "make it a rule that planted chips stay as figma draws them"),
            tool("10:15", "e1", "Edit", file_path="/r/docs/rulings.json", old_string="x",
                 new_string='{"id": "planted-chips-as-drawn", "topic": "x"}'),
            say("10:16", "Ruled as planted-chips-as-drawn."),
            typed("10:17", "remember that the planted owner is the design lead"),
            tool("10:17", "m1", "Write", file_path="/Users/x/.claude/projects/p/memory/people_planted.md", content="x"),
            typed("10:18", "remind me to have u check the planted hero again later"),
            say("10:18", "Will do."),
            typed("10:19", "fix the planted tooltip"),
            typed("10:20", "nvm, forget it"),
            typed("10:21", "always put the planted gutter at twenty four pixels on every phone band please"),
            typed("10:22", "my token: figd_ABCDEFGHIJ1234567890xyz should not be stored"),
            '{"type": "user", "uuid": "partial", "timestamp": "2026-10-20T10:23:00.000Z", "message": {"role": "user", "cont',
        ]
        log = os.path.join(proj, "s1.jsonl")
        with open(log, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))          # the last line has no newline: still being written
        for name, first in (("h1", {"entrypoint": "sdk-cli"}), ("h2", {"cwd": "/private/tmp/x"}),
                            ("h3", {"cwd": os.path.join(t, "experiments", "D")})):
            r0 = json.loads(typed("09:00", "fix the headless thing"))
            r0.update(first)
            r0["sessionId"] = name
            with open(os.path.join(proj, name + ".jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps(r0) + "\n")
        r0 = json.loads(typed("09:00", "Scripted helper prompt for the call. fix things"))
        r0["sessionId"] = "h4"
        with open(os.path.join(proj, "h4.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(r0) + "\n")
        def s2(line):
            r = json.loads(line)
            r["sessionId"] = "s2"
            return json.dumps(r)
        lines2 = [s2(x) for x in [
            typed("11:00", '<scheduled-task name="planted-check">This is an automated run of a scheduled task.'),
            typed("11:01", "verify the planted push when it lands"),
            say("11:02", "The planted push has landed; I confirmed it on the remote."),
            typed("11:03", "run the planted export"),
            say("11:04", "The export is staged. The upload needs your go."),
            typed("11:05", "draft the planted release note"),
            say("11:06", "Drafted it."),
            typed("11:07", "check why the planted banner wraps"),
            typed("11:08", "and fix the planted banner wrap in Banner.tsx"),
            tool("11:08", "e9", "Edit", file_path="/Users/x/Desktop/repos/site/Banner.tsx", old_string="a", new_string="b"),
            say("11:09", "Fixed the wrap."),
            typed("11:10", "ok let me know when the planted runs finish"),
            say("11:10", "Still running."),
            typed("11:20", "ok let me know when the planted runs finish"),
            say("11:21", "Still running, 3 of 5."),
        ]]
        with open(os.path.join(proj, "s2.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines2) + "\n")

        # Questions put off on a card. s3: the known bad case (a "Later" with no "Now" beside it, then a
        # reply about it and nothing that names it) and three that must read as before. s4: what closes one.
        def card(ts, tid, question, labels, pick, header="Planted"):
            q = {"question": question, "header": header,
                 "options": [{"label": l, "description": ""} for l in labels]}
            return [tool(ts, tid, "AskUserQuestion", questions=[q]),
                    result(ts, tid, "answered", {"answers": {question: pick}})]

        def qid(tid):
            return "q-" + hashlib.sha1(f"card:{tid}:0".encode()).hexdigest()[:6]
        lines3 = ([typed("12:00", "set up the planted machine")]
                  + card("12:01", "b1", "Port the planted backup job to a launchd job on this Mac?",
                         ["Set it up (Recommended)", "Later"], "Later")
                  + [say("12:03", "Done: the planted backup job is parked for later, and I noted it here.")]
                  + card("12:04", "b2", "Port the planted cron job?", ["Now", "Later"], "Later")
                  + [say("12:05", "Understood.")]
                  + card("12:06", "b3", "Which planted accent colour?", ["Blue (Recommended)", "Green"], "Green")
                  + card("12:08", "b4", "Decision #2377: does the planted branch keep following the draft PR?",
                         ["Defer (Recommended)", "Resolve: yes", "Skip"], "Defer (Recommended)"))
        lines4 = ([typed("13:00", "tidy the planted storage")]
                  + card("13:01", "a1", "Move the planted transcript store to the new disk?",
                         ["Move it (Recommended)", "Not yet"], "Not yet")
                  + card("13:02", "e1", "Prune the planted caption cache?", ["Prune it (Recommended)", "Defer"], "Defer")
                  + card("13:05", "e2", f"Triage {qid('e1')} (planted caption cache): prune it now?",
                         ["Resolve: prune it (Recommended)", "Defer", "Skip"], "Skip")
                  + card("13:10", "a2", f"Triage {qid('a1')} (planted transcript store): move it now?",
                         ["Resolve: move it (Recommended)", "Defer", "Skip"], "Resolve: move it (Recommended)")
                  + card("13:20", "s1q", "Rotate the planted log files weekly?", ["Yes (Recommended)", "Hold"], "Hold")
                  + card("13:30", "s2q", "Rotate the planted log files weekly?", ["Yes (Recommended)", "Hold"],
                         "Yes (Recommended)")
                  + card("13:40", "c1", "Archive the planted inbox?", ["Archive it (Recommended)", "Wait for the sync"],
                         "Wait for the sync")
                  + card("13:45", "c2", f"{qid('c1')}: archive the planted inbox now?", ["Archive it now", "Later"],
                         "Later"))
        # Open requests for later (s5), and cards in another session (s6) that name them by id, or don't.
        rids = {"close": "a1b2c3d4-0000-4000-8000-000000000001", "keep": "b2c3d4e5-0000-4000-8000-000000000002",
                "other": "c3d4e5f6-0000-4000-8000-000000000003"}

        def in_session(sid, ls):
            return [json.dumps({**json.loads(x), "sessionId": sid}) for x in ls]

        def asked(ts, uid, text):
            return rec("user", ts, uuid=uid, message={"role": "user", "content": text}, origin={"kind": "human"})
        lines5 = in_session("s5", [asked("14:00", rids["close"], "remind me to re-run the planted export next week"),
                                   say("14:00", "Will do."),
                                   asked("14:01", rids["keep"], "remind me to prune the planted thumbnails next week"),
                                   say("14:01", "Will do."),
                                   asked("14:02", rids["other"], "remind me to re-sort the planted folders next week"),
                                   say("14:02", "Will do.")])
        lines6 = in_session("s6", [typed("15:00", "go through the planted ledger")]
                            + card("15:01", "k1", "Request a1b2c3d4 (the planted export) ran: close it?",
                                   ["Close it, done (Recommended)", "Leave it for triage"],
                                   "Close it, done (Recommended)", header="a1b2c3d4")
                            + card("15:02", "k2", "Request b2c3d4e5 (the planted thumbnails): close it?",
                                   ["Close it (Recommended)", "Leave it for triage"], "Leave it for triage")
                            + card("15:03", "k3", "The planted folders, and commit 0badcafe: re-sort them now?",
                                   ["Yes (Recommended)", "No"], "Yes (Recommended)"))
        # A pick put off beside a "Now" (s7). The known bad case: the last of three questions on one card, "Later,
        # through /triage" beside "Now, here", then an unrelated edit, a commit and a reply that leads with a
        # result; it read done on that reply. And the known good one: a "Later" beside a "Now" that a later triage
        # card names by id and the user answers there.
        tri = [{"question": "The planted tools commit adds 508 net lines and its hook refuses that. How should it land?",
                "header": "Tools commit",
                "options": [{"label": "You run it with --no-verify (Recommended)", "description": "one command"},
                            {"label": "Hold it uncommitted", "description": "unversioned"}]},
               {"question": "Your cap holds the planted eval: no paid runs. Should the benchmark run anyway?",
                "header": "Benchmark",
                "options": [{"label": "Keep the cap, skip it (Recommended)", "description": "no run cost"},
                            {"label": "Lift the cap for one fixed run", "description": "one paid run"}]},
               {"question": "Ten planted facts disagree between sources, and some card pieces were held as your "
                            "call. When do you want to settle them?", "header": "Conflicts",
                "options": [{"label": "Later, through /triage (Recommended)", "description": "kept unresolved"},
                            {"label": "Now, here", "description": "I put them on question cards, four at a time."}]}]
        tri_picks = ["you run it with --no-verify? what would that change?",
                     "Keep the cap, skip it (Recommended)", "Later, through /triage (Recommended)"]
        lines7 = in_session("s7", [typed("16:00", "redesign the planted notes system"),
                                   tool("16:01", "t26", "AskUserQuestion", questions=tri),
                                   result("16:02", "t26", "answered",
                                          {"answers": {q["question"]: a for q, a in zip(tri, tri_picks)}}),
                                   tool("16:03", "w26", "Edit", file_path="/Users/x/Desktop/notes/layer.md",
                                        old_string="a", new_string="b"),
                                   tool("16:04", "g26", "Bash", command="git commit -qm x"),
                                   result("16:04", "g26", "[wt/notes e6425e8] Add the planted rules"),
                                   say("16:05", "The last stale doc from this change is fixed. Its status line now "
                                                "says the new design replaced it. The two doc checks pass on it, "
                                                "and no other doc links to it.")]
                            + card("16:10", "g1", "Port the planted mail rule to the new server?",
                                   ["Now", "Later (Recommended)"], "Later (Recommended)")
                            + card("16:20", "g2", f"Triage {qid('g1')} (planted mail rule): port it now?",
                                   ["Resolve: port it (Recommended)", "Defer", "Skip"], "Resolve: port it (Recommended)"))
        # Parks and hand-offs (s8): a lone "Park..." puts it off; a park beside other work, alone or among a
        # multi-select's picks, is a request; a hand-off is not closed by a result-led reply that shares one word.
        multi = {"question": "Before the planted archive, which of these should I do?", "header": "Planted",
                 "multiSelect": True, "options": [{"label": l, "description": ""} for l in (
                     "Send the planted heads-ups", "Park the planted decisions", "None")]}
        lines8 = in_session("s8", card("17:00", "p1", "Rename the planted team page?",
                                       ["Park until the name is set (Recommended)", "Rename it now"],
                                       "Park until the name is set (Recommended)")
                            + card("17:02", "p2", "How should the planted bands close?",
                                   ["Design first, park the rest (Recommended)", "Stop here"],
                                   "Design first, park the rest (Recommended)")
                            + [tool("17:04", "p3", "AskUserQuestion", questions=[multi]),
                               result("17:04", "p3", "answered", {"answers": {
                                   multi["question"]: ["Send the planted heads-ups", "Park the planted decisions"]}})]
                            + card("17:10", "h1", "Run the planted export audit here, or in its own session?",
                                   ["Separate session (Recommended)", "Here"], "Separate session (Recommended)")
                            + [say("17:11", "Fixed the audit banner in Header.tsx.")]
                            # an open card request has an id too, and a later card that names it closes it
                            + card("17:30", "h3", "Run the planted backup audit here, or in a separate session?",
                                   ["Separate session (Recommended)", "Here"], "Separate session (Recommended)")
                            + card("17:40", "h4", f"Request {hashlib.sha1(b'card:h3:0').hexdigest()[:8]} (the planted "
                                   "backup audit) ran there: close it?", ["Close it, done (Recommended)", "Leave it"],
                                   "Close it, done (Recommended)")
                            # typed answers: a put-off is a question; "wait, why...?" is not; nor one a tracker holds
                            + card("18:00", "t1", "Put the planted mockups on the board?",
                                   ["Put them on (Recommended)", "Not yet"], "not yet. we're still in call, ask me after")
                            + [say("18:01", "Done: the planted mockups are on the board, and the call notes too.")]
                            + card("18:02", "t2", "Rerun the planted export?", ["Rerun it (Recommended)", "Skip"],
                                   "wait, why does the planted export fail?")
                            + card("18:04", "t3", "Decision #2380: the planted pool owner?", ["Ask them (Recommended)"],
                                   "hold for the planted pool owner")
                            # a hand-off by the question's words only when the question asks it
                            + card("19:00", "u1", "The peer's planted commit stays local for its own session. "
                                   "Push 1234abd only?", ["Push 1234abd only (Recommended)", "Don't push yet"],
                                   "Push 1234abd only (Recommended)")
                            + card("19:02", "u2", "Should each planted page get its own value?",
                                   ["Per page (Recommended)", "One value"], "Per page (Recommended)")
                            + card("19:04", "u3", "Which planted move should get its own session now?",
                                   ["Queryable planted record (Recommended)", "None"], "Queryable planted record (Recommended)"))
        for name, ls in (("s3", lines3), ("s4", lines4), ("s5", lines5), ("s6", lines6), ("s7", lines7),
                         ("s8", lines8)):
            with open(os.path.join(proj, name + ".jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(ls) + "\n")
        os.makedirs(os.path.join(proj, "s1", "subagents"))
        with open(os.path.join(proj, "s1", "subagents", "agent-a.jsonl"), "w", encoding="utf-8") as f:
            f.write(typed("10:00", "subagent words") + "\n")
        led = os.path.join(t, "ledger")
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            entries, _ = run(led, projects, stores_kw=kw)
        by_text = {(e.get("text") or "")[:40]: e for e in entries.values()}
        if os.environ.get("LEDGER_DEBUG"):
            for e in sorted(entries.values(), key=lambda e: e["ts"]):
                print("   ", e["ts"][11:16], e["source"], e["kind"], e["state"], e.get("ref"), "|", e.get("why", "")[:60],
                      "|", (e.get("text") or "")[:50])

        def get(prefix):
            return next((e for k, e in by_text.items() if k.startswith(prefix)), None)
        texts = " | ".join(e.get("text") or "" for e in entries.values())
        check("a planted done request is done, with the commit sha",
              (get("fix the planted widget") or {}).get("state") == "done"
              and get("fix the planted widget").get("ref") == "1234abc")
        lost = get("/ambition research the smartest")
        check("a planted lost request (a proposal that needs the user, then nothing) is open",
              lost and lost["kind"] == "request" and lost["state"] == "open")
        fig = get("y even need this")
        check("a free-text card answer that asks for work is a request, open", fig and fig["kind"] == "request"
              and fig["state"] == "open" and fig.get("answer_kind") == "typed" and fig.get("differs") is True)
        push = get("Push it (Recommended)")
        check("a card pick is an answer, answered, with the card fields", push and push["kind"] == "answer"
              and push["state"] == "answered" and push.get("recommended") == ["Push it (Recommended)"]
              and push.get("answer_kind") == "recommended" and push.get("differs") is False and push.get("q_index") == 0)
        check("a mid-turn message is recorded; a queued task notice is not",
              (get("also add the planted footer") or {}).get("source") == "mid-turn" and "<task-notification>" not in texts)
        check("tool results, compaction summaries, task notices, skill bodies, peer messages, /compact and subagent "
              "logs are not entries",
              not any(s in texts for s in ("1234abc]", "being continued", "Base directory", "cross-session",
                                           "/compact", "subagent words", "created #2999")))
        check("a typed slash command keeps its words", get("/ambition think bigger about the") is not None)
        check("headless, scratch-folder, skip_cwd and skip_first sessions are skipped",
              "headless thing" not in texts and "Scripted helper" not in texts)
        check("a prompt a script sent into a desktop session (turnOrigin sdk) is not the user's words; the user's "
              "own turns, tagged turnOrigin human, still are",
              "planted scripted brief" not in texts and get("fix the planted widget crash") is not None)
        check("a work item the session created after it: filed", (get("file an ado item") or {}).get("state") == "filed"
              and get("file an ado item").get("ref") == "2999")
        check("a rulings file entry the session wrote after it: ruled",
              (get("make it a rule") or {}).get("ref") == "planted-chips-as-drawn"
              and get("make it a rule")["state"] == "ruled")
        check("a memory card the session wrote after it: card", (get("remember that the planted") or {}).get("state") == "card")
        check("a request for later with nothing durable: open", (get("remind me to have u check") or {}).get("state") == "open")
        check("a later 'nvm' drops it, quoting it", (get("fix the planted tooltip") or {}).get("state") == "dropped"
              and "nvm" in get("fix the planted tooltip").get("ref", ""))
        check("an open request quoted in a memory card dated after it: card",
              (get("always put the planted gutter") or {}).get("state") == "card"
              and get("always put the planted gutter").get("ref") == "feedback_planted.md")
        tok = get("my [REDACTED]")
        check("a credential shape is redacted before it is stored", tok and "figd_" not in tok["text"]
              and "[REDACTED]" in tok["text"])
        check("a line still being written is not read", get("partial") is None and all(e["id"] != "partial"
                                                                                       for e in entries.values()))
        check("every entry names the configured role and is personal",
              all(e.get("decided_by_role") == "user" and e.get("visibility") == "personal" for e in entries.values()))
        rule = get("make it a rule") or {}
        check("a ruled entry carries the ruling's scope (its match block, and the tools its source names)",
              rule.get("scope", {}).get("name_re") == "chip" and rule["scope"].get("slug_re") == "home"
              and rule["scope"].get("tools") == ["tools/qa_audit.py"])
        check("a scheduled-task prompt is not the user's words", "scheduled-task" not in texts)
        check("'verify X when it lands', answered by 'X has landed': done",
              (get("verify the planted push") or {}).get("state") == "done")
        handed = get("run the planted export") or {}
        check("a reply that leaves the next step to the user, and the next message: done, quoting where it stopped",
              handed.get("state") == "done" and "needs your go" in handed.get("why", ""))
        wraps = get("check why the planted banner") or {}
        check("a request added to before any reply takes the next request's state and is pointed back at",
              wraps.get("state") == "done" and (get("and fix the planted banner") or {}).get("follow_up_of") == wraps.get("id"))
        twice = [e for e in entries.values() if (e.get("text") or "").startswith("ok let me know when the planted")]
        listed = [r for r in open_requests(led, "s2") if r["text"].startswith("ok let me know when the planted")]
        check("a request typed twice is open once, at its latest copy",
              len(twice) == 2 and all(e["state"] == "open" for e in twice) and len(listed) == 1
              and listed[0]["ts"] == max(e["ts"] for e in twice))
        cq = {e["card_id"]: e for e in entries.values() if e["source"] == "card"}
        bad = cq.get("b1") or {}
        check("a card question put off ('Later', no 'Now' beside it) is an open question under its id, and a "
              "reply about it does not close it",
              bad.get("kind") == "question" and bad.get("state") == "open" and bad.get("qid") == qid("b1"))
        check("a question answered on a later card that names its id (a triage pass) is closed, by that card",
              (cq.get("a1") or {}).get("state") == "answered" and cq["a1"].get("ref") == "card:a2:0")
        check("a later card that names its id and is skipped leaves it open",
              (cq.get("e1") or {}).get("kind") == "question" and cq["e1"].get("state") == "open")
        check("the same question answered again on a later card closes it",
              (cq.get("s1q") or {}).get("state") == "answered" and cq["s1q"].get("ref") == "card:s2q:0")
        check("put off again on a later card that names it: listed once, at the later card",
              (cq.get("c1") or {}).get("repeated_as") == "card:c2:0" and cq["c1"].get("state") == "open"
              and (cq.get("c2") or {}).get("kind") == "question" and cq["c2"].get("state") == "open")
        check("an ordinary card pick still reads as an answer, answered",
              (cq.get("b3") or {}).get("kind") == "answer" and cq["b3"].get("state") == "answered")
        check("a 'Now / Later' pick is a question put off, open under its id; a reply does not close it",
              (cq.get("b2") or {}).get("kind") == "question" and cq["b2"].get("state") == "open"
              and cq["b2"].get("qid") == qid("b2") and "defers" not in cq["b2"])
        check("a 'Defer' on a question that names a work item stays an answer: the tracker holds it",
              (cq.get("b4") or {}).get("kind") == "answer" and cq["b4"].get("state") == "answered")
        held = entries.get("card:t26:2") or {}
        q_held = "q-" + hashlib.sha1(b"card:t26:2").hexdigest()[:6]
        check("a 'Later' picked beside a 'Now' is a question put off, open under its id: the edit, the commit and "
              "the reply that leads with a result do not close it",
              held.get("kind") == "question" and held.get("state") == "open" and held.get("qid") == q_held)
        check("a 'Later' beside a 'Now' that a later triage card names by id, answered there, is closed by that card",
              (cq.get("g1") or {}).get("state") == "answered" and cq["g1"].get("ref") == "card:g2:0")
        check("a lone 'Park until the name is set' puts the question off: open under its id",
              (cq.get("p1") or {}).get("kind") == "question" and cq["p1"].get("state") == "open")
        check("a park beside other work ('Design first, park the rest', or one of a multi-select's picks) is a request",
              (cq.get("p2") or {}).get("kind") == "request" and (cq.get("p3") or {}).get("kind") == "request")
        check("a hand-off pick ('Separate session') stays open on a result-led reply that shares one of its words",
              (cq.get("h1") or {}).get("kind") == "request" and cq["h1"].get("state") == "open")
        check("a typed 'not yet. we're still in call' puts the question off like a pick: open under its id, and a "
              "result-led reply about it does not close it",
              (cq.get("t1") or {}).get("kind") == "question" and cq["t1"].get("state") == "open"
              and cq["t1"].get("qid") == qid("t1"))
        check("a typed 'wait, why...?' is not a put-off, nor a typed 'hold for...' on a question that names a work item",
              (cq.get("t2") or {}).get("kind") not in (None, "question") and (cq.get("t3") or {}).get("kind")
              not in (None, "question"))
        check("a pick is handed to its own session by the question's words only when the question asks it: not "
              "'...stays local for its own session. Push...?' or 'get its own value?'",
              (cq.get("u1") or {}).get("kind") == "answer" and (cq.get("u2") or {}).get("kind") == "answer"
              and (cq.get("u3") or {}).get("kind") == "request" and cq["u3"].get("defers") is True)
        listed_q = [r["qid"] for r in open_questions(led)]
        check("--questions lists the open ones by id, oldest first, each once",
              listed_q == [qid("b1"), qid("b2"), qid("e1"), qid("c2"), q_held, qid("p1"), qid("t1")])
        rq = {k: entries.get(v) or {} for k, v in rids.items()}
        check("an open request closed on a later card, in another session, that names its id is answered, "
              "by that card", rq["close"].get("state") == "answered" and rq["close"].get("ref") == "card:k1:0")
        check("a later card that names its id and leaves it open ('Leave it for triage') keeps it open",
              rq["keep"].get("kind") == "request" and rq["keep"].get("state") == "open")
        check("a card that names no open request's id (a commit's 8 hex digits) closes none",
              rq["other"].get("kind") == "request" and rq["other"].get("state") == "open")
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            show_open(open_requests(led, "s5"))
        check("--open prints each open request with the id a card names it by, and not the closed one",
              "b2c3d4e5  remind me to prune" in shown.getvalue() and "c3d4e5f6  remind me" in shown.getvalue()
              and "a1b2c3d4" not in shown.getvalue())
        shown8 = io.StringIO()
        with contextlib.redirect_stdout(shown8):
            show_open(open_requests(led, "s8"))
        check("an open card request goes by 8 hex digits of its entry id's sha1: --open prints them, and a later card "
              "that names them closes it", f"{rid_of(cq.get('h1') or {'id': ''})}  Separate session" in shown8.getvalue()
              and (cq.get("h3") or {}).get("state") == "answered" and cq["h3"].get("ref") == "card:h4:0")
        count = len(entries)
        # incremental: finish the partial line and add one more; a re-run reads only those
        with open(log, "a", encoding="utf-8") as f:
            f.write('ent": "the partial line, finished"}, "origin": {"kind": "human"}, "sessionId": "s1"}\n')
            f.write(typed("10:24", "push the planted fix now") + "\n")
        st = json.load(open(os.path.join(led, "state.json")))
        before = st["files"][log]["off"]
        with contextlib.redirect_stdout(io.StringIO()):
            entries2, _ = run(led, projects, stores_kw=kw)
        st2 = json.load(open(os.path.join(led, "state.json")))
        check("a re-run reads only the new lines: two more entries, offset moved to the end",
              len(entries2) == count + 2 and st2["files"][log]["off"] == os.path.getsize(log) > before)
        with contextlib.redirect_stdout(io.StringIO()):
            entries3, _ = run(led, projects, stores_kw=kw)
        check("a third run with nothing new changes nothing", len(entries3) == len(entries2))
        # a rewritten (shorter) log is read again from the start, and its entries are not doubled
        with open(log, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[:4]) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            entries4, _ = run(led, projects, stores_kw=kw)
        check("a log rewritten shorter is read from the start without doubling entries", len(entries4) == len(entries2))
        check("later runs leave every card's kind and state as the first run set them",
              all((entries4.get(e["id"]) or {}).get("state") == e["state"]
                  and entries4[e["id"]].get("kind") == e["kind"] for e in cq.values()))
        rows = open_requests(led, "s1")
        check("open_requests lists this session's open requests, oldest first",
              [r["text"][:20] for r in rows] == sorted([r["text"][:20] for r in rows], key=lambda x: x) or
              all(rows[i]["ts"] <= rows[i + 1]["ts"] for i in range(len(rows) - 1)))
        check("open_requests includes the planted lost one and not the done one",
              any(r["text"].startswith("/ambition research") for r in rows)
              and not any(r["text"].startswith("fix the planted widget") for r in rows))
        waited = None
        with locked(led):
            try:
                with locked(led, wait=1):
                    waited = False
            except TimeoutError:
                waited = True
        with locked(led, wait=1):
            free = True
        check("a second run waits for the lock and gives up with an error, never writes alongside",
              waited is True and free)
        SKIP_CWD, SKIP_FIRST, ROLE, RULINGS_NAME = saved
    print(f"selftest: {sum(results)} of {len(results)} checks passed")
    return 0 if all(results) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ledger.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rebuild", action="store_true", help="forget the offsets and the ledger, read every log again")
    ap.add_argument("--relink", action="store_true", help="classify and link every entry again, settled ones too")
    ap.add_argument("--open", action="store_true", help="print the open requests, oldest first")
    ap.add_argument("--session", help="with --open: one session, by id or prefix")
    ap.add_argument("--questions", action="store_true", help="print the open questions put off on a card, with ids")
    ap.add_argument("--calibrate", metavar="LABELS", help="score kind and state against a hand-label file")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dir", default=LEDGER_DIR, help=f"ledger folder (default ledger.dir, else {LEDGER_DIR})")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.calibrate:
        return calibrate(a.dir, a.calibrate)
    if a.open:
        rows = open_requests(a.dir, a.session)
        print(f"{len(rows)} open requests" + (f" in session {a.session}" if a.session else ""))
        show_open(rows)
        return 0
    if a.questions:
        rows = open_questions(a.dir)
        print(f"{len(rows)} open questions")
        show_open(rows)
        return 0
    _, problems = run(a.dir, rebuild=a.rebuild, relink=a.relink)
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
