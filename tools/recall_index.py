#!/usr/bin/env python3
"""recall_index.py - the index recall.py's logs source reads, so a search no longer reads every log.

    python tools/recall_index.py update     bring it up to date with every log copy recall reads, now
    python tools/recall_index.py status     what it holds: copies, records, size on disk, last update
    python tools/recall_index.py check      every stored record already redacted, no sealed log held: N of M

WHY. Without it each recall call reads every log copy whole. With a few thousand sessions, backups
and zips that is tens of GB on every call, and the logs source is most of recall's time.

WHERE IT LIVES. One SQLite file, index.db, in `recall.index_dir` (kit_config), default
~/.cache/agent-workbench/recall_logs/. Keep that folder outside any folder a cloud drive, a backup or a
mirror job copies: the index is large (roughly half the size of the logs it holds) and holds the logs'
text, redacted. The lock, the background update's log and a corrupt index set aside sit beside it.
--index FILE names another file for one run.

WHAT IT HOLDS. For each log copy recall reads, its records, each stored once however many copies hold
it: the record as JSON with every key and string passed through cc_logs.redact (an image's or
document's data emptied first), zlib-compressed, and an FTS5 trigram index over its text. The text
is every string in the record, plus the keys and numbers inside a tool call's input, which is all
recall's matcher reads. A disk log is read on from the byte where the last update stopped while it
only grows; one that shrank, or whose bytes before that point changed, is read again whole. A copy
inside a zip is read once. A line still being written (no newline yet) is held until the next update
replaces it.

REDACTION. Nothing reaches the index before cc_logs.redact has passed over every key and string in
the record, so a credential pasted into a session is stored as [redacted], as recall prints it.
`check` proves it on what is stored: each record read back and redacted again must come out
unchanged. recall.py --selftest plants a token and finds it in no stored record, FTS row or byte of
the index file.

SEALS. A log a .recall-sealed marker holds (seals.py, its lifted state included) is never written
here, and a log a newer seal holds is deleted at the next update, its records with it unless an
unsealed copy holds them too.

WHAT IT IS NOT. It finds candidates, never hits: recall.py runs its own scan on each candidate record,
so every rule there (forms, roles, typed, headless, the date window, metadata) decides as before. A
term inside a string redact rewrote, such as a long mcp__ tool name or the value after "token", is
not in the index; recall.py sends a query it cannot answer here to the whole-log scan.

KEPT CURRENT by recall.py on each call (up to its CONFIG index_budget bytes, the rest read whole while
a background `update` catches up), and by `update` here. A missing or corrupt index makes recall read
every copy whole and say so in COVERAGE; a corrupt one is set aside and built again.

EXIT. update and status: 0 done, 2 the index could not be read (status). check: 0 every record and
copy passed, 1 any miss (the first one named).

BLIND TO: a copy that only a cloud placeholder holds, which is never read; a copy deleted from disk,
which stays in the index, never searched, until `update --rebuild`.
"""
import argparse, collections, fcntl, hashlib, io, json, os, sqlite3, subprocess, sys, time, zipfile, zlib

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
import cc_logs  # noqa: E402
import kit_config  # noqa: E402

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".cache", "agent-workbench", "recall_logs")


def index_dir():
    """kit_config recall.index_dir, else DEFAULT_DIR: a folder no cloud drive or mirror job copies."""
    d = kit_config.get("recall", "index_dir")
    return os.path.abspath(os.path.expanduser(d)) if isinstance(d, str) and d.strip() else DEFAULT_DIR


