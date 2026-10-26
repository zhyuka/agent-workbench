"""seals.py - the .recall-sealed rule, held once for every tool that reads notes, transcripts or logs.

    import seals as sealing        (recall.py and log_extract.py do)

A folder that holds a `.recall-sealed` marker is held back from each tool that reads it, and so is any
Claude Code log one of the marker's `logs` patterns names. The marker is JSON: reason, owner, until (an
ISO date; the seal holds through that day, then is ignored and COVERAGE says it expired) and logs, a list
of patterns such as agent-<id>, a session id or a project-folder name. A pattern matches one whole
component of a log copy's path, with or without its extension, on disk, in a zip or in a zip inside a
zip; * and ? work inside a component. It is matched against the log's key too, so a session id seals
that run's claude -p capture (key headless/<session>.jsonl), whose file name does not carry the id.
A session id seals its subagents too. When any copy of a log
matches, every copy of it is held back, and so is a log copy on disk inside the sealed folder. A marker
that will not parse still seals its folder, as it was put there to hide it; its logs patterns are then
unknown. A tool's --include-sealed lifts every seal that has not expired, and its COVERAGE says so.

WHAT IS HERE
  walk(dirs, found, ...)          os.walk the folders, finding markers on the way; each folder comes with
                                  the innermost seal holding it
  find(dirs, ...)                 the markers alone
  parse(folder, today, lift)      one marker as a seal: active, expired, lifted or unreadable
  sealed_at(path, seals)          the innermost seal, in force or lifted, whose folder holds path
  hold_files(files, seals, what)  a tool's files less those a seal holds, with counts
  glob_rx(pattern)                a logs pattern as a regex over a log copy's path
  hold_logs(inv, copies, seals)   a cc_logs inventory's copies less those a seal holds, with counts
  plural, logs_part, line         the COVERAGE line each tool prints per seal

Each tool finds markers in the folders it reads, before it reads any: recall.py in its notes and
transcripts folders, log_extract.py in recall's notes folders. A marker outside those folders is never
found.
"""
import collections
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import cc_logs  # noqa: E402

# Why seal at all: an experiment's sealed predictions and held-out checks sit in notes, and the logs hold
# the very agents under test. Without a seal, one search by an agent in the experiment reads the answers.
# This module holds the rule once so every tool that reads the same notes and logs honours it the same way.

HOME = os.path.expanduser("~")
SEAL = ".recall-sealed"
FIELDS = ("reason", "owner", "until", "logs")


def _why(e):
    return cc_logs.redact(f"{type(e).__name__}: {e}").splitlines()[0][:200]


def parse(d, today, lift):
    """The marker in folder d as a seal: dir, reason, owner, until, logs, error, and state: active,
    expired (past its until date, ignored), lifted (--include-sealed) or unreadable. An unreadable
    marker still seals its folder, as it was put there to hide it; its logs patterns are then unknown."""
    s = {"dir": d, "reason": "", "owner": "", "until": "", "logs": [], "error": "", "state": "active"}
    try:
        with open(os.path.join(d, SEAL), encoding="utf-8") as fh:
            m = json.load(fh)
        if not isinstance(m, dict):
            raise ValueError("not a JSON object")
        s.update(reason=str(m.get("reason", "")), owner=str(m.get("owner", "")))
        missing = [k for k in FIELDS if k not in m]
        if missing:
            raise ValueError("no " + ", ".join(missing))
        if not isinstance(m["logs"], list) or not all(isinstance(p, str) and p.strip() for p in m["logs"]):
            raise ValueError("logs is not a list of patterns")
        until = datetime.date.fromisoformat(str(m["until"])[:10])
        s.update(until=until.isoformat(), logs=[p.strip() for p in m["logs"]])
        if today > until:
            s["state"] = "expired"
    except (OSError, ValueError) as e:
        s.update(state="unreadable", error=_why(e))
    if lift and s["state"] != "expired":
        s["state"] = "lifted"
    return s


def walk(dirs, found, prune=(), today=None, lift=False, markers=True):
    """os.walk each folder in dirs, leaving out the folder names in prune, and find the markers on the way.
    Yields (folder, its file names, the innermost seal in force or lifted that holds it, else None). Every
    marker met, expired ones too, is appended to found when the walk reaches its folder, so a seal is known
    before any file under it is yielded. markers=False finds none: a self-test's broken rule."""
    today = today or datetime.date.today()
    for d in dirs:
        under = {}                                        # folder -> the innermost seal holding it, or None
        for root, dn, fn in os.walk(d):
            dn[:] = [x for x in dn if x not in prune]
            s = under.get(os.path.dirname(root))
            if markers and SEAL in fn:
                got = parse(root, today, lift)
                found.append(got)
                if got["state"] != "expired":
                    s = got
            under[root] = s
            yield root, fn, s


