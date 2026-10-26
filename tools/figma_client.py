#!/usr/bin/env python3
"""One Figma REST client for every tool and gate: a shared budget, lanes, a 429 pause, a call log.

WHY THIS EXISTS
  Every tool that builds its own HTTP client draws on the same per-user Figma budget (a personal
  access token counts per user and plan) without knowing about the others. Half of them give up on
  the first 429, and no log says which tool spent the budget. Under load that turns a check into
  DID NOT RUN, and a commit-time gate can block on a batch job's 429s. One client fixes all three.

WHAT IT DOES
  Budget. One token bucket per REST tier, in <state dir>/bucket.json, locked with flock, so every
  process and every checkout's copy of this file draws on the same budget. Limits a minute, from
  the top seat's published rate limits (20 / 100 / 150) less about 10%: T1 18, T2 90, T3 135.
  Override them with FIGMA_CLIENT_LIMITS for a smaller plan.
  T1 is GET file, file nodes and image renders; T2 comments, versions, variables, image fills,
  webhooks, projects; T3 /meta, components, styles, /me. An endpoint it does not know is T1.
  Figma sends no budget headers on a success, so the bucket is the only gauge there is.

  Lanes, by reserve: gate > interactive > batch. A lane takes a token only while more than its
  reserve is left: batch leaves 6, interactive 3, gate 0. Commit-time gates are served while a
  heavy batch job is starved, with no central queue.

  429. Any 429 sets a pause until now + Retry-After that every process obeys, empties that tier's
  bucket, and the call is retried. A 429 never ends a call while its next try still fits before
  the caller's deadline. Gate calls default to a 120 s deadline; interactive and batch wait.
  A 5xx or a dropped connection on a GET is retried 4 times (1, 2, 4, 8 s); a POST or DELETE is
  retried only on a 429, which Figma refuses before acting on.

  Cache. A ?version=<id> read of a file or its nodes is kept forever once that version is closed,
  meaning the file has a newer one: a saved version never changes, but the newest one still takes
  edits. A live read of a file the mirror does not hold is served from cache only when the caller
  asks (max_age, or FIGMA_CLIENT_MAX_AGE). prefetch_nodes() reads many nodes in ids= batches and
  files each one where a single-node read with max_age finds it.

  Mirror (optional; off until configured). A file's version moves every half hour or so of
  editing, so a cache keyed on the version alone is either stale or always missing, and tree reads
  of a large, busy file can spend most of the Tier 1 budget. For the files and pages named in
  kit_config's `figma.mirror` ({"<file key>": ["<page name>", ...]}), the pages are kept on disk
  under <state dir>/mirror/<key>/, cut into chunks (each frame or other non-section child of a page
  or section) with the sections and pages as an outline above them. A page is matched by name, read
  from the depth=1 tree at every outline refresh; ids are never configured. Every chunk and the
  outline carry the file's version AND last_touched_at from the read that filled them.
  Every live read of that file starts with /meta (Tier 3, memoised META_TTL = 10 s across
  processes). A part whose stamp is older than that answer is re-read before it is used, one
  batched /nodes call for all the stale parts a read needs plus the stale chunks read in the last
  HOT_WINDOW (up to CHUNK_BATCH_BYTES), one process at a time behind a lock: the others wait on
  the lock and then find it current. So the mirror is current as of the last edit on every read,
  and a read with no edit since costs no Tier 1 call. If /meta cannot be read the read raises;
  nothing stale is ever served. A /nodes answer has REST's own shape: depth and geometry honoured,
  the document/components/componentSets/styles envelope. An id the mirror does not hold (a library
  component, a node on another page) is asked of Figma in one batched /nodes call per read, kept
  until the next edit, and never declared dead by the mirror. A render is kept under the node's
  subtree hash, scale and format, so a node that did not change is not rendered again across edits
  or versions (its URL is reused for RENDER_TTL). GET files/KEY (?ids=, ?depth=), any other Tier 1
  read of the file, and any answer the mirror cannot build exactly go to Figma once per edit and
  are kept until the next. ?version= reads keep the versioned cache above. `mirror sync` fills the
  mirror (expect hundreds of MB and dozens of calls for a few large pages).

  Lineage. Tools often read GET files/KEY?ids= (plus nodes?ids=<ancestors>&depth=1 for siblings)
  or a whole-file files/KEY?depth=N only to learn where a node sits: its ancestors, their
  properties, their children. Those reads are expensive and the mirror can only pass them to Figma
  once per edit. lineage(key, ids) answers that question directly: {id: [entry, ...]} from the page
  down to the node itself, each entry the node's own properties without children (and without
  geometry), and every entry but the last also with "kids", its children's properties without
  their children, in paint order (bottom first). None for an id Figma does not have. It is a
  structural answer, not a REST lookalike: no envelope, no components/styles maps, no DOCUMENT
  root. For a mirrored file the outline gives the chain down to a chunk's root as it was last read,
  and each hop is checked against the current kids of the pages and sections it crosses: from the
  outline when it is current (it holds each chunk's own properties, so a section's kids need no
  chunk read), else from one nodes?ids=<them>&depth=1 read per edit, kept in shells.json. Below a
  chunk's root, the index's parent pointers, each checked against the chunk body plan() made
  current. A hop that disagrees is never answered from: that id goes to Figma. An id the mirror
  does not hold, a file it does not cover, or a version= read gets Figma's own answer through get():
  files?ids=<ids>, then nodes?ids=<ancestors>&depth=1 for the kids, kids of a page or section
  already read at this edit taken from there; a section's kids read that way may cost the mirror
  its outline re-read (files?depth=1 and nodes?depth=3), once per edit.
  drawn_over(chain, box) is the one copy of the "layers outside a node that paint over it" rule
  (figma_evidence calls it). The --audit fails a tree read of a file (files/KEY?ids=, ?depth=, or
  files/KEY whole) outside this file unless its line carries `figma-tree-read: <why>`.

  Log. <state dir>/calls.jsonl, one line per attempt or cache hit: ts, tool, job (the
  parent that set FIGMA_CLIENT_TOOL, when the call names its own tool), pid, lane, method, endpoint,
  tier, status, retry_after, cache_hit, bytes, ms, wait_ms. Never the token and never a response
  body. A call that ends without an answer gets a line of its own: status "gave-up" when the rate
  limit outlasted its deadline (no token in its lane's budget, or a 429 whose Retry-After passes
  the deadline), "no-token" when no token resolved. A 429 line also has rate_limit_type and
  plan_tier, Figma's X-Figma-Rate-Limit-Type and X-Figma-Plan-Tier headers (null when absent). A
  read the mirror answered has mirror "served"; a call the mirror made says why (meta, outline,
  refresh, missing, render, edit, sync). `status` sums the last minutes by tool.

USE
  python    import figma_client
            figma_client.get("files/KEY/nodes?ids=1:2", lane="gate", tool="my_tool")   # parsed JSON
            figma_client.get_bytes(path, ...), .post(path, body, ...), .delete(path, ...)
            figma_client.prefetch_nodes(KEY, ids, query="geometry=paths")
            figma_client.lineage(KEY, ids, kids=True, version=None, **get's keywords)   # see Lineage
            figma_client.lineage_or_exit(...)        lineage(), DID NOT RUN as get_or_exit stops
            figma_client.drawn_over(lineage[id], box)   # layers outside the node that paint over box
            figma_client.lineage_in_tree(tree, ids)  # lineage's answer off a tree holding every child
            Errors: urllib.error.HTTPError for a 4xx/5xx it does not retry; RateLimited (an
            HTTPError, code 429) past the deadline; NoToken; urllib.error.URLError on the network.
  CLI       python figma_client.py get <path> [--lane gate|interactive|batch] [--tool NAME]
                                   [--deadline S] [--deadline-at EPOCH] [--timeout S] [--max-age S]
            prints the JSON body. Exit 0 ok; 3 HTTP error (the body, when there is one, on stdout,
            `HTTP <code>` on stderr); 4 rate limited past the deadline; 5 network; 6 no token.
            python figma_client.py lineage <key> <id> [<id> ...] [--no-kids] [--version V] [--lane ...]
                                   [--tool NAME] [--deadline S] [--deadline-at EPOCH] [--timeout S]
                                   prints lineage()'s JSON; exit codes as get's
            python figma_client.py status [--minutes N]     bucket levels, pause, calls by tool
            python figma_client.py mirror sync|status [--key KEY]   fill the mirror; what it holds
            python figma_client.py --audit <dir> [<dir> ...]  N of M Figma callers routed here
            python figma_client.py --selftest [--break NAME]  offline, against a local mock; --break
                                   plants one fault (off, never-current, version-only, nolock, dead,
                                   render-stamp, meta-fallback, no-429-headers, no-hot, trust-pointers,
                                   no-fallback, stale-outline) to show a check fail
  A path may be "files/...", "/v1/files/..." or a full api.figma.com URL (a next_page link).

  Environment, for a parent job to set what its children inherit:
    FIGMA_CLIENT_LANE, FIGMA_CLIENT_TOOL       lane and tool name when the call names none
    FIGMA_CLIENT_DEADLINE, FIGMA_CLIENT_DEADLINE_AT   seconds per call; an epoch the whole job ends by
    FIGMA_CLIENT_MAX_AGE                        let live tree reads be served from cache this young
    FIGMA_CLIENT_MIRROR=0                       read Figma directly, as before the mirror
    FIGMA_CLIENT_META_TTL                       seconds a /meta answer stands for every process (10)
    FIGMA_CLIENT_DIR                            state, cache and log (default
                                                ~/.cache/agent-workbench/figma_client)
    FIGMA_CLIENT_LIMITS ("1=18,2=90,3=135")     calls per budget window per tier, for a smaller plan
  For tests only: FIGMA_API_BASE (where calls go), FIGMA_CLIENT_PERIOD (seconds per budget window),
  FIGMA_MIRROR_DIR (where the mirror lives), FIGMA_MIRROR_FILES ("KEY=Page A|Page B;KEY2=..." in
  place of kit_config's figma.mirror).

BLIND TO
  Calls that do not come through here: the Figma MCP server, Figma's own apps, and any tool that
  builds its own request (--audit catches those only in the folders it is given). Figma's own bucket: if something else spends the budget, the first sign is a 429,
  which this then rides out. Whether the limits above are still Figma's (a file owned by a team on
  a lower plan gets far fewer; set FIGMA_CLIENT_LIMITS).
  The mirror is blind to an edit made less than META_TTL seconds before a read, to anything a
  node's REST JSON does not carry (a render's subtree hash cannot see a change it does not show),
  and to the rule it uses to rebuild the components/componentSets/styles maps, which was read off
  several hundred cached REST answers and is checked against live REST, not proven for every
  node type. A render URL Figma expires early is served until RENDER_TTL.
  lineage() is blind to anything a node's REST JSON does not carry, as above, and its entries hold
  no geometry=paths fields. Its tree-read rule in --audit reads one line at a time: a path built
  across lines, or from a base variable named anything but {NAME} right before ?ids=, is not seen.
"""

import argparse
import collections
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:  # Windows: msvcrt locks a byte instead
    fcntl = None
    import msvcrt

HERE = os.path.dirname(os.path.abspath(__file__))
FIGMA_HOST = "api.figma.com"
DEFAULT_BASE = "https://" + FIGMA_HOST
LIMITS = {1: 18, 2: 90, 3: 135}            # per PERIOD seconds
PERIOD = 60.0
RESERVE = {"gate": 0, "interactive": 3, "batch": 6}
LANES = tuple(RESERVE)
DEADLINE = {"gate": 120.0, "interactive": None, "batch": None}
RETRY_5XX = (1, 2, 4, 8)
LOG_MAX = 50 * 1024 * 1024

# The mirror. Pages are named, not numbered: the ids are read from the file's depth=1 tree at every
# outline refresh. A name also takes pages whose words start with it ("Components" takes
# "-> Components" and "Components (old)"); _page_wanted has the rule. Configured in kit_config's
# figma.mirror; empty means the mirror is off.
MIRROR_FILES = {}
META_TTL = 10.0                 # seconds one /meta answer stands for every process
RENDER_TTL = 7 * 86400          # seconds a render URL is reused; Figma keeps them about 30 days
OUTLINE_DEPTH = 3               # the outline reads pages this deep, and again under a section cut there
CHUNK_BATCH_BYTES = 40e6        # one refresh call asks for about this much at most
CHUNK_BATCH_IDS = 25
HOT_WINDOW = 6 * 3600           # a refresh also re-reads stale chunks read this recently, in the same call
# Fields REST sends only with geometry=paths (read off cached answers both ways).
GEOM_KEYS = frozenset(("fillGeometry", "strokeGeometry", "size", "relativeTransform", "vectorNetwork",
                       "count", "starInnerScale"))
ENV_KEYS = ("name", "lastModified", "thumbnailUrl", "version", "role", "editorType", "linkAccess")
RENDER_PARAMS = frozenset(("ids", "scale", "format", "use_absolute_bounds", "version", "svg_outline_text",
                           "svg_include_id", "svg_include_node_id", "svg_simplify_stroke", "contents_only"))


class NoToken(RuntimeError):
    pass


class RateLimited(urllib.error.HTTPError):
    """A 429 that outlasted the caller's deadline, or a token that could not be had before it."""

    def __init__(self, url, why):
        super().__init__(url, 429, "figma_client: " + why, {}, io.BytesIO(b""))


# ---------------------------------------------------------------- settings, read at call time

def base():
    return (os.environ.get("FIGMA_API_BASE") or DEFAULT_BASE).rstrip("/")


def state_dir():
    return os.environ.get("FIGMA_CLIENT_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "agent-workbench",
                                                             "figma_client")


def limits():
    out = dict(LIMITS)
    for part in (os.environ.get("FIGMA_CLIENT_LIMITS") or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[int(k)] = float(v)
    return out


def period():
    return float(os.environ.get("FIGMA_CLIENT_PERIOD") or PERIOD)


def _env_float(name):
    v = os.environ.get(name)
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _tool(tool):
    return tool or os.environ.get("FIGMA_CLIENT_TOOL") or os.path.splitext(os.path.basename(sys.argv[0] or "python"))[0]


def _job(tool):
    """The parent job that set FIGMA_CLIENT_TOOL for its children, when the call names its own tool."""
    job = os.environ.get("FIGMA_CLIENT_TOOL")
    return job if job and job != _tool(tool) else None


def resolve_lane(lane=None, default_lane="interactive"):
    """An explicit lane wins, then the parent job's FIGMA_CLIENT_LANE, then the tool's default."""
    got = lane or os.environ.get("FIGMA_CLIENT_LANE") or default_lane
    if got not in RESERVE:
        raise ValueError(f"lane must be one of {', '.join(LANES)}, not {got!r}")
    return got


# ---------------------------------------------------------------- paths and tiers

def normalise(path_or_url):
    """'/v1/...' with spaces and other unsafe characters quoted; % stays, so nothing is quoted twice."""
    s = str(path_or_url)
    if "://" in s:
        u = urllib.parse.urlsplit(s)
        if u.netloc not in (FIGMA_HOST, urllib.parse.urlsplit(base()).netloc):
            raise ValueError(f"not a Figma REST URL: {u.scheme}://{u.netloc}")
        s = u.path + ("?" + u.query if u.query else "")
    s = s.lstrip("/")
    if not re.match(r"v\d+/", s):
        s = "v1/" + s
    return "/" + urllib.parse.quote(s, safe="/?&=%:,;+@!$'()*~")


_T3 = re.compile(r"^/v1/(me$|files/[^/]+/(meta|components|component_sets|styles)$"
                 r"|(components|component_sets|styles)/|teams/[^/]+/(components|component_sets|styles)$)")
_T2 = re.compile(r"^/v\d+/(files/[^/]+/(comments|versions|variables|images|dev_resources|webhooks)"
                 r"|webhooks|projects/|teams/[^/]+/(projects|webhooks)|dev_resources|activity_logs)")


def tier(path):
    p = normalise(path).split("?", 1)[0]
    return 3 if _T3.match(p) else 2 if _T2.match(p) else 1


def canonical(p):
    """The cache key: query pairs sorted and quoted one way, so ids=1:2 and ids=1%3A2 are one read."""
    path, _, q = p.partition("?")
    pairs = sorted(urllib.parse.parse_qsl(q, keep_blank_values=True))
    return path + ("?" + "&".join(f"{k}={urllib.parse.quote(v, safe=':,;')}" for k, v in pairs) if pairs else "")


def endpoint(method, p):
    """What the log keeps: long ids lists become a count."""
    path, _, q = p.partition("?")
    parts = []
    for k, v in urllib.parse.parse_qsl(q, keep_blank_values=True):
        if k == "ids" and len(v) > 40:
            v = f"<{len(v.split(','))} ids>"
        elif len(v) > 60:
            v = v[:57] + "..."
        parts.append(f"{k}={v}")
    return f"{method} {path}" + ("?" + "&".join(parts) if parts else "")


def _file_key(p):
    m = re.match(r"^/v1/(?:files|images)/([^/?]+)", p)
    return m.group(1) if m else None


# ---------------------------------------------------------------- shared state, under one lock

def _flock(fd):
    if fcntl:
        fcntl.flock(fd, fcntl.LOCK_EX)
    else:
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.05)


def _funlock(fd):
    if fcntl:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _atomic(path, data):
    tmp = f"{path}.{os.getpid()}.{random.randrange(1 << 30)}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


