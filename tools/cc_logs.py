#!/usr/bin/env python3
"""cc_logs.py - every Claude Code log this machine holds, on disk or inside a backup zip, one entry per log.

    python tools/cc_logs.py               coverage: logs, copies, bytes, where they sit, what was skipped
    python tools/cc_logs.py --list        one line per log: key, kind, copies, the copies that get read
    python tools/cc_logs.py --refresh     re-read every zip listing and checksum instead of the cache
    python tools/cc_logs.py --selftest    planted tree, then the same tree with each rule broken

recall.py and log_extract.py read logs through this. Nothing else should keep its own folder list.

WHY THIS EXISTS. A search that reads only ~/.claude/projects reads a fraction of the record once logs
have been archived, copied off another machine or backed up into zips; some exist only inside a zip.
A search of two folders that prints "no hits" is reporting on part of the record and saying it is all.

WHERE IT LOOKS. All of HOME (`logs.root` in kit_config, default ~), pruning only the folders named
node_modules, .git, .venv and site-packages (see PRUNE; COVERAGE names them).
`logs.backups` (optional, a list of folders) names where your backups sit; copies there are labelled
"backup" in the coverage line. A backup folder must sit under the root to be walked.

WHAT COUNTS AS A LOG, by path shape:
  main       <uuid>.jsonl                          a session
  subagent   <uuid>/subagents/**/*.jsonl           its subagents and workflow agents
  history    history.jsonl under a .claude folder  every prompt typed, all projects
  vscode     chatSessions/<uuid>.jsonl             VS Code chat sessions (kind/v records, not CC)
  headless   any other .jsonl whose first record   a `claude -p --output-format stream-json` capture,
             is {"type": "system", "session_id"}   keyed headless/<session>.jsonl
Every other .jsonl is counted as skipped, by folder, so a new shape shows up as a number, not a gap.
A capture is the only copy of an eval or batch run made with --no-session-persistence. One whose run
also kept a session log here is not read, since that log holds the same conversation. The first line
of each skipped .jsonl is read once per size and mtime to tell (the cache's "sniff"); a cloud
placeholder is never opened. A capture inside a zip is not recognised.
Zips are listed member by member, and a zip inside a zip is opened too: backup layouts are not fixed.

COPIES. The same log sits in several places (live folder, archive, backup zips). Copies with the same
size and CRC-32 are one copy. Every copy that differs in either is read, because a resumed or
compacted session can rewrite records. Readers dedup records by `uuid`. Disk checksums and zip
listings are cached in ~/.cache/cc_logs_index.json against size and mtime: paths, numbers and hashes,
no log text. A disk entry holds the SHA-1 of the log's first and last 4 KB too, so a log that only
grew (longer, those bytes unchanged) is checksummed on from where the last run stopped. The first
run reads every byte once; after that only changed files. The same file holds
the folder index (see _find): each folder's ctime, subfolders and log and zip names, so a run lists
again only the folders that changed. A file a run did not reach (its folder not searched or not
opened) keeps its cached entry while it exists: dropping it would make the next run, after one slow
walk of a cloud folder, checksum every backup copy again.

SKIPPED ON PURPOSE. The OneDrive app's own cache under `Library/Group Containers` mirrors the
OneDrive folder and holds cloud-only placeholders (no blocks on disk); it is counted, not read. A
zip with no blocks anywhere else is NOT skipped quietly: it is listed under DID NOT READ.

A CLOUD PLACEHOLDER ANYWHERE (flagged dataless, or a non-empty file with no blocks; OneDrive, iCloud
and other file providers make them) is never opened, checksummed or listed: reading one makes the
provider download it first, which can stall a run for many minutes. A placeholder checksummed while
it was local keeps its cached checksum; one identical to a copy that is read costs nothing. Every
other placeholder (no checksum on record, or bytes no read copy has, as a backup taken before a log
was rewritten can hold) is DID NOT READ, on one line with the count, and recall exits 2. Size is no
proof of a copy held: a log's backup can be larger than the live file.

A ZIP THAT WILL NOT OPEN. A truncated zip has no central directory, so zipfile cannot list it. Its
local file headers are walked instead, in order (a stored member with a data descriptor ends where a
descriptor states the distance back to its data). The walk is cached like a listing. The zip prints
once as UNREADABLE, SKIPPED when every log inside has an identical copy elsewhere and the member cut
off at the end is not a log or a zip and holds no log or zip header in what remains. Otherwise it is
listed under DID NOT READ, and recall exits 2. A cut-off download in the Trash is the usual case.

A NESTED ZIP THAT WILL NOT OPEN puts its outer zip under DID NOT READ, and recall exits 2; the other
logs in the outer zip are still read. A member named .zip whose bytes hold no zip signature at all is
not an archive and cannot hold a log (macOS `__MACOSX/._*.zip` resource forks, Windows Recycle Bin
`$I` records). It prints as NOT A ZIP, SKIPPED, and `--list` names each one. So is a file on disk
named .zip with no zip signature anywhere in it; a file with one anywhere is walked as above.

A FILE THAT GOES AWAY after the walk lists it and before it is read is listed under DID NOT READ as
gone, and the rest are still read; recall exits 2, as the logs in it were not. Another session
moving a backup folder mid-run must not take the whole logs source down.

A FILE THAT CANNOT BE READ (any OSError but gone, such as a file-provider read that times out) is
listed under DID NOT READ with the error, and the rest are still read; recall exits 2.

A FOLDER THAT GOES AWAY while find walks it (find prints No such file or directory) is looked up
in the last run's cache. It is a gap, named with the count, and recall exits 2, when a log it held
then has no identical copy found this run, or when there is no cache to say what it held. One that
held no log, or whose logs all turned up elsewhere (moved within the root and walked there), prints
one line and leaves the exit alone. Otherwise a folder of logs moved during the walk could give a
clean answer.

HELPERS for other tools: project_dir(cwd) is ~/.claude/projects/<the folder Claude Code keeps for
cwd>; memory_dir(arg, cwd) is arg, else that folder's memory/.
"""
import argparse, collections, contextlib, errno, hashlib, io, json, os, re, struct, subprocess, sys, tempfile, time, zipfile, zlib

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

HOME = os.path.expanduser("~")
CACHE = os.path.join(HOME, ".cache", "cc_logs_index.json")
MIRROR = "Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite/"   # the OneDrive app's own cache
U = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SHAPES = (("vscode", re.compile(r"(?:^|/)(?:chatSessions|emptyWindowChatSessions)/(" + U + r"\.jsonl)$")),
          ("subagent", re.compile(r"(?:^|/)(" + U + r"/subagents/.+\.jsonl)$")),
          ("main", re.compile(r"(?:^|/)(" + U + r"\.jsonl)$")),
          ("history", re.compile(r"(?:^|/)\.claude[^/]*/(history\.jsonl)$")))
SHAPE_NEEDS = {"vscode": "hatSessions/", "subagent": "/subagents/", "main": ".jsonl", "history": "history.jsonl"}
HEADLESS = "headless/"                    # key prefix of a claude -p capture (see the docstring)
PREFER = {"disk": 0, "zip": 1, "nested": 2}

# Output redaction, three rulesets. Logs hold pasted PATs and keys in plain text.
# 1. Shapes: Figma, Anthropic, GitHub, Slack, AWS prefixes, JWTs; any 32+ word-character run (ADO PATs
#    are 52) except a full git SHA.
# 2. Base64 runs of 40+ with + or /, mixed case and a digit, at 4.5+ bits per character. A path or a
#    hex SHA stays under that; a random key sits near 5.3.
# 3. Context: the value after token/secret/password/pat/key/authorization, if 12+ characters.
SECRET = re.compile(r"\bfigd_[\w\-]+|\bsk-[\w\-]{20,}|\bgh[pousr]_\w{20,}|\bxox[abp]-[\w\-]{10,}|\bAKIA[0-9A-Z]{16}"
                    r"|\beyJ[\w\-]{10,}\.[\w\-]{10,}\.[\w\-]{10,}|\b(?![0-9a-f]{40}\b)(?![0-9a-f]{64}\b)\w{32,}\b")
B64 = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
SECRET_CTX = re.compile(r"(?i)((?:token|secret|password|passwd|\bpat|api[_-]?key|authorization|bearer|basic)"
                        r"[\"']?\s*[:=]?\s*[\"']?)([^\s\"',;]{12,})")


def _bits(s):
    n = len(s)
    return -sum(c / n * __import__("math").log2(c / n) for c in collections.Counter(s).values())


def _b64(m):
    s = m.group(0)
    mixed = all(re.search(p, s) for p in (r"[+/]", r"\d", r"[A-Z]", r"[a-z]"))
    return "[redacted]" if mixed and _bits(s) >= 4.5 else s


def redact(s):
    s = B64.sub(_b64, SECRET.sub("[redacted]", s))
    return SECRET_CTX.sub(lambda m: m.group(1) + "[redacted]", s)


def _setting(*keys):
    """A kit_config value, or None when kit_config is not there or the key is unset."""
    try:
        import kit_config
        return kit_config.get(*keys)
    except Exception:
        return None


def default_root():
    """The folder walked for logs: kit_config logs.root, else HOME."""
    r = _setting("logs", "root")
    return os.path.abspath(os.path.expanduser(r)) if isinstance(r, str) and r else HOME


def backup_dirs():
    """kit_config logs.backups as absolute paths (a folder or a list of folders); [] when unset."""
    b = _setting("logs", "backups") or []
    b = [b] if isinstance(b, str) else b
    return [os.path.abspath(os.path.expanduser(x)).rstrip(os.sep) for x in b if isinstance(x, str) and x]


def where(path, backups=None):
    """Short label for the place a copy sits: live (~/.claude), backup (a logs.backups folder), cloud
    (~/Library/CloudStorage), other."""
    if path.startswith(os.path.join(HOME, ".claude") + os.sep):
        return "live"
    for b in backup_dirs() if backups is None else backups:
        if path == b or path.startswith(b + os.sep):
            return "backup"
    if path.startswith(os.path.join(HOME, "Library", "CloudStorage") + os.sep):
        return "cloud"
    return "other"


def project_slug(cwd):
    """The folder name Claude Code keeps a project's logs and memory under: the absolute path with every
    character that is not a letter or digit replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(os.path.expanduser(cwd)))


def project_dir(cwd=None):
    """~/.claude/projects/<slug> for cwd, else $CLAUDE_PROJECT_DIR, else the working folder."""
    cwd = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return os.path.join(HOME, ".claude", "projects", project_slug(cwd))


def memory_dir(arg=None, cwd=None):
    """The memory folder: arg when given, else the current project's ~/.claude/projects/<slug>/memory."""
    if arg:
        return os.path.abspath(os.path.expanduser(arg))
    return os.path.join(project_dir(cwd), "memory")


def shape(path):
    """(kind, key) for a log path, or None. A shape's search runs only on a path holding the text every match
    of it holds (SHAPE_NEEDS), which cuts the cost several times over on tens of thousands of paths."""
    path = path.replace("\\", "/")
    for kind, rx in SHAPES:
        m = SHAPE_NEEDS[kind] in path and rx.search(path)
        if m:
            return kind, ("vscode/" if kind == "vscode" else "") + m.group(1)
    return None


class Copy(collections.namedtuple("Copy", "kind key size crc via path member inner")):
    """One copy of one log. via: disk | zip | nested. member: path inside the zip; inner: inside a nested zip."""

    def label(self):
        tail = f" :: {self.member}" + (f" :: {self.inner}" if self.inner else "") if self.member else ""
        return self.path.replace(HOME, "~") + tail

    def open(self):
        """Binary file object over the copy's bytes."""
        if self.via == "disk":
            return open(self.path, "rb")
        zf = zipfile.ZipFile(self.path)
        if self.via == "zip":
            return zf.open(self.member)
        return zipfile.ZipFile(io.BytesIO(zf.read(self.member))).open(self.inner)


SF_DATALESS = 0x40000000   # macOS st_flags: a file-provider placeholder, its data not on disk


