#!/usr/bin/env python3
"""verify_ledger.py -- "was this verified?" answered by a file, not by memory.

WHY THIS EXISTS. "Skipped a step but reported done" and "claimed it without checking the live source"
are the same defect: a completion claim with nothing behind it. A written rule against it gets read
and then not applied. A script that refuses cannot be skipped, which is why this is a script.

THE MECHANISM. A verdict
is keyed on (ARTIFACT, SHA). A new commit has no row, so `check` prints NOT-VERIFIED and exits 2
WITHOUT anyone deciding to invalidate anything. Re-editing a page silently voids its verdict, by
construction. That is the whole trick and it is why this beats a checklist.

    verify_ledger.py record <artifact> <sha> <verdict> <evidence> [verifier]
    verify_ledger.py check  <artifact> <sha>     # exit 0 verified, 2 NOT-VERIFIED, 3 non-passing
    verify_ledger.py summary
    verify_ledger.py sha [path]                  # HEAD, plus a hash of uncommitted and untracked
                                                 # changes; or a file's content hash

VERDICTS. Only the *-verified values pass. `eyeball-only` exists so that looking at a page is
recordable but never counts: the failure it guards against is a screenshot standing in for a
measurement. `blocked` is not a pass. `failed` needs a fix, not a re-check.
"""
import contextlib, hashlib, os, subprocess, sys, time

try:
    import fcntl
    msvcrt = None
except ImportError:  # Windows
    fcntl = None
    import msvcrt

# The Windows console is cp1252 and this file prints a glyph outside it. Without this the
# run dies with UnicodeEncodeError -- often at the LAST print, throwing away completed work.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Answer -h/--help with this module's own docstring, before any module-level work can pre-empt it.
if __name__ == "__main__" and any(_a in ("-h", "--help") for _a in __import__("sys").argv[1:]):
    print((__doc__ or "").strip() or "No docstring.")
    raise SystemExit(0)


HEADER = "artifact\tsha\tverdict\tevidence\tverifier\tts"
PASSING = {"qa-audit-verified", "glyph-contrast-verified", "build-gate-verified", "live-verified"}
VERDICTS = PASSING | {"eyeball-only", "blocked", "failed"}
STORE = os.environ.get("VERIFY_LEDGER") or os.path.join(
    os.path.expanduser("~"), ".claude", "verify-ledger.tsv")


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def cell(v, name):
    """One TSV cell. Strips separators and neutralises spreadsheet injection."""
    s = str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()
    if not s:
        die(f"{name} is empty")
    return "'" + s if s[0] in "=+-@" else s


def rows():
    if not os.path.exists(STORE):
        return []
    with open(STORE, encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    if not lines:
        return []
    if lines[0] != HEADER:
        die(f"{STORE} has an invalid header; expected: {HEADER}")
    out = []
    for i, ln in enumerate(lines[1:], start=2):
        parts = ln.split("\t")
        if len(parts) != 6:
            die(f"{STORE} line {i} is malformed ({len(parts)} cells, expected 6)")
        if parts[2] not in VERDICTS:
            die(f"{STORE} line {i} has invalid verdict {parts[2]!r}")
        out.append(parts)
    return out


@contextlib.contextmanager
def locked():
    """Hold an exclusive lock on STORE + ".lock" while record() reads the ledger and writes it back.
    Without it, two records at once each read the same rows, and the second replace dropped the first
    one's row. The lock file stays; the lock goes when this exits or the process dies."""
    fd = os.open(STORE + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:  # LK_LOCK gives up after ten one-second tries; keep waiting
                    pass
        try:
            yield
        finally:
            if not fcntl:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def record(artifact, sha, verdict, evidence, verifier="self"):
    if verdict not in VERDICTS:
        die("verdict must be one of: " + ", ".join(sorted(VERDICTS)))
    os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
    with locked():
        existing = rows()
        line = "\t".join([cell(artifact, "artifact"), cell(sha, "sha"), verdict,
                          cell(evidence, "evidence"), cell(verifier, "verifier"),
                          time.strftime("%Y-%m-%dT%H:%M:%S")])
        keep = [r for r in existing if not (r[0] == artifact and r[1] == sha)]
        tmp = f"{STORE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(HEADER + "\n")
            for r in keep:
                f.write("\t".join(r) + "\n")
            f.write(line + "\n")
        os.replace(tmp, STORE)
    print(f"recorded {artifact} @ {sha[:12]} -> {verdict}")


def check(artifact, sha):
    for r in rows():
        if r[0] == artifact and r[1] == sha:
            if r[2] in PASSING:
                print(f"VERIFIED\t{r[2]}\t{r[3]}\t{r[5]}")
                sys.exit(0)
            print(f"NOT-A-PASS\t{r[2]}\t{r[3]}", file=sys.stderr)
            sys.exit(3)
    print(f"NOT-VERIFIED\t{artifact}\t{sha[:12]}", file=sys.stderr)
    print("no row for this artifact at this revision; the artifact changed or was never verified",
          file=sys.stderr)
    sys.exit(2)


def summary():
    rs = rows()
    if not rs:
        print(f"{STORE}: empty")
        return
    counts = {}
    for r in rs:
        counts[r[2]] = counts.get(r[2], 0) + 1
    passing = sum(v for k, v in counts.items() if k in PASSING)
    print(f"{STORE}: {len(rs)} rows, {passing} passing, "
          f"{len({r[0] for r in rs})} distinct artifacts")
    for k in sorted(counts):
        print(f"  {k:26} {counts[k]:4d}{'' if k in PASSING else '   (not a pass)'}")


def sha_of(path=None):
    if path:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:40]
    # Not bare HEAD: HEAD does not move when you edit the working tree,
    # so the documented contract -- "an edit voids its own verdict, by construction" -- held only
    # if you committed between recording and checking. During a build, which is exactly when the
    # ledger is used, you can record a verdict, edit the page, re-check at the same HEAD and get
    # VERIFIED / exit 0 on the changed file. Fold the working-tree diff into the key so a dirty
    # tree is a different sha from the clean commit it sits on.
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        die("not a git repo and no path given; pass a file path to hash its contents")
    try:
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                               text=True, check=True).stdout.strip()
    except Exception:
        dirty = ""
    if not dirty:
        return head
    diff = subprocess.run(["git", "diff", "HEAD"], capture_output=True,
                          text=True).stdout
    h = hashlib.sha256(diff.encode("utf-8", "replace"))
    # `git diff HEAD` leaves out untracked files, so a file a build had just added could be edited
    # after its verdict and still check VERIFIED. A new page's files stay untracked until someone commits them, so fold each one in.
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True).stdout.strip()
    others = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"],
                            capture_output=True, cwd=top or None).stdout
    for rel in sorted(p for p in others.split(b"\0") if p):
        h.update(b"\0" + rel + b"\0")
        try:
            with open(os.path.join(top.encode(), rel), "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
        except OSError:
            pass  # a broken symlink, or a file deleted mid-run: its path still counts
    return head + "-dirty-" + h.hexdigest()[:8]


def main():
    a = sys.argv[1:]
    if not a:
        die(__doc__, 64)
    cmd = a[0]
    if cmd == "record" and len(a) >= 5:
        record(a[1], a[2], a[3], a[4], a[5] if len(a) > 5 else "self")
    elif cmd == "check" and len(a) == 3:
        check(a[1], a[2])
    elif cmd == "summary":
        summary()
    elif cmd == "sha":
        print(sha_of(a[1] if len(a) > 1 else None))
    else:
        die(__doc__, 64)


if __name__ == "__main__":
    main()