PATH = os.path.join(index_dir(), "index.db")
VERSION = "1"
# FTS5 layout. detail=full matches a term exactly but takes about 2.5 times the space of detail=none in
# 2,048-character rows overlapping by 512; the looser layout returns some extra candidates for a term,
# which recall's scan drops.
DETAIL = "none"     # FTS5 detail: no positions, so a term is looked up as all of its trigrams in one row
CHUNK = 2048        # body characters per FTS row; 0 = the whole body in one row
OVERLAP = 512       # characters each row repeats from the one before, so a match up to this long sits in one
CB = 16             # rowid = record id << CB | chunk number
HEAD = 4096         # bytes hashed at a disk copy's start and before its offset, to tell growth from a rewrite
MAX_ALT = 200       # an alternative longer than this is not looked up here (chunk overlap)
POOL_BYTES = 32 << 20   # an update reading more than this prepares records in a process pool


class Unusable(Exception):
    """The index cannot be read: recall scans every copy whole and says why."""


# ------------------------------------------------------------------------------------------- records

def _clean(o, red):
    """o with every key and string redacted, and an image's or document's base64 data emptied."""
    if isinstance(o, str):
        return red(o)
    if isinstance(o, dict):
        if o.get("type") in ("image", "document") and isinstance(o.get("source"), dict):
            o = dict(o, source={k: ("" if k == "data" else v) for k, v in o["source"].items()})
        return {red(k): _clean(v, red) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v, red) for v in o]
    return o


def _body(o, out, seen, inp=False):
    """The text recall's matcher can read in a record: every string, and inside a tool call's input
    (which it reads as JSON) the keys and numbers too. Each distinct string once."""
    if isinstance(o, str):
        if o and o not in seen:
            seen.add(o)
            out.append(o)
    elif isinstance(o, dict):
        tool = o.get("type") == "tool_use"
        for k, v in o.items():
            if inp and k not in seen:
                seen.add(k)
                out.append(k)
            _body(v, out, seen, inp or (tool and k == "input"))
    elif isinstance(o, list):
        for v in o:
            _body(v, out, seen, inp)
    elif inp and isinstance(o, (int, float)) and not isinstance(o, bool):
        s = str(o)
        if s not in seen:
            seen.add(s)
            out.append(s)


def _chunks(text):
    if not CHUNK or len(text) <= CHUNK:
        return [text]
    out, i = [], 0
    while i + CHUNK < len(text) and len(out) < (1 << CB) - 1:
        out.append(text[i:i + CHUNK])
        i += CHUNK - OVERLAP
    out.append(text[i:])
    return out


def _utf8(s):
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:  # a lone surrogate from a \ud83d escape: SQLite takes UTF-8 only
        return s.encode("utf-8", "replace").decode("utf-8")