def _cloud_only(st):
    """True for a cloud placeholder: flagged dataless, or a non-empty file with no blocks on disk.
    Reading one makes the file provider download it first (see A CLOUD PLACEHOLDER ANYWHERE)."""
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS) or (st.st_blocks == 0 and st.st_size > 0)


def _headless(p, old, new):
    """The session_id if p is a `claude -p --output-format stream-json` capture (its first record is
    {"type": "system", "session_id": ...}), else None. Read once per size and mtime (the cache's "sniff");
    a cloud placeholder is never opened."""
    try:
        st = os.stat(p)
    except OSError:
        return None
    sig, hit = [st.st_size, st.st_mtime_ns], old.get(p)
    if hit and hit[:2] == sig:
        sid = hit[2]
    elif _cloud_only(st):
        return None
    else:
        sid = None
        try:
            with open(p, "rb") as fh:
                d = json.loads(fh.readline(1 << 16))
            if isinstance(d, dict) and d.get("type") == "system" and isinstance(d.get("session_id"), str):
                sid = d["session_id"]
        except (OSError, ValueError):
            pass
    new[p] = sig + [sid]
    return sid


def _crc_file(path):
    """(CRC-32 of the whole file, its marks)."""
    crc = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
        return crc, _marks(fh, fh.tell())


MARK = 4096   # bytes hashed at a log's start and before its end, so a log that only grew is told from one rewritten


def _marks(fh, size):
    """The SHA-1 of a file's first MARK bytes and of the MARK bytes before byte size, as hex."""
    n = min(MARK, size)
    fh.seek(0)
    head = fh.read(n)
    fh.seek(size - n)
    return hashlib.sha1(head).hexdigest() + hashlib.sha1(fh.read(n)).hexdigest()


def _crc_grown(path, hit, size):
    """(CRC-32 of the file, its marks) read on from where the cached entry hit stopped, when the file only
    grew since: longer, and its first and last MARK bytes then unchanged. Else None, and it is read whole.
    Without this, a live session log is read whole on every run."""
    if not hit or len(hit) < 4 or not hit[3] or size <= hit[0]:
        return None
    with open(path, "rb") as fh:
        if _marks(fh, hit[0]) != hit[3]:
            return None
        crc = hit[2]
        fh.seek(hit[0])
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
        return crc, _marks(fh, fh.tell())


LOCAL, CENTRAL, DESCRIPTOR, END = b"PK\x03\x04", b"PK\x01\x02", b"PK\x07\x08", b"PK\x05\x06"
CHUNK = 64 << 20
NOT_ZIP = "not a zip"


def _zip_signed(raw):
    """True when the bytes hold any zip header or end record, so they may be an archive."""
    return any(s in raw for s in (LOCAL, CENTRAL, END))


def _signed_file(path):
    """_zip_signed over a whole file, CHUNK bytes at a time, overlapped so a signature across two reads counts."""
    with open(path, "rb") as fh:
        tail = b""
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            if _zip_signed(tail + chunk):
                return True
            tail = chunk[-3:]
    return False


def _list_nested(name, read):
    """The logs in one nested zip, and None or the reason it could not be listed. read() returns its
    bytes. The reason is NOT_ZIP when the bytes hold no zip signature: a file that only ends in .zip."""
    try:
        raw = read()
    except Exception as e:
        return [], type(e).__name__
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as inner:
            return [(name, j.file_size, j.CRC, j.filename.replace("\\", "/"))
                    for j in inner.infolist() if j.filename.endswith(".jsonl")], None
    except Exception as e:
        return [], type(e).__name__ if _zip_signed(raw) else NOT_ZIP


def _list_zip(path):
    """[(name, size, crc, inner_name|None)], nested_zips, [[nested name, reason it could not be listed]]."""
    out, nested, bad = [], 0, []
    with zipfile.ZipFile(path) as zf:
        for i in zf.infolist():
            name = i.filename.replace("\\", "/")
            if name.endswith(".jsonl"):
                out.append((name, i.file_size, i.CRC, None))
            elif name.lower().endswith(".zip"):
                nested += 1
                logs, why = _list_nested(name, lambda: zf.read(i))
                out += logs
                if why:
                    bad.append([name, why])
    return out, nested, bad


def _may_hold_log(name):
    return bool(shape(name)) or name.lower().endswith(".zip")


def _held(logs, key, size, crc):
    """True when a readable copy of the log has the same size and CRC-32."""
    return any(c.size == size and c.crc == crc for c in logs.get(key, []))


def _descriptor(fh, start, size):
    """(crc, length, next header offset) for the stored member whose data starts at `start`, or None
    when the file ends first. A signed descriptor counts only if its two sizes equal the distance
    back to `start` and a header or the end of the file follows it, so PK\\x07\\x08 in data is passed."""
    pos = start
    while pos < size:
        fh.seek(pos)
        buf = fh.read(CHUNK + 32)
        i = buf.find(DESCRIPTOR)
        while 0 <= i and (i < CHUNK or len(buf) < CHUNK + 32):
            n = pos + i - start
            for fmt, dlen, mask in (("<III", 16, 0xFFFFFFFF), ("<IQQ", 24, (1 << 64) - 1)):
                if i + dlen > len(buf):
                    continue
                crc, c, u = struct.unpack(fmt, buf[i + 4:i + dlen])
                after = buf[i + dlen:i + dlen + 4]
                if c == u == n & mask and (after in (LOCAL, CENTRAL) or pos + i + dlen == size):
                    return crc, n, pos + i + dlen
            i = buf.find(DESCRIPTOR, i + 1)
        pos += CHUNK
    return None


def _headers_in(fh, start, size):
    """Names of the local headers found in bytes start..size that may hold a log."""
    found, pos = [], start
    while pos < size:
        fh.seek(pos)
        buf = fh.read(CHUNK + 30 + 0xFFFF)
        i = buf.find(LOCAL)
        while 0 <= i < CHUNK:
            nl = struct.unpack("<H", buf[i + 26:i + 28])[0] if i + 28 <= len(buf) else 0
            name = buf[i + 30:i + 30 + nl].decode("utf-8", "replace")
            if nl and _may_hold_log(name):
                found.append(name)
            i = buf.find(LOCAL, i + 1)
        pos += CHUNK
    return found


def _walk_zip(path):
    """A zip with no central directory, read from its local file headers in order.

    Returns members, nested, bad as _list_zip does, plus what the walk saw: members walked, the byte it
    stopped at, the member cut off there, and `unknown`, the reason the rest cannot be cleared (None
    when every byte is accounted for: whole members, then a cut member that cannot hold a log)."""
    size = os.path.getsize(path)
    out, nested, bad, walked, cut, unknown = [], 0, [], 0, None, None
    with open(path, "rb") as fh:
        off = 0
        while True:
            fh.seek(off)
            sig = fh.read(4)
            if not sig or sig == CENTRAL:
                break
            h = fh.read(26) if sig == LOCAL else b""
            if len(h) < 26:
                unknown = f"no local header at byte {off:,}" if sig != LOCAL else f"the file ends inside a header at byte {off:,}"
                break
            _, flag, method, _, _, crc, cs, us, nl, xl = struct.unpack("<HHHHHIIIHH", h)
            nx = fh.read(nl + xl)
            name = nx[:nl].decode("utf-8" if flag & 0x800 else "cp437", "replace").replace("\\", "/")
            if len(nx) < nl + xl:
                cut = name if len(nx) >= nl else None
                if cut is None or _may_hold_log(cut):
                    unknown = f"the file ends inside the header of {cut or 'a member'!r}"
                break
            x = nx[nl:]
            while 0xFFFFFFFF in (cs, us) and len(x) >= 4:          # zip64 sizes sit in extra field 0x0001
                tag, ln = struct.unpack("<HH", x[:4])
                if tag == 1:
                    vals = list(struct.unpack(f"<{ln // 8}Q", x[4:4 + ln - ln % 8]))
                    us = vals.pop(0) if us == 0xFFFFFFFF and vals else us
                    cs = vals.pop(0) if cs == 0xFFFFFFFF and vals else cs
                    break
                x = x[4 + ln:]
            data = off + 30 + nl + xl
            if not flag & 8:
                nxt = data + cs if data + cs <= size else None
            elif method == 0:
                got = _descriptor(fh, data, size)
                nxt = got and got[2]
                if got:
                    crc, cs, us = got[0], got[1], got[1]
            else:
                unknown = f"{name!r} uses method {method} with a data descriptor; the walk cannot find its end"
                break
            if nxt is None:
                cut = name
                inside = _headers_in(fh, data, size)
                if _may_hold_log(name) or inside:
                    unknown = (f"cut off inside {name!r}" if _may_hold_log(name) else
                               f"a header for {inside[0]!r} sits in the cut-off bytes of {name!r}")
                break
            walked += 1
            if name.endswith(".jsonl"):
                out.append((name, us, crc, None))
            elif name.lower().endswith(".zip"):
                nested += 1

                def read():
                    fh.seek(data)
                    raw = fh.read(cs)
                    return raw if method == 0 else zlib.decompress(raw, -15)
                logs, why = _list_nested(name, read)
                out += logs
                if why:
                    bad.append([name, why])
            off = nxt
    return out, nested, bad, {"walked": walked, "at": off, "size": size, "cut": cut, "unknown": unknown}


PRUNE = ("node_modules", ".git", ".venv", "site-packages")   # folder names never walked: package trees and git
# internals, where no tool writes a log. In a home with Python environments, .venv and site-packages can hold
# nearly half the folders a walk visits and no log or zip. The folder index records the list, so a cache walked
# under another one is not used.


FIND_TIMEOUT = 30
EACH_TICK, EACH_RATE = 0.05, 20_000   # _each adds a thread when a tick takes fewer folders than this rate
FIND_RERUNS = 3   # a piece whose find quits is run again, alone, up to this many times
FIND_QUIT = ("find: fts_read:", "find: ftsopen:")   # find's own walk failed and it quit, naming no folder
DIR_SLACK_NS = 1_000_000_000   # a folder whose ctime is within this of the walk's start is re-listed next run
FULL_WALK_EVERY = 24 * 3600    # an index older than this is not used: the run walks every folder, so a change
                               # the index cannot see (a folder whose ctime did not move) is missed a day at most
QUIT_NOTE = "NOT SEARCHED in full, find quit on"
GONE = ": No such file or directory"   # it went away during the walk: a gap only if a log it held is now nowhere
VANISHED = "gone before it was read: the walk listed it, then it was moved or deleted"   # its logs were missed
UNREAD = "could not be read"                                                          # an OSError but gone


def why_unread(e):
    """The DID NOT READ reason for a file whose read failed: gone, or the error, such as
    `could not be read: TimeoutError: [Errno 60] Operation timed out` from a file provider."""
    return VANISHED if isinstance(e, FileNotFoundError) else f"{UNREAD}: {type(e).__name__}: {e}"[:200]


def _find_one(root):
    """(log and zip files, folders, errors) under root.

    A find under ~/Library that does not return in FIND_TIMEOUT is killed and its folder named as
    NOT SEARCHED, which makes this and recall exit 2. Library/Containers and Library/Group Containers
    can stop returning at all: macOS guards other apps' data and can hold a read until a prompt is
    answered. Nothing else is timed: a large work folder can take many seconds, longer under load.

    A find that quits on FIND_QUIT has walked only part of its folder, and its error names no folder,
    so the folder is named as NOT SEARCHED in full. `_find` reruns it, alone, up to FIND_RERUNS times;
    only a quit on every rerun stays NOT SEARCHED in full and makes both exit 2. The quit,
    `find: fts_read: Interrupted system call`, comes in bursts, often in ~/Library/Containers, and a
    single rerun can quit too; a fresh try a little later usually finishes. A line naming a path,
    such as one denied subfolder, is not a quit: find goes on past it. That folder was still not
    read, so it makes both exit 2 too, unless it ends in GONE; see Inventory._went_away."""
    try:
        r = subprocess.run(["/usr/bin/find", root, "(", *[a for n in PRUNE for a in ("-o", "-name", n)][1:], ")", "-prune",
                            "-o", "-type", "d", "-print0",
                            "-o", "-type", "f", "(", "-name", "*.jsonl", "-o", "-iname", "*.zip", ")", "-print0"],
                           capture_output=True,
                           timeout=FIND_TIMEOUT if os.sep + "Library" + os.sep in root + os.sep else None)
    except subprocess.TimeoutExpired:
        return [], [], [f"{root}: NOT SEARCHED, find timed out after {FIND_TIMEOUT}s"]
    files, dirs = [], []
    for p in r.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if p:                                      # a folder named like a log is rare: ask the disk
            (files if _log_named(p) and not os.path.isdir(p) else dirs).append(p)
    return (files, dirs, [f"{root}: {QUIT_NOTE} {ln[len('find: '):]}" if ln.startswith(FIND_QUIT) else ln
                          for ln in r.stderr.decode("utf-8", "replace").splitlines() if ln.strip()])