@contextlib.contextmanager
def locked():
    """The bucket state, read and written under an exclusive flock. Not fsynced: a lost write only
    refills a bucket early, and the next 429 corrects that."""
    d = state_dir()
    os.makedirs(d, exist_ok=True)
    fd = os.open(os.path.join(d, "bucket.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _flock(fd)
        path = os.path.join(d, "bucket.json")
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
            if not isinstance(st, dict):
                raise ValueError
        except (OSError, ValueError):
            st = {}
        st.setdefault("tiers", {})
        before = json.dumps(st, sort_keys=True)
        yield st
        after = json.dumps(st, sort_keys=True)
        if after != before:
            _atomic(path, after.encode("utf-8"))
        log = os.path.join(d, "calls.jsonl")
        try:
            if os.path.getsize(log) > LOG_MAX:
                os.replace(log, os.path.join(d, "calls.1.jsonl"))
        except OSError:
            pass
    finally:
        try:
            _funlock(fd)
        finally:
            os.close(fd)


def _bucket(st, t, now):
    cap = limits()[t]
    b = st["tiers"].setdefault(str(t), {"level": cap, "at": now})
    b["level"] = min(cap, float(b.get("level", cap)) + max(0.0, now - float(b.get("at", now))) * cap / period())
    b["at"] = now
    return b


def _acquire(t, lane, deadline_at, url):
    """Take one token of tier t for this lane, waiting as long as it must.
    Returns (seconds waited in all, seconds of that spent in a 429 pause)."""
    cap = limits()[t]
    need = max(1.0, min(RESERVE[lane] + 1.0, cap))
    t0 = time.time()
    paused = 0.0
    while True:
        with locked() as st:
            now = time.time()
            pause = float(st.get("pause_until") or 0)
            if pause > now:
                wait, in_pause = pause - now, True
            else:
                b = _bucket(st, t, now)
                if b["level"] >= need:
                    b["level"] -= 1.0
                    return now - t0, paused
                wait, in_pause = (need - b["level"]) / (cap / period()), False
        if deadline_at is not None and time.time() + wait > deadline_at:
            raise RateLimited(url, f"no T{t} token for the {lane} lane before the deadline "
                                   f"(next in {wait:.1f}s, deadline in {max(0.0, deadline_at - time.time()):.1f}s)")
        s = min(wait, 1.0) + random.uniform(0, 0.05)
        time.sleep(s)
        paused += s if in_pause else 0.0


def _pause(t, retry_after):
    with locked() as st:
        now = time.time()
        st["pause_until"] = max(float(st.get("pause_until") or 0), now + retry_after)
        _bucket(st, t, now)["level"] = 0.0
        st["last_429"] = {"at": round(now, 3), "tier": t, "retry_after": retry_after, "pid": os.getpid()}


def _retry_after(headers, streak):
    v = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        return max(0.5, float(v))
    except (TypeError, ValueError):
        pass
    if v:
        try:
            import email.utils
            return max(0.5, email.utils.parsedate_to_datetime(v).timestamp() - time.time())
        except (TypeError, ValueError):
            pass
    return float(min(60, 5 * 2 ** streak))


def _log(rec):
    try:
        d = state_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "calls.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except OSError:
        pass  # a log that cannot be written never fails the call


def _record(tool, lane, method, p, t, status, ms, nbytes=0, retry_after=None, cache_hit=False, wait=(0.0, 0.0),
            extra=None):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z", "tool": _tool(tool), "job": _job(tool),
           "pid": os.getpid(),
           "lane": lane, "method": method, "endpoint": endpoint(method, p), "tier": t, "status": status,
           "retry_after": retry_after, "cache_hit": cache_hit, "bytes": nbytes, "ms": int(ms),
           "wait_ms": int(wait[0] * 1000), "pause_ms": int(wait[1] * 1000)}
    rec.update(extra or {})
    _log(rec)


def _rate_headers(headers):
    """What a 429 says about whose budget ran out. A 429 after very few logged calls means something
    this log does not see shares the budget; these two headers say which."""
    h = headers or {}
    return {"rate_limit_type": h.get("X-Figma-Rate-Limit-Type"), "plan_tier": h.get("X-Figma-Plan-Tier")}


def log_path():
    return os.path.join(state_dir(), "calls.jsonl")


def log_mark():
    """Where the log ends now; calls_since(mark) reads what was written after it."""
    try:
        return os.path.getsize(log_path())
    except OSError:
        return 0


def calls_since(mark, prefix=None):
    """The log lines written since log_mark() gave `mark`, those whose tool or job starts with
    `prefix` when one is given. A log rotated in between is read from its old file too."""
    out, chunks = [], []
    try:
        if os.path.getsize(log_path()) < mark:
            chunks.append((os.path.join(state_dir(), "calls.1.jsonl"), mark))
            mark = 0
    except OSError:
        return out
    chunks.append((log_path(), mark))
    for path, off in chunks:
        try:
            with open(path, "rb") as f:
                f.seek(off)
                text = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if prefix is None or str(r.get("tool") or "").startswith(prefix) or str(r.get("job") or "").startswith(prefix):
                out.append(r)
    return out


def waits(lines):
    """{'calls', 'n429', 'pause_s', 'wait_s'} over some log lines: how a job met the rate limit."""
    return {"calls": len(lines), "n429": sum(1 for r in lines if r.get("status") == 429),
            "pause_s": sum(r.get("pause_ms") or 0 for r in lines) / 1000.0,
            "wait_s": sum(r.get("wait_ms") or 0 for r in lines) / 1000.0}


# ---------------------------------------------------------------- cache

def _vcache_eligible(method, p):
    path, _, q = p.partition("?")
    return (method == "GET" and re.match(r"^/v1/files/[^/]+(/nodes)?$", path) is not None
            and "version" in dict(urllib.parse.parse_qsl(q)))


def _live_eligible(method, p):
    return method == "GET" and re.match(r"^/v1/files/[^/]+(/nodes|/meta|/variables/local)?(\?|$)", p) is not None


def _prune_live(older_than=86400):
    """Drop live entries a day old: they are only ever served to a caller that asked for minutes."""
    root = os.path.join(state_dir(), "live")
    cut = time.time() - older_than
    for d, _, files in os.walk(root):
        for fn in files:
            try:
                if os.path.getmtime(os.path.join(d, fn)) < cut:
                    os.remove(os.path.join(d, fn))
            except OSError:
                pass


def _cpath(kind, key, p):
    h = hashlib.sha256(canonical(p).encode("utf-8")).hexdigest()[:40]
    return os.path.join(state_dir(), kind, key or "_", h + ".json")


def _closed(key):
    try:
        with open(os.path.join(state_dir(), "versioned", key, "closed.txt"), encoding="utf-8") as f:
            return {l.strip() for l in f if l.strip()}
    except OSError:
        return set()


def meta_ttl():
    v = _env_float("FIGMA_CLIENT_META_TTL")
    return META_TTL if v is None else v


def _file_stamp(key, lane, tool, deadline_at, ttl=None, token=None, need_edit=True):
    """(version, last_touched_at) of the file, asked of /meta (T3) at most once per `ttl` seconds
    across processes (bucket.json "current": [version, asked at, last_touched_at]). A /meta that
    cannot be read raises, and so does one without a last edit when need_edit: the mirror then has
    no way to show an answer current, so it gives none."""
    ttl = meta_ttl() if ttl is None else ttl
    with locked() as st:
        memo = (st.get("current") or {}).get(key)
    if memo and time.time() - float(memo[1]) < ttl and (not need_edit or (len(memo) > 2 and memo[2])):
        return memo[0], (memo[2] if len(memo) > 2 else None)
    meta = json.loads(_request("GET", f"/v1/files/{key}/meta", None, lane, tool, deadline_at, 60, {}, token,
                               None, False, mirror=False, why="meta").data)
    f = meta.get("file") or meta
    v, lta = f.get("version"), f.get("last_touched_at")
    with locked() as st:
        st.setdefault("current", {})[key] = [v, time.time(), lta]
    if need_edit and (not v or _ts(lta) is None):
        raise MirrorError(f"/meta for {key} gave no version or last_touched_at, so no mirror answer can be shown current")
    return v, lta


def _current_version(key, lane, tool, deadline_at):
    """The file's newest version, asked of /meta (T3) at most once a minute across processes."""
    return _file_stamp(key, lane, tool, deadline_at, ttl=60, need_edit=False)[0]


def _older(v, cur):
    if v == cur or cur is None:
        return False
    try:
        return int(v) < int(cur)
    except (TypeError, ValueError):
        return True


def _vcache_get(p, lane, tool, deadline_at):
    key = _file_key(p)
    path = _cpath("versioned", key, p)
    if not os.path.exists(path):
        return None
    v = dict(urllib.parse.parse_qsl(p.partition("?")[2])).get("version")
    if v not in _closed(key):
        try:
            cur = _current_version(key, lane, tool, deadline_at)
        except Exception:  # noqa: BLE001 -- no answer about closure is a miss, never a stale hit
            return None
        if not _older(v, cur):
            return None
        with open(os.path.join(state_dir(), "versioned", key, "closed.txt"), "a", encoding="utf-8") as f:
            f.write(v + "\n")
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _store(kind, p, data):
    path = _cpath(kind, _file_key(p), p)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic(path, data)
    except OSError:
        pass


def _live_get(p, max_age):
    path = _cpath("live", _file_key(p), p)
    try:
        if time.time() - os.path.getmtime(path) <= max_age:
            with open(path, "rb") as f:
                return f.read()
    except OSError:
        pass
    return None


# ---------------------------------------------------------------- the mirror

class MirrorError(urllib.error.URLError):
    """/meta answered without a version or last edit: no mirror answer can be shown current."""


class _CannotCompose(Exception):
    """The mirror cannot build this answer exactly; the read goes to Figma, kept until the next edit."""


def mirror_files():
    """{file key: (page name, ...)}: FIGMA_MIRROR_FILES, else kit_config's figma.mirror, else none."""
    raw = os.environ.get("FIGMA_MIRROR_FILES")
    if raw is None:
        try:
            if HERE not in sys.path:
                sys.path.insert(0, HERE)
            import kit_config
            conf = kit_config.get("figma", "mirror") or {}
        except Exception:  # noqa: BLE001 -- no config (or none readable) means no mirror, never a crash
            conf = {}
        out = dict(MIRROR_FILES)
        if isinstance(conf, dict):
            for k, v in conf.items():
                if isinstance(v, (list, tuple)):
                    out[str(k)] = tuple(str(x) for x in v)
        return out
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = tuple(x.strip() for x in v.split("|") if x.strip())
    return out


def _mirror_on(key):
    return bool(key) and os.environ.get("FIGMA_CLIENT_MIRROR", "1") != "0" and key in mirror_files()


def mirror_root():
    return os.environ.get("FIGMA_MIRROR_DIR") or os.path.join(state_dir(), "mirror")


def _page_wanted(name, wants):
    """'   -> Components *' reads as 'Components', 'Components * (old)' as 'Components old': a page is
    wanted when its words are a wanted name or start with one."""
    norm = re.sub(r"[\W_]+", " ", str(name or "")).strip()
    return any(norm == w or norm.startswith(w + " ") for w in wants)


def _ts(s):
    """Figma's '2026-10-24T08:20:32.962Z' as epoch seconds; None when it is not that shape."""
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?(?:Z|[+-]00:?00)?$", str(s or ""))
    if not m:
        return None
    import calendar
    return calendar.timegm(tuple(int(x) for x in m.groups()[:6]) + (0, 0, 0)) + float(m.group(7) or 0)


def _covers(have, cur):
    """A mirror part stamped `have` holds the file as of `cur` or later: its version is not older
    and its last edit is not older. Both, since a version alone moves only every 30-40 minutes of
    editing and an edit since it is exactly what a version-keyed cache misses."""
    if not have or not cur:
        return False
    a, b = _ts(have[1]), _ts(cur[1])
    return a is not None and b is not None and a >= b and not _older(have[0], cur[0])


def _newer(a, b):
    return (_ts(a[1]) or 0, int(a[0]) if str(a[0]).isdigit() else 0) > (_ts(b[1]) or 0, int(b[0]) if str(b[0]).isdigit() else 0)


def _safe(nid):
    return re.sub(r"[^0-9A-Za-z]+", "-", nid)[:80] + "-" + hashlib.sha256(nid.encode("utf-8")).hexdigest()[:8]


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.get("children") or []))


def _prune(n, depth, geometry):
    """A copy of n down to `depth` levels of children (None: all), as REST cuts it: a node at the cut
    keeps `children: []` when it has children at all. Without geometry, GEOM_KEYS are dropped."""
    out = {k: v for k, v in n.items() if k != "children" and (geometry or k not in GEOM_KEYS)}
    if "children" in n:
        out["children"] = [] if depth == 0 else [_prune(c, None if depth is None else depth - 1, geometry)
                                                  for c in n["children"]]
    return out


def _own(n):
    """A node's own properties, as a lineage entry holds them: no children, no geometry=paths fields."""
    return {k: v for k, v in n.items() if k != "children" and k not in GEOM_KEYS}


def _outline_props(nid, ol):
    """A section's or a chunk's own properties from the outline."""
    if nid in ol["shells"]:
        return dict(ol["shells"][nid]["props"])
    p = (ol["chunks"].get(nid) or {}).get("props")
    if p is None:
        raise _CannotCompose(f"the outline holds no properties for {nid}")
    return dict(p)


_INHERIT = re.compile(r"^inherit\w*StyleId$")
_NO_SCAN = frozenset(("children",)) | GEOM_KEYS


def _style_refs_in(v, out):
    if isinstance(v, dict):
        for k, x in v.items():
            if isinstance(x, str):
                if _INHERIT.match(k):
                    out.add(x)
            elif isinstance(x, (dict, list)):
                _style_refs_in(x, out)
    elif isinstance(v, list):
        for x in v:
            if isinstance(x, (dict, list)):
                _style_refs_in(x, out)


def _refs(root):
    """What REST lists in the envelope maps for a tree: each INSTANCE's componentId and each COMPONENT,
    each COMPONENT_SET, each style a node's `styles` names, and each inherit*StyleId (a text style
    kept inside `style` or `styleOverrideTable`). Read off 752 cached answers: it matched 710 of 710
    /nodes entries. GET files/KEY follows another rule (a whole-file read lists every local style,
    files?ids= adds components a prototype link points at), so those reads are never built here."""
    comps, sets, styles = set(), set(), set()
    for n in _walk(root):
        if n.get("componentId"):
            comps.add(n["componentId"])
        t = n.get("type")
        if t == "COMPONENT":
            comps.add(n["id"])
        elif t == "COMPONENT_SET":
            sets.add(n["id"])
        for k, v in n.items():
            if k in _NO_SCAN:
                continue
            if k == "styles" and isinstance(v, dict):
                styles.update(x for x in v.values() if isinstance(x, str))
            elif isinstance(v, (dict, list)):
                _style_refs_in(v, styles)
    return comps, sets, styles


def _set_entry(k, v, in_tree):
    """A component set's entry takes one of two forms in /nodes: {key, name, description,
    documentationLinks} when the set's own node is in the answer's tree, {key, name, description,
    remote} when the tree only uses one of its variants (every cached entry seen followed this). A set the mirror only saw in the first form is a local one: remote is false."""
    if in_tree:
        if "documentationLinks" not in v:
            raise _CannotCompose(f"component set {k}'s own entry is not in the maps the mirror holds")
        return {x: v[x] for x in ("key", "name", "description", "documentationLinks") if x in v}
    if "remote" in v:
        return {x: v[x] for x in ("key", "name", "description", "remote") if x in v}
    return dict({x: v[x] for x in ("key", "name", "description") if x in v}, remote=False)


def _maps(root, pool):
    comps, sets, styles = _refs(root)
    pc, ps, pst = pool
    try:
        c = {k: pc[k] for k in comps}
        in_tree = set(sets)
        sets |= {v["componentSetId"] for v in c.values() if v.get("componentSetId")}
        return c, {k: _set_entry(k, ps[k], k in in_tree) for k in sets}, {k: pst[k] for k in styles}
    except KeyError as e:
        raise _CannotCompose(f"{e} is in the tree but not in the maps the mirror holds")