def canonical(raw, red=True):
    """The record on a log line as the index stores it: (JSON bytes, decoded record), or None for a line
    that is not a JSON object. A line written with \\u escapes keeps them, so recall's byte prefilter
    sees what it would in the log. red=False only for recall's self-test, which breaks the rule on purpose."""
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    r = _clean(d, cc_logs.redact if red else (lambda s: s))
    try:
        s = json.dumps(r, ensure_ascii=raw.isascii(), separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        s = json.dumps(r, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return s, r


def prepare(raw, red=True):
    """(sha1, compressed JSON, FTS rows) for one log line, or None. The FTS rows are built from the
    redacted record, so neither store holds what redact removes."""
    got = canonical(raw, red)
    if got is None:
        return None
    s, r = got
    out = []
    _body(r, out, set())
    return hashlib.sha1(s).digest(), zlib.compress(s, 6), [_utf8(c) for c in _chunks("\n".join(out))]


# ------------------------------------------------------------------------------------------- storage

SCHEMA = f"""
create table if not exists meta(k text primary key, v text);
create table if not exists copy(id integer primary key, path text not null, member text not null,
    inner text not null, key text not null, size integer, crc integer, mtime integer, offset integer,
    done integer, lines integer, head blob, tail blob, unique(path, member, inner));
create table if not exists rec(id integer primary key, h blob unique not null, z blob not null, nch integer not null);
create table if not exists link(copy integer not null, n integer not null, rec integer not null,
    primary key(copy, n)) without rowid;
create index if not exists link_rec on link(rec);
create virtual table if not exists fts using fts5(b, content='', contentless_delete=1, detail={DETAIL},
    tokenize='trigram case_sensitive 0');
"""


def layout():
    return f"v{VERSION} detail={DETAIL} chunk={CHUNK}/{OVERLAP}"


def connect(path, create=True):
    """An open index, its schema made if it is new. Raises Unusable for a file SQLite cannot read or one
    built with another layout."""
    if not create and not os.path.exists(path):
        raise Unusable(f"no index at {path}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.execute("pragma journal_mode=wal")
        conn.execute("pragma synchronous=normal")
        if not conn.execute("select name from sqlite_master where name='meta'").fetchone():
            conn.executescript("begin immediate;" + SCHEMA + "insert or ignore into meta values ('layout', '%s');"
                               "commit;" % layout())
        got = (conn.execute("select v from meta where k='layout'").fetchone() or [""])[0]
        if got != layout():
            conn.close()
            raise Unusable(f"built as {got or 'unknown'}, this code reads {layout()}: `recall_index.py update "
                           f"--rebuild` makes it again")
        return conn
    except sqlite3.DatabaseError as e:
        raise Unusable(f"{type(e).__name__}: {e}") from e


class Lock:
    """The one writer: an flock beside the index. try_acquire() returns False at once when another
    process holds it."""

    def __init__(self, path):
        self.path = path + ".lock"
        self.fd = None

    def try_acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def held_elsewhere(self):
        if self.try_acquire():
            self.release()
            return False
        return True


def ident(c):
    return c.path, c.member or "", c.inner or ""


COLS = ("id", "path", "member", "inner", "key", "size", "crc", "mtime", "offset", "done", "lines", "head", "tail")


def rows(conn):
    """{(path, member, inner): row dict} for every stored copy."""
    return {(r[1], r[2], r[3]): dict(zip(COLS, r)) for r in conn.execute(f"select {', '.join(COLS)} from copy")}


def rows_where(conn, k):
    r = conn.execute(f"select {', '.join(COLS)} from copy where path=? and member=? and inner=?", k).fetchone()
    return dict(zip(COLS, r)) if r else None


def _drop_recs(conn, recs):
    """Delete the records in recs that no copy links any more, and their FTS rows."""
    gone = [r for r in set(recs) if not conn.execute("select 1 from link where rec=? limit 1", (r,)).fetchone()]
    for r in gone:
        nch = conn.execute("select nch from rec where id=?", (r,)).fetchone()
        if nch:
            conn.executemany("delete from fts where rowid=?", [((r << CB) | k,) for k in range(nch[0])])
            conn.execute("delete from rec where id=?", (r,))
    return len(gone)


def drop_copy(conn, cid):
    recs = [r for (r,) in conn.execute("select rec from link where copy=?", (cid,))]
    conn.execute("delete from link where copy=?", (cid,))
    conn.execute("delete from copy where id=?", (cid,))
    return _drop_recs(conn, recs)


# ---------------------------------------------------------------------------------------------- sync

def _hash_at(fh, start, n):
    fh.seek(max(0, start))
    return hashlib.sha1(fh.read(n)).digest()


def _marks(data_head, fh, offset):
    """(head hash, tail hash) for a disk copy read to offset."""
    return hashlib.sha1(data_head[:min(HEAD, offset)]).digest(), _hash_at(fh, offset - min(HEAD, offset), min(HEAD, offset))


def _why(e):
    return cc_logs.VANISHED if isinstance(e, FileNotFoundError) else f"{type(e).__name__}: {e}"[:200]


def read_copy(t, start, red=True):
    """Read one copy from byte start and prepare its lines. t: a plain cc_logs.Copy tuple. Runs in a pool
    worker for a big update. -> dict with the prepared lines [(n offset from the first line read, prep)],
    or {"error"}."""
    c = cc_logs.Copy(*t)
    try:
        if c.via == "disk":
            with open(c.path, "rb") as fh:
                st = os.fstat(fh.fileno())
                head = fh.read(HEAD)
                fh.seek(start)
                data = fh.read()
                end = start + len(data)
                cut = data.rfind(b"\n") + 1
                offset = start + cut
                hd, tl = _marks(head, fh, offset)
            body, tail = data[:cut], data[cut:]
            out = {"size": end, "mtime": st.st_mtime_ns, "crc": None, "offset": offset, "head": hd, "tail": tl}
        else:
            with zipfile.ZipFile(c.path) as zf:
                if c.via == "zip":
                    data = zf.read(c.member)
                else:
                    with zipfile.ZipFile(io.BytesIO(zf.read(c.member))) as inner:
                        data = inner.read(c.inner)
            body, tail = data, b""
            out = {"size": c.size, "mtime": None, "crc": c.crc, "offset": len(data), "head": None, "tail": None}
    except Exception as e:
        return {"error": _why(e)}
    lines = body.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    prepped = []
    for i, ln in enumerate(lines):
        if ln.strip():
            p = prepare(ln, red)
            if p:
                prepped.append((i, p))
    out["done"] = len(lines)
    out["lines"] = len(lines) + (1 if tail else 0)
    if tail.strip():
        p = prepare(tail, red)
        if p:
            prepped.append((len(lines), p))
    out["prepped"] = prepped
    out["bytes"] = len(data)
    return out


def _store(conn, c, row, start_n, got, full):
    """Write one copy's read into the index, in one transaction."""
    conn.execute("begin immediate")
    try:
        if row:
            cid = row["id"]
            old = [r for (r,) in conn.execute("select rec from link where copy=? and n>=?", (cid, 0 if full else row["done"]))]
            conn.execute("delete from link where copy=? and n>=?", (cid, 0 if full else row["done"]))
        else:
            cid = conn.execute("insert into copy(path, member, inner, key) values (?,?,?,?)",
                               ident(c) + (c.key,)).lastrowid
            old = []
        links = []
        for i, (h, z, chs) in got["prepped"]:
            hit = conn.execute("select id from rec where h=?", (h,)).fetchone()
            if hit:
                rid = hit[0]
            else:
                rid = conn.execute("insert into rec(h, z, nch) values (?,?,?)", (h, z, len(chs))).lastrowid
                conn.executemany("insert into fts(rowid, b) values (?,?)", [((rid << CB) | k, ch) for k, ch in enumerate(chs)])
            links.append((cid, start_n + i, rid))
        conn.executemany("insert or replace into link values (?,?,?)", links)
        conn.execute("update copy set key=?, size=?, crc=?, mtime=?, offset=?, done=?, lines=?, head=?, tail=? where id=?",
                     (c.key, got["size"], got["crc"], got["mtime"], got["offset"], start_n + got["done"],
                      start_n + got["lines"], got["head"], got["tail"], cid))
        _drop_recs(conn, old)
        conn.execute("commit")
    except BaseException:
        conn.execute("rollback")
        raise
    return cid


def plan(conn, copies):
    """-> ({ident: ("ok", row) | ("error", why)}, [(copy, row, start, start_n, full, pending bytes)])."""
    have = rows(conn)
    status, todo, zips = {}, [], {}
    for c in copies:
        k = ident(c)
        row = have.get(k)
        if c.via == "disk":
            try:
                st = os.stat(c.path)
            except OSError as e:
                status[k] = ("error", _why(e))
                continue
            if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime_ns:
                status[k] = ("ok", row)
                continue
            start, full = 0, True
            if row and row["offset"] and st.st_size >= row["offset"]:
                try:
                    with open(c.path, "rb") as fh:
                        n = min(HEAD, row["offset"])
                        same = (hashlib.sha1(fh.read(n)).digest() == row["head"]
                                and _hash_at(fh, row["offset"] - n, n) == row["tail"])
                except OSError as e:
                    status[k] = ("error", _why(e))
                    continue
                if same:
                    start, full = row["offset"], False
            todo.append((c, row, start, 0 if full else row["done"], full, st.st_size - start))
        else:
            if c.path not in zips:
                try:
                    os.stat(c.path)
                    zips[c.path] = None
                except OSError as e:
                    zips[c.path] = _why(e)
            if zips[c.path]:
                status[k] = ("error", zips[c.path])
            elif row and row["size"] == c.size and row["crc"] == c.crc:
                status[k] = ("ok", row)
            else:
                todo.append((c, row, 0, 0, True, c.size))
    return status, todo


def sync(conn, copies, budget=None, red=True, workers=1, log=None):
    """Bring the index up to date for copies, the smallest change first, until budget bytes are read
    (None: no limit). -> ({ident: ("ok", row) | ("error", why) | ("pending", None)}, stats)."""
    t0 = time.time()
    status, todo = plan(conn, copies)
    todo.sort(key=lambda w: w[5])
    take, used = [], 0
    for w in todo:
        if budget is not None and used + w[5] > budget:
            status[ident(w[0])] = ("pending", None)
            continue
        take.append(w)
        used += w[5]
    stats = {"copies": 0, "bytes": 0, "records": 0, "pending": sum(1 for s in status.values() if s[0] == "pending"),
             "pending_bytes": sum(w[5] for w in todo if status.get(ident(w[0]), ("",))[0] == "pending")}

    def done(w, got):
        c, row, start, start_n, full, _ = w
        if "error" in got:
            status[ident(c)] = ("error", got["error"])
            return
        _store(conn, c, row, start_n, got, full)
        status[ident(c)] = ("ok", rows_where(conn, ident(c)))
        stats["copies"] += 1
        stats["bytes"] += got["bytes"]
        stats["records"] += len(got["prepped"])
        if log and stats["copies"] % 200 == 0:
            log(f"{stats['copies']} of {len(take)} copies, {stats['bytes'] / 1e9:.2f} GB, {time.time() - t0:.0f}s")

    if workers > 1 and len(take) > 1 and used > POOL_BYTES:   # a pool costs about a second to start
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as ex:
            ahead = collections.deque()   # at most 2 per worker in flight, so prepared copies don't pile up in memory
            for w in take:
                ahead.append((w, ex.submit(read_copy, tuple(w[0]), w[2], red)))
                if len(ahead) >= 2 * workers:
                    done(*(lambda w, f: (w, f.result()))(*ahead.popleft()))
            while ahead:
                done(*(lambda w, f: (w, f.result()))(*ahead.popleft()))
    else:
        for w in take:
            done(w, read_copy(tuple(w[0]), w[2], red))
    stats["seconds"] = time.time() - t0
    return status, stats


def purge(conn, held_keys):
    """Delete every stored copy of a log a seal holds. -> (copies, records) deleted."""
    gone = [(cid,) for cid, key in conn.execute("select id, key from copy") if key in held_keys]
    recs = 0
    if gone:
        conn.execute("begin immediate")
        try:
            for (cid,) in gone:
                recs += drop_copy(conn, cid)
            conn.execute("commit")
        except BaseException:
            conn.execute("rollback")
            raise
    return len(gone), recs


def stored_copies(conn):
    """Every stored copy as a cc_logs.Copy, for the seal check."""
    out = []
    for path, member, inner, key, size, crc in conn.execute("select path, member, inner, key, size, crc from copy"):
        kind = cc_logs.shape(inner or member or path)
        out.append(cc_logs.Copy(kind[0] if kind else "main", key, size, crc,
                                "disk" if not member else ("nested" if inner else "zip"), path, member or None,
                                inner or None))
    return out


def sealed_keys(inv, conn, seals):
    """The seals with lifted ones held, and the keys of every log they hold among the inventory's copies
    and the index's own (a log gone from disk since it was stored is checked too)."""
    import seals as sealing
    held = [dict(s, state="active") if s["state"] == "lifted" else s for s in seals]
    logs = collections.defaultdict(list)
    for k, cs in inv.logs.items():
        logs[k] += cs
    for c in stored_copies(conn):
        logs[c.key].append(c)
    view = argparse.Namespace(logs=logs)
    every = [c for cs in logs.values() for c in cs]
    keep = {c.key for c in sealing.hold_logs(view, every, held)[0]}
    return held, {c.key for c in every} - keep


# --------------------------------------------------------------------------------------------- query

def _trigrams(term):
    return sorted({term[i:i + 3] for i in range(len(term) - 2)})


def _q(s):
    return '"' + s.replace('"', '""') + '"'


def term_expr(term):
    """MATCH text for records holding term, any case; None for a term under 3 characters."""
    if len(term) < 3:
        return None
    if DETAIL == "full":
        return _q(term)
    return "(" + " AND ".join(_q(t) for t in _trigrams(term)) + ")"


def all_of(terms):
    """MATCH text for records holding every term; None when no term can be looked up (each under 3)."""
    parts = [e for e in (term_expr(t) for t in terms) if e]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")"


def lookup(conn, exprs, cids):
    """The stored records of copies cids that match any of exprs, compressed (zlib.decompress gives the line).
    -> ({copy id: [(n, compressed line)]}, records)."""
    ids = set()
    for e in exprs:
        ids.update(r >> CB for (r,) in conn.execute("select rowid from fts where fts match ?", (e,)))
    out = collections.defaultdict(list)
    if not ids:
        return out, 0
    arr = json.dumps(sorted(ids))
    links = collections.defaultdict(list)
    for cid, n, rec in conn.execute("select copy, n, rec from link where rec in (select value from json_each(?))", (arr,)):
        if cid in cids:
            links[rec].append((cid, n))
    want = json.dumps(sorted(links))
    for rec, z in conn.execute("select id, z from rec where id in (select value from json_each(?))", (want,)):
        for cid, n in links[rec]:
            out[cid].append((n, z))
    for v in out.values():
        v.sort()
    return out, len(links)


# ---------------------------------------------------------------------------------------------- spawn

def spawn_update(path):
    """Start `update` in the background, detached, unless a writer holds the lock. -> True if started."""
    if Lock(path).held_elsewhere():
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".update.log", "ab") as log:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "update", "--index", path],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    return True


# ------------------------------------------------------------------------------------------------ CLI

def size_on_disk(path):
    return sum(os.path.getsize(path + ext) for ext in ("", "-wal", "-shm") if os.path.exists(path + ext))


def update(path, workers, rebuild=False, quiet=False):
    import recall
    say = (lambda m: None) if quiet else (lambda m: print(m, flush=True))
    lock = Lock(path)
    if not lock.try_acquire():
        say("another update holds the index; nothing done")
        return 0
    try:
        if rebuild and os.path.exists(path):   # a cache, built from the logs: nothing in it is held anywhere else
            say(f"--rebuild: deleting {path} ({size_on_disk(path) / 1e9:.2f} GB) to build it again")
            for ext in ("", "-wal", "-shm"):
                if os.path.exists(path + ext):
                    os.remove(path + ext)
        try:
            conn = connect(path)
        except Unusable as e:
            say(f"index unusable ({e}); moving it aside and building again")
            for ext in ("", "-wal", "-shm"):
                if os.path.exists(path + ext):
                    os.replace(path + ext, path + ".corrupt" + ext)
            conn = connect(path)
        t0 = time.time()
        opts = argparse.Namespace(include_sealed=False, refresh=False)
        opts.sealed = recall.find_seals(opts)
        seals = opts.sealed["seals"]
        inv, copies, (kept, _) = recall._inventory(opts, seals)
        held, held_keys = sealed_keys(inv, conn, seals)
        n, r = purge(conn, held_keys)
        import seals as sealing
        indexable = sealing.hold_logs(inv, copies, held)[0]
        say(f"inventory and seals {time.time() - t0:.0f}s: {len(indexable)} copies to index, "
            f"{sum(h.get('log copies', 0) for h in kept.values())} sealed copies left out; a newer seal's log "
            f"deleted from it: {n} copies, {r} records")
        status, st = sync(conn, indexable, None, True, workers, say)
        bad = [(k, s[1]) for k, s in status.items() if s[0] == "error"]
        say(f"updated {st['copies']} copies, {st['bytes'] / 1e9:.2f} GB, {st['records']:,} records, in "
            f"{st['seconds']:.0f}s; {len(bad)} could not be read" + (f", first {bad[0][0][0]}: {bad[0][1]}" if bad else ""))
        conn.execute("insert or replace into meta values ('updated', ?)", (time.strftime("%Y-%m-%dT%H:%M:%S"),))
        conn.close()
        say(f"index {path}: {size_on_disk(path) / 1e9:.2f} GB on disk")
        return 0
    finally:
        lock.release()


def status_cmd(path):
    try:
        conn = connect(path, create=False)
    except Unusable as e:
        print(f"index unusable: {e}")
        return 2
    n_copy, n_rec = (conn.execute(f"select count(*) from {t}").fetchone()[0] for t in ("copy", "rec"))
    n_link = conn.execute("select count(*) from link").fetchone()[0]
    upd = (conn.execute("select v from meta where k='updated'").fetchone() or ["never"])[0]
    print(f"index {path}: {layout()}; {n_copy:,} copies, {n_link:,} lines, {n_rec:,} distinct records; "
          f"{size_on_disk(path) / 1e9:.2f} GB on disk; last full update {upd}")
    return 0


def check_cmd(path):
    """Every stored record unchanged by redact (so it holds no token redact knows), and no copy of a sealed
    log stored. Evidence: N of M."""
    import recall
    try:
        conn = connect(path, create=False)
    except Unusable as e:
        print(f"check DID NOT RUN: index unusable: {e}")
        return 2
    n = bad = 0
    first = None
    for rid, z in conn.execute("select id, z from rec"):
        n += 1
        s = zlib.decompress(z)
        got = canonical(s)
        if got is None or got[0] != s:
            bad += 1
            first = first or rid
    print(f"records: {n - bad:,} of {n:,} unchanged by a second redact" + (f"; first not: record {first}" if bad else ""))
    opts = argparse.Namespace(include_sealed=False, refresh=False)
    seals = recall.find_seals(opts)["seals"]
    inv = cc_logs.Inventory(recall.CONFIG["root"], cache=recall.CONFIG["cache"])
    _, held_keys = sealed_keys(inv, conn, seals)
    stored = [k for (k,) in conn.execute("select key from copy")]
    hit = [k for k in stored if k in held_keys]
    print(f"copies: {len(stored) - len(hit):,} of {len(stored):,} stored copies belong to no sealed log"
          f" ({len(held_keys)} sealed logs, {len(seals)} seals)" + (f"; first sealed: {hit[0]}" if hit else ""))
    return 0 if not bad and not hit else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("update", "status", "check"))
    ap.add_argument("--index", default=PATH, help=f"the index file (default {PATH.replace(os.path.expanduser('~'), '~')})")
    ap.add_argument("--workers", type=int, default=min(os.cpu_count() or 4, 12))
    ap.add_argument("--rebuild", action="store_true", help="update: delete the index and build it again")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if a.cmd == "update":
        return update(a.index, a.workers, a.rebuild, a.quiet)
    if a.cmd == "status":
        return status_cmd(a.index)
    return check_cmd(a.index)


if __name__ == "__main__":
    sys.exit(main())