def _log_named(p):
    return p.endswith(".jsonl") or p.lower().endswith(".zip")


def _named(err):
    """The folder a walk error names."""
    if ": NOT SEARCHED" in err:
        return err.split(": NOT SEARCHED")[0]
    return err.removeprefix("find: ").rpartition(": ")[0]


def _finds(pieces):
    """{folder: (files, folders, errors)} for each piece, 16 finds at a time, and the reruns. A find that
    quits is rerun after the others finish, up to FIND_RERUNS times; a lone find quit too, so what helps
    is a fresh try, not running alone. What any run printed is kept."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(16) as ex:
        done = list(zip(pieces, ex.map(_find_one, pieces)))
    out, reruns = {}, []
    for piece, (files, dirs, err) in done:
        tries = 0
        while any(QUIT_NOTE in e for e in err) and tries < FIND_RERUNS:
            tries += 1
            more, more_dirs, err = _find_one(piece)
            files, dirs = list(dict.fromkeys(files + more)), list(dict.fromkeys(dirs + more_dirs))
        if tries:
            reruns.append((piece, tries, not any("NOT SEARCHED" in e for e in err)))
            err = [e + f", and again when rerun alone, {tries} times" if QUIT_NOTE in e else e for e in err]
        out[piece] = (files, dirs, err)
    return out, reruns


def _each(folders, fn):
    """{folder: fn(folder), or the OSError it raised}, for at most FIND_TIMEOUT. One thread takes folders
    from a queue, and another starts, up to 16, each EACH_TICK the work goes slower than EACH_RATE a
    second: a folder held up, or a disk not in cache. So one held up holds up only itself. 16 threads from
    the start spend more time trading the interpreter lock than in lstat, several times what one thread
    takes on a warm disk. A folder still unanswered then is left out. The threads are daemons, so one
    that macOS holds for a prompt cannot hold the process at exit."""
    import threading
    todo, lock, out, taken, drained = iter(list(folders)), threading.Lock(), {}, [0], [False]

    def run():
        while True:
            with lock:
                d = next(todo, None)
                taken[0] += 1
                drained[0] = drained[0] or d is None
            if d is None:
                return
            try:
                out[d] = fn(d)
            except OSError as e:
                out[d] = e
    threads, end, last = [], time.monotonic() + FIND_TIMEOUT, -1
    while True:
        live, wait = [t for t in threads if t.is_alive()], end - time.monotonic()
        if wait <= 0 or (drained[0] and not live):
            break
        if not drained[0] and len(threads) < 16 and taken[0] - last < EACH_RATE * EACH_TICK:
            threads.append(threading.Thread(target=run, daemon=True))
            threads[-1].start()
            live.append(threads[-1])
        last = taken[0]
        live[0].join(min(EACH_TICK, wait))
    return dict(out)


def _sweep(folders):
    """{folder: its lstat st_ctime_ns, or the OSError}; a folder left out had no answer in FIND_TIMEOUT, so
    it is listed again, and that listing is timed too."""
    return _each(folders, lambda d: os.lstat(d).st_ctime_ns)


def _list_one(d):
    """(log and zip names, subfolder names) directly in folder d, as find's -type f and -type d see them."""
    files, subs = [], []
    with os.scandir(d) as it:
        for e in it:
            if e.is_dir(follow_symlinks=False):
                if e.name not in PRUNE:
                    subs.append(e.name)
            elif e.is_file(follow_symlinks=False) and _log_named(e.name):
                files.append(e.name)
    return files, subs


def _relist(folders):
    """{folder: (log and zip names, subfolder names), or the line saying why not} for each folder, listed
    here (_each): one find per folder is far slower for thousands of folders. A listing that has not
    returned in FIND_TIMEOUT is NOT SEARCHED."""
    folders = list(folders)
    out = _each(folders, lambda d: _list_one(d))
    return {d: (f"{d}: {out[d].strerror}" if isinstance(out[d], OSError) else out[d]) if d in out
            else f"{d}: NOT SEARCHED, listing it did not return in {FIND_TIMEOUT}s" for d in folders}


def _index(files, dirs, errs):
    """{folder: [None, subfolders, logs and zips]} for what one finished find listed; _stamp fills in the
    times. A folder find named in an error is left out, so it is walked again next run."""
    out = {d: [None, [], []] for d in dirs}
    for d in dirs:
        up = out.get(os.path.dirname(d))
        if up is not None:
            up[1].append(os.path.basename(d))
    for f in files:
        up = out.get(os.path.dirname(f))
        if up is not None:
            up[2].append(os.path.basename(f))
    for e in errs:
        bad = _named(e)
        for d in [d for d in out if d == bad or d.startswith(bad + os.sep)]:
            del out[d]
    return out


def _trusted(ct, t0):
    """A folder's ctime, or None when it is not clearly older than t0, the start of the walk: the folder may
    have changed after find read it, under a timestamp no newer than the one already seen, so it is re-listed
    next run."""
    return ct if isinstance(ct, int) and ct < t0 - DIR_SLACK_NS else None


def _stamp(dirs, t0):
    """Fill in each new index entry's ctime, after its listing; a folder gone since is dropped."""
    for d, ct in _sweep(dirs).items():
        if isinstance(ct, OSError):
            dirs.pop(d, None)
        else:
            dirs[d][0] = _trusted(ct, t0)
    return dirs


def _find(root, index=None):
    """(every .jsonl and .zip under root, the folders find could not open, the finds that quit and were
    rerun with how many reruns each took and whether the last one finished, the folder index, a line
    saying what was walked).

    THE FOLDER INDEX. A folder's st_ctime moves whenever an entry in it is added, removed or renamed, and
    on chmod; a file growing inside it leaves it alone (on disk and in cloud folders alike). So
    with the last run's index, one lstat per folder (about a second per hundred thousand) finds the folders that
    changed; only those are listed again, here (_relist), and only folders new since then are walked in
    full, by find. Every other folder's listing is the one the index holds. An index whose last full walk
    is FULL_WALK_EVERY old is not used, so a change no ctime shows is missed a day at most. A full walk of a
    large HOME takes tens of seconds and more under load, while only a few folders change between runs;
    a mirroring job (rsync -a sets folder times) can change thousands at once, and those are re-listed.

    WITHOUT AN INDEX (first run, --refresh): root, Desktop and Library are split one level and the
    pieces run in parallel. Library/Containers stays one piece: one find per app container is slower
    than one find over all of them, and rerunning the one find costs less."""
    t0 = time.time_ns()
    if index is not None and root in index:
        return _find_changed(root, index, t0)
    pieces, paths, denied, dirs = [], [], [], {}
    todo = [root]
    while todo:
        d = todo.pop()
        try:
            ct = os.lstat(d).st_ctime_ns
            entries = list(os.scandir(d))
        except OSError as e:
            denied.append(f"{d}: {e.strerror}")
            continue
        here = dirs[d] = [ct, [], []]
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if e.name in PRUNE:
                    continue
                here[1].append(e.name)
                (todo if d == root and e.name in ("Desktop", "Library") else pieces).append(e.path)
            elif e.is_file(follow_symlinks=False) and _log_named(e.name):
                here[2].append(e.name)
                paths.append(e.path)
    got, reruns = _finds(pieces)
    walked = {}
    for piece in pieces:
        files, found, err = got[piece]
        paths += files
        denied += err
        if not any("NOT SEARCHED" in e for e in err):
            walked.update(_index(files, found, err))
    for d, e in dirs.items():
        e[0] = _trusted(e[0], t0)
    dirs.update(_stamp(walked, t0))
    return paths, denied, reruns, dirs, f"walked {len(dirs):,} folders"


def _ancestors(d, root):
    while d != root and len(d) > len(root):
        d = os.path.dirname(d)
        yield d


def _dirs_out(dirs, root, full_at):
    """The folder index as the cache holds it: paths relative to root, when every folder was last walked, and
    the PRUNE list it was walked under."""
    n = len(root.rstrip(os.sep)) + 1
    return {"root": root, "full_at": full_at, "prune": list(PRUNE),
            "dirs": {("" if d == root else d[n:]): e for d, e in dirs.items()}}


def _dirs_in(cache, root):
    """The last run's folder index, if it walked this root under this PRUNE list and its last full walk is
    under FULL_WALK_EVERY old; else None. A folder's cached listing leaves out what was pruned, so an index
    walked under another list would never reach a name taken off it."""
    w = cache.get("walk") or {}
    if w.get("root") != root or not isinstance(w.get("dirs"), dict) or w.get("prune") != list(PRUNE) \
            or not time.time() - w.get("full_at", 0) < FULL_WALK_EVERY:
        return None
    base = root.rstrip(os.sep) + os.sep
    return {(root if k == "" else base + k): e for k, e in w["dirs"].items()}


def _find_changed(root, index, t0):
    """_find with the last run's folder index: re-list each folder whose ctime moved, walk each new one."""
    seen = _sweep(index)                               # before any listing, so a later change moves it again
    keep, relist = {}, []
    for d, e in index.items():
        ct = seen.get(d)
        if isinstance(ct, OSError) and ct.errno in (errno.ENOENT, errno.ENOTDIR):
            continue                                   # gone since the last run
        if isinstance(ct, int) and ct == e[0]:
            keep[d] = e
        else:                                          # changed, not trusted, or its stat failed or hung
            relist.append(d)
    above = set(relist)                                # a stat that failed or hung under a folder re-listed
    relist = [d for d in relist if isinstance(seen.get(d), int) or not any(   # anyway is that folder's to name
        a in above for a in _ancestors(d, root))]
    listed = _relist(relist)
    denied, tried = [], set()
    for d in relist:
        got = listed[d]
        if isinstance(got, str):                       # not opened, gone or not searched: named, and walked
            denied.append(got)                         # next run
            tried.add(d)
            continue
        keep[d] = [_trusted(seen.get(d), t0), got[1], got[0]]
    if root not in keep:                               # root itself could not be re-listed: walk it all
        p, de, rr, dirs, how = _find(root)
        return p, denied + de, rr, dirs, how
    paths = []
    dirs, new, todo = {}, [], [root]
    while todo:                                        # what root still reaches, and the folders it reaches
        d = todo.pop()                                 # that no index entry lists
        e = keep.get(d)
        if e is None:
            if d not in tried and os.path.lexists(d):
                new.append(d)
            continue
        dirs[d] = e
        pre = d if d.endswith(os.sep) else d + os.sep  # os.path.join's answer, at a tenth of its cost
        paths += [pre + f for f in e[2]]
        todo += [pre + s for s in e[1]]
    got, reruns = _finds(new)
    walked = {}
    for d in new:
        files, found, err = got[d]
        paths += files
        denied += err
        if not any("NOT SEARCHED" in e for e in err):
            walked.update(_index(files, found, err))
    dirs.update(_stamp(walked, t0))
    return (paths, denied, reruns, dirs,
            f"{len(dirs):,} folders, {len(relist)} re-listed, {len(new)} walked new")