def _hash_tree(n, parent, out):
    """Post-order: out[id] = [parent id, hash of the node's own fields and its children's hashes]."""
    kids = n.get("children")
    hs = [_hash_tree(c, n["id"], out) for c in kids or []]
    own = json.dumps({k: v for k, v in n.items() if k != "children"}, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256((own + "|" + ("-" if kids is None else ",".join(hs))).encode("utf-8")).hexdigest()[:24]
    out[n["id"]] = [parent, h]
    return h


def _render_key(nid, subtree_hash, stamp, params):
    """A render's cache key: the node's subtree hash when the mirror has it, so an unchanged band
    keeps its render across edits and versions; else the edit it was asked at."""
    basis = ["h", subtree_hash] if subtree_hash else ["e", stamp[0], stamp[1]]
    return hashlib.sha256(json.dumps([nid] + basis + sorted(params.items())).encode("utf-8")).hexdigest()[:40]


_JC = {}                                # path -> (file identity, parsed): index, outline, small files
_CHUNKS = collections.OrderedDict()     # the last few chunk files read, parsed
_N2C = {}                               # mirror dir -> (index object, {node id: chunk id})


def _read_json(path, lru=False):
    try:
        st = os.stat(path)
    except OSError:
        return None
    ident = (st.st_ino, st.st_mtime_ns, st.st_size)
    store = _CHUNKS if lru else _JC
    hit = store.get(path)
    if hit and hit[0] == ident:
        if lru:
            store.move_to_end(path)
        return hit[1]
    try:
        with open(path, "rb") as f:
            obj = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    store[path] = (ident, obj)
    if lru:
        while len(store) > 6:
            store.popitem(last=False)
    return obj


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic(path, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


@contextlib.contextmanager
def _mirror_lock(d, name, deadline_at):
    """One refresh at a time across processes. A reader waits here, not on Figma, and past its
    deadline gives up the way a rate limit would."""
    os.makedirs(d, exist_ok=True)
    fd = os.open(os.path.join(d, name), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if deadline_at is not None and time.time() > deadline_at:
                        raise RateLimited(os.path.join(d, name), "another process's mirror refresh outlasted the deadline")
                    time.sleep(0.1)
        else:
            _flock(fd)
        yield
    finally:
        try:
            _funlock(fd)
        except OSError:
            pass
        os.close(fd)


def _batches(ids, size_of, max_bytes=None, max_n=None):
    max_bytes = CHUNK_BATCH_BYTES if max_bytes is None else max_bytes
    max_n = CHUNK_BATCH_IDS if max_n is None else max_n
    out, cur, size = [], [], 0
    for i in ids:
        s = size_of(i)
        if cur and (size + s > max_bytes or len(cur) >= max_n):
            out.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += s
    if cur:
        out.append(cur)
    return out


class _Mirror:
    """One read's view of one file's mirror, held current as of `stamp` (version, last_touched_at).

    On disk, under mirror_root()/<key>/:
      outline.json        the depth=1 tree's pages; the cited pages' sections, each with its kids; and
                          which ids are chunks, each with its own properties (chunk_props), with
                          the stamp it was read at
      index.json          {chunk id: {stamp, bytes, nodes: {id: [parent id, subtree hash]}}}
      shells.json         lineage's pages and sections with their kids, read at one stamp
      chunks/<id>.json    a chunk's REST entry (geometry=paths), its stamp and the call's envelope
      extra/<id>.json     ids the mirror does not hold, as REST answered them, per depth, per edit
      renders/<key>.json  a render URL under _render_key
      edit/<hash>.*       any other Tier 1 read of the file, per edit
    """

    def __init__(self, key, stamp, lane, tool, deadline_at, token):
        self.key, self.stamp = key, tuple(stamp)
        self.lane, self.tool, self.deadline_at, self.token = lane, tool, deadline_at, token
        self.dir = os.path.join(mirror_root(), key)
        self.env = None

    # ---- disk and Figma
    def p(self, *parts):
        return os.path.join(self.dir, *parts)

    def lock(self, name="refresh.lock"):
        return _mirror_lock(self.dir, name, self.deadline_at)

    def fetch(self, path, why, timeout=600):
        r = _request("GET", normalise(path), None, self.lane, self.tool, self.deadline_at, timeout, {}, self.token,
                     None, False, mirror=False, why=why)
        return json.loads(r.data.decode("utf-8"))

    def take_env(self, env):
        """Answer with the envelope of the newest call the answer draws on."""
        if env and (self.env is None or (env.get("_at") or 0) >= (self.env.get("_at") or 0)):
            self.env = env

    def envelope(self):
        if not self.env:
            raise _CannotCompose("no envelope")
        return {k: self.env.get(k) for k in ENV_KEYS}

    def index(self, force=False):
        if force:
            _JC.pop(self.p("index.json"), None)
        return _read_json(self.p("index.json")) or {}

    def n2c(self):
        idx = self.index()
        hit = _N2C.get(self.dir)
        if hit and hit[0] is idx:
            return hit[1]
        m = {}
        for c, e in idx.items():
            for nid in e.get("nodes") or {}:
                o = m.get(nid)
                if o is None or _newer(e["stamp"], idx[o]["stamp"]):
                    m[nid] = c
        _N2C[self.dir] = (idx, m)
        return m

    def current(self, c, idx=None):
        idx = self.index() if idx is None else idx
        return c in idx and _covers(idx[c].get("stamp"), self.stamp)

    def chunk(self, c):
        body = _read_json(self.p("chunks", _safe(c) + ".json"), lru=True)
        if body is None or not _covers(body.get("stamp"), self.stamp):
            raise _CannotCompose(f"chunk {c} is not on disk at this edit")
        if "_nodes" not in body:
            body["_nodes"] = {n["id"]: n for n in _walk(body["entry"]["document"])}
        self.take_env(body.get("env"))
        self.touch(c)
        return body

    def touch(self, c):
        """Note that chunk c was read (at most every 10 minutes per chunk): the hot set a refresh
        brings along. A lost write only means one more call after the next edit."""
        hot = _read_json(self.p("hot.json")) or {}
        if time.time() - float(hot.get(c) or 0) > 600:
            try:
                _write_json(self.p("hot.json"), dict(hot, **{c: time.time()}))
            except OSError:
                pass

    def hot_stale(self, idx, exclude):
        """Stale chunks read in the last HOT_WINDOW, newest first."""
        hot = _read_json(self.p("hot.json")) or {}
        cut = time.time() - HOT_WINDOW
        return [c for c, at in sorted(hot.items(), key=lambda kv: -float(kv[1]))
                if float(at) >= cut and c not in exclude and c in idx and not self.current(c, idx)]

    # ---- the outline: pages, sections, and which ids are chunks
    def outline(self, refresh=False):
        ol = _read_json(self.p("outline.json"))
        if (ol and _covers(ol.get("stamp"), self.stamp)) or not refresh:
            if ol and _covers(ol.get("stamp"), self.stamp):
                self.take_env(ol.get("env"))
            return ol
        with self.lock():
            _JC.pop(self.p("outline.json"), None)
            ol = _read_json(self.p("outline.json"))
            if not (ol and _covers(ol.get("stamp"), self.stamp)):
                ol = self._read_outline()
                _write_json(self.p("outline.json"), ol)
        self.take_env(ol.get("env"))
        return ol

    def _read_outline(self):
        sk = self.fetch(f"files/{self.key}?depth=1", "outline")
        env = dict({k: sk.get(k) for k in ENV_KEYS}, _at=time.time())
        doc = sk.get("document") or {}
        wants = mirror_files().get(self.key) or ()
        pages = [{k: v for k, v in pg.items() if k != "children"} for pg in doc.get("children") or []]
        mirrored = [pg["id"] for pg in pages if _page_wanted(pg.get("name"), wants)]
        shells, chunks, cut = {}, {}, {}
        todo, rounds = [(pid, None, pid) for pid in mirrored], 0
        while todo and rounds < 5:
            rounds += 1
            where = {t[0]: t for t in todo}
            r = self.fetch(f"files/{self.key}/nodes?ids={','.join(where)}&depth={OUTLINE_DEPTH}", "outline")
            todo = []

            def walk(n, parent, page, lvl):
                if n.get("type") in ("CANVAS", "SECTION") and "children" in n:
                    if lvl == OUTLINE_DEPTH:
                        todo.append((n["id"], parent, page))      # cut here: read it again from itself
                        cut[n["id"]] = _own(n)
                        return
                    shells[n["id"]] = {"props": {k: v for k, v in n.items() if k != "children"},
                                       "kids": [c["id"] for c in n["children"]], "parent": parent, "page": page}
                    for c in n["children"]:
                        walk(c, n["id"], page, lvl + 1)
                else:
                    # A chunk's own properties come with this read for free; lineage() takes a
                    # section's kids from here, so it never reads a chunk to list them.
                    chunks[n["id"]] = {"parent": parent, "page": page, "props": _own(n)}

            for nid, (_, parent, page) in where.items():
                e = (r.get("nodes") or {}).get(nid)
                if e:
                    walk(e["document"], parent, page, 0)
        for nid, parent, page in todo:                    # nested past the rounds: read whole
            chunks[nid] = {"parent": parent, "page": page, "props": cut.get(nid)}
        return {"stamp": list(self.stamp), "env": env, "doc": {k: v for k, v in doc.items() if k != "children"},
                "pages": pages, "mirrored": mirrored, "shells": shells, "chunks": chunks, "chunk_props": True}

    def chunks_under(self, sid, depth, ol):
        if depth == 0:
            return []
        out = []
        for k in ol["shells"][sid]["kids"]:
            if k in ol["shells"]:
                out += self.chunks_under(k, None if depth is None else depth - 1, ol)
            else:
                out.append(k)
        return out

    # ---- refresh
    def refresh(self, cids, why="refresh"):
        """Re-read the chunks that are older than this read's stamp, one batched call, under the lock.
        Another process that refreshed them while this one waited leaves nothing to do. The call also
        brings the other stale chunks read in the last HOT_WINDOW, as far as CHUNK_BATCH_BYTES goes,
        so the next reads after an edit (the rest of a page's bands) find them current."""
        with self.lock():
            idx = dict(self.index(force=True))
            todo = [c for c in dict.fromkeys(cids) if not self.current(c, idx)]
            if not todo:
                return

            def size(c):
                return (idx.get(c) or {}).get("bytes") or 2e6

            room = CHUNK_BATCH_BYTES - sum(size(c) for c in todo)
            for c in self.hot_stale(idx, set(todo)):
                if len(todo) >= CHUNK_BATCH_IDS or size(c) > room:
                    break
                todo.append(c)
                room -= size(c)
            ol = _read_json(self.p("outline.json")) or {}
            self._fetch_chunks(todo, idx, ol, why, size)
            _write_json(self.p("index.json"), idx)

    def _fetch_chunks(self, ids, idx, ol, why, size_of):
        for batch in _batches(ids, size_of):
            try:
                r = self.fetch(f"files/{self.key}/nodes?ids={','.join(batch)}&geometry=paths", why)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    raise
                if len(batch) == 1:
                    raise _CannotCompose(f"Figma answered {e.code} for chunk {batch[0]}")
                h = len(batch) // 2
                self._fetch_chunks(batch[:h], idx, ol, why, size_of)
                self._fetch_chunks(batch[h:], idx, ol, why, size_of)
                continue
            env = dict({k: r.get(k) for k in ENV_KEYS}, _at=time.time())
            for c in batch:
                e = (r.get("nodes") or {}).get(c)
                if e and e.get("document"):
                    parent = ((ol.get("chunks") or {}).get(c) or (ol.get("shells") or {}).get(c) or {}).get("parent")
                    self._store(e["document"], e, env, idx, ol, parent)
                else:
                    self._drop(c, idx)

    def _store(self, node, entry, env, idx, ol, parent):
        if node["id"] in (ol.get("shells") or {}) and node.get("children") is not None:
            for c in node["children"]:          # a section read whole (sync): file each chunk in it
                self._store(c, entry, env, idx, ol, node["id"])
            return
        # The chunk keeps the call's map entries for what its tree uses, as Figma wrote them (a set's
        # form depends on the answer's tree; _maps picks it per answer).
        pc, ps, pst = entry.get("components") or {}, entry.get("componentSets") or {}, entry.get("styles") or {}
        ck, sk, stk = _refs(node)
        comps = {k: pc[k] for k in ck if k in pc}
        sk |= {v["componentSetId"] for v in comps.values() if v.get("componentSetId")}
        sets, styles = {k: ps[k] for k in sk if k in ps}, {k: pst[k] for k in stk if k in pst}
        hashes = {}
        _hash_tree(node, parent, hashes)
        body = {"stamp": list(self.stamp), "env": env,
                "entry": {"document": node, "components": comps, "componentSets": sets, "styles": styles,
                          "schemaVersion": entry.get("schemaVersion", 0)}}
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        path = self.p("chunks", _safe(node["id"]) + ".json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic(path, data)
        idx[node["id"]] = {"stamp": list(self.stamp), "bytes": len(data), "nodes": hashes}

    def _drop(self, c, idx):
        idx.pop(c, None)
        try:
            os.remove(self.p("chunks", _safe(c) + ".json"))
        except OSError:
            pass

    # ---- where an id is
    def plan(self, ids, depth, shells=True):
        """Make the parts these ids need current; return {id: ('chunk', cid) | ('shell', sid)} for the
        ids the mirror holds at this edit. An id left out is asked of Figma (extra)."""
        n2c = self.n2c()
        ol = _read_json(self.p("outline.json"))
        if shells and ol and any(i in ol["shells"] for i in ids):
            ol = self.outline(refresh=True)
        ol_now = ol if ol and _covers(ol.get("stamp"), self.stamp) else None
        want = []
        for i in ids:
            if shells and ol_now and i in ol_now["shells"]:
                want += self.chunks_under(i, depth, ol_now)
            elif i in n2c:
                want.append(n2c[i])
            elif ol_now and i in ol_now["chunks"]:
                want.append(i)          # a chunk the outline lists and no read has filled yet
        idx = self.index()
        stale = [c for c in dict.fromkeys(want) if not self.current(c, idx)]
        if stale:
            self.refresh(stale)
            idx, n2c = self.index(), self.n2c()
        out = {}
        for i in ids:
            if shells and ol_now and i in ol_now["shells"]:
                out[i] = ("shell", i)
            else:
                c = n2c.get(i)
                if c is not None and self.current(c, idx):
                    out[i] = ("chunk", c)
        return out

    # ---- building REST's answers
    def tree(self, nid, loc, depth, geometry, ol, used):
        kind, ref = loc
        if kind == "chunk":
            body = self.chunk(ref)
            used.append(body)
            if nid not in body["_nodes"]:
                raise _CannotCompose(f"{nid} is not in chunk {ref}")
            return _prune(body["_nodes"][nid], depth, geometry)
        if geometry:
            raise _CannotCompose("the outline is read without geometry")
        sh = ol["shells"][nid]
        out = dict(sh["props"])
        if depth == 0:
            out["children"] = []
            return out
        kids = []
        for k in sh["kids"]:
            sub = ("shell", k) if k in ol["shells"] else ("chunk", k)
            kids.append(self.tree(k, sub, None if depth is None else depth - 1, geometry, ol, used))
        out["children"] = kids
        return out

    @staticmethod
    def pool(used):
        """The maps of the chunks an answer draws on; a set seen in both forms keeps both fields."""
        pc, ps, pst = {}, {}, {}
        for b in used:
            e = b["entry"]
            pc.update(e.get("components") or {})
            for k, v in (e.get("componentSets") or {}).items():
                ps.setdefault(k, {}).update(v)
            pst.update(e.get("styles") or {})
        return pc, ps, pst

    def nodes(self, ids, depth, geometry):
        ids = list(dict.fromkeys(ids))
        locs = self.plan(ids, depth)
        ol = self.outline() if any(v[0] == "shell" for v in locs.values()) else None
        res, missing = {}, []
        for i in ids:
            if i not in locs:
                missing.append(i)
                continue
            used = []
            t = self.tree(i, locs[i], depth, geometry, ol, used)
            comps, sets, styles = _maps(t, self.pool(used))
            res[i] = {"document": t, "components": comps, "componentSets": sets, "styles": styles,
                      "schemaVersion": (used[0]["entry"].get("schemaVersion", 0) if used else 0)}
        if missing:
            res.update(self.extra(missing, depth, geometry))
        return dict(self.envelope(), nodes={i: res[i] for i in ids})

    def extra(self, ids, depth, geometry):
        """Ids the mirror does not hold, as Figma answers them: one batched call for those not yet
        asked at this edit, kept until the next. Figma's null is passed on as Figma's; the mirror
        itself never calls an id dead."""
        dk = "full" if depth is None else str(depth)

        def cached(i):
            e = _read_json(self.p("extra", _safe(i) + ".json")) or {}
            for k in ("full", dk):
                x = e.get(k)
                if x and _covers(x.get("stamp"), self.stamp):
                    return x
            return None

        got = {i: cached(i) for i in ids}
        if any(v is None for v in got.values()):
            with self.lock():
                need = [i for i in ids if got[i] is None and cached(i) is None]
                got.update({i: cached(i) for i in ids if got[i] is None and i not in need})
                if need:
                    r = self.fetch(f"files/{self.key}/nodes?ids={','.join(need)}&geometry=paths"
                                   + ("" if depth is None else f"&depth={depth}"), "missing")
                    env = dict({k: r.get(k) for k in ENV_KEYS}, _at=time.time())
                    for i in need:
                        x = {"stamp": list(self.stamp), "depth": depth, "env": env,
                             "entry": (r.get("nodes") or {}).get(i)}
                        path = self.p("extra", _safe(i) + ".json")
                        e = dict(_read_json(path) or {})
                        e[dk] = x
                        _write_json(path, e)
                        got[i] = x
        out = {}
        for i in ids:
            x = got[i]
            self.take_env(x.get("env"))
            ent = x.get("entry")
            if not ent:
                out[i] = ent
            elif x.get("depth") == depth:
                out[i] = dict(ent, document=_prune(ent["document"], None, geometry))
            else:
                t = _prune(ent["document"], depth, geometry)
                comps, sets, styles = _maps(t, (ent.get("components") or {}, ent.get("componentSets") or {},
                                                ent.get("styles") or {}))
                out[i] = {"document": t, "components": comps, "componentSets": sets, "styles": styles,
                          "schemaVersion": ent.get("schemaVersion", 0)}
        return out

    # ---- lineage: each id's ancestors, page first
    def lineage(self, ids, kids):
        """({id: [entry, ...]} for each id the mirror holds at this edit, {page or section id: {props,
        kids} or None} current at this edit for the ones its chains cross). An id the mirror does not
        hold, or whose chain it cannot show current, is left out, and the caller asks Figma.

        The outline gives the chain above a chunk's root as it was last read; each hop is then checked
        against the pages' and sections' current kids (current_shells), which also give their own
        properties, their kids' and a chunk root's. Below a chunk's root: the index's parent pointers,
        each hop checked against the chunk body plan() made current, and that body's properties. A hop
        that does not hold (a frame moved to another section) raises for that id, never answers."""
        ol = self.outline()
        if not ol:
            return {}, {}
        shells, chunks = ol["shells"], ol["chunks"]
        inner = [i for i in ids if i not in shells and i not in chunks]
        locs = self.plan(inner, 0, shells=False) if inner else {}
        idx = self.index() if locs else {}
        ups = {}
        for i in ids:
            try:
                if i in shells or i in chunks:
                    ups[i] = (None, self.outline_chain(i, ol))
                elif i in locs:
                    c = locs[i][1]
                    body = self.chunk(c)
                    ups[i] = (body, self.outline_chain(c, ol)[:-1] + self.chunk_chain(i, c, body, idx))
            except _CannotCompose as e:
                self.miss(i, e)
        sids = list(dict.fromkeys(x for _, up in ups.values() for x in up if x in shells))
        now = self.current_shells(sids, ol) if sids else {}
        out = {}
        for i, (body, up) in ups.items():
            try:
                out[i] = self.entries(up, now, body, kids)
            except _CannotCompose as e:
                self.miss(i, e)
        return out, now

    def miss(self, i, e):
        _record(self.tool, self.lane, "LINEAGE", normalise(f"files/{self.key}?ids={i}"), 1, "mirror-miss", 0,
                extra={"mirror": "fallback", "why": str(e)[:200]})

    def current_shells(self, sids, ol):
        """{id: {"props", "kids"} or None (gone)} for these pages and sections, current at this read's
        stamp: from the outline when it is current, else from one nodes?ids=<them>&depth=1 read per
        edit for the ones not yet read since it (shells.json), one process at a time."""
        if _covers(ol.get("stamp"), self.stamp) and ol.get("chunk_props"):
            try:
                return {s: {"props": dict(ol["shells"][s]["props"]),
                            "kids": [_outline_props(x, ol) for x in ol["shells"][s]["kids"]]} for s in sids}
            except _CannotCompose:
                pass
        path = self.p("shells.json")

        def held():
            d = _read_json(path) or {}
            return d if _covers(d.get("stamp"), self.stamp) else {"stamp": list(self.stamp), "shells": {}}
        d = held()
        if any(s not in d["shells"] for s in sids):
            with self.lock("shells.lock"):
                _JC.pop(path, None)
                d = dict(held())
                need = [s for s in sids if s not in d["shells"]]
                if need:
                    r = self.fetch(f"files/{self.key}/nodes?ids={','.join(need)}&depth=1", "lineage")
                    d["shells"] = dict(d["shells"])
                    for s in need:
                        doc = ((r.get("nodes") or {}).get(s) or {}).get("document")
                        d["shells"][s] = None if not doc else {"props": _own(doc),
                                                               "kids": [_own(c) for c in doc.get("children") or []]}
                    _write_json(path, d)
        return {s: d["shells"].get(s) for s in sids}

    @staticmethod
    def outline_chain(i, ol):
        """[page, ..., i] for a section or a chunk's root, each hop checked against its parent's kids."""
        shells, chunks = ol["shells"], ol["chunks"]
        chain = [i]
        while True:
            cur = chain[-1]
            p = (shells.get(cur) or chunks.get(cur) or {}).get("parent")
            if p is None:
                break
            if p in chain or p not in shells or cur not in shells[p]["kids"]:
                raise _CannotCompose(f"the outline's parent for {cur} disagrees with its kids")
            chain.append(p)
        if chain[-1] not in (ol.get("mirrored") or ()):
            raise _CannotCompose(f"{i} does not reach a mirrored page in the outline")
        return chain[::-1]

    @staticmethod
    def chunk_chain(i, c, body, idx):
        """[c, ..., i] inside chunk c, from the index's parent pointers. Each hop is checked against
        the chunk body: a pointer the body does not bear out raises, so it never answers."""
        nodes, have = ((idx.get(c) or {}).get("nodes") or {}), body["_nodes"]
        if i not in have:
            raise _CannotCompose(f"{i} is not in chunk {c}")
        chain = [i]
        while chain[-1] != c:
            cur = chain[-1]
            p = (nodes.get(cur) or [None])[0]
            if p is None or p in chain or p not in have or cur not in [k.get("id") for k in have[p].get("children") or []]:
                raise _CannotCompose(f"the index's parent pointer for {cur} in chunk {c} disagrees with the chunk")
            chain.append(p)
        return chain[::-1]

    @staticmethod
    def entries(up, now, body, kids):
        """lineage's entries for the ids in `up`, page first: a page's or section's from `now`
        (current_shells), each hop below it checked against its current kids; a node's inside a chunk
        from the chunk body; a chunk's root as the last entry from its parent's current kids."""
        nodes = body["_nodes"] if body else {}
        out = []
        for k, nid in enumerate(up):
            more = kids and k < len(up) - 1
            if nid in now:
                sh = now[nid]
                if sh is None:
                    raise _CannotCompose(f"{nid} is no longer in the file")
                below = [x.get("id") for x in sh["kids"]]
                if k + 1 < len(up) and up[k + 1] not in below:
                    raise _CannotCompose(f"{up[k + 1]} is no longer a child of {nid}")
                e = dict(sh["props"])
                if more:
                    e["kids"] = [dict(x) for x in sh["kids"]]
            elif nid in nodes:
                n = nodes[nid]
                e = _own(n)
                if more:
                    e["kids"] = [_own(x) for x in n.get("children") or []]
            elif k == len(up) - 1 and k > 0 and up[k - 1] in now:
                e = dict(next(x for x in now[up[k - 1]]["kids"] if x.get("id") == nid))
            else:
                raise _CannotCompose(f"{nid} is on a lineage the mirror cannot fill")
            out.append(e)
        return out

    def renders(self, ids, qd):
        ids = list(dict.fromkeys(ids))
        params = {k: v for k, v in qd.items() if k not in ("ids", "version")}
        locs = self.plan(ids, None, shells=False)
        idx = self.index()
        hashes = {i: idx[c]["nodes"][i][1] for i, (_, c) in locs.items() if i in idx[c]["nodes"]}
        keys = {i: _render_key(i, hashes.get(i), self.stamp, params) for i in ids}

        def cached(i):
            e = _read_json(self.p("renders", keys[i] + ".json"))
            return e if e and e.get("url") and time.time() - float(e.get("at") or 0) < RENDER_TTL else None

        got = {i: cached(i) for i in ids}
        env = next((g["env"] for g in got.values() if g), None)
        if any(v is None for v in got.values()):
            with self.lock("render.lock"):
                miss = [i for i in ids if got[i] is None and cached(i) is None]
                got.update({i: cached(i) for i in ids if got[i] is None and i not in miss})
                if miss:
                    q = urllib.parse.urlencode(dict(params, ids=",".join(miss)), safe=":,;")
                    r = self.fetch(f"images/{self.key}?{q}", "render")
                    env = {k: v for k, v in r.items() if k != "images"}
                    if r.get("err"):
                        raise _CannotCompose(f"render error {r.get('err')}")
                    for i in miss:
                        u = (r.get("images") or {}).get(i)
                        got[i] = {"url": u, "at": time.time(), "env": env}
                        if u:
                            _write_json(self.p("renders", keys[i] + ".json"), got[i])
        return dict(env or {"err": None}, images={i: (got[i] or {}).get("url") for i in ids})

    def edit_cached(self, p):
        """Any other Tier 1 read of the file: asked of Figma once per edit, kept until the next."""
        h = hashlib.sha256(canonical(p).encode("utf-8")).hexdigest()[:40]
        meta_p, body_p = self.p("edit", h + ".json"), self.p("edit", h + ".body")

        def cached():
            m = _read_json(meta_p)
            if m and _covers(m.get("stamp"), self.stamp):
                try:
                    with open(body_p, "rb") as f:
                        return f.read()
                except OSError:
                    return None
            return None

        hit = cached()
        if hit is not None:
            return hit
        with self.lock("edit.lock"):
            _JC.pop(meta_p, None)
            hit = cached()
            if hit is not None:
                return hit
            r = _request("GET", p, None, self.lane, self.tool, self.deadline_at, 600, {}, self.token, None, False,
                         mirror=False, why="edit")
            os.makedirs(os.path.dirname(body_p), exist_ok=True)
            _atomic(body_p, r.data)
            _write_json(meta_p, {"stamp": list(self.stamp), "path": endpoint("GET", p), "at": time.time()})
            return r.data


def _mirror_shape(key, path, qd):
    """'nodes' and 'render' are built from the mirror; 'edit' is any other Tier 1 read of the file,
    asked of Figma once per edit. GET files/KEY (?ids=, ?depth=) is 'edit' on purpose: its maps
    also list components a prototype link points at, and its set entries take the other form, and
    both rules were seen in only a handful of live answers, too few to build from. A ?version= read
    keeps the versioned cache: even the newest version's envelope differs from a live read's."""
    if "version" in qd:
        return None
    ids = [i for i in (qd.get("ids") or "").split(",") if i]
    if "depth" in qd and not (qd["depth"].isdigit() and int(qd["depth"]) > 0):
        return "edit"
    if "geometry" in qd and qd["geometry"] != "paths":
        return "edit"
    if path == f"/v1/files/{key}/nodes":
        return "nodes" if ids and set(qd) <= {"ids", "depth", "geometry"} else "edit"
    if path == f"/v1/files/{key}":
        return "edit"
    if path == f"/v1/images/{key}":
        return "render" if ids and set(qd) <= RENDER_PARAMS else "edit"
    return None


def _mirror_get(p, lane, tool, deadline_at, token):
    """The body for a read of a mirrored file, current as of its last edit, or None to go to Figma
    as before (a read the mirror does not cover: comments, versions, variables, /meta itself, any
    ?version=). /meta failing raises here, so nothing stale is served."""
    key = _file_key(p)
    path, _, q = p.partition("?")
    qd = dict(urllib.parse.parse_qsl(q, keep_blank_values=True))
    shape = _mirror_shape(key, path, qd)
    if shape is None:
        return None
    stamp = _file_stamp(key, lane, tool, deadline_at, token=token)
    m = _Mirror(key, stamp, lane, tool, deadline_at, token)
    ids = [i for i in (qd.get("ids") or "").split(",") if i]
    geometry = qd.get("geometry") == "paths"
    try:
        try:
            if shape == "nodes":
                return json.dumps(m.nodes(ids, int(qd["depth"]) if "depth" in qd else None, geometry)).encode("utf-8")
            if shape == "render":
                return json.dumps(m.renders(ids, qd)).encode("utf-8")
        except _CannotCompose as e:
            _record(tool, lane, "GET", p, tier(p), "mirror-miss", 0, extra={"mirror": "fallback", "why": str(e)[:200]})
        return m.edit_cached(p)
    except (urllib.error.URLError, http.client.HTTPException, NoToken, RateLimited):
        raise
    except Exception as e:  # noqa: BLE001 -- a fault in the mirror itself: read Figma live, never stale
        _record(tool, lane, "GET", p, tier(p), "mirror-error", 0, extra={"mirror": "error", "why": f"{type(e).__name__}: {e}"[:200]})
        return None


def _mirror_key(key):
    if key:
        return key
    files = mirror_files()
    if not files:
        raise SystemExit("figma_client: no mirror configured. Set figma.mirror in kit_config "
                         "({\"<file key>\": [\"<page name>\", ...]}) or pass --key.")
    return next(iter(files))


def mirror_sync(key=None, lane="batch", tool="figma-mirror-sync", deadline=None):
    """Fill the mirror: the outline, then every chunk older than the last edit, read section by
    section (a section whole when it fits CHUNK_BATCH_BYTES, else its chunks). Returns a summary."""
    key = _mirror_key(key)
    lane = resolve_lane(lane)
    deadline_at = _deadline_at(lane, deadline)
    stamp = _file_stamp(key, lane, tool, deadline_at)
    m = _Mirror(key, stamp, lane, tool, deadline_at, None)
    mark = log_mark()
    ol = m.outline(refresh=True)
    idx = m.index()
    units = []
    for pid in ol["mirrored"]:
        for k in ol["shells"][pid]["kids"]:
            under = m.chunks_under(k, None, ol) if k in ol["shells"] else [k]
            if any(not m.current(c, idx) for c in under):
                units.append(k)

    def size_of(u):
        under = m.chunks_under(u, None, ol) if u in ol["shells"] else [u]
        return sum((idx.get(c) or {}).get("bytes") or 8e6 for c in under)

    for batch in _batches(units, size_of):
        with m.lock():
            cur = dict(m.index(force=True))
            try:
                m._fetch_chunks(batch, cur, ol, "sync", size_of)
            except _CannotCompose:
                for u in batch:     # one section too big to read whole: read its chunks instead
                    under = m.chunks_under(u, None, ol) if u in ol["shells"] else [u]
                    m._fetch_chunks([c for c in under if not m.current(c, cur)], cur, ol, "sync",
                                    lambda c: (cur.get(c) or {}).get("bytes") or 2e6)
            _write_json(m.p("index.json"), cur)
    idx = m.index(force=True)
    want = [c for pid in ol["mirrored"] for c in m.chunks_under(pid, None, ol)]
    calls = [r for r in calls_since(mark, _tool(tool)) if r.get("tier") == 1 and not r.get("cache_hit")
             and r.get("pid") == os.getpid()]
    return {"key": key, "stamp": list(stamp), "pages": [pg["name"] for pg in ol["pages"] if pg["id"] in ol["mirrored"]],
            "page_ids": ol["mirrored"], "sections": len(ol["shells"]) - len(ol["mirrored"]), "chunks": len(want),
            "current": sum(1 for c in want if m.current(c, idx)),
            "nodes": sum(len(idx[c]["nodes"]) for c in want if c in idx),
            "bytes": sum(idx[c]["bytes"] for c in want if c in idx), "t1_calls": len(calls)}


def mirror_status(key=None):
    key = _mirror_key(key)
    d = os.path.join(mirror_root(), key)
    ol = _read_json(os.path.join(d, "outline.json")) or {}
    idx = _read_json(os.path.join(d, "index.json")) or {}
    with locked() as st:
        memo = (st.get("current") or {}).get(key)
    cur = (memo[0], memo[2]) if memo and len(memo) > 2 else None
    print(f"mirror: {d}")
    print(f"outline stamp {ol.get('stamp')}; pages {[pg['name'] for pg in ol.get('pages', []) if pg['id'] in ol.get('mirrored', [])]}")
    print(f"last /meta {list(cur) if cur else None}")
    print(f"chunks {len(idx)}, nodes {sum(len(e['nodes']) for e in idx.values())}, "
          f"{sum(e['bytes'] for e in idx.values()) / 1e6:.1f} MB; current at the last /meta: "
          f"{sum(1 for e in idx.values() if cur and _covers(e['stamp'], cur))}")


# ---------------------------------------------------------------- the request

class Response:
    def __init__(self, status, headers, data, cache_hit=False):
        self.status, self.headers, self.data, self.cache_hit = status, headers, data, cache_hit

    def json(self):
        return json.loads(self.data.decode("utf-8")) if self.data else None


_AUTH = None


def _auth():
    """figma_auth.py beside this file, loaded by its path under a name of its own. A plain
    `import figma_auth` lets a caller whose sys.path or sys.modules holds another figma_auth.py (a
    test folder's copy, an older checkout's) hand this client that resolver instead."""
    global _AUTH
    if _AUTH is None:
        spec = importlib.util.spec_from_file_location("figma_client_auth", os.path.join(HERE, "figma_auth.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _AUTH = mod
    return _AUTH


def _token(token):
    if token:
        return token
    tok = _auth().token(required=False)
    if not tok:
        raise NoToken("no Figma token (FIGMA_TOKEN / FIGMA_PAT / FIGMA_ACCESS_TOKEN or the macOS login "
                      "Keychain); `python figma_auth.py --source` says where it looked")
    return tok


def _deadline_at(lane, deadline):
    d = deadline if deadline is not None else _env_float("FIGMA_CLIENT_DEADLINE")
    if d is None:
        d = DEADLINE[lane]
    ends = [x for x in (time.time() + d if d is not None else None, _env_float("FIGMA_CLIENT_DEADLINE_AT"))
            if x is not None]
    return min(ends) if ends else None


def _request(method, p, body, lane, tool, deadline_at, timeout, headers, token, max_age, version_cache,
             mirror=True, why=None):
    t = tier(p)
    url = base() + p
    why_log = {"mirror": why} if why else None
    if method == "GET" and version_cache and _vcache_eligible(method, p):
        hit = _vcache_get(p, lane, tool, deadline_at)
        if hit is not None:
            _record(tool, lane, method, p, t, 200, 0, len(hit), cache_hit=True)
            return Response(200, {}, hit, True)
    if mirror and method == "GET" and _mirror_on(_file_key(p)):
        t0 = time.time()
        hit = _mirror_get(p, lane, tool, deadline_at, token)
        if hit is not None:
            _record(tool, lane, method, p, t, 200, (time.time() - t0) * 1000, len(hit), cache_hit=True,
                    extra={"mirror": "served"})
            return Response(200, {}, hit, True)
    if max_age is not None and _live_eligible(method, p):
        hit = _live_get(p, max_age)
        if hit is not None:
            _record(tool, lane, method, p, t, 200, 0, len(hit), cache_hit=True)
            return Response(200, {}, hit, True)
    # A call that never reaches Figma is logged too: with no token, or when the lane's budget had no
    # token for it before the deadline. Otherwise the log could not show a give-up, and a quiet log
    # would read as "every call answered".
    try:
        tok = _token(token)
    except NoToken:
        _record(tool, lane, method, p, t, "no-token", 0)
        raise
    data = None
    hdrs = {"X-Figma-Token": tok}
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    streak = soft = 0
    while True:
        t_wait = time.time()
        try:
            waited = _acquire(t, lane, deadline_at, url)
        except RateLimited:
            _record(tool, lane, method, p, t, "gave-up", 0, wait=(time.time() - t_wait, 0.0), extra=why_log)
            raise
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = r.read()
                status, rh = r.status, {k.lower(): v for k, v in r.headers.items()}
            _record(tool, lane, method, p, t, status, (time.time() - t0) * 1000, len(out), wait=waited, extra=why_log)
            if method == "GET" and version_cache and _vcache_eligible(method, p):
                _store("versioned", p, out)
            elif max_age is not None and _live_eligible(method, p):
                _store("live", p, out)
            return Response(status, rh, out)
        except urllib.error.HTTPError as e:
            out = e.read() or b""
            ms = (time.time() - t0) * 1000
            if e.code == 429:
                ra = _retry_after(e.headers, streak)
                streak += 1
                _record(tool, lane, method, p, t, 429, ms, len(out), retry_after=ra, wait=waited,
                        extra=dict(_rate_headers(e.headers), **(why_log or {})))
                _pause(t, ra)
                if deadline_at is not None and time.time() + ra > deadline_at:
                    _record(tool, lane, method, p, t, "gave-up", 0, retry_after=ra, extra=why_log)
                    raise RateLimited(url, f"429 from Figma, Retry-After {ra:g}s, past the deadline")
                continue
            _record(tool, lane, method, p, t, e.code, ms, len(out), wait=waited, extra=why_log)
            if method == "GET" and e.code in (500, 502, 503, 504) and soft < len(RETRY_5XX):
                if deadline_at is None or time.time() + RETRY_5XX[soft] <= deadline_at:
                    time.sleep(RETRY_5XX[soft])
                    soft += 1
                    continue
            raise urllib.error.HTTPError(url, e.code, e.msg, e.headers, io.BytesIO(out))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            _record(tool, lane, method, p, t, "net:" + type(e).__name__, (time.time() - t0) * 1000, wait=waited,
                    extra=why_log)
            if (method == "GET" and soft < len(RETRY_5XX)
                    and (deadline_at is None or time.time() + RETRY_5XX[soft] <= deadline_at)):
                time.sleep(RETRY_5XX[soft])
                soft += 1
                continue
            raise


def request(method, path, body=None, *, lane=None, default_lane="interactive", tool=None, deadline=None,
            timeout=120, headers=None, token=None, max_age=None, version_cache=True):
    """One paced Figma REST call. Returns a Response (status, headers, data, cache_hit)."""
    lane = resolve_lane(lane, default_lane)
    if max_age is None:
        max_age = _env_float("FIGMA_CLIENT_MAX_AGE")
    return _request(method.upper(), normalise(path), body, lane, tool, _deadline_at(lane, deadline), timeout,
                    headers, token, max_age, version_cache)


def get(path, **kw):
    """GET, parsed JSON."""
    return request("GET", path, **kw).json()


def get_or_exit(path, **kw):
    """get(), for a tool whose convention is to stop with DID NOT RUN when Figma cannot answer: a
    rate limit past the deadline, a 5xx after its retries or no network ends the run that way, and a
    4xx (a bad id, a rejected token) is raised as the HTTPError it is."""
    return _or_exit(lambda: get(path, **kw), endpoint("GET", normalise(path)))


def _or_exit(call, what):
    try:
        return call()
    except RateLimited as e:
        raise SystemExit(f"DID NOT RUN: Figma's rate limit outlasted the deadline ({e.msg})")
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            raise SystemExit(f"DID NOT RUN: Figma answered {e.code} after retries on {what}")
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise SystemExit(f"DID NOT RUN: Figma did not answer ({type(e).__name__}: {getattr(e, 'reason', e)})")


def get_bytes(path, **kw):
    return request("GET", path, **kw).data


def post(path, body, **kw):
    return request("POST", path, body, **kw).json()


def delete(path, **kw):
    return request("DELETE", path, **kw).json()


def prefetch_nodes(key, ids, query="", batch=8, **kw):
    """Read many nodes in ids= batches and file each one where a single-node read with max_age
    finds it (`files/KEY/nodes?ids=<one>&<query>`). Returns the number of calls made. For a job
    that would otherwise read one band at a time: 42 bands are 6 calls, not 42."""
    ids = list(dict.fromkeys(ids))
    kw.setdefault("default_lane", "batch")
    _prune_live()
    calls = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        q = f"files/{key}/nodes?ids={urllib.parse.quote(','.join(chunk), safe=':,;')}" + (f"&{query}" if query else "")
        resp = get(q, **kw)
        calls += 1
        for nid in chunk:
            one = {k: v for k, v in resp.items() if k != "nodes"}
            one["nodes"] = {nid: (resp.get("nodes") or {}).get(nid)}
            _store("live", normalise(f"files/{key}/nodes?ids={nid}" + (f"&{query}" if query else "")),
                   json.dumps(one).encode("utf-8"))
    return calls


# ---------------------------------------------------------------- lineage

LINEAGE_BATCH = 40      # ids per files?ids= / nodes?ids= read when Figma is asked


def lineage(key, ids, kids=True, version=None, **kw):
    """{id: [entry, ...] or None}: where each node sits, from its page down to the node itself.

    A structural answer, not a REST lookalike: no envelope, no components/componentSets/styles maps
    (a caller that needs those reads them itself), no DOCUMENT root. Each entry is the node's own
    properties without children and without geometry=paths fields. With kids, every entry but the
    last also has "kids": its children's properties without their children, in paint order (bottom
    first). None for an id Figma does not have. kw are get()'s keywords (lane, default_lane, tool,
    deadline, timeout, token, ...).

    For a mirrored file and no version, it is built from the mirror, current at the last edit (the
    module docstring, Lineage): no Figma call when nothing changed, and when something did, one
    nodes?ids=<the pages and sections crossed>&depth=1 read per edit plus the stale chunks the ids
    sit in. An id the mirror does not hold, a file it does not cover, or a version= read is asked of
    Figma through get(): files?ids=<ids> and, with kids, nodes?ids=<their ancestors>&depth=1 (a page
    or section already read current at this edit gives its kids from there). /meta failing raises,
    as get() does: nothing stale is ever answered."""
    ids = [str(i) for i in dict.fromkeys(ids) if i]
    lane = resolve_lane(kw.get("lane"), kw.get("default_lane", "interactive"))
    tool, token = kw.get("tool"), kw.get("token")
    deadline_at = _deadline_at(lane, kw.get("deadline"))
    got, now = {}, {}
    if ids and version is None and _mirror_on(key):
        t0 = time.time()
        stamp = _file_stamp(key, lane, tool, deadline_at, token=token)
        m = _Mirror(key, stamp, lane, tool, deadline_at, token)
        try:
            got, now = m.lineage(ids, kids)
        except (urllib.error.URLError, http.client.HTTPException, NoToken, RateLimited):
            raise
        except Exception as e:  # noqa: BLE001 -- a fault in the mirror itself: ask Figma, never answer stale
            _record(tool, lane, "LINEAGE", normalise(f"files/{key}?ids={','.join(ids)}"), 1, "mirror-error", 0,
                    extra={"mirror": "error", "why": f"{type(e).__name__}: {e}"[:200]})
            got, now = {}, {}
        if got:
            _record(tool, lane, "LINEAGE", normalise(f"files/{key}?ids={','.join(got)}"), 1, 200,
                    (time.time() - t0) * 1000, len(json.dumps(got)), cache_hit=True, extra={"mirror": "served"})
    rest = [i for i in ids if i not in got]
    if rest:
        got.update(_lineage_rest(key, rest, kids, version, now, kw))
    return {i: got.get(i) for i in ids}


def lineage_or_exit(key, ids, **kw):
    """lineage(), stopping with DID NOT RUN where get_or_exit() would."""
    return _or_exit(lambda: lineage(key, ids, **kw), f"the lineage of {len(list(ids))} node(s) in {key}")


def _lineage_rest(key, ids, kids, version, now, kw):
    """Figma's own answer for ids the mirror does not give: files?ids= has the path from the root to
    each id (each ancestor trimmed to the path), and nodes?ids=<ancestors>&depth=1 each ancestor's
    children. A page or section in `now` (current at this edit, from _Mirror.lineage) gives its kids
    from there instead; a version= read never takes them."""
    vq = f"&version={urllib.parse.quote(str(version), safe='')}" if version else ""
    paths = {}
    for k in range(0, len(ids), LINEAGE_BATCH):
        part = ids[k:k + LINEAGE_BATCH]
        want = set(part)
        doc = (get(f"files/{key}?ids={urllib.parse.quote(','.join(part), safe=':,;')}{vq}", **kw) or {}).get("document") or {}
        stack = [(c, []) for c in reversed(doc.get("children") or [])]
        while stack:
            n, up = stack.pop()
            path = up + [n]
            if n.get("id") in want and n["id"] not in paths:
                paths[n["id"]] = path
            stack.extend((c, path) for c in reversed(n.get("children") or []))
    kid_map = {}
    if kids:
        need = list(dict.fromkeys(n["id"] for p in paths.values() for n in p[:-1]))
        for a in need:
            if not version and (now or {}).get(a):
                kid_map[a] = [dict(x) for x in now[a]["kids"]]
        ask = [a for a in need if a not in kid_map]
        for k in range(0, len(ask), LINEAGE_BATCH):
            part = ask[k:k + LINEAGE_BATCH]
            got = (get(f"files/{key}/nodes?ids={urllib.parse.quote(','.join(part), safe=':,;')}&depth=1{vq}", **kw)
                   or {}).get("nodes") or {}
            for a in part:
                d = (got.get(a) or {}).get("document")
                if d is None:
                    raise urllib.error.URLError(f"figma_client.lineage: {a} left {key} between two reads; ask again")
                kid_map[a] = [_own(c) for c in d.get("children") or []]
    out = {}
    for i in ids:
        p = paths.get(i)
        out[i] = None if not p else [dict(_own(n), **({"kids": kid_map[n["id"]]} if kids and j < len(p) - 1 else {}))
                                      for j, n in enumerate(p)]
    return out


def lineage_in_tree(tree, ids):
    """lineage()'s answer read off a tree that holds every child of each node on the way down: a
    whole-file read, a section's /nodes entry, a fixture. Not off a files?ids= answer, which trims
    each ancestor's children to the path. A DOCUMENT root is left out, as lineage() leaves it."""
    out = {i: None for i in ids}
    stack = [(tree, [])]
    while stack:
        n, up = stack.pop()
        path = up if n.get("type") == "DOCUMENT" else up + [n]
        if n.get("id") in out and out[n["id"]] is None and path:
            out[n["id"]] = [dict(_own(x), **({"kids": [_own(c) for c in x.get("children") or []]}
                                              if j < len(path) - 1 else {})) for j, x in enumerate(path)]
        stack.extend((c, path) for c in reversed(n.get("children") or []))
    return out


def drawn_over(chain, box):
    """[{id, name, type, on, box: [x, y, w, h]}] for each visible layer outside the last node of
    `chain` (one lineage() list, read with kids) that Figma paints over `box` (absolute design px: a
    dict with x, y, width, height, or a tuple), bottom of the paint order first; None when chain is
    None. A later sibling of the node or of any ancestor paints over it, wherever the ancestors that
    clip let `box` show. The one copy of this rule: figma_evidence calls it.

    WHY. A REST render draws the node's own subtree, and so does every tool built on one, so a layer
    left on the section above a screen is on the canvas and in no render, and a comparison against
    the render misses it. /v1/images contents_only=false would draw them, but can answer 400 on large
    files. A child hanging past a clipping ancestor is not painted over by the next frame where the
    ancestor cuts it off."""
    if chain is None:
        return None
    if isinstance(box, dict):
        box = (box.get("x"), box.get("y"), box.get("width"), box.get("height"))
    if not box or any(v is None for v in box):
        return []
    x0, y0, x1, y1 = box[0], box[1], box[0] + box[2], box[1] + box[3]
    for n in chain:
        c = n.get("absoluteBoundingBox") if n.get("clipsContent") else None
        if c:
            x0, y0 = max(x0, c["x"]), max(y0, c["y"])
            x1, y1 = min(x1, c["x"] + c["width"]), min(y1, c["y"] + c["height"])
    if x1 <= x0 or y1 <= y0:
        return []

    def meets(b):
        return (b and b.get("x") is not None and b["x"] < x1 and x0 < b["x"] + b["width"]
                and b["y"] < y1 and y0 < b["y"] + b["height"])
    hits = []
    for k in range(len(chain) - 2, -1, -1):       # the nearest parent first: the bottom of the paint order
        parent, child = chain[k], chain[k + 1]
        if parent.get("type") == "DOCUMENT":
            continue
        if "kids" not in parent:
            raise ValueError("drawn_over needs a lineage read with kids")
        sibs = parent["kids"]
        order = [s.get("id") for s in sibs]
        if child.get("id") not in order:
            raise ValueError(f"lineage is not consistent: {child.get('id')} is not among {parent.get('id')}'s kids")
        for s in sibs[order.index(child["id"]) + 1:]:
            b = s.get("absoluteBoundingBox")
            if s.get("visible") is not False and meets(b):
                hits.append({"id": s.get("id"), "name": (s.get("name") or "")[:60], "type": s.get("type"),
                             "on": parent.get("id"), "box": [b["x"], b["y"], b["width"], b["height"]]})
    return hits


# ---------------------------------------------------------------- status

def status(minutes=10):
    with locked() as st:
        now = time.time()
        levels = {t: round(_bucket(st, t, now)["level"], 1) for t in sorted(limits())}
        pause = float(st.get("pause_until") or 0) - now
        last = st.get("last_429")
    print(f"state: {state_dir()}")
    print("buckets: " + ", ".join(f"T{t} {lv:g} of {limits()[t]:g}" for t, lv in levels.items())
          + f" (per {period():g}s)")
    print(f"pause: {pause:.1f}s left" if pause > 0 else "pause: none")
    if last:
        print(f"last 429: {time.strftime('%H:%M:%S', time.localtime(last['at']))} T{last['tier']}, "
              f"Retry-After {last['retry_after']}")
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - minutes * 60)) + "Z"
    rows = {}
    try:
        with open(os.path.join(state_dir(), "calls.jsonl"), "rb") as f:
            f.seek(max(0, os.path.getsize(f.name) - 4 * 1024 * 1024))
            for line in f.read().decode("utf-8", "replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", "") < since:
                    continue
                k = (f"{r['job']} > {r.get('tool')}" if r.get("job") else r.get("tool"), r.get("lane"))
                c = rows.setdefault(k, {"calls": 0, "429": 0, "cache": 0, "other": 0})
                c["calls"] += 1
                c["429"] += r.get("status") == 429
                c["cache"] += bool(r.get("cache_hit"))
                c["other"] += r.get("status") not in (200, 429)
    except OSError:
        pass
    print(f"last {minutes} min, by tool and lane: {sum(c['calls'] for c in rows.values())} line(s)")
    for (tool, lane), c in sorted(rows.items(), key=lambda kv: -kv[1]["calls"]):
        print(f"  {tool:28} {lane:12} {c['calls']:5} calls  {c['429']:4} x 429  {c['cache']:4} cache  "
              f"{c['other']:4} other")


# ---------------------------------------------------------------- --audit

AUDIT_EXT = (".py", ".mjs", ".js", ".cjs", ".ts", ".sh")
AUDIT_SKIP = {"__pycache__", "node_modules", ".git", "_archive", "out", "dist", ".fidelity"}
ALLOW_MARK = "figma-client: allow"
RAW = (("names the Figma API host", re.compile(r"api\.figma\.com")),
       ("sets the token header itself", re.compile(r"X-Figma-Token", re.I)),
       ("takes figma_auth.headers()", re.compile(r"figma_auth\.headers\(")))
USES = re.compile(r"\bfigma_client\b|figma-rest\.mjs")   # JS callers may reach it through a figma-rest.mjs wrapper
# The client, and the resolver's own header helper (a caller that takes it is still flagged).
AUDIT_ALLOW = {"figma_client.py"}
# A tree read of a file outside this client: files/<key>?ids=, ?depth=, ?<a built query>, files/<key>
# passed whole, or <BASE>?ids= / ?depth= on a base variable. lineage() answers where a node sits; a
# read kept on purpose carries TREE_MARK and its reason on the same line.
TREE_MARK = "figma-tree-read:"
_TREE_KEY = r"(?:\{[^{}]*\}|\$\{[^{}]*\}|[A-Za-z0-9]{22}|['\"`]\s*\+\s*[\w.\[\]]+\s*\+\s*['\"`])"
TREE = re.compile(r"files/" + _TREE_KEY + r"\?(?:[^'\"`]*?\b(?:ids|depth)=|\{|\$\{)"
                  r"|(?<![/\w$])\$?\{\w+\}\?(?:ids|depth)="
                  r"|files/(?:\{[^{}]*\}|\$\{[^{}]*\})['\"`]\s*(?:[,)]|\+.*\?(?:ids|depth)=)"
                  r"|files/['\"`]\s*\+\s*[\w.\[\]]+\s*[,)]")


def tree_reason(line):
    """The reason a tree read gives after TREE_MARK (three words at least), or None."""
    i = line.find(TREE_MARK)
    if i < 0:
        return None
    why = re.sub(r"[\"'`)]+\s*$", "", line[i + len(TREE_MARK):]).strip()
    return why if len(why.split()) >= 3 else None


def code_lines(path, text):
    """(line number, text) of each line that is code: comment lines, Python docstrings and JS block
    comments dropped. A string assigned in code still counts."""
    out, in_doc, in_block = [], None, False
    py = path.endswith(".py")
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if py:
            if in_doc:
                if in_doc in s:
                    in_doc = None
                continue
            m = re.match(r'^[rRbBuU]?("""|\'\'\')', s)
            if m:
                q = m.group(1)
                if s.count(q) < 2 or s == q:
                    in_doc = q
                continue
            if s.startswith("#"):
                continue
        else:
            if in_block:
                if "*/" in s:
                    in_block = False
                continue
            if s.startswith("/*"):
                in_block = "*/" not in s
                continue
            if s.startswith("//") or s.startswith("*") or (path.endswith(".sh") and s.startswith("#")):
                continue
        out.append((i, line))
    return out


def audit(dirs, allow=AUDIT_ALLOW):
    """{'callers': [...], 'routed': [...], 'raw': [(file, line, why)], 'allowed': [(file, line)],
    'tree': [(file, line, text)], 'tree_kept': [(file, line, reason)], 'files': n}"""
    res = {"callers": [], "routed": [], "raw": [], "allowed": [], "tree": [], "tree_kept": [], "files": 0}
    for d in dirs:
        for root, subdirs, files in os.walk(d):
            subdirs[:] = sorted(s for s in subdirs if s not in AUDIT_SKIP)
            for fn in sorted(files):
                if not fn.endswith(AUDIT_EXT) or fn in allow:
                    continue
                p = os.path.join(root, fn)
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
                res["files"] += 1
                hits = []
                for n, line in code_lines(p, text):
                    if TREE.search(line):
                        why = tree_reason(line)
                        (res["tree_kept"].append((p, n, why)) if why else res["tree"].append((p, n, line.strip()[:120])))
                    for why, rx in RAW:
                        if rx.search(line):
                            if ALLOW_MARK in line:
                                res["allowed"].append((p, n))
                            else:
                                hits.append((p, n, why))
                            break
                uses = any(USES.search(line) for _, line in code_lines(p, text))
                if hits or uses:
                    res["callers"].append(p)
                    (res["raw"].extend(hits) if hits else res["routed"].append(p))
    return res


def audit_cli(dirs):
    missing = [d for d in dirs if not os.path.isdir(d)]
    if missing:
        print(f"figma_client --audit: DID NOT RUN, no such folder: {', '.join(missing)}")
        return 2
    r = audit(dirs)
    m, n = len(r["callers"]), len(r["routed"])
    raw_files = sorted({f for f, _, _ in r["raw"]})
    print(f"figma_client --audit: {n} of {m} Figma caller(s) under {', '.join(dirs)} go through figma_client.py; "
          f"{len(raw_files)} call Figma themselves ({r['files']} file(s) read).")
    for f, line, why in r["raw"]:
        print(f"  RAW  {f}:{line}  {why}")
    for f, line in r["allowed"]:
        print(f"  allowed by '{ALLOW_MARK}'  {f}:{line}")
    print(f"figma_client --audit: {len(r['tree'])} tree read(s) of a file outside figma_client.lineage; "
          f"{len(r['tree_kept'])} kept with a reason.")
    for f, line, text in r["tree"]:
        print(f"  TREE {f}:{line}  {text}")
    for f, line, why in r["tree_kept"]:
        print(f"  kept {f}:{line}  {why}")
    if r["tree"]:
        print(f"  Use figma_client.lineage(key, ids) for where a node sits, or mark a read kept on purpose "
              f"'{TREE_MARK} <why>' on its line.")
    if not m:
        print("  No caller found at all, which is a check that saw nothing, not a pass.")
        return 2
    return 1 if raw_files or r["tree"] else 0


# ---------------------------------------------------------------- CLI

def _cli_get(a):
    try:
        if a.cmd == "lineage":
            r = Response(200, {}, json.dumps(lineage(a.key, a.ids, kids=not a.no_kids, version=a.version, lane=a.lane,
                                                     tool=a.tool or "cli", deadline=a.deadline,
                                                     timeout=a.timeout)).encode("utf-8"))
        else:
            r = request("GET", a.path, lane=a.lane, tool=a.tool or "cli", deadline=a.deadline, timeout=a.timeout,
                        max_age=a.max_age)
    except RateLimited as e:
        print(f"figma_client: RATE LIMITED {e.msg}", file=sys.stderr)
        return 4
    except urllib.error.HTTPError as e:
        body = e.read()
        if body:
            sys.stdout.buffer.write(body)
            sys.stdout.flush()
        what = f"lineage {a.key}" if a.cmd == "lineage" else endpoint('GET', normalise(a.path))
        print(f"figma_client: HTTP {e.code} {what}", file=sys.stderr)
        return 3
    except NoToken as e:
        print(f"figma_client: NO TOKEN, {e}", file=sys.stderr)
        return 6
    except (urllib.error.URLError, OSError) as e:
        print(f"figma_client: NETWORK {type(e).__name__}: {getattr(e, 'reason', e)}", file=sys.stderr)
        return 5
    sys.stdout.buffer.write(r.data)
    sys.stdout.flush()
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--selftest"]:
        brk = argv[argv.index("--break") + 1] if "--break" in argv[:-1] else None
        return selftest(brk)
    if argv[:1] == ["--audit"]:
        if len(argv) < 2:
            print("usage: figma_client.py --audit <dir> [<dir> ...]", file=sys.stderr)
            return 2
        return audit_cli(argv[1:])
    if argv[:1] == ["_burst"]:
        return _burst(argv[1:])
    if argv[:1] == ["_mread"]:
        return _mread(argv[1:])
    ap = argparse.ArgumentParser(prog="figma_client.py", description=__doc__.split("\n\n")[0],
                                 epilog="Also: --audit <dir> [<dir> ...], --selftest. The module docstring has the rest.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="GET a Figma REST path and print the JSON body")
    g.add_argument("path")
    g.add_argument("--lane", choices=LANES)
    g.add_argument("--tool")
    g.add_argument("--deadline", type=float, help="seconds this call may wait on the rate limit")
    g.add_argument("--deadline-at", type=float, help="an epoch time the whole job must end by")
    g.add_argument("--timeout", type=float, default=180, help="seconds for one HTTP attempt")
    g.add_argument("--max-age", type=float, help="serve a live tree read from cache this young")
    li = sub.add_parser("lineage", help="print lineage()'s JSON: each id's ancestors from its page down to it")
    li.add_argument("key")
    li.add_argument("ids", nargs="+")
    li.add_argument("--no-kids", action="store_true", help="leave out each ancestor's kids")
    li.add_argument("--version", help="the file at this saved version (asked of Figma, kept once closed)")
    li.add_argument("--lane", choices=LANES)
    li.add_argument("--tool")
    li.add_argument("--deadline", type=float, help="seconds a call may wait on the rate limit")
    li.add_argument("--deadline-at", type=float, help="an epoch time the whole job must end by")
    li.add_argument("--timeout", type=float, default=180, help="seconds for one HTTP attempt")
    s = sub.add_parser("status", help="bucket levels, the pause, and calls by tool")
    s.add_argument("--minutes", type=float, default=10)
    mi = sub.add_parser("mirror", help="sync: fill the mirror now; status: what it holds")
    mi.add_argument("action", choices=("sync", "status"))
    mi.add_argument("--key")
    mi.add_argument("--lane", choices=LANES, default="batch")
    mi.add_argument("--tool", default="figma-mirror-sync")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        status(a.minutes)
        return 0
    if a.cmd == "mirror":
        if a.action == "status":
            mirror_status(a.key)
            return 0
        print(json.dumps(mirror_sync(a.key, lane=a.lane, tool=a.tool), indent=1))
        return 0
    if a.deadline_at:
        os.environ["FIGMA_CLIENT_DEADLINE_AT"] = str(a.deadline_at)
    return _cli_get(a)


# ---------------------------------------------------------------- selftest

DUMMY = "selftest-not-a-token-0000"


def _burst(argv):
    """Selftest worker: N calls, raw urllib (today's tools) or through the client. Prints a summary."""
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("raw", "client"))
    ap.add_argument("n", type=int)
    ap.add_argument("--path", default="/v1/files/BURST/nodes?ids=1:{i}")
    ap.add_argument("--lane", default="interactive")
    ap.add_argument("--deadline", type=float)
    a = ap.parse_args(argv)
    if not base().startswith("http://127.0.0.1:"):
        print("refusing: the selftest worker only calls a local mock")
        return 2
    got = {"ok": 0, "429": 0, "other": 0, "deadline": 0}
    for i in range(a.n):
        p = a.path.replace("{i}", str(os.getpid() * 100 + i))
        try:
            if a.mode == "raw":
                urllib.request.urlopen(urllib.request.Request(base() + p, headers={"X-Figma-Token": DUMMY}), timeout=10).read()
            else:
                get(p, lane=a.lane, tool="selftest-burst", deadline=a.deadline)
            got["ok"] += 1
        except RateLimited:
            got["deadline"] += 1
        except urllib.error.HTTPError as e:
            got["429" if e.code == 429 else "other"] += 1
    print(json.dumps(got))
    return 4 if got["deadline"] else 0


def _apply_break(name):
    """Selftest only: plant one fault, so the check it should trip can be seen to fail."""
    g = globals()
    if name == "off":                       # the client as it was before the mirror
        os.environ["FIGMA_CLIENT_MIRROR"] = "0"
    elif name == "never-current":           # the mirror never trusts what it holds
        g["_covers"] = lambda have, cur: False
    elif name == "version-only":            # a cache keyed on the version alone
        g["_covers"] = lambda have, cur: bool(have) and bool(cur) and have[0] == cur[0]
    elif name == "nolock":                  # every process refreshes for itself
        g["_mirror_lock"] = lambda *a, **k: contextlib.nullcontext()
    elif name == "dead":                    # an id the mirror lacks is declared dead without asking
        def dead(self, ids, depth, geometry):
            self.take_env((self.outline() or {}).get("env"))
            return {i: None for i in ids}
        _Mirror.extra = dead
    elif name == "render-stamp":            # renders keyed on the edit, not the subtree hash
        orig_rk = g["_render_key"]
        g["_render_key"] = lambda nid, h, stamp, params: orig_rk(nid, None, stamp, params)
    elif name == "meta-fallback":           # /meta down: serve what the mirror holds
        orig_fs = g["_file_stamp"]

        def fs(key, *a, **k):
            try:
                return orig_fs(key, *a, **k)
            except Exception:  # noqa: BLE001
                ol = _read_json(os.path.join(mirror_root(), key, "outline.json")) or {}
                if ol.get("stamp"):
                    return tuple(ol["stamp"])
                raise
        g["_file_stamp"] = fs
    elif name == "no-429-headers":          # the 429 line without Figma's two headers
        g["_rate_headers"] = lambda headers: {}
    elif name == "no-hot":                  # a refresh brings only the chunks its own read needs
        _Mirror.hot_stale = lambda self, idx, exclude: []
    elif name == "trust-pointers":          # lineage follows the index's parent pointers unchecked
        def unchecked(i, c, body, idx):
            nodes, chain = (idx.get(c) or {}).get("nodes") or {}, [i]
            while chain[-1] != c and chain[-1] in nodes and len(chain) < 64:
                chain.append(nodes[chain[-1]][0])
            return chain[::-1]
        _Mirror.chunk_chain = staticmethod(unchecked)
    elif name == "no-fallback":             # an id the mirror lacks gets None, Figma never asked
        g["_lineage_rest"] = lambda key, ids, kids, version, now, kw: {i: None for i in ids}
    elif name == "stale-outline":           # lineage trusts the outline's pages and sections as last read
        _Mirror.current_shells = lambda self, sids, ol: {
            s: {"props": dict(ol["shells"][s]["props"]), "kids": [_outline_props(x, ol) for x in ol["shells"][s]["kids"]]}
            for s in sids}
    else:
        raise SystemExit(f"figma_client: unknown --break {name}")


def _mread(argv):
    """Selftest worker: wait for a start time, read one path through the client, print what it saw."""
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--at", type=float, default=0.0)
    a = ap.parse_args(argv)
    if not base().startswith("http://127.0.0.1:"):
        print("refusing: the selftest worker only calls a local mock")
        return 2
    if os.environ.get("FIGMA_SELFTEST_BREAK"):
        _apply_break(os.environ["FIGMA_SELFTEST_BREAK"])
    time.sleep(max(0.0, a.at - time.time()))
    try:
        r = get(a.path, lane="gate", tool="selftest-mread")
        doc = (next(iter((r.get("nodes") or {}).values())) or {}).get("document") or {}
        print(json.dumps({"ok": 1, "characters": doc.get("characters")}))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": 0, "error": f"{type(e).__name__}: {e}"[:300]}))
    return 0


class _MockFile:
    """A small design file for the mirror checks: a cited page with sections nested past
    OUTLINE_DEPTH, frames, an instance, a component, a text style, geometry; a page that is not
    cited; and two ids REST answers that no page holds (a library component, an off-page frame).
    It answers /meta, /nodes, files?ids=, files?depth= and renders the way REST does, using the
    client's own pruning and map rule: the checks here count calls, live REST checks the shape."""

    def __init__(self, key):
        self.key, self.version, self.n, self.renders = key, 100, 0, 0
        self.lta = "2026-10-24T08:00:00.000Z"
        geo = {"fillGeometry": [{"path": "M0 0L1 1", "windingRule": "NONZERO"}], "size": {"x": 1.0, "y": 1.0},
               "relativeTransform": [[1, 0, 0], [0, 1, 0]]}

        def node(i, t, name, kids=None, **kw):
            d = dict({"id": i, "name": name, "type": t}, **kw)
            if kids is not None:
                d["children"] = kids
            return d

        def txt(i):
            return node(i, "TEXT", "t" + i, characters="hello " + i, styles={"text": "S:1"},
                        style={"fontSize": 16, "inheritTextStyleId": "S:1"})

        self.doc = node("0:0", "DOCUMENT", "Document", [
            node("1:1", "CANVAS", "V3", [
                node("2:1", "SECTION", "S", [
                    node("3:1", "FRAME", "F1", [txt("4:1"), node("4:2", "INSTANCE", "inst",
                                                                  [node("I4:2;9:1", "VECTOR", "v", **geo)],
                                                                  componentId="C:1")], **geo),
                    node("3:2", "FRAME", "F2", [node("4:3", "RECTANGLE", "r", **geo)]),
                    node("2:2", "SECTION", "S2", [
                        node("3:3", "FRAME", "F3", [txt("4:4")]),
                        node("2:3", "SECTION", "S3", [node("3:5", "FRAME", "F5", [txt("4:6")])]),
                    ]),
                ]),
                node("3:4", "FRAME", "F4", [node("4:5", "COMPONENT", "C", [txt("4:7")])]),
            ], backgroundColor={"r": 1, "g": 1, "b": 1, "a": 1}),
            node("1:2", "CANVAS", "Other", [node("5:1", "FRAME", "O1", [txt("5:2")])]),
        ], scrollBehavior="SCROLLS")
        self.lib = [node("L:1", "COMPONENT", "lib", []), node("L:2", "FRAME", "offpage", [txt("L:3")])]
        self.maps = ({"C:1": {"key": "c1", "name": "C1", "componentSetId": "CS:1"}, "4:5": {"key": "c45", "name": "C"},
                      "L:1": {"key": "l1", "name": "lib"}},
                     {"CS:1": {"key": "cs1", "name": "CS"}},
                     {"S:1": {"key": "s1", "name": "body", "styleType": "TEXT"}})

    def find(self, nid):
        for root in [self.doc] + self.lib:
            for n in _walk(root):
                if n["id"] == nid:
                    return n
        return None

    def path_to(self, nid, n=None, acc=()):
        n = n or self.doc
        acc = acc + (n["id"],)
        if n["id"] == nid:
            return acc
        for c in n.get("children") or []:
            got = self.path_to(nid, c, acc)
            if got:
                return got
        return None

    def edit(self, nid, version=False):
        self.n += 1
        n = self.find(nid)
        if "characters" in n:
            n["characters"] = f"edit {self.n}"
        else:
            n["name"] = f"{n['name']} edit {self.n}"
        if version:
            self.version += 1
        self.lta = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1792828800 + 60 * self.n)) + ".000Z"

    def entry(self, n, depth, geometry):
        t = _prune(n, depth, geometry)
        c, s, st = _maps(t, self.maps)
        return {"document": t, "components": c, "componentSets": s, "styles": st, "schemaVersion": 0}

    def answer(self, bare, path):
        q = dict(urllib.parse.parse_qsl(path.partition("?")[2], keep_blank_values=True))
        ids = [i for i in (q.get("ids") or "").split(",") if i]
        depth = int(q["depth"]) if (q.get("depth") or "").isdigit() else None
        geometry = q.get("geometry") == "paths"
        env = {"name": "Mock File", "lastModified": self.lta[:19] + "Z", "thumbnailUrl": "https://example.invalid/t",
               "version": str(self.version), "role": "editor", "editorType": "figma", "linkAccess": "org_edit"}
        if bare.endswith("/meta"):
            return {"file": {"name": env["name"], "version": env["version"], "last_touched_at": self.lta}}
        if bare.startswith("/v1/images/"):
            self.renders += 1
            return {"err": None, "images": {i: (f"https://example.invalid/r/{i}/{self.renders}" if self.find(i) else None)
                                            for i in ids}}
        if bare.endswith("/nodes"):
            return dict(env, nodes={i: (self.entry(self.find(i), depth, geometry) if self.find(i) else None)
                                    for i in ids})
        if ids:
            onpath = {x for i in ids for x in (self.path_to(i) or ())}

            def build(n):     # every page is listed, as REST does; off the paths with children []
                if n["id"] in ids:
                    return _prune(n, None, geometry)
                out = {k: v for k, v in n.items() if k != "children" and (geometry or k not in GEOM_KEYS)}
                if n["type"] == "DOCUMENT":
                    out["children"] = [build(c) if c["id"] in onpath else _prune(c, 0, geometry) for c in n["children"]]
                elif "children" in n:
                    out["children"] = [build(c) for c in n["children"] if c["id"] in onpath] if n["id"] in onpath else []
                return out
            doc = build(self.doc)
        else:
            doc = _prune(self.doc, depth, geometry)
        c, s, st = _maps(doc, self.maps)
        return dict(env, document=doc, components=c, componentSets=s, styles=st, schemaVersion=0)

    def direct(self, path):
        """What this file answers for a client path, without HTTP: the checks' reference."""
        p = normalise(path)
        return self.answer(p.split("?", 1)[0], p)

    def lineage_truth(self, nid, kids=True):
        """lineage()'s answer for nid, read off the whole file: the checks' reference."""
        p = self.path_to(nid)
        if not p:
            return None
        ns = [self.find(i) for i in p[1:]]
        return [dict(_own(n), **({"kids": [_own(c) for c in n.get("children") or []]} if kids and j < len(ns) - 1 else {}))
                for j, n in enumerate(ns)]


class _Mock:
    """A strict Figma stand-in: a leaky bucket per tier at Figma's own limits a PERIOD, 429 with
    Retry-After when full, and scripted answers per path for the pause, 5xx and deadline cases."""

    def __init__(self, per_period=None, period_s=3.0):
        import threading
        self.lock = threading.Lock()
        self.limits = per_period or {1: 20, 2: 100, 3: 150}
        self.period = period_s
        self.events = []        # (t, method, path, status)
        self.script = {}        # path without query -> [status or (status, retry_after[, headers]), ...]
        self.forever = {}       # path without query -> (status, retry_after)
        self.files = {}         # file key -> _MockFile that answers its reads
        self.delay = {}         # path without query -> seconds a 200 takes
        self.reset()

    def reset(self, period_s=None):
        with self.lock:
            if period_s:
                self.period = period_s
            self.level = {1: 0.0, 2: 0.0, 3: 0.0}
            self.at = time.time()
            self.events = []

    def answer(self, method, path, token):
        hdrs = {}
        with self.lock:
            now = time.time()
            bare = path.split("?", 1)[0]
            ra = None
            if token != DUMMY:
                status = 403
            elif self.script.get(bare):
                s = self.script[bare].pop(0)
                status, ra = (s[0], s[1]) if isinstance(s, tuple) else (s, None)
                hdrs = s[2] if isinstance(s, tuple) and len(s) > 2 else {}
            elif bare in self.forever:
                status, ra = self.forever[bare]
            else:
                t = tier(path)
                for k in self.level:
                    self.level[k] = max(0.0, self.level[k] - (now - self.at) * self.limits[k] / self.period)
                self.at = now
                if self.level[t] + 1 > self.limits[t]:
                    status = 429
                    ra = max(1, int((self.level[t] + 1 - self.limits[t]) / (self.limits[t] / self.period)) + 1)
                else:
                    self.level[t] += 1
                    status = 200
            self.events.append((now, method, path, status))
            delay = self.delay.get(bare, 0) if status == 200 else 0
        if delay:
            time.sleep(delay)
        if status == 200:
            key = _file_key(path) or ""
            if key in self.files:
                with self.lock:
                    body = self.files[key].answer(bare, path)
            elif bare.endswith("/meta"):
                body = {"file": {"version": "200", "last_touched_at": "2026-10-23T00:00:00Z", "name": key}}
            elif bare.endswith("/nodes"):
                q = dict(urllib.parse.parse_qsl(path.partition("?")[2]))
                body = {"name": key, "version": q.get("version", "200"),
                        "nodes": {i: {"document": {"id": i, "type": "FRAME", "q": q.get("geometry", "")}}
                                  for i in (q.get("ids") or "").split(",") if i}}
            else:
                body = {"ok": True, "method": method, "path": path}
        elif status == 404:
            body = {"status": 404, "err": "Not found"}
        else:
            body = {"status": status, "err": "mock"}
        return status, ra, json.dumps(body).encode("utf-8"), hdrs

    def serve(self):
        import http.server
        import threading
        mock = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                status, ra, body, extra = mock.answer(self.command, self.path, self.headers.get("X-Figma-Token"))
                self.send_response(status)
                if ra is not None:
                    self.send_header("Retry-After", str(ra))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_DELETE = _do

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def hits(self, fragment, status=None, since=0.0):
        with self.lock:
            return [e for e in self.events if fragment in e[2] and e[0] >= since and (status is None or e[3] == status)]


def selftest(brk=None):
    import shutil
    import subprocess
    import tempfile
    checks = []

    def chk(name, ok, detail=""):
        checks.append((name, bool(ok)))
        print(f"  {'ok  ' if ok else 'MISS'}  {name}" + (f"  ({detail})" if detail else ""))

    tmp = tempfile.mkdtemp(prefix="figma_client_selftest.")
    mock = _Mock()
    mbase = mock.serve()
    saved = {k: os.environ.get(k) for k in ("FIGMA_API_BASE", "FIGMA_CLIENT_DIR", "FIGMA_CLIENT_PERIOD",
                                            "FIGMA_CLIENT_LIMITS", "FIGMA_TOKEN", "FIGMA_PAT", "FIGMA_ACCESS_TOKEN",
                                            "FIGMA_CLIENT_LANE", "FIGMA_CLIENT_DEADLINE", "FIGMA_CLIENT_DEADLINE_AT",
                                            "FIGMA_CLIENT_MAX_AGE", "FIGMA_CLIENT_TOOL", "FIGMA_AUTH_NO_KEYCHAIN",
                                            "CLAUDE_CONFIG_DIR", "FIGMA_CLIENT_MIRROR", "FIGMA_CLIENT_META_TTL",
                                            "FIGMA_MIRROR_DIR", "FIGMA_MIRROR_FILES", "FIGMA_SELFTEST_BREAK")}
    if brk:
        print(f"figma_client selftest: PLANTED BREAK {brk!r}; the checks it should trip must MISS")
        if brk != "off":
            _apply_break(brk)

    def env(state, period_s):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("FIGMA_",))}
        e.update({"FIGMA_API_BASE": mbase, "FIGMA_CLIENT_DIR": os.path.join(tmp, state),
                  "FIGMA_CLIENT_PERIOD": str(period_s), "FIGMA_TOKEN": DUMMY})
        return e

    def use(state, period_s):
        for k in list(os.environ):
            if k.startswith("FIGMA_"):
                del os.environ[k]
        os.environ.update(env(state, period_s))

    def spawn(args, e):
        return subprocess.Popen([sys.executable, os.path.abspath(__file__), "_burst", *args], env=e,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def summary(p):
        out = p.communicate(timeout=120)[0].strip().splitlines()
        try:
            return json.loads(out[-1])
        except (ValueError, IndexError):
            return {"error": " ".join(out)[-300:]}

    print(f"figma_client selftest: a local mock at {mbase}, state under {tmp}")
    bad429 = good429 = done = None
    try:
        assert mbase.startswith("http://127.0.0.1:")
        use("unit", 60)
        # ---- paths and tiers
        chk("tiers: nodes, file and renders are T1; comments, versions, variables and image fills T2; "
            "/meta, components and /me T3",
            [tier(p) for p in ("files/K/nodes?ids=1:2", "files/K?depth=1", "images/K?ids=1:2", "files/K/comments",
                               "files/K/versions", "files/K/variables/local", "files/K/images", "files/K/meta",
                               "files/K/components", "me", "teams/9/projects")] == [1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 2])
        nxt = "https://" + FIGMA_HOST + "/v1/files/K/versions?page_size=30&after=2026-10-20 20:04:48 UTC"
        chk("a full api URL (a next_page link) keeps its path, with raw spaces quoted and % kept",
            normalise(nxt) == "/v1/files/K/versions?page_size=30&after=2026-10-20%2020:04:48%20UTC"
            and normalise("files/K/nodes?ids=1%3A2") == "/v1/files/K/nodes?ids=1%3A2")
        try:
            normalise("https://example.com/v1/me")
            other = False
        except ValueError:
            other = True
        chk("a URL on any other host is refused, so the token never goes there", other)

        # ---- BAD: four processes bursting raw urllib, as today's tools do
        mock.reset(3.0)
        t0 = time.time()
        procs = [spawn(["raw", "10"], env("bad", 3.0)) for _ in range(4)]
        raw = [summary(p) for p in procs]
        bad429 = len(mock.hits("/BURST/", 429, t0))
        chk("BAD case: 4 processes x 10 raw urllib calls against a strict 20-a-window mock get 429s",
            bad429 > 0, f"{bad429} x 429 of {len(mock.hits('/BURST/', None, t0))} calls; workers {raw}")

        # ---- GOOD: the same burst through the client
        mock.reset(3.0)
        t0 = time.time()
        procs = [spawn(["client", "10"], env("good", 3.0)) for _ in range(4)]
        good = [summary(p) for p in procs]
        good429 = len(mock.hits("/BURST/", 429, t0))
        done = sum(g.get("ok", 0) for g in good)
        chk("GOOD case: the same 4 x 10 through the client get no 429 and all 40 complete",
            good429 == 0 and done == 40, f"{good429} x 429, {done} of 40 done in {time.time() - t0:.1f}s; workers {good}")

        # ---- lanes: batch starved at 5 tokens while gate and interactive are served
        mock.reset(60.0)
        use("lanes", 60)
        with locked() as st:
            _bucket(st, 1, time.time())["level"] = 5.0
        t0 = time.time()
        b = spawn(["client", "1", "--lane", "batch", "--deadline", "10", "--path", "/v1/files/LANE/nodes?ids=9:9"],
                  env("lanes", 60))
        time.sleep(0.6)
        get("files/LANE/nodes?ids=1:1", lane="gate", tool="selftest")
        gate_at = time.time()
        get("files/LANE/nodes?ids=1:2", lane="interactive", tool="selftest")
        try:
            get("files/LANE/nodes?ids=1:3", lane="interactive", tool="selftest", deadline=0.5)
            starved_i = False
        except RateLimited:
            starved_i = True
        get("files/LANE/nodes?ids=1:4", lane="gate", tool="selftest")
        bsum = summary(b)
        b_end = time.time()
        chk("lanes: at 5 tokens a gate call is served while a batch call waits, and batch never reaches Figma",
            b.returncode == 4 and bsum.get("deadline") == 1 and not mock.hits("ids=9:9") and gate_at < b_end
            and mock.hits("ids=1:1", 200), f"batch exit {b.returncode} {bsum}, gate served at "
                                           f"{gate_at - t0:.1f}s, batch gave up at {b_end - t0:.1f}s")
        chk("lanes: interactive is served down to its reserve of 3, then waits, while gate still goes",
            mock.hits("ids=1:2", 200) and starved_i and not mock.hits("ids=1:3") and mock.hits("ids=1:4", 200))

        # ---- the global pause: one 429 holds every process, on any tier
        mock.reset(60.0)
        use("pause", 60)
        mock.script["/v1/files/PAUSE/meta"] = [(429, 2)]
        t0 = time.time()
        a_p = spawn(["client", "1", "--path", "/v1/files/PAUSE/meta"], env("pause", 60))
        pause_until = 0.0
        while time.time() - t0 < 5:
            with locked() as st:
                pause_until = float(st.get("pause_until") or 0)
            if pause_until > time.time():
                break
            time.sleep(0.05)
        b_p = spawn(["client", "1", "--lane", "gate", "--path", "/v1/files/OTHER/nodes?ids=2:{i}"], env("pause", 60))
        sa, sb = summary(a_p), summary(b_p)
        first = mock.hits("/PAUSE/meta", 429)
        retry = mock.hits("/PAUSE/meta", 200)
        other_hits = mock.hits("/OTHER/nodes")
        chk("pause: after a 429 with Retry-After 2 the call is retried no sooner, and succeeds",
            first and retry and retry[0][0] - first[0][0] >= 1.95 and sa.get("ok") == 1,
            f"retried after {retry[0][0] - first[0][0]:.2f}s" if first and retry else str(sa))
        chk("pause: another process's gate call on another tier waits out the same pause",
            pause_until and other_hits and other_hits[0][0] >= pause_until - 0.05 and sb.get("ok") == 1,
            f"it reached the mock {other_hits[0][0] - pause_until:+.2f}s from the pause's end" if other_hits else str(sb))

        # ---- deadline: a 429 that never ends is ridden out until the deadline, then raised
        mock.reset(60.0)
        use("deadline", 60)
        mock.forever["/v1/files/ALWAYS/meta"] = (429, 1)
        t0 = time.time()
        try:
            get("files/ALWAYS/meta", lane="gate", tool="selftest", deadline=2.5)
            raised = False
        except RateLimited:
            raised = True
        tries = len(mock.hits("/ALWAYS/meta"))
        chk("deadline: retries a 429 while the next try fits before the deadline, then raises RateLimited (an HTTPError 429)",
            raised and tries >= 2 and time.time() - t0 <= 3.0 and issubclass(RateLimited, urllib.error.HTTPError),
            f"{tries} tries in {time.time() - t0:.1f}s")
        try:
            get_or_exit("files/ALWAYS/meta", lane="gate", tool="selftest", deadline=0.5)
            goe = ""
        except SystemExit as e:
            goe = str(e)
        mock.forever["/v1/files/GONE/meta"] = (404, None)
        try:
            get_or_exit("files/GONE/meta", lane="gate", tool="selftest")
            g404 = False
        except urllib.error.HTTPError as e:
            g404 = e.code == 404
        chk("get_or_exit: a rate limit past the deadline stops with DID NOT RUN; a 404 is still raised as an HTTPError",
            goe.startswith("DID NOT RUN") and g404, goe)
        del mock.forever["/v1/files/ALWAYS/meta"]
        # A give-up in _acquire (no token in the lane's budget before the deadline) never reaches Figma.
        with locked() as st:
            _bucket(st, 3, time.time())["level"] = 0.0
        try:
            get("files/EMPTY/meta", lane="gate", tool="selftest-giveup", deadline=0.2)
            empty_raised = False
        except RateLimited:
            empty_raised = True
        with open(log_path(), encoding="utf-8") as f:
            dl = [json.loads(l) for l in f if l.strip()]
        gave = [l for l in dl if l.get("status") == "gave-up"]
        chk("log: a give-up is logged, both after a 429 past the deadline and when the budget had no token in time",
            empty_raised and any(l["endpoint"].endswith("/ALWAYS/meta") and l.get("retry_after") for l in gave)
            and any(l["endpoint"].endswith("/EMPTY/meta") and l["tool"] == "selftest-giveup" for l in gave)
            and not mock.hits("/EMPTY/meta"), f"{len(gave)} gave-up line(s)")

        # No token anywhere: the environment empty and the Keychain off.
        use("notoken", 60)
        os.environ.pop("FIGMA_TOKEN", None)
        os.environ["FIGMA_AUTH_NO_KEYCHAIN"] = "1"
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(tmp, "no-claude-config")
        try:
            get("files/NOTOKEN/meta", lane="gate", tool="selftest-notoken")
            no_tok = False
        except NoToken:
            no_tok = True
        try:
            with open(log_path(), encoding="utf-8") as f:
                nl = [json.loads(l) for l in f if l.strip()]
        except OSError:
            nl = []           # nothing logged at all
        chk("log: a call with no token is logged as no-token and never reaches Figma",
            no_tok and any(l.get("status") == "no-token" and l["tool"] == "selftest-notoken" for l in nl)
            and not mock.hits("/NOTOKEN/"), f"{len(nl)} line(s)")

        # Another figma_auth.py first on the caller's path and already imported (a test folder's copy):
        # the client still asks its own, which finds no token, so the decoy's token never reaches Figma.
        decoy_dir = os.path.join(tmp, "decoy")
        os.makedirs(decoy_dir, exist_ok=True)
        with open(os.path.join(decoy_dir, "figma_auth.py"), "w", encoding="utf-8") as f:
            f.write("def token(required=True):\n    return 'decoy-token'\n")
        was_mod = sys.modules.pop("figma_auth", None)
        sys.path.insert(0, decoy_dir)
        try:
            import figma_auth as _decoy  # noqa: F401  the caller's own copy, imported before any call
            try:
                get("files/DECOY/meta", lane="gate", tool="selftest-decoy")
                decoy_used = True
            except NoToken:
                decoy_used = False
            except urllib.error.HTTPError:   # the mock refused the decoy's token: it was sent
                decoy_used = True
        finally:
            sys.path.remove(decoy_dir)
            sys.modules.pop("figma_auth", None)
            if was_mod is not None:
                sys.modules["figma_auth"] = was_mod
        chk("the token comes from the figma_auth.py beside the client, not one the caller imported first",
            not decoy_used and not mock.hits("/DECOY/"))

        # ---- 5xx, POST
        use("misc", 60)
        mock.script["/v1/files/FLAKY/meta"] = [503]
        r = get("files/FLAKY/meta", tool="selftest")
        chk("a 503 on a GET is retried", r and len(mock.hits("/FLAKY/meta")) == 2)
        mock.script["/v1/files/VK/comments"] = [(429, 1)]
        r1 = post("files/VK/comments", {"message": "x"}, lane="gate", tool="selftest")
        mock.script["/v1/files/VK/comments"] = [503]
        try:
            post("files/VK/comments", {"message": "y"}, lane="gate", tool="selftest")
            post503 = False
        except urllib.error.HTTPError as e:
            post503 = e.code == 503
        chk("a POST is retried after a 429 but not after a 503, so a comment is never posted twice",
            r1 and r1.get("method") == "POST" and post503 and len(mock.hits("/VK/comments")) == 3)

        # ---- cache
        mock.reset(60.0)
        use("cache", 60)
        for _ in range(3):
            get("files/VK/nodes?ids=1:2&version=100", tool="selftest")
        closed_fetch = len(mock.hits("/VK/nodes?ids=1:2&version=100"))
        for _ in range(2):
            get("files/VK/nodes?ids=1:2&version=200", tool="selftest")
        newest_fetch = len(mock.hits("/VK/nodes?ids=1:2&version=200"))
        chk("cache: a closed version is read once and then kept; the newest version is read every time",
            closed_fetch == 1 and newest_fetch == 2 and len(mock.hits("/VK/meta")) == 1,
            f"closed {closed_fetch}, newest {newest_fetch}, /meta {len(mock.hits('/VK/meta'))}")
        for _ in range(2):
            get("files/VK/nodes?ids=3:3", tool="selftest")
        for _ in range(2):
            get("images/VK?ids=1:2&version=100", tool="selftest")
        chk("cache: a live read and a render are never served from cache unless asked",
            len(mock.hits("/VK/nodes?ids=3:3")) == 2 and len(mock.hits("/images/VK")) == 2)
        for _ in range(2):
            get("files/VK/nodes?ids=4:4", tool="selftest", max_age=60)
        chk("cache: a live read with max_age is read once", len(mock.hits("ids=4:4")) == 1)
        n = prefetch_nodes("VK", ["5:1", "5:2", "5:3"], query="geometry=paths", tool="selftest")
        one = get("files/VK/nodes?ids=5:2&geometry=paths", tool="selftest", max_age=60)
        chk("prefetch: 3 nodes in one ids= call, then a single-node read with max_age is served from it",
            n == 1 and len(mock.hits("/VK/nodes?ids=5")) == 1 and list(one["nodes"]) == ["5:2"]
            and one["nodes"]["5:2"]["document"]["q"] == "paths")

        # ---- the mirror (a file the mock models: pages, nested sections, frames, an instance, styles)
        mock.reset(60.0)
        use("mirror", 60)
        menv = {"FIGMA_MIRROR_FILES": "MIR=V3", "FIGMA_MIRROR_DIR": os.path.join(tmp, "mirror-store"),
                "FIGMA_CLIENT_META_TTL": "0"}
        if brk:
            menv["FIGMA_SELFTEST_BREAK"] = brk
        if brk == "off":
            menv["FIGMA_CLIENT_MIRROR"] = "0"
        os.environ.update(menv)
        mf = _MockFile("MIR")
        mock.files["MIR"] = mf

        def t1_since(since):
            return [e for e in mock.events if e[0] >= since and tier(e[2]) == 1]

        def mget(path, **kw):
            return get(path, lane="gate", tool="selftest-mirror", **kw)

        try:
            summ = mirror_sync("MIR", lane="gate", tool="selftest-mirror")
        except Exception as e:  # noqa: BLE001
            summ = {"error": f"{type(e).__name__}: {e}"}
        chk("mirror: sync finds the named page in the depth=1 tree, reads a section cut at OUTLINE_DEPTH again, "
            "and fills all 5 chunks", summ.get("page_ids") == ["1:1"] and summ.get("chunks") == 5
            and summ.get("current") == 5, summ)

        cases = ["files/MIR/nodes?ids=4:1", "files/MIR/nodes?ids=3:1&depth=1", "files/MIR/nodes?ids=3:1&geometry=paths",
                 "files/MIR/nodes?ids=2:1&depth=2", "files/MIR/nodes?ids=1:1", "files/MIR/nodes?ids=4:2,3:3,L:2",
                 "files/MIR/nodes?ids=4:5", "files/MIR?ids=I4:2;9:1", "files/MIR?depth=2"]
        differ = []
        for c in cases:
            try:
                if mget(c) != mf.direct(c):
                    differ.append(c)
            except Exception as e:  # noqa: BLE001
                differ.append(f"{c} raised {type(e).__name__}: {e}")
        chk("mirror: answers equal the mock's REST answers: /nodes whole, at depth 1 and 2, with geometry, a "
            "section, a page, a component, an id no page holds; files?ids= and files?depth= (kept per edit)",
            not differ, differ)

        mget("images/MIR?ids=3:1&scale=2&format=png")
        t0 = time.time()
        for c in cases + ["images/MIR?ids=3:1&scale=2&format=png"]:
            mget(c)
        n1, nmeta = len(t1_since(t0)), len(mock.hits("/MIR/meta", None, t0))
        chk("(a) with last_touched_at unchanged, 10 reads (/nodes, depth, geometry, a section, a page, files?ids=, "
            "files?depth=, a render) make 0 Tier 1 calls and ask /meta each time", n1 == 0 and nmeta >= 10,
            f"{n1} Tier 1 call(s), {nmeta} /meta")

        mf.edit("4:1")
        mock.delay["/v1/files/MIR/nodes"] = 1.5
        start = time.time() + 4.0
        t0 = time.time()
        menv_proc = dict(env("mirror", 60), **menv)
        procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "_mread", "files/MIR/nodes?ids=4:1",
                                   "--at", str(start)], env=menv_proc, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True) for _ in range(2)]
        outs = [summary(p) for p in procs]
        mock.delay.clear()
        refreshes = mock.hits("/MIR/nodes", None, t0)
        want = mf.find("4:1")["characters"]
        chk("(b) after last_touched_at moves, two processes reading at the same moment make exactly one refresh "
            "call between them, and both read the edit", len(refreshes) == 1
            and all(o.get("characters") == want for o in outs),
            f"{len(refreshes)} /nodes call(s); workers {outs}; the edit reads {want!r}")

        t0 = time.time()
        r1 = mget("files/MIR/nodes?ids=L:1,L:3,9:99")
        first = mock.hits("/MIR/nodes", None, t0)
        r2 = mget("files/MIR/nodes?ids=L:1,L:3,9:99")
        both = mock.hits("/MIR/nodes", None, t0)
        nodes1 = r1.get("nodes") or {}
        chk("(c) ids the mirror does not hold (a library component, an off-page node, an id Figma does not have): "
            "one batched /nodes call, kept for the edit; Figma's answers passed on, none declared dead by the mirror",
            len(first) == 1 and all(x in first[0][2] for x in ("L:1", "L:3", "9:99")) and len(both) == 1
            and nodes1.get("L:1") and nodes1.get("L:3") and "9:99" in nodes1 and nodes1["9:99"] is None and r2 == r1,
            f"{len(first)} call(s) on the first read, {len(both) - len(first)} on the second; "
            f"L:1 {'answered' if nodes1.get('L:1') else 'null'}, L:3 {'answered' if nodes1.get('L:3') else 'null'}")

        mget("images/MIR?ids=3:1,3:2&scale=2&format=png")
        mf.edit("4:3", version=True)
        t0 = time.time()
        ra = mget("images/MIR?ids=3:1&scale=2&format=png")
        same_band = mock.hits("/images/MIR", None, t0)
        rb = mget("images/MIR?ids=3:2&scale=2&format=png")
        changed_band = mock.hits("/images/MIR", None, t0)
        chk("(d) after an edit and a version bump, the render of a band whose subtree hash did not change comes from "
            "cache (0 render calls), and the band that changed is rendered again (1)",
            len(same_band) == 0 and len(changed_band) == 1 and (ra.get("images") or {}).get("3:1")
            and (rb.get("images") or {}).get("3:2"), f"{len(same_band)} render call(s) for the unchanged band, "
                                                      f"{len(changed_band) - len(same_band)} for the changed one")

        mf.edit("4:4")
        t0 = time.time()
        hot_reads = [mget(q) for q in ("files/MIR/nodes?ids=4:1", "files/MIR/nodes?ids=4:4", "files/MIR/nodes?ids=4:7")]
        hot_calls = mock.hits("/MIR/nodes", None, t0)
        chk("mirror: after an edit, the first read's refresh also brings the other chunks read lately, so three "
            "reads in three chunks make one call, and the edited one reads the edit",
            len(hot_calls) == 1 and hot_reads[1]["nodes"]["4:4"]["document"]["characters"] == mf.find("4:4")["characters"],
            f"{len(hot_calls)} /nodes call(s)")

        t0 = time.time()
        fa, fb = mget("files/MIR?ids=I4:2;9:1"), mget("files/MIR?ids=I4:2;9:1")
        fcalls = mock.hits("/v1/files/MIR?", None, t0)
        chk("mirror: a GET files/KEY read after an edit goes to Figma once, then is kept until the next edit",
            len(fcalls) == 1 and fa == fb == mf.direct("files/MIR?ids=I4:2;9:1"), f"{len(fcalls)} call(s) for 2 reads")

        mock.forever["/v1/files/MIR/meta"] = (503, None)
        t0 = time.time()
        try:
            served = mget("files/MIR/nodes?ids=4:1", deadline=2.5)
            raised = None
        except (urllib.error.URLError, http.client.HTTPException) as e:
            served, raised = None, f"{type(e).__name__} {getattr(e, 'code', '')}"
        del mock.forever["/v1/files/MIR/meta"]
        held = os.path.exists(os.path.join(menv["FIGMA_MIRROR_DIR"], "MIR", "chunks", _safe("3:1") + ".json"))
        meta_failed = mock.hits("/MIR/meta", 503, t0)
        chk("(e) /meta failing: the read raises and nothing is served, though the mirror holds the node",
            raised and "503" in raised and meta_failed and served is None and held
            and not mock.hits("/MIR/nodes", None, t0),
            f"raised {raised}; /meta answered 503 {len(meta_failed)} time(s); served "
            f"{'something' if served is not None else 'nothing'}; mirror holds 3:1: {held}")

        # ---- lineage: where a node sits, from the mirror, checked against the file itself
        def lin(ids, **kw):
            return lineage("MIR", ids, lane="gate", tool="selftest-lineage", **kw)

        def lin_calls(since):
            return [e for e in t1_since(since) if "/MIR" in e[2]]

        held_ids = ["4:1", "I4:2;9:1", "3:1", "2:1", "2:3", "4:6", "1:1", "4:7"]
        t0 = time.time()
        try:
            got = lin(held_ids + ["9:99", "L:1"])
            want = {i: mf.lineage_truth(i) for i in held_ids + ["9:99", "L:1"]}
            bad = [i for i in want if got.get(i) != want[i]]
        except Exception as e:  # noqa: BLE001
            got, bad = {}, [f"raised {type(e).__name__}: {e}"]
        chk("lineage: page-to-node entries and each ancestor's kids equal the file's own, for a node in a chunk, "
            "inside an instance, a chunk's root, a section, a section nested past OUTLINE_DEPTH and a node in it, "
            "a page and a component; None for an id Figma does not have and a library component", not bad,
            f"differ: {bad}" if bad else f"{len(lin_calls(t0))} Tier 1 call(s) on the first read: "
                                         f"{[e[2] for e in lin_calls(t0)]}")
        tree_ok = lineage_in_tree(mf.doc, held_ids) == {i: mf.lineage_truth(i) for i in held_ids}
        chk("lineage_in_tree reads the same answer off a whole tree", tree_ok)
        t0 = time.time()
        again = lin(held_ids + ["9:99", "L:1"])
        chk("(f) lineage again with nothing edited: 0 Tier 1 calls, the same answer",
            not lin_calls(t0) and again == got, f"{len(lin_calls(t0))} Tier 1 call(s)")

        t0 = time.time()
        try:
            off = lin(["5:2"])
            off_ok = off == {"5:2": mf.lineage_truth("5:2")}
        except Exception as e:  # noqa: BLE001
            off, off_ok = {}, False
        fb = lin_calls(t0)
        chk("(g) an id outside the mirror (a page it does not hold) falls back to Figma: one files?ids= read and "
            "one nodes?ids=<ancestors>&depth=1 read, and the answer equals the file's",
            off_ok and len(fb) == 2 and any("/v1/files/MIR?ids=5:2" in e[2] for e in fb)
            and any("depth=1" in e[2] and "/nodes" in e[2] for e in fb), f"{len(fb)} call(s): {[e[2] for e in fb]}")

        ipath = os.path.join(menv["FIGMA_MIRROR_DIR"], "MIR", "index.json")
        planted = None
        try:
            idx_now = json.load(open(ipath, encoding="utf-8"))
            c41 = next(c for c, e in idx_now.items() if "4:1" in e["nodes"])
            planted = json.loads(json.dumps(idx_now))
            planted[c41]["nodes"]["4:1"][0] = "4:2"          # 4:1 claims the instance beside it as its parent
            _write_json(ipath, planted)
            mark = log_mark()
            t0 = time.time()
            wrong = lin(["4:1"])
            miss_lines = [r for r in calls_since(mark) if r.get("status") == "mirror-miss" and "4:1" in r.get("endpoint", "")]
            ptr_ok = (wrong == {"4:1": mf.lineage_truth("4:1")} and miss_lines
                      and any("/v1/files/MIR?ids=4:1" in e[2] for e in lin_calls(t0)))
            ptr_detail = f"chain {[e['id'] for e in (wrong.get('4:1') or [])]}; {len(miss_lines)} mirror-miss line(s)"
        except Exception as e:  # noqa: BLE001
            ptr_ok, ptr_detail = False, f"raised {type(e).__name__}: {e}"
        finally:
            if planted is not None:
                _write_json(ipath, idx_now)
        chk("(h) a planted wrong parent pointer in the index is caught against the chunk body: the read is asked "
            "of Figma and logged as a mirror-miss, never answered from the pointer", ptr_ok, ptr_detail)

        mf.edit("4:6")
        mf.edit("3:5")
        t0 = time.time()
        try:
            fresh = lin(["4:6"])
            fresh_ok = fresh == {"4:6": mf.lineage_truth("4:6")}
        except Exception as e:  # noqa: BLE001
            fresh, fresh_ok = {}, False
        refreshed = [e for e in lin_calls(t0) if "/nodes" in e[2] and "3:5" in e[2] and "geometry=paths" in e[2]]
        whole = [e for e in lin_calls(t0) if "/v1/files/MIR?" in e[2]]
        shell_reads = [e for e in lin_calls(t0) if "/nodes" in e[2] and "depth=1" in e[2] and "geometry" not in e[2]]
        chk("(i) after an edit inside a chunk (a text and the chunk's root renamed), lineage refreshes that chunk "
            "and answers the edit, never the stale copy; the pages and sections above it take one nodes?depth=1 "
            "read and no whole-file read",
            fresh_ok and refreshed and not whole and len(shell_reads) == 1,
            f"name {((fresh.get('4:6') or [{}] * 4)[-2] or {}).get('name')!r} vs {mf.find('3:5')['name']!r}; "
            f"{len(refreshed)} refresh call(s) of 3:5; shells {[e[2] for e in shell_reads]}; whole-file {len(whole)}")

        # (j) a frame moved to another section after the outline was read: its stale chain is caught
        s21, s22 = mf.find("2:1"), mf.find("2:2")
        moved = next(c for c in s21["children"] if c["id"] == "3:2")
        s21["children"].remove(moved)
        s22["children"].insert(0, moved)
        mf.edit("4:4")
        mark = log_mark()
        t0 = time.time()
        try:
            mv = lin(["4:3", "4:1"])
            mv_bad = [i for i in mv if mv[i] != mf.lineage_truth(i)]
        except Exception as e:  # noqa: BLE001
            mv, mv_bad = {}, [f"raised {type(e).__name__}: {e}"]
        mv_calls = [e[2] for e in lin_calls(t0)]
        mv_miss = [r for r in calls_since(mark) if r.get("status") == "mirror-miss" and "4:3" in r.get("endpoint", "")]
        chk("(j) a frame moved to another section since the outline was read: the stale chain is caught against "
            "the section's current kids and that id is asked of Figma; a node that did not move is still answered "
            "from the mirror",
            not mv_bad and mv_miss and any("/v1/files/MIR?ids=4:3" in c for c in mv_calls)
            and not any("/v1/files/MIR?ids=" in c and "4:1" in c for c in mv_calls),
            f"differ: {mv_bad}; chain of 4:3 {[e['id'] for e in (mv.get('4:3') or [])]}; {len(mv_miss)} mirror-miss; "
            f"calls {mv_calls}")
        s22["children"].remove(moved)
        s21["children"].insert(1, moved)
        mf.edit("4:4")

        t0 = time.time()
        vgot = lin(["4:1"], version="100", kids=False)
        vcalls = lin_calls(t0)
        chk("lineage at a saved version asks Figma with version= (one files?ids= read without kids), never the mirror",
            vgot == {"4:1": mf.lineage_truth("4:1", kids=False)} and len(vcalls) == 1 and "version=100" in vcalls[0][2],
            f"{[e[2] for e in vcalls]}")

        p = subprocess.run([sys.executable, os.path.abspath(__file__), "lineage", "MIR", "4:1", "--no-kids",
                            "--lane", "gate", "--tool", "selftest-cli"], env=dict(env("mirror", 60), **{
                                k: v for k, v in menv.items() if k != "FIGMA_SELFTEST_BREAK"}),
                           capture_output=True, text=True, timeout=120)
        try:
            cli_lin = p.returncode == 0 and json.loads(p.stdout) == {"4:1": mf.lineage_truth("4:1", kids=False)}
        except ValueError:
            cli_lin = False
        chk("CLI: lineage prints lineage()'s JSON and exits 0", cli_lin, p.stderr.strip()[-200:])

        def nd(i, x, y, w, h, **kw):
            return dict({"id": i, "name": i, "type": kw.pop("type", "TEXT"),
                         "absoluteBoundingBox": {"x": x, "y": y, "width": w, "height": h}}, **kw)
        scr, band = nd("1:1", 0, 0, 390, 5000, type="FRAME"), nd("1:2", 0, 0, 390, 707, type="INSTANCE")
        sec = nd("9:1", -50, -50, 9000, 9000, type="SECTION")
        sec_kids = [nd("7:9", 300, 400, 30, 8), scr, nd("7:10", 300, 500, 26, 6), nd("7:11", 300, 520, 26, 6, visible=False),
                    nd("7:12", 8000, 8000, 300, 88)]
        chain = [dict(nd("0:1", 0, 0, 0, 0, type="CANVAS"), kids=[sec]), dict(sec, kids=sec_kids), scr]
        over = drawn_over(chain, band["absoluteBoundingBox"])     # the screen rendered, cut to the band
        hero = nd("2:2", 0, 0, 1440, 700, type="FRAME", clipsContent=True)
        mock_f = nd("2:3", 100, 600, 800, 400, type="FRAME")
        clip = [dict(nd("0:1", 0, 0, 0, 0, type="CANVAS"), kids=[nd("2:1", 0, 0, 1440, 5000, type="FRAME")]),
                dict(nd("2:1", 0, 0, 1440, 5000, type="FRAME"), kids=[hero, nd("2:4", 0, 700, 1440, 900, type="FRAME")]),
                dict(hero, kids=[mock_f]), mock_f]
        clipped = drawn_over(clip, mock_f["absoluteBoundingBox"])
        clip[2]["clipsContent"] = False
        unclipped = drawn_over(clip, mock_f["absoluteBoundingBox"])
        chk("drawn_over: the label on the section after the screen and over the band is listed, on the section; one "
            "under the screen, a hidden one and one beside it are not; None for no lineage",
            [o["id"] for o in over] == ["7:10"] and over[0]["on"] == "9:1" and drawn_over(None, (0, 0, 1, 1)) is None,
            f"got {over}")
        chk("drawn_over: a frame over only the part a clipping ancestor cuts off is not listed, and is once the "
            "ancestor does not clip", clipped == [] and [o["id"] for o in unclipped] == ["2:4"],
            f"clipped {clipped}, unclipped {unclipped}")

        mock.script["/v1/files/RLH/meta"] = [(429, 1, {"X-Figma-Rate-Limit-Type": "low",
                                                       "X-Figma-Plan-Tier": "enterprise"})]
        get("files/RLH/meta", lane="gate", tool="selftest-429")
        with open(log_path(), encoding="utf-8") as f:
            l429 = [json.loads(x) for x in f if '/v1/files/RLH/meta"' in x and '"status":429' in x]
        chk("log: a 429 line carries Figma's X-Figma-Rate-Limit-Type and X-Figma-Plan-Tier",
            l429 and l429[0].get("rate_limit_type") == "low" and l429[0].get("plan_tier") == "enterprise",
            {k: l429[0].get(k) for k in ("status", "retry_after", "rate_limit_type", "plan_tier")} if l429 else "no 429 line")
        for k in menv:
            os.environ.pop(k, None)

        # ---- CLI
        e = env("cache", 60)
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "get", "files/VK/meta", "--lane", "gate",
                            "--tool", "selftest-cli"], env=e, capture_output=True, text=True, timeout=60)
        try:
            cli_ok = p.returncode == 0 and json.loads(p.stdout)["file"]["version"] == "200"
        except (ValueError, KeyError):
            cli_ok = False
        chk("CLI: get prints the JSON body and exits 0", cli_ok, p.stderr.strip()[-200:])
        mock.forever["/v1/files/NOPE2/meta"] = (404, None)
        p404 = subprocess.run([sys.executable, os.path.abspath(__file__), "get", "files/NOPE2/meta", "--lane", "gate"],
                              env=e, capture_output=True, text=True, timeout=60)
        chk("CLI: a 404 exits 3 with the error body on stdout and HTTP 404 on stderr",
            p404.returncode == 3 and '"err"' in p404.stdout and "HTTP 404" in p404.stderr, p404.stderr.strip())

        # ---- the log
        lines = []
        for state in ("good", "lanes", "pause", "deadline", "misc", "cache"):
            try:
                with open(os.path.join(tmp, state, "calls.jsonl"), encoding="utf-8") as f:
                    lines += [json.loads(l) for l in f if l.strip()]
            except OSError:
                pass
        want = {"ts", "tool", "pid", "lane", "endpoint", "tier", "status", "retry_after", "cache_hit", "bytes", "ms"}
        raw_log = "".join(open(os.path.join(root, f), encoding="utf-8").read()
                          for root, _, fs in os.walk(tmp) for f in fs if f == "calls.jsonl")
        chk("log: every line has ts, tool, pid, lane, endpoint, tier, status, retry_after, cache_hit, bytes, ms",
            lines and all(want <= set(l) for l in lines), f"{len(lines)} lines")
        chk("log: holds 429s with their Retry-After and cache hits, and never the token",
            any(l["status"] == 429 and l["retry_after"] for l in lines) and any(l["cache_hit"] for l in lines)
            and DUMMY not in raw_log)

        # ---- --audit, on a planted tree
        plant = os.path.join(tmp, "plant")
        os.makedirs(plant)
        host = "api." + "figma.com"
        files = {
            "figma_client.py": f'BASE = "https://{host}"\n',
            "raw_url.py": f'import urllib.request\nurllib.request.urlopen("https://{host}/v1/me")\n',
            "raw_header.py": 'import os, urllib.request\nreq = urllib.request.Request(os.environ["B"], headers={"X-Figma-Token": "t"})\n',
            "raw_auth.py": "import figma_auth, urllib.request\nh = figma_auth.headers()\n",
            "raw.mjs": f"const r = await fetch(`https://{host}/v1/files/${{k}}/meta`);\n",
            "routed.py": 'import figma_client\nfigma_client.get("files/K/meta")\n',
            "routed.mjs": "spawnSync('python', ['tools/figma_client.py', 'get', p]);\n",
            "comment_only.py": f'"""Reads https://{host}/v1/files/K in prose."""\n# see https://{host}/v1/me\nx = 1\n',
            "fixture.py": f'PAGE = {{"next_page": "https://{host}/v1/files/K/versions?x=1"}}  # {ALLOW_MARK}\n',
            "nothing.py": "print('no Figma here')\n",
        }
        for fn, body in files.items():
            with open(os.path.join(plant, fn), "w", encoding="utf-8") as f:
                f.write(body)
        r = audit([plant])
        raw_files = sorted({os.path.basename(f) for f, _, _ in r["raw"]})
        chk("audit: a raw URL, a raw token header, figma_auth.headers() and a raw fetch in .mjs are each caught",
            raw_files == ["raw.mjs", "raw_auth.py", "raw_header.py", "raw_url.py"], str(raw_files))
        chk("audit: the client, comments, docstrings and a marked fixture are not; routed files count as routed",
            len(r["callers"]) == 6 and sorted(os.path.basename(f) for f in r["routed"]) == ["routed.mjs", "routed.py"]
            and len(r["allowed"]) == 1, f"{len(r['routed'])} of {len(r['callers'])}")
        for fn in ("raw_url.py", "raw_header.py", "raw_auth.py", "raw.mjs"):
            os.remove(os.path.join(plant, fn))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = audit_cli([plant])
        chk("audit: the clean tree exits 0 and reports 2 of 2", code == 0 and "2 of 2" in out.getvalue(),
            out.getvalue().strip().splitlines()[0])

        # ---- --audit: a tree read of a file outside lineage(), on a second planted tree
        trees = os.path.join(tmp, "trees")
        os.makedirs(trees)
        fc = "import figma_client\n"
        bad_trees = {
            "t_ids.py": fc + 'd = figma_client.get(f"files/{key}?ids={nid}")\n',
            "t_depth.py": fc + 'd = figma_client.get(f"/v1/files/{key}?depth=4")\n',
            "t_whole.py": fc + 'd = figma_client.get(f"/v1/files/{key}", timeout=600)\n',
            "t_plus.py": fc + 'q = f"/files/{file_key}" + (f"?depth={depth}" if depth else "")\n',
            "t_base.py": fc + 'd = figma_client.get(f"{API}?ids={part}&version={v}&depth=1000")\n',
            "t_query.py": fc + 'd = figma_client.get(f"{API}/files/{key}?{urllib.parse.urlencode(q)}")\n',
            "t_format.py": fc + 'FILES = "/v1/files/{}?ids={}"\n',
            "t_tpl.mjs": "import { figmaGet } from './lib/figma-rest.mjs';\n"
                         "const d = await figmaGet(`${API}/files/${FILE_KEY}?ids=${need.join(',')}&depth=6`);\n",
            "t_concat.mjs": "import { figmaGet } from './lib/figma-rest.mjs';\nconst d = await figmaGet('/v1/files/' + KEY);\n",
            "t_noreason.py": fc + f'd = figma_client.get(f"/v1/files/{{key}}")  # {TREE_MARK}\n',
        }
        good_trees = {
            "g_fine.py": fc + 'API = "/v1/files/" + FILE_KEY\n'
                              'a = figma_client.get(f"files/{key}/nodes?ids={x}&depth=1")\n'
                              'b = figma_client.get(f"{API}/nodes?depth=1&ids={x}")\n'
                              'c = figma_client.get(f"images/{key}?ids={x}&scale=2")\n'
                              'd = figma_client.lineage(key, ids)\n'
                              '# figma_client.get(f"files/{key}?ids={x}") in a comment\n',
            "g_kept.py": fc + f'd = figma_client.get(f"/v1/files/{{key}}", timeout=600)  # {TREE_MARK} the index needs every page\n',
        }
        for fn, body in dict(bad_trees, **good_trees).items():
            with open(os.path.join(trees, fn), "w", encoding="utf-8") as f:
                f.write(body)
        r = audit([trees])
        flagged = sorted({os.path.basename(f) for f, _, _ in r["tree"]})
        chk("audit: a files?ids=, a ?depth=, a whole-file read, a <BASE>?ids= read, a built query, a format string, "
            "two .mjs forms and a mark with no reason are each caught as a tree read",
            flagged == sorted(bad_trees), f"caught {flagged}")
        chk("audit: nodes?ids=, a base path, images?ids=, lineage() and a comment are not; a read marked with its "
            "reason is kept, with the reason",
            not any(os.path.basename(f).startswith("g_") for f, _, _ in r["tree"])
            and [(os.path.basename(f), why) for f, _, why in r["tree_kept"]] == [("g_kept.py", "the index needs every page")],
            f"kept {r['tree_kept']}")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code_bad = audit_cli([trees])
        for fn in bad_trees:
            os.remove(os.path.join(trees, fn))
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            code_good = audit_cli([trees])
        chk("audit: a tree read fails the audit (exit 1); the same tree with them taken out passes (exit 0)",
            code_bad == 1 and code_good == 0, f"exit {code_bad} then {code_good}")
    finally:
        mock.httpd.shutdown()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    missed = sum(not ok for _, ok in checks)
    print(f"figma_client selftest: {len(checks) - missed} of {len(checks)} checks pass. "
          f"BAD (raw urllib) {bad429} x 429; GOOD (through the client) {good429} x 429, {done} of 40 done.")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