def find(dirs, prune=(), today=None, lift=False, markers=True):
    """Every marker under dirs, as seals, expired ones included."""
    found = []
    for _ in walk(dirs, found, prune, today, lift, markers):
        pass
    return found


def sealed_at(path, seals):
    """The innermost seal, in force or lifted, whose folder holds path; else None."""
    best = None
    for s in seals:
        if s["state"] != "expired" and (path == s["dir"] or path.startswith(s["dir"] + os.sep)) \
                and (best is None or len(s["dir"]) > len(best["dir"])):
            best = s
    return best


def hold_files(files, seals, what, path=lambda f: f):
    """A source's files less those a seal holds -> (files to search, {seal folder: {what: n}})."""
    keep, held = [], collections.defaultdict(collections.Counter)
    for f in files:
        s = sealed_at(path(f), seals)
        if s:
            held[s["dir"]][what] += 1
        if not s or s["state"] == "lifted":
            keep.append(f)
    return keep, {d: dict(c) for d, c in held.items()}


def glob_rx(p):
    """A logs pattern as a regex over a copy's path: one whole component, with or without its extension;
    * and ? match inside a component."""
    body = "".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in p)
    return re.compile(rf"(?:^|/){body}(?:\.[^/]*)?(?:/|$)")


def hold_logs(inv, copies, seals):
    """The log copies less those a seal holds -> (copies to read, {seal folder: {"log copies", "logs"}},
    {seal folder: [its logs patterns that matched no copy]}). inv is a cc_logs.Inventory and copies the
    ones a tool would read. A log is sealed when a pattern matches the path of any of its copies in inv
    (on disk, in a zip, in a zip inside a zip), or a copy sits on disk inside a sealed folder. Then every
    copy of it is, so an archive copy with no project folder in its path goes too."""
    live = [s for s in seals if s["state"] != "expired"]
    if not live:
        return copies, {}, {}
    pats = [(s, p, glob_rx(p), max(re.split(r"[*?]", p), key=len)) for s in live for p in s["logs"]]
    of, used = {}, set()
    for key, cs in inv.logs.items():
        for c in cs:
            at = "/".join(x for x in (c.path, c.member, c.inner) if x).replace("\\", "/")
            for s, p, rx, lit in pats:   # the key too: a claude -p capture's name does not carry its session id
                if any(lit in t and rx.search(t) for t in (at, c.key)):
                    used.add((s["dir"], p))
                    of.setdefault(key, s)
            s = sealed_at(c.path, live)
            if s:
                of.setdefault(key, s)
    keep, held, keys = [], collections.defaultdict(collections.Counter), collections.defaultdict(set)
    for c in copies:
        s = of.get(c.key)
        if s:
            held[s["dir"]]["log copies"] += 1
            keys[s["dir"]].add(c.key)
        if not s or s["state"] == "lifted":
            keep.append(c)
    for d, ks in keys.items():
        held[d]["logs"] = len(ks)
    nomatch = {s["dir"]: [p for p in s["logs"] if (s["dir"], p) not in used] for s in live}
    return keep, {d: dict(c) for d, c in held.items()}, {d: ps for d, ps in nomatch.items() if ps}


def plural(n, one, many):
    return f"{n:,} {one if n == 1 else many}"


def logs_part(h):
    """What a seal held back from the logs, as COVERAGE words it: N log copies of M logs."""
    return plural(h.get("log copies", 0), "log copy", "log copies") + " of " + plural(h.get("logs", 0), "log", "logs")


def line(s, what, miss=None):
    """One COVERAGE line for seal s: its folder, until date, reason and owner, and what it held back, so a
    seal never reads as a clean zero. what: the worded count from each source that ran, in order; miss:
    the seal's logs patterns that matched no log copy, given only when logs were read."""
    at, why = s["dir"].replace(HOME, "~"), cc_logs.redact(f'"{s["reason"]}", owner "{s["owner"]}"')
    if s["state"] == "expired":
        return f"seal EXPIRED on {s['until']}, ignored, so its folder and logs were searched as usual: {at} ({why})"
    lifted = s["state"] == "lifted"
    out = (("seal LIFTED by --include-sealed" if lifted else "seal") + f": {at} "
           + (f"until {s['until']}" if s["until"] else "with no until date") + f" ({why}): "
           + ("searched anyway: " if lifted else "not searched: ")
           + (", ".join(what) or "no source that reads files or logs ran"))
    if s["error"]:
        out += f"; marker unreadable ({s['error']}): its folder counts as sealed, its logs patterns are unknown"
    if miss:
        out += (f"; {len(miss)} of {len(s['logs'])} logs patterns matched no log copy: "
                + ", ".join(repr(p) for p in miss[:3]) + (f" (+{len(miss) - 3} more)" if len(miss) > 3 else ""))
    return out + ("" if lifted else "; --include-sealed searches it")


if __name__ == "__main__":
    print(__doc__)