class Inventory:
    def __init__(self, root=HOME, refresh=False, cache=CACHE):
        t0 = time.time()
        self.root, self.cache_path = root, cache
        last = self._load()                            # what the last run saw; read even with --refresh
        old = {} if refresh else last
        new = {"disk": {}, "zip": {}, "sniff": {}}
        self.logs = collections.defaultdict(list)      # key -> [Copy]
        self.skipped = collections.Counter()           # folder of a .jsonl that is not a log shape
        self.headless = []                             # (path, session_id): a claude -p capture read as a log
        self.headless_logged = 0                       # captures not read: their run's session log is
        self.zips = collections.Counter()              # read / mirror / nested / nested_broken
        self.did_not_read = []                         # (path, reason)
        self.unreadable = []                           # (path, reason): walked, every log in it held elsewhere
        self.not_zip = []                              # (path, member): named .zip, holds no zip signature
        self.not_zip_files = []                        # path: a file on disk named .zip, no zip signature in it
        self.cloud = set()                             # paths of cloud placeholders: never opened
        self.cloud_unread = []                         # (path, label, key): a placeholder no read copy matches
        self.reread = 0
        walks = []
        paths, self.denied, self.find_reruns, dirs, self.walked = _find(root, None if refresh else _dirs_in(last, root))
        full_at = t0 if self.walked.startswith("walked") else last["walk"]["full_at"]
        if not self.walked.startswith("walked"):
            self.walked += f"; every folder walked {(time.time() - full_at) / 3600:.0f}h ago"
        new["walk"] = _dirs_out(dirs, root, full_at)
        self.not_searched = [d for d in self.denied if "NOT SEARCHED" in d]   # find gave up on these
        self.not_opened = [d for d in self.denied if "NOT SEARCHED" not in d and not d.endswith(GONE)]
        base = root.rstrip(os.sep) + os.sep
        for p in paths:
            rel = p[len(base):] if p.startswith(base) else os.path.relpath(p, root)
            if p.lower().endswith(".zip"):
                if rel.startswith(MIRROR):
                    self.zips["mirror"] += 1
                    continue
                st = self._stat(p)
                if not st:
                    continue
                sig = [st.st_size, st.st_mtime_ns]
                hit = old.get("zip", {}).get(p)
                if hit and hit[:2] == sig and isinstance(hit[4], list):  # a bare count: an older cache format
                    entry = hit
                elif _cloud_only(st):                      # listing it would download it: what it holds is unknown
                    self.cloud.add(p)
                    self.cloud_unread.append((p, p.replace(HOME, "~"), None))
                    continue
                else:
                    self.reread += 1
                    try:
                        entry = sig + list(_list_zip(p))
                    except FileNotFoundError:
                        self.did_not_read.append((p, VANISHED))
                        continue
                    except Exception as e:
                        if st.st_blocks == 0:
                            self.did_not_read.append((p, "no data on disk (cloud-only)"))
                            continue
                        if isinstance(e, OSError):                 # the read failed, not the zip: no walk
                            self.did_not_read.append((p, why_unread(e)))
                            continue
                        try:
                            if not _signed_file(p):                # not an archive, so no log: NOT A ZIP
                                entry = sig + [[], 0, [], NOT_ZIP]
                            else:
                                entry = sig + list(_walk_zip(p)) + [type(e).__name__]
                                entry[5]["signed"] = True
                        except Exception as e2:
                            self.did_not_read.append((p, VANISHED if isinstance(e2, FileNotFoundError) else
                                                      f"{type(e).__name__}; walking its headers failed:"
                                                      f" {type(e2).__name__}"))
                            continue
                new["zip"][p] = entry
                if _cloud_only(st):                        # listed while local: its members are known, not read
                    self.cloud.add(p)
                if entry[5:] == [NOT_ZIP]:
                    self.not_zip_files.append(p)
                    continue
                members, nested, bad = entry[2], entry[3], entry[4]
                self.zips["nested"] += nested
                self.not_zip += [(p, name) for name, why in bad if why == NOT_ZIP]
                broken = [(name, why) for name, why in bad if why != NOT_ZIP]
                self.zips["nested_broken"] += len(broken)
                broken = [f"nested zip {broken[0][0]!r} could not be listed: {broken[0][1]}"
                          + (f", and {len(broken) - 1} more" if len(broken) > 1 else "")] if broken else []
                if len(entry) > 5:
                    walks.append((p, members, entry[5], entry[6], broken))
                    continue
                self.zips["read"] += 1
                if broken:
                    self.did_not_read.append((p, broken[0] + "; the other logs in this zip were read"))
                for name, size, crc, inner in members:
                    got = shape(inner or name)
                    if got:
                        self.logs[got[1]].append(Copy(got[0], got[1], size, crc, "nested" if inner else "zip",
                                                      p, name, inner))
                    else:
                        self.skipped[os.path.dirname(inner or name)[-60:] + " (in a zip)"] += 1
                continue
            got = shape(rel)
            if not got:
                sid = _headless(p, old.get("sniff", {}), new["sniff"]) if p.endswith(".jsonl") else None
                if not sid:
                    self.skipped[os.path.dirname(rel)[-60:]] += 1
                    continue
                got = ("headless", HEADLESS + sid + ".jsonl")
            st = self._stat(p)
            if not st:
                continue
            sig = [st.st_size, st.st_mtime_ns]
            hit = old.get("disk", {}).get(p)
            if _cloud_only(st):
                self.cloud.add(p)
            if hit and hit[:2] == sig:
                crc, mark = hit[2], (hit[3] if len(hit) > 3 else None)
            elif p in self.cloud:
                crc = mark = None                          # never read a placeholder; no checksum says what it holds
            else:
                self.reread += 1
                try:
                    crc, mark = _crc_grown(p, hit, st.st_size) or _crc_file(p)
                except OSError as e:
                    self.did_not_read.append((p, why_unread(e)))
                    continue
            if crc is not None:
                new["disk"][p] = sig + [crc, mark]
            self.logs[got[1]].append(Copy(got[0], got[1], st.st_size, crc, "disk", p, None, None))
        for p, members, walk, why, broken in walks:
            held, missing = 0, []
            for name, size, crc, inner in members:
                got = shape(inner or name)
                if not got:
                    self.skipped[os.path.dirname(inner or name)[-60:] + " (in a zip)"] += 1
                    continue
                held += 1
                if not _held(self.logs, got[1], size, crc):
                    missing.append(inner or name)
            head = (f"{why}, no central directory; local headers walked to byte {walk['at']:,} of {walk['size']:,}:"
                    f" {walk['walked']} whole members, {held} logs")
            gaps = ([f"{len(missing)} with no identical copy elsewhere, first {missing[0]!r}"] if missing else []) \
                + ([walk["unknown"]] if walk["unknown"] else []) + broken
            if gaps:
                self.did_not_read.append((p, head + "; " + "; ".join(gaps)))
            else:
                self.unreadable.append((p, head + (", each held identically elsewhere" if held else "")
                                        + (f"; cut off inside {walk['cut']!r}, no log or zip header in what remains"
                                           if walk["cut"] else "")))
        self.vanished = [self._went_away(d.removeprefix("find: ")[:-len(GONE)], last)
                         for d in self.denied if d.endswith(GONE)]               # (path, note, gap)
        self.not_searched += [f"{p} went away during the walk{note}" for p, note, gap in self.vanished if gap]
        for key in [k for k in self.logs if k.startswith(HEADLESS)]:      # the run kept a session log, which
            if key[len(HEADLESS):] in self.logs:                          # holds the same conversation: read that
                self.headless_logged += len(self.logs.pop(key))
        self.headless = [(c.path, k[len(HEADLESS):-6]) for k, cs in self.logs.items() if k.startswith(HEADLESS)
                         for c in cs]
        for key, copies in self.logs.items():
            got = {(c.size, c.crc) for c in copies if c.path not in self.cloud}
            self.cloud_unread += [(c.path, c.label(), key) for c in copies
                                  if c.path in self.cloud and (c.crc is None or (c.size, c.crc) not in got)]
        if self.cloud_unread:
            n, keys = len(self.cloud_unread), {k for _, _, k in self.cloud_unread if k}
            zips = sum(1 for _, _, k in self.cloud_unread if k is None)
            by_path = {c.path: c for cs in self.logs.values() for c in cs if c.path in self.cloud and not c.member}
            unsummed = sum(1 for p, _, k in self.cloud_unread if k and by_path.get(p) and by_path[p].crc is None)
            alone = sum(1 for k in keys if all(c.path in self.cloud for c in self.logs[k]))
            size = sum(os.path.getsize(p) if k is None else 0 for p, _, k in self.cloud_unread) + sum(
                c.size for k in keys for c in self.logs[k] if c.path in self.cloud and not c.member)
            self.did_not_read.append((min(p for p, _, _ in self.cloud_unread),
                                      f"cloud-only: {n} cloud placeholder{'s' if n > 1 else ''} with no data on disk,"
                                      f" none read, as reading one downloads it: {n - zips} log copies ({unsummed} never"
                                      f" checksummed, {n - zips - unsummed} with bytes no read copy has) and {zips} zips"
                                      f" not listed while local. {alone} of their {len(keys)} logs have no copy that is"
                                      f" read; the other {len(keys) - alone} were read from a copy that may lack records a"
                                      f" placeholder holds. Keeping them on this device downloads about"
                                      f" {size / 1e9:.1f} GB to read them (--list names each)"))
        for kind in ("disk", "zip"):                   # a file this walk did not reach (a folder not searched or
            for p, e in last.get(kind, {}).items():    # not opened) keeps its entry while it exists, so the next
                if p not in new[kind] and os.path.lexists(p):   # run that reaches it does not read it again
                    new[kind][p] = e
        self._save(new)
        self.seconds = time.time() - t0

    def _went_away(self, path, last):
        """(path, note, gap) for a path that went away while find walked it. The last run's cache says which logs
        it held; it is a gap when one of them has no identical copy found this run, or when there is no cache."""
        if not last.get("disk") and not last.get("zip"):
            return path, ": no cache from a last run says what it held", True
        under = lambda q: q == path or q.startswith(path + os.sep)
        logs = []                                      # (label, key, size, crc) of each log it held at the last run
        for q, e in last.get("disk", {}).items():
            got = under(q) and shape(os.path.relpath(q, self.root))
            if got:
                logs.append((q, got[1], e[0], e[2]))
        for q, e in last.get("zip", {}).items():
            if under(q):
                for name, size, crc, inner in e[2]:
                    got = shape(inner or name)
                    if got:
                        logs.append((f"{q} :: {inner or name}", got[1], size, crc))
        missing = sorted(lab for lab, key, size, crc in logs if not _held(self.logs, key, size, crc))
        n = f"{len(logs)} log{'s' * (len(logs) != 1)}"
        if not logs:
            return path, " (held no log at the last run)", False
        if not missing:
            return path, f" ({n} it held at the last run, each found elsewhere)", False
        return path, (f": {len(missing)} of the {n} it held at the last run not found this run,"
                      f" first {missing[0].replace(HOME, '~')}"), True

    def _stat(self, p):
        """os.stat(p), or None with p under DID NOT READ when it went away after the walk listed it
        or could not be read."""
        try:
            return os.stat(p)
        except OSError as e:
            self.did_not_read.append((p, why_unread(e)))
            return None

    def _load(self):
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        # A temp file of its own per call: with one fixed name, two runs at once (recall.py in parallel)
        # replaced each other's temp file away and the second os.replace raised FileNotFoundError.
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.cache_path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data))             # json.dump encodes in Python; dumps in C, several times faster
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.cache_path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def to_read(self, exclude_session=None):
        """One copy per distinct (size, CRC) of every log, disk first. Excludes a session and its subagents.
        A cloud placeholder is never one of them: those no read copy matches are in cloud_unread."""
        out = []
        for key, copies in self.logs.items():
            if exclude_session and key.startswith(exclude_session):
                continue
            seen = set()
            for c in sorted(copies, key=lambda c: (PREFER[c.via], c.path)):
                if c.path not in self.cloud and (c.size, c.crc) not in seen:
                    seen.add((c.size, c.crc))
                    out.append(c)
        return out

    def coverage(self, exclude_session=None):
        """Lines that say what this inventory covers and what it does not."""
        reads = self.to_read(exclude_session)
        n_copies = sum(len(v) for v in self.logs.values())
        places = collections.Counter(where(c.path) + ("-zip" if c.member else "") for c in reads)
        only_zip = sum(1 for v in self.logs.values() if all(c.member for c in v))
        n_cloud = sum(1 for v in self.logs.values() for c in v if c.path in self.cloud)
        runs = collections.Counter(os.path.dirname(p) for p, _ in self.headless)
        runs_line = [f"  claude -p runs saved as stream-json with no session log: {len(self.headless)} copies of"
                     f" {len({s for _, s in self.headless})} runs in {len(runs)} folders, read as kind headless; a hit in"
                     f" one is a claude -p record, never typed by you; {self.headless_logged} more not read, as their"
                     f" run's session log is (" + "; ".join(f"{n} {k.replace(HOME, '~')[-60:]}" for k, n in runs.most_common(3))
                     + ")"] if runs or self.headless_logged else []
        lines = [f"{len(self.logs)} logs ({', '.join(f'{n} {k}' for k, n in collections.Counter(v[0].kind for v in self.logs.values()).most_common())}),"
                 f" {n_copies} copies; reading {len(reads)} distinct copies, {sum(c.size for c in reads) / 1e9:.2f} GB"
                 + (f" (session {exclude_session[:8]} excluded)" if exclude_session else ""),
                 f"  read from: {', '.join(f'{k} {n}' for k, n in places.most_common())}; {only_zip} logs exist only in a zip",
                 f"  zips: {self.zips['read']} read, {self.zips['nested']} nested zips"
                 f" ({self.zips['nested'] - len(self.not_zip) - self.zips['nested_broken']} opened,"
                 f" {len(self.not_zip)} not a zip, {self.zips['nested_broken']} could not be listed),"
                 f" {self.zips['mirror']} OneDrive app-cache placeholders not read",
                 f"  cloud placeholders, never read: {n_cloud} log copies and"
                 f" {sum(1 for p in self.cloud if p.lower().endswith('.zip'))} zips;"
                 f" {n_cloud - sum(1 for _, _, k in self.cloud_unread if k)} copies identical to a copy that is read",
                 f"  .jsonl not a log shape: {sum(self.skipped.values())} in {len(self.skipped)} folders"
                 + (" (" + "; ".join(f"{n} {k}" for k, n in self.skipped.most_common(3)) + ")" if self.skipped else ""),
                 *runs_line,
                 f"  inventory {self.seconds:.1f}s, {self.reread} files re-read, {self.walked};"
                 f" {len(self.denied)} folders could not be"
                 f" opened ({sum(1 for d in self.denied if '/Library/' in d)} of them macOS-protected Library folders)",
                 f"  NOT WALKED, ON PURPOSE: folders named {', '.join(PRUNE)} (installed packages and git internals)"]
        for p, why in self.unreadable:
            lines.append(f"  UNREADABLE, SKIPPED {p.replace(HOME, '~')}: {why}")
        if self.not_zip:
            p, name = min(self.not_zip)
            lines.append(f"  NOT A ZIP, SKIPPED {len(self.not_zip)} members named .zip in"
                         f" {len({q for q, _ in self.not_zip})} zips hold no zip signature, so no log;"
                         f" first {p.replace(HOME, '~')} :: {name} (--list names each)")
        if self.not_zip_files:
            lines.append(f"  NOT A ZIP, SKIPPED {len(self.not_zip_files)} files named .zip hold no zip signature, so no"
                         f" log; first {min(self.not_zip_files).replace(HOME, '~')} (--list names each)")
        for p, why in self.did_not_read:
            lines.append(f"  DID NOT READ {p.replace(HOME, '~')}: {why}")
        gaps = set(self.not_searched + self.not_opened)            # each makes the exit 2, so each is named
        for d in self.denied:
            if (d in gaps or "/Library/" not in d) and not d.endswith(GONE):    # GONE prints just below
                lines.append(f"  could not open: {d.replace(HOME, '~')[:140]}")
        for p, note, gap in self.vanished:
            lines.append(f"  went away during the walk: {p.replace(HOME, '~')}{note}")
        for piece, tries, finished in self.find_reruns:
            if finished:                                   # one that did not finish prints just above
                lines.append(f"  find quit partway in {piece.replace(HOME, '~')} and finished when rerun alone"
                             + (f", on rerun {tries}" if tries > 1 else ""))
        return lines


def iter_lines(copy):
    """(line_number, raw bytes) for every line of a copy. A copy that fails to open raises."""
    with copy.open() as fh:
        for n, line in enumerate(fh):
            yield n, line


def strings(o):
    """Every string inside a decoded record, depth first."""
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)


# ---------------------------------------------------------------------------------------------- selftest

class _Pipe(io.RawIOBase):
    """An unseekable sink, so zipfile writes data descriptors, as a streamed download does."""

    def __init__(self):
        self.b = bytearray()

    def writable(self):
        return True

    def write(self, d):
        self.b += d
        return len(d)


@contextlib.contextmanager
def find_times_out(folder):
    """A timed find of `folder` raises TimeoutExpired, as when macOS holds the read for a prompt."""
    real = subprocess.run

    def run(cmd, **k):
        if cmd[:2] == ["/usr/bin/find", folder] and k.get("timeout"):
            raise subprocess.TimeoutExpired(cmd, k["timeout"])
        return real(cmd, **k)
    subprocess.run = run
    try:
        yield
    finally:
        subprocess.run = real


@contextlib.contextmanager
def find_quits(folder, error="fts_read: Interrupted system call", times=None):
    """The find of `folder` walks it, then prints `find: <error>` and exits 1, as BSD find does when
    fts_read fails; with `times`, only its first `times` finds do. Yields (folder, start, end) for
    every find run."""
    real, spans, seen = subprocess.run, [], collections.Counter()

    def run(cmd, **k):
        if cmd[:1] != ["/usr/bin/find"]:
            return real(cmd, **k)
        seen[cmd[1]] += 1
        n, t0 = seen[cmd[1]], time.monotonic()
        r = real(cmd, **k)
        spans.append((cmd[1], t0, time.monotonic()))
        if cmd[1] == folder and (times is None or n <= times):
            return subprocess.CompletedProcess(cmd, 1, r.stdout, r.stderr + f"find: {error}\n".encode())
        return r
    subprocess.run = run
    try:
        yield spans
    finally:
        subprocess.run = real


@contextlib.contextmanager
def goes_away(path, ns, fn, before=False):
    """`path` is moved out of the tree just after ns[fn] returns, or with `before`, just before ns[fn]
    is called on it, as when another session moves a backup folder to the Trash mid-run.
    It is moved back on exit. ns: the globals fn is looked up in, such as vars(cc_logs)."""
    real, away = ns[fn], tempfile.mkdtemp()
    held = os.path.join(away, os.path.basename(path))

    def run(*a, **k):
        if before and a[:1] == (path,) and os.path.exists(path):
            os.replace(path, held)
        r = real(*a, **k)
        if not before and os.path.exists(path):
            os.replace(path, held)
        return r
    ns[fn] = run
    try:
        yield
    finally:
        ns[fn] = real
        if os.path.exists(held):
            os.replace(held, path)
        os.rmdir(away)


@contextlib.contextmanager
def times_out(path, ns, fn):
    """ns[fn] raises a file provider's read timeout when called on `path`
    (`TimeoutError: [Errno 60] Operation timed out`). ns: as in goes_away."""
    real = ns[fn]

    def run(*a, **k):
        if a[:1] == (path,):
            raise TimeoutError(60, "Operation timed out")
        return real(*a, **k)
    ns[fn] = run
    try:
        yield
    finally:
        ns[fn] = real


def plant_cut_zip(path, members, stream=True, keep=10):
    """A zip cut off `keep` bytes into its last member's data, so it has no central directory.
    stream: stored members with data descriptors; else deflated members with sizes in the header."""
    sink = _Pipe() if stream else io.BytesIO()
    with zipfile.ZipFile(sink, "w") as zf:
        for name, data in members[:-1]:
            zf.writestr(name, data, zipfile.ZIP_STORED if stream else zipfile.ZIP_DEFLATED)
        at = len(sink.b) if stream else sink.tell()
        zf.writestr(*members[-1])
    raw = bytes(sink.b) if stream else sink.getvalue()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(raw[:at + 30 + len(members[-1][0].encode()) + keep])
    return path


def _plant(root):
    """A HOME-shaped tree with one of each case. Returns the keys that must be found."""
    u = ["%08x-0000-4000-8000-%012x" % (i, i) for i in range(1, 8)]
    rec = lambda i, t: (json.dumps({"uuid": f"r{i}", "type": "user", "message": {"content": t}}) + "\n").encode()
    a = rec(1, "alpha") + rec(2, "beta")

    def w(rel, data):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def z(rel, members):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with zipfile.ZipFile(p, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        return p

    w(f".claude/projects/S/{u[0]}.jsonl", a)                                   # live
    w(f"Desktop/_archive/x/session-logs/{u[0]}.jsonl", a)                      # identical copy: not re-read
    w(f".claude/projects/S/{u[0]}/subagents/agent-a1.jsonl", rec(3, "sub"))    # subagent
    w(f".claude-old/projects/S/{u[1]}/subagents/workflows/wf_1/agent-b.jsonl", rec(4, "wf"))
    z("backup/.claude.zip", {f".claude/projects/S/{u[2]}.jsonl": rec(5, "zip only"),
                                f".claude/projects/S/{u[0]}.jsonl": a + rec(6, "resumed, longer"),
                                ".claude/history.jsonl": b'{"display":"h1","timestamp":1}\n'})
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr(f"logs/{u[3]}.jsonl", rec(7, "nested"))
    z("Library/CloudStorage/Drive/backup/d/outer.zip", {"inner.zip": inner.getvalue()})
    w(f"Desktop/same-size/{u[4]}.jsonl", rec(8, "aaaa"))                       # same size, other bytes
    w(f"Desktop/same-size2/{u[4]}.jsonl", rec(8, "bbbb"))
    w(f"restore/home/AppData/Roaming/Code/User/workspaceStorage/h/chatSessions/{u[5]}.jsonl",
      b'{"kind":0,"v":{"x":"vs"}}\n')
    w(".claude-old/history.jsonl", b'{"display":"h2","timestamp":2}\n')
    w("Desktop/scratch/mine/prompts.jsonl", b"{}\n")                           # not a log
    w(MIRROR + "OneDrive.noindex/cached.zip", b"\0\0")                         # mirror placeholder
    w("Downloads/broken.zip", LOCAL + b" not really")                          # unreadable zip elsewhere
    cut = lambda rel, *a, **k: plant_cut_zip(os.path.join(root, rel), *a, **k)
    png =b"\x89PNG" + b"PK\x07\x08" + b"\1" * 12 + b"x" * 200                # a false descriptor inside data
    mp4 = b"\0\0\0\x18ftypmp42" + b"m" * 500
    fork = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + b"\0" * 188     # a macOS ._ resource fork
    w("Downloads/__MACOSX/._loose.zip", fork)                                  # one on disk, not in a zip
    cut(".Trash/cut-media.zip", [("media/", b""), ("media/a.png", png), (f".claude/projects/S/{u[0]}.jsonl", a),
                                 ("__MACOSX/media/._b.zip", fork),
                                 ("media/b.mp4", mp4)], stream=True)           # its log is held elsewhere
    cut("Downloads/cut-only-copy.zip", [(f".claude/projects/S/{u[6]}.jsonl", rec(9, "only here")),
                                        ("media/c.mp4", mp4)], stream=False)   # its log is held nowhere else
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr(f"logs/{u[6]}.jsonl", rec(10, "inside a cut member"))
    cut("Downloads/cut-hides-log.zip", [("media/d.png", png), ("bundle.bin", inner.getvalue() + b"\0" * 400)],
        stream=True, keep=len(inner.getvalue()) - 22)                          # a log header in the cut-off bytes;
                                                                               # -22 drops the inner end record, or
                                                                               # zipfile opens the inner zip instead
    z("Downloads/brand.zip", {"__MACOSX/brand/._Logo.zip": fork, "brand/logo.png": png})   # not a zip, no log
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr(f"logs/{u[6]}.jsonl", rec(11, "in a broken nested zip"))
    broken = inner.getvalue()[:-22]                                            # its end record gone
    z("Downloads/nested-broken.zip", {"inner/truncated.zip": broken,
                                      f".claude/projects/S/{u[2]}.jsonl": rec(5, "zip only")})
    cut("Downloads/cut-broken-nested.zip", [("x/broken.zip", broken), ("media/e.mp4", mp4)], stream=True)
    with open(w("Library/CloudStorage/OD/evicted.zip", b""), "wb") as fh:      # sparse: no blocks, like cloud-only
        fh.truncate(4096)
    return {f"{u[0]}.jsonl": 2, f"{u[0]}/subagents/agent-a1.jsonl": 1,
            f"{u[1]}/subagents/workflows/wf_1/agent-b.jsonl": 1, f"{u[2]}.jsonl": 1, f"{u[3]}.jsonl": 1,
            f"{u[4]}.jsonl": 2, f"vscode/{u[5]}.jsonl": 1, "history.jsonl": 2}


def selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("pass " if cond else "FAIL ") + name)

    with tempfile.TemporaryDirectory() as root:
        want = _plant(root)
        cache = os.path.join(root, "cache.json")
        inv = Inventory(root, cache=cache)
        reads = collections.Counter(c.key for c in inv.to_read())
        check("every planted log found, with the right number of distinct copies read",
              dict(reads) == want)
        check("identical archive copy not read twice", reads[next(k for k in want if want[k] == 2 and "/" not in k
                                                                  and not k.startswith("history"))] == 2)
        check("zip-only and nested-zip logs found", sum(1 for c in inv.to_read() if c.via != "disk") >= 3)
        check("non-log .jsonl counted as skipped", sum(inv.skipped.values()) == 1)
        check("mirror placeholder counted, not read", inv.zips["mirror"] == 1)
        dnr = {os.path.basename(p): why for p, why in inv.did_not_read}
        skip = {os.path.basename(p): why for p, why in inv.unreadable}
        check("broken zip outside the mirror listed under DID NOT READ",
              "the file ends inside a header at byte 0" in dnr.get("broken.zip", ""))
        check("a loose file named .zip with no zip signature (a macOS ._ fork) is NOT A ZIP, SKIPPED, printed once,"
              " not DID NOT READ", "._loose.zip" not in dnr and sum(
                  "NOT A ZIP, SKIPPED 1 files named .zip hold no zip signature" in ln and "._loose.zip" in ln
                  for ln in inv.coverage()) == 1)
        check("a sparse zip (no blocks, like cloud-only) listed under DID NOT READ", "cloud-only" in dnr.get("evicted.zip", ""))
        check("a cut zip whose logs are all held elsewhere is UNREADABLE, SKIPPED, not DID NOT READ",
              list(skip) == ["cut-media.zip"] and "4 whole members, 1 logs, each held identically" in skip["cut-media.zip"]
              and "'media/b.mp4'" in skip["cut-media.zip"])
        check("a cut zip holding a log found nowhere else is DID NOT READ, naming the log",
              f"first '.claude/projects/S/{'%08x-0000-4000-8000-%012x' % (7, 7)}.jsonl'" in dnr.get("cut-only-copy.zip", ""))
        check("a log header in a cut member's remaining bytes keeps its zip under DID NOT READ",
              "sits in the cut-off bytes of 'bundle.bin'" in dnr.get("cut-hides-log.zip", ""))
        check("coverage prints each walked zip once", [sum(z in ln and ln.lstrip().startswith(("UNREADABLE", "DID NOT"))
                                                           for ln in inv.coverage()) for z in (
            "cut-media.zip", "cut-only-copy.zip", "cut-hides-log.zip", "cut-broken-nested.zip")] == [1, 1, 1, 1])
        check("a member named .zip with no zip signature is NOT A ZIP, SKIPPED and keeps its zip off DID NOT READ",
              sorted(os.path.basename(p) for p, _ in inv.not_zip) == ["brand.zip", "cut-media.zip"]
              and "brand.zip" not in dnr and "cut-media.zip" in skip)
        check("a nested zip that will not open puts its zip under DID NOT READ; its other logs are still read",
              "nested zip 'inner/truncated.zip' could not be listed: BadZipFile" in dnr.get("nested-broken.zip", "")
              and any(c.path.endswith("nested-broken.zip") for cs in inv.logs.values() for c in cs))
        check("a walked zip holding a nested zip that will not open is DID NOT READ",
              "nested zip 'x/broken.zip' could not be listed" in dnr.get("cut-broken-nested.zip", ""))
        check("coverage counts the not-a-zip members on one line",
              sum("NOT A ZIP, SKIPPED 2 members named .zip in 2 zips" in ln for ln in inv.coverage()) == 1)
        check("nested copy reads back", b"nested" in next(c for c in inv.to_read() if c.via == "nested").open().read())
        again = Inventory(root, cache=cache)
        check("second run served from the cache, walks included; the sparse zip is not retried either",
              again.reread == 0 and again.did_not_read == inv.did_not_read and again.unreadable == inv.unreadable
              and sorted(again.not_zip) == sorted(inv.not_zip)
              and collections.Counter(c.key for c in again.to_read()) == reads)
        with open(cache, encoding="utf-8") as fh:
            data = json.load(fh)
        for e in data["zip"].values():
            e[4] = len(e[4])                                                   # an older cache format
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        old = Inventory(root, cache=cache)
        check("a cache entry holding a bare count of nested failures is re-listed, not trusted",
              old.reread == len(data["zip"]) and old.did_not_read == inv.did_not_read
              and sorted(old.not_zip) == sorted(inv.not_zip))
        check("a find that returns leaves no folder NOT SEARCHED or not opened",
              inv.not_searched == [] and inv.not_opened == [])
        held = os.path.join(root, "Library", "Containers")
        os.makedirs(held, exist_ok=True)
        with find_times_out(held):
            inv_h = Inventory(root, cache=cache)
        check("a Library folder whose find times out is NOT SEARCHED, printed once",
              inv_h.not_searched == [f"{held}: NOT SEARCHED, find timed out after {FIND_TIMEOUT}s"]
              and sum("Library/Containers: NOT SEARCHED" in ln for ln in inv_h.coverage()) == 1)
        # The finds below fail in a folder that has not changed, which a run with the folder index does not list
        # again, so these runs walk it all (--refresh), as a first run does. The folder index block below
        # plants the same failures in folders that changed.
        quit_at = os.path.join(root, ".claude")
        with find_quits(quit_at) as spans:
            inv_q = Inventory(root, refresh=True, cache=cache)
        copies = lambda i: sorted(c.path for cs in i.logs.values() for c in cs)
        check(f"a find that quits on fts_read, and on each of its {FIND_RERUNS} reruns, is NOT SEARCHED in full,"
              " printed once; what it printed first is kept",
              inv_q.not_searched == [f"{quit_at}: NOT SEARCHED in full, find quit on fts_read: Interrupted system call,"
                                     f" and again when rerun alone, {FIND_RERUNS} times"]
              and sum(s[0] == quit_at for s in spans) == 1 + FIND_RERUNS
              and inv_q.find_reruns == [(quit_at, FIND_RERUNS, False)]
              and sum("/.claude: NOT SEARCHED in full" in ln for ln in inv_q.coverage()) == 1
              and not any("finished when rerun" in ln for ln in inv_q.coverage())
              and copies(inv_q) == copies(inv))
        with find_quits(quit_at, times=1) as spans:
            inv_r = Inventory(root, refresh=True, cache=cache)
        mine = [s for s in spans if s[0] == quit_at]
        check("a find that quits once is rerun alone, after every other find, and is not NOT SEARCHED",
              inv_r.not_searched == [] and copies(inv_r) == copies(inv) and len(mine) == 2
              and mine[1][1] >= max(end for f, _, end in spans if f != quit_at)
              and inv_r.find_reruns == [(quit_at, 1, True)]
              and sum(ln.endswith("/.claude and finished when rerun alone") for ln in inv_r.coverage()) == 1)
        with find_quits(quit_at, times=3) as spans:
            inv_3 = Inventory(root, refresh=True, cache=cache)
        mine = [s for s in spans if s[0] == quit_at]
        check("a find that quits on its first try and its first 2 reruns, and finishes on the 3rd, is not NOT SEARCHED",
              inv_3.not_searched == [] and copies(inv_3) == copies(inv) and len(mine) == 4
              and min(s[1] for s in mine[1:]) >= max(end for f, _, end in spans if f != quit_at)
              and inv_3.find_reruns == [(quit_at, 3, True)]
              and sum(ln.endswith("/.claude and finished when rerun alone, on rerun 3") for ln in inv_3.coverage()) == 1)
        with find_quits(quit_at, error=f"{quit_at}/locked: Permission denied"):
            inv_d = Inventory(root, refresh=True, cache=cache)
        check("a find error naming one denied subfolder is not a quit; it is printed as could not open, and not opened",
              inv_d.not_searched == [] and inv_d.denied == [f"find: {quit_at}/locked: Permission denied"]
              and inv_d.not_opened == inv_d.denied
              and sum("could not open: find: " in ln and "/.claude/locked" in ln for ln in inv_d.coverage()) == 1
              and copies(inv_d) == copies(inv))
        with find_quits(quit_at, error=f"{quit_at}/tmp-1{GONE}"):
            inv_g = Inventory(root, refresh=True, cache=cache)
        check("a folder that went away during the walk and held no log at the last run is printed once, not a gap",
              inv_g.denied == [f"find: {quit_at}/tmp-1{GONE}"] and inv_g.not_opened == [] and inv_g.not_searched == []
              and [ln.strip() for ln in inv_g.coverage() if "/.claude/tmp-1" in ln]
              == [f"went away during the walk: {quit_at.replace(HOME, '~')}/tmp-1 (held no log at the last run)"])
        with find_quits(quit_at, error=f"{root}/Library/Mail: Operation not permitted"):
            inv_m = Inventory(root, refresh=True, cache=cache)
        check("a macOS-protected Library folder is not opened, and is named, not only counted",
              inv_m.not_opened == [f"find: {root}/Library/Mail: Operation not permitted"]
              and sum("/Library/Mail: Operation not permitted" in ln for ln in inv_m.coverage()) == 1)
        locked, desk = os.path.join(root, "Downloads", "locked"), os.path.join(root, "Desktop")
        os.makedirs(locked, exist_ok=True)
        try:
            os.chmod(locked, 0)
            inv_l = Inventory(root, cache=cache)
            os.chmod(locked, 0o755)
            os.chmod(desk, 0)
            inv_k = Inventory(root, refresh=True, cache=cache)
        finally:
            os.chmod(locked, 0o755)
            os.chmod(desk, 0o755)
        check("a real subfolder find may not read is not opened, by find's own message",
              inv_l.not_opened == [f"find: {locked}: Permission denied"])
        check("a real top folder that cannot be listed is not opened",
              inv_k.not_opened == [f"{desk}: Permission denied"])

        def while_gone(path, fn, before=False, refresh=False):
            """The inventory with `path` moved away around fn, or the error that stopped it."""
            try:
                with goes_away(path, globals(), fn, before):
                    return Inventory(root, refresh=refresh, cache=cache)
            except OSError as e:
                return e

        def gone_named(i, path):
            """`path` once under DID NOT READ as gone, in the list and in coverage, and every other copy read."""
            return (isinstance(i, Inventory) and [why for p, why in i.did_not_read if p == path] == [VANISHED]
                    and sum(path.replace(HOME, "~") in ln and VANISHED in ln for ln in i.coverage()) == 1
                    and sorted(c.label() for c in i.to_read())
                    == sorted(c.label() for c in inv.to_read() if c.path != path))
        zipped = os.path.join(root, "backup", ".claude.zip")
        disk = os.path.join(root, "Desktop", "same-size", "%08x-0000-4000-8000-%012x.jsonl" % (5, 5))
        check("a zip that goes away after the walk lists it is DID NOT READ as gone, once; every other copy is read",
              gone_named(while_gone(zipped, "_find"), zipped))
        check("a zip that goes away after its stat, before it is listed, is DID NOT READ as gone",
              gone_named(while_gone(zipped, "_list_zip", before=True, refresh=True), zipped))
        check("a log that goes away after its stat, before its checksum, is DID NOT READ as gone",
              gone_named(while_gone(disk, "_crc_file", before=True, refresh=True), disk))

        def while_timing_out(path, ns, fn):
            """The inventory with reads of `path` through fn timing out, or the error that stopped it."""
            try:
                with times_out(path, ns, fn):
                    return Inventory(root, refresh=True, cache=cache)
            except OSError as e:
                return e

        def unread_named(i, path):
            """`path` once under DID NOT READ with the timeout, in the list and in coverage, and every other
            copy read."""
            why = f"{UNREAD}: TimeoutError: [Errno 60] Operation timed out"
            return (isinstance(i, Inventory) and [w for p, w in i.did_not_read if p == path] == [why]
                    and sum(path.replace(HOME, "~") in ln and why in ln for ln in i.coverage()) == 1
                    and sorted(c.label() for c in i.to_read())
                    == sorted(c.label() for c in inv.to_read() if c.path != path))
        check("a log whose checksum read times out is DID NOT READ with the error, once; every other copy is read",
              unread_named(while_timing_out(disk, globals(), "_crc_file"), disk))
        check("a log whose stat times out is DID NOT READ with the error",
              unread_named(while_timing_out(disk, vars(os), "stat"), disk))
        check("a zip whose read times out is DID NOT READ with the error, not walked as a broken zip",
              unread_named(while_timing_out(zipped, globals(), "_list_zip"), zipped))

        # A folder that goes away while find walks it is a gap only when a log it held at the last run is found
        # nowhere this run. backup's zip holds 3 logs; the u[2] one is also, byte for byte, in nested-broken.zip.
        pcb = os.path.join(root, "backup")

        def while_moved(to):
            """The inventory with backup moved to `to` (None: out of the tree) as find reports it gone."""
            Inventory(root, cache=cache)                                       # the last run saw backup
            away = tempfile.mkdtemp()
            dest = to or os.path.join(away, "backup")
            os.replace(pcb, dest)
            try:
                with find_quits(quit_at, error=f"{pcb}{GONE}"):
                    return Inventory(root, refresh=True, cache=cache)
            finally:
                os.replace(dest, pcb)
                os.rmdir(away)
        said = lambda i: [ln.strip() for ln in i.coverage() if "went away during the walk" in ln]
        out = while_moved(None)
        check("a folder that went away during the walk is a gap when a log it held at the last run is found nowhere"
              " this run, named once with the count",
              len(out.not_searched) == 1 and out.not_searched[0].startswith(
                  f"{pcb} went away during the walk: 2 of the 3 logs it held at the last run not found this run")
              and len(said(out)) == 1)
        out = while_moved(os.path.join(root, ".Trash", "backup"))
        check("a folder that went away during the walk with each of its logs found elsewhere is printed once, not a gap",
              out.not_searched == [] and said(out) == [f"went away during the walk: {pcb.replace(HOME, '~')}"
                                                       " (3 logs it held at the last run, each found elsewhere)"])
        nocache = os.path.join(root, "no-cache.json")
        with find_quits(quit_at, error=f"{quit_at}/tmp-1{GONE}"):
            fresh = Inventory(root, cache=nocache)
        os.remove(nocache)
        check("with no cache from a last run, a folder that went away during the walk is a gap",
              fresh.not_searched == [f"{quit_at}/tmp-1 went away during the walk: no cache from a last run says what"
                                     " it held"] and len(said(fresh)) == 1)
        check("excluding a session drops its subagents too",
              not any(c.key.startswith("00000001") for c in inv.to_read("00000001")))
        import random
        rnd = random.Random(7)
        b64key = "".join(rnd.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/") for _ in range(86)) + "=="
        pat = "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(52))
        check("redact: planted shapes", all("[redacted]" in redact(s) for s in (
            "figd_" + "x" * 30, "sk-ant-api03-" + "A1b2" * 8, "ghp_" + "Z9" * 15, pat, "key " + b64key,
            "token=" + "abcd1234" * 2, "Authorization: Basic " + "QWxh" * 5)))
        keep = ("commit " + "0123456789abcdef" * 2 + "01234567" + " fixed the mobile card;"
                " ~/work/repos/shopFront/apps/web/src/components/ProductListingCard2/index.astro")
        check("redact: a full git SHA, a long path and ordinary text survive", redact(keep) == keep)

        # Mutations: break one rule at a time; the planted tree must notice.
        global SHAPES
        saved = SHAPES
        SHAPES = tuple(s for s in saved if s[0] != "subagent")
        inv_m = Inventory(root, refresh=True, cache=cache)
        mut1 = dict(collections.Counter(c.key for c in inv_m.to_read())) != want
        SHAPES = saved
        orig = _list_zip
        globals()["_list_zip"] = lambda p: (lambda r: ([m for m in r[0] if m[3] is None], r[1], r[2]))(orig(p))
        inv_m = Inventory(root, refresh=True, cache=cache)
        mut2 = dict(collections.Counter(c.key for c in inv_m.to_read())) != want
        globals()["_list_zip"] = orig
        check("mutation: dropping the subagent shape is caught", mut1)
        check("mutation: not opening nested zips is caught", mut2)
        for fn, fake, name, broken in (
                ("_held", lambda *a: True, "every walked log counted as held elsewhere",
                 lambda i: not any(p.endswith("cut-only-copy.zip") for p, _ in i.did_not_read)),
                ("_descriptor", lambda *a: None, "data descriptors not found",
                 lambda i: not any(p.endswith("cut-media.zip") for p, _ in i.unreadable)),
                ("_headers_in", lambda *a: [], "a cut member's remaining bytes not searched",
                 lambda i: not any(p.endswith("cut-hides-log.zip") for p, _ in i.did_not_read)),
                ("_zip_signed", lambda raw: False, "every nested failure counted as not a zip",
                 lambda i: not any(p.endswith("nested-broken.zip") for p, _ in i.did_not_read)),
                ("_zip_signed", lambda raw: True, "a file that only ends in .zip counted as a broken zip",
                 lambda i: any(p.endswith("brand.zip") for p, _ in i.did_not_read)),
                ("_signed_file", lambda path: True, "a file on disk that only ends in .zip counted as a broken zip",
                 lambda i: any(p.endswith("._loose.zip") for p, _ in i.did_not_read))):
            orig = globals()[fn]
            globals()[fn] = fake
            check(f"mutation: {name} is caught", broken(Inventory(root, refresh=True, cache=cache)))
            globals()[fn] = orig

    # Cloud placeholders: copies checksummed while local, then evicted (same size and mtime, no data).
    with tempfile.TemporaryDirectory() as root:
        k = ["%08x-0000-4000-8000-%012x" % (i, i) for i in (21, 22)]
        rec = lambda i, t: (json.dumps({"uuid": f"p{i}", "type": "user", "message": {"content": t}}) + "\n").encode()

        def put(rel, data):
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(data)
            return p

        def evict(p):
            """What a file provider does to free space: the same size and mtime, no data on disk."""
            st = os.stat(p)
            with open(p, "wb") as fh:
                fh.truncate(st.st_size)
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))

        both = rec(1, "live") + rec(2, "both")
        live = put(f".claude/projects/S/{k[0]}.jsonl", both)
        held = put(f"Library/CloudStorage/Drive/backup/cc-logs/projects/S/{k[0]}.jsonl", both)         # backed up whole
        other = put(f"Library/CloudStorage/Drive/backup/cc-logs/old/projects/S/{k[0]}.jsonl",
                    both + rec(3, "only in this copy"))                                            # larger, other bytes
        only = put(f"Library/CloudStorage/Drive/backup/cc-logs/old/x/projects/S/{k[1]}.jsonl", rec(4, "old machine only"))
        ozip = os.path.join(root, "Library/CloudStorage/Drive/backup/logs.zip")
        with zipfile.ZipFile(ozip, "w") as zf:
            zf.writestr(f".claude/projects/S/{k[1]}.jsonl", rec(4, "old machine only"))
        cache = os.path.join(root, "cache.json")
        Inventory(root, cache=cache)                                        # checksummed while the copies were local
        for p in (held, other, only, ozip):
            evict(p)
        read, cloud = [], {held, other, only, ozip}
        crc, lst = _crc_file, _list_zip
        globals()["_crc_file"] = lambda p: (read.append(p), crc(p))[1]
        globals()["_list_zip"] = lambda p: (read.append(p), lst(p))[1]
        try:
            ev = Inventory(root, cache=cache)
            cold = Inventory(root, cache=os.path.join(root, "cold.json"))   # no cache says what they held
        finally:
            globals()["_crc_file"], globals()["_list_zip"] = crc, lst
        unread = lambda i: sorted({p for p, _, _ in getattr(i, "cloud_unread", [])})
        check("placeholder: none is checksummed or listed, with a cache or without", not cloud & set(read))
        check("placeholder: none is read; its log's local copy is",
              [c.path for c in ev.to_read()] == [live] and [c.path for c in cold.to_read()] == [live])
        check("placeholder: one identical to a copy that is read (by its cached checksum) is not a gap",
              held not in unread(ev))
        check("placeholder: one that matches no copy read, or holds its log's only copy, is DID NOT READ",
              unread(ev) == sorted([other, only, ozip]) and unread(cold) == sorted(cloud))
        check("placeholder: coverage names them on one DID NOT READ line, and the inventory counts it as a gap",
              sum("DID NOT READ" in ln and "cloud placeholder" in ln for ln in ev.coverage()) == 1
              and sum("cloud placeholder" in w for _, w in ev.did_not_read) == 1)

    # PRUNE: a folder by one of those names is never walked, by the root listing, a piece's find, a folder
    # listed again or a new folder's find; an index walked under another prune list is not used.
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as aside:
        cache = os.path.join(aside, "cache.json")
        names = iter("%08x-0000-4000-8000-%012x.jsonl" % (i, i) for i in range(50, 90))
        copies = lambda i: sorted(c.path for cs in i.logs.values() for c in cs)

        def put(*rel):
            p = os.path.join(root, *rel, next(names))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(b'{"uuid": "p", "type": "user", "message": {"content": "x"}}\n')
            return p
        kept = [put("proj"), put("Desktop", "proj")]
        for n in PRUNE:
            put(n)                                     # in the root listing
            put("proj", n, "lib")                      # in a piece's find
            put("Desktop", "proj", n)                  # in a find under Desktop
        walked = Inventory(root, cache=cache)
        time.sleep(DIR_SLACK_NS / 1e9 + 0.1)
        Inventory(root, cache=cache)
        kept.append(put("proj"))                       # proj is listed again
        for n in PRUNE:
            put("proj", n, "more")
            put("new", n)                              # new is walked by its own find
        kept.append(put("new"))
        again = Inventory(root, cache=cache)
        check("PRUNE: no log under a pruned folder name is found, by the full walk or by the index",
              copies(walked) == sorted(kept[:2]) and copies(again) == sorted(kept)
              and not again.walked.startswith("walked"))
        with open(cache, encoding="utf-8") as fh:
            c = json.load(fh)
        c["walk"]["prune"] = ["node_modules"]
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(c, fh)
        check("PRUNE: an index walked under another prune list is not used: the run walks every folder",
              Inventory(root, cache=cache).walked.startswith("walked"))

    # A log that only grew is checksummed on from where the last run stopped; one rewritten too is read whole.
    with tempfile.TemporaryDirectory() as root:
        key = "%08x-0000-4000-8000-%012x.jsonl" % (9, 9)
        p = os.path.join(root, ".claude", "projects", "S", key)
        os.makedirs(os.path.dirname(p))
        line = lambda n: (json.dumps({"uuid": f"g{n}", "type": "user", "message": {"content": "y" * 300}}) + "\n").encode()
        caches = iter(range(100))

        def grow(n, change=None):
            """(the inventory's CRC, the file's CRC, read whole) after a log of n records grows by 5, its byte
            change first set to x. Each call starts from a cache of its own."""
            cache, whole, real = os.path.join(root, f"grow{next(caches)}.json"), [], _crc_file
            with open(p, "wb") as fh:
                fh.write(b"".join(line(i) for i in range(n)))
            Inventory(root, cache=cache)
            with open(p, "r+b") as fh:
                if change is not None:
                    fh.seek(change)
                    fh.write(b"x")
                fh.seek(0, 2)
                fh.write(b"".join(line(i) for i in range(n, n + 5)))
            globals()["_crc_file"] = lambda q: (whole.append(q), real(q))[1]
            try:
                got = Inventory(root, cache=cache).logs[key][0].crc
            finally:
                globals()["_crc_file"] = real
            with open(p, "rb") as fh:
                return got, zlib.crc32(fh.read()), bool(whole)

        size = len(b"".join(line(i) for i in range(40)))                    # about 13 KB, over two MARKs
        check("a grown log is checksummed on from the last run, not read whole, to the whole file's CRC",
              all(g == t and not w for g, t, w in (grow(40), grow(2))))
        check("a grown log whose first or last MARK bytes changed too is read whole, to the right CRC",
              all(g == t and w for g, t, w in (grow(40, 10), grow(40, size - 100))))
        real = _crc_grown

        def unmarked(path, hit, size):
            """_crc_grown handed the file's own marks, so it trusts any growth."""
            if not hit or len(hit) < 4:
                return None
            with open(path, "rb") as fh:
                mark = _marks(fh, hit[0])
            return real(path, hit[:3] + [mark], size)
        globals()["_crc_grown"] = unmarked
        try:
            caught = [g != t for g, t, w in (grow(40, 10), grow(40, size - 100))]
        finally:
            globals()["_crc_grown"] = real
        check("mutation: trusting growth without the marks is caught (head and tail changes)", caught == [True, True])

    # The folder index: with the last run's index, a run lists again only the folders whose ctime moved.
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as aside:
        _plant(root)
        cache = os.path.join(aside, "cache.json")      # outside the tree, so saving it changes no folder in it
        copies = lambda i: sorted(c.path for cs in i.logs.values() for c in cs)
        log = lambda n: (json.dumps({"uuid": f"i{n}", "type": "user", "message": {"content": "x"}}) + "\n").encode()
        finds, listed = [], []

        @contextlib.contextmanager
        def counting():
            """Each folder walked by a find, and each folder listed again on its own."""
            real, lister = subprocess.run, _list_one

            def run(cmd, **k):
                if cmd[:1] == ["/usr/bin/find"]:
                    finds.append(cmd[1])
                return real(cmd, **k)
            finds.clear()
            listed.clear()
            subprocess.run = run
            globals()["_list_one"] = lambda d: (listed.append(d), lister(d))[1]
            try:
                yield
            finally:
                subprocess.run = real
                globals()["_list_one"] = lister

        def settle():
            """Every folder clearly older than the next walk's start, and an index that trusts each one."""
            time.sleep(DIR_SLACK_NS / 1e9 + 0.1)
            Inventory(root, cache=cache)

        first = Inventory(root, cache=cache)
        settle()
        with counting():
            same = Inventory(root, cache=cache)
        check("index: a run with nothing changed lists no folder, and finds every log the full walk found",
              finds == [] and listed == [] and copies(same) == copies(first)
              and " 0 re-listed, 0 walked new;" in same.walked)

        deep = os.path.join(root, "Desktop", "_archive", "x", "session-logs")
        added = os.path.join(deep, "%08x-0000-4000-8000-%012x.jsonl" % (31, 31))
        with open(added, "wb") as fh:
            fh.write(log(1))
        two = os.path.join(root, "Desktop", "same-size2")
        os.remove(os.path.join(two, "%08x-0000-4000-8000-%012x.jsonl" % (5, 5)))
        os.rename(os.path.join(root, ".claude-old"), os.path.join(root, ".claude-old2"))
        made = os.path.join(root, "Downloads", "new", "a")
        os.makedirs(made)
        with open(os.path.join(made, "%08x-0000-4000-8000-%012x.jsonl" % (32, 32)), "wb") as fh:
            fh.write(log(2))
        with counting():
            inc = Inventory(root, cache=cache)
        full = Inventory(root, refresh=True, cache=os.path.join(aside, "full.json"))
        check("index: after a log is added deep, one removed, a folder renamed and a new tree made, the run finds"
              " what a full walk finds", copies(inc) == copies(full) and added in copies(inc)
              and not any("/.claude-old/" in p for p in copies(inc)) and any("/.claude-old2/" in p for p in copies(inc)))
        check("index: only the folders that changed are listed again, and only the new ones walked",
              sorted(listed) == sorted([deep, two, root, os.path.join(root, "Downloads")])
              and sorted(finds) == sorted([os.path.join(root, ".claude-old2"), os.path.join(root, "Downloads", "new")]))
        with counting():
            Inventory(root, cache=cache)
        check("index: a folder that changed within a second of the walk's start is listed again by the next run",
              deep in listed)

        settle()
        late = os.path.join(deep, "%08x-0000-4000-8000-%012x.jsonl" % (33, 33))
        with open(late, "wb") as fh:
            fh.write(log(3))
        with open(cache, encoding="utf-8") as fh, open(os.path.join(aside, "stale.json"), "w", encoding="utf-8") as to:
            to.write(fh.read())
        orig = _sweep
        globals()["_sweep"] = lambda folders: {d: e[0] for d, e in folders.items()}   # every folder looks unchanged
        try:
            stale = Inventory(root, cache=os.path.join(aside, "stale.json"))
        finally:
            globals()["_sweep"] = orig
        check("mutation: an index that misses a changed folder is caught (the log added there is not found)",
              late not in copies(stale) and late in copies(Inventory(root, cache=cache)))

        settle()
        qd = os.path.join(root, ".claude")
        with open(os.path.join(qd, "touched.txt"), "w") as fh:
            fh.write("x")
        import threading
        gate, lister, wait = threading.Event(), _list_one, FIND_TIMEOUT

        def held(d):
            if d == qd:
                gate.wait(10)                          # as when macOS holds a read for a prompt
            return lister(d)
        globals()["_list_one"], globals()["FIND_TIMEOUT"] = held, 1
        try:
            q = Inventory(root, cache=cache)
        finally:
            globals()["_list_one"], globals()["FIND_TIMEOUT"] = lister, wait
            gate.set()
        with counting():
            q2 = Inventory(root, cache=cache)
        names, gate = [f"f{i}" for i in range(40)], threading.Event()
        globals()["FIND_TIMEOUT"] = 1
        try:
            got = _each(names, lambda d: gate.wait(10) if d == "f0" else d)
        finally:
            globals()["FIND_TIMEOUT"] = wait
            gate.set()
        check("a folder whose stat or listing is held up holds up only itself: every other folder is answered",
              sorted(got) == sorted(names[1:]))
        check("index: a listing that does not return in time is NOT SEARCHED, and the next run walks that folder in"
              " full",
              q.not_searched == [f"{qd}: NOT SEARCHED, listing it did not return in 1s"]
              and qd in finds and copies(q2) == copies(Inventory(root, refresh=True, cache=os.path.join(aside, "f.json"))))

        with open(cache, encoding="utf-8") as fh:
            data = json.load(fh)
        data["walk"]["full_at"] -= FULL_WALK_EVERY
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        with counting():
            aged = Inventory(root, cache=cache)
        walks = list(finds)
        after = Inventory(root, cache=cache)
        check("index: one whose last full walk is a day old is not used; that run walks every folder, the next uses"
              " the new index", aged.walked.startswith("walked ") and len(walks) > 3
              and " re-listed, " in after.walked and after.walked.endswith("walked 0h ago"))

        settle()
        desk = os.path.join(root, "Desktop")
        os.chmod(desk, 0)
        try:
            shut = Inventory(root, cache=cache)
        finally:
            os.chmod(desk, 0o755)
        check("index: a folder that can no longer be listed is not opened, named once, not each folder under it",
              shut.not_opened == [f"{desk}: Permission denied"])

        bench = os.path.join(root, "Desktop", "bench", "runs")
        os.makedirs(bench)
        cap, logged = os.path.join(bench, "task1.jsonl"), os.path.join(bench, "task2.jsonl")
        for p, n in ((cap, 41), (logged, 1)):                 # session 1 has a log in the planted tree
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "system", "subtype": "init",
                                     "session_id": "%08x-0000-4000-8000-%012x" % (n, n)}) + "\n")
        with open(os.path.join(bench, "notes.jsonl"), "w", encoding="utf-8") as fh:   # some other jsonl
            fh.write(json.dumps({"type": "row", "session_id": "x"}) + "\n")
        runs = [Inventory(root, cache=cache) for _ in range(2)]
        key41 = HEADLESS + "%08x-0000-4000-8000-%012x.jsonl" % (41, 41)
        check("a claude -p capture whose session has no log here is read as kind headless, also from the cache;"
              " one whose session has a log is not read; another jsonl there stays skipped",
              all([p for p, _ in r.headless] == [cap] and r.headless_logged == 1
                  and [(c.kind, c.path) for c in r.to_read() if c.key.startswith(HEADLESS)] == [("headless", cap)]
                  and key41 in r.logs and logged not in {c.path for cs in r.logs.values() for c in cs}
                  and any(k.endswith("bench/runs") for k in r.skipped) and sum(r.skipped.values()) >= 1
                  and sum("read as kind headless" in ln and "1 more not read" in ln for ln in r.coverage()) == 1
                  for r in runs))

        cloud = os.path.join(root, "Library", "CloudStorage")
        backup = os.path.join(cloud, "OD", "_BACKUP", "S", "%08x-0000-4000-8000-%012x.jsonl" % (34, 34))
        os.makedirs(os.path.dirname(backup))
        with open(backup, "wb") as fh:
            fh.write(log(4))
        Inventory(root, cache=cache)                   # checksums the new log and lists the zips
        with find_times_out(cloud):
            gap = Inventory(root, refresh=True, cache=cache)
        back = Inventory(root, cache=cache)
        check("a run that could not search a folder keeps the cached checksums of what it held: the next run that"
              " reaches the folder reads none of it again",
              gap.not_searched == [f"{cloud}: NOT SEARCHED, find timed out after {FIND_TIMEOUT}s"]
              and backup not in copies(gap) and backup in copies(back) and back.reread == 0)
    # The helpers other tools use to find a project's folders.
    check("project_dir: every character that is not a letter or digit becomes '-'",
          project_dir("/a/b.c/d_e") == os.path.join(HOME, ".claude", "projects", "-a-b-c-d-e"))
    check("memory_dir: an argument wins, else the project's memory folder",
          memory_dir("/x/mem") == "/x/mem"
          and memory_dir(None, "/a/b") == os.path.join(HOME, ".claude", "projects", "-a-b", "memory"))
    bk = os.path.join(HOME, "my-backups")
    check("where: live, a configured backup folder, other",
          where(os.path.join(HOME, ".claude", "projects", "x.jsonl"), [bk]) == "live"
          and where(os.path.join(bk, "y.zip"), [bk]) == "backup"
          and where(os.path.join(HOME, "my-backups2", "y.zip"), [bk]) == "other")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="one line per log")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    inv = Inventory(default_root(), refresh=a.refresh)
    print("\n".join(inv.coverage()))
    if a.list:
        reads = collections.defaultdict(list)
        for c in inv.to_read():
            reads[c.key].append(c)
        for key in sorted(inv.logs):
            cs = inv.logs[key]
            print(f"{cs[0].kind:8} {key}  {len(cs)} copies, read {len(reads[key])}: "
                  + " | ".join(c.label() for c in reads[key]))
        for p, name in sorted(inv.not_zip):
            print(f"not a zip {p.replace(HOME, '~')} :: {name}")
        for p in sorted(inv.not_zip_files):
            print(f"not a zip {p.replace(HOME, '~')}")
        for p, label, key in sorted(inv.cloud_unread):
            print(f"placeholder, not read {label}" + (f"  ({key})" if key else "  (zip, contents unknown)"))
    return 0 if inv.logs and not inv.did_not_read and not inv.not_searched and not inv.not_opened else 2


if __name__ == "__main__":
    sys.exit(main())
