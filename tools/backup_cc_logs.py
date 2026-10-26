#!/usr/bin/env python3
"""backup_cc_logs.py - copy ~/.claude/projects to a backup folder so session logs outlive Claude Code's
log cleanup, and ~/.claude/skills so your personal skills outlive this machine.

    python tools/backup_cc_logs.py [--dest DIR]   copy what is new or changed, print one summary line
    python tools/backup_cc_logs.py --selftest     planted tree: new, unchanged, appended, rewritten, deleted, skills

WHY THIS EXISTS. Claude Code deletes a session log `cleanupPeriodDays` after it was last written
(30 by default; an organisation can set it). The logs are the record of what was asked, decided and
done, and nothing else keeps them. Run this daily (and at login) from a scheduler.

WHERE IT COPIES. <backup>/cc-logs/, where <backup> is `backup.folder` in the agent-workbench config: any
folder a cloud drive syncs (iCloud Drive, Dropbox, Google Drive, OneDrive) or an external disk.
--dest names the cc-logs folder directly. It refuses to run when the backup folder is missing,
because a copy into a folder that does not sync protects nothing.

WHAT IT DOES. Mirrors every file under ~/.claude/projects (session logs, subagent logs, tool results,
memory) into `cc-logs/projects/`, keeping the same relative paths, so a tool that reads session logs
finds each copy by its usual shape. It never deletes: a log the cleanup removes stays in the backup.
  - A file whose size and mtime match its copy is skipped without being read.
  - A grown .jsonl whose old copy is a byte prefix of it is overwritten (an append loses nothing).
  - Any other change keeps the old copy at `cc-logs/superseded/<YYYYMMDD-HHMMSS>/projects/<same path>`
    first, because a resumed or compacted session can rewrite records.
  - A .jsonl is copied up to its last newline, so a session being written never leaves a half record.
Writes are atomic: temp file in the target folder, fsync, os.replace.

ALSO ~/.claude/skills. Claude Code loads personal skills from there, and that folder is often not a
git repo. Each file goes to `cc-logs/skills/<same path>` by the rules above, so an edited file keeps
its old copy under `superseded/<stamp>/skills/` and a deleted skill stays in the backup. Skips
`synced/` (the desktop app's own copies of its skills) and `.trash/`. A missing folder is skipped.

NOT COPIED. ~/.claude/history.jsonl (a dot-folder in a cloud drive is not reliably synced), `.DS_Store`.

Exit 0 when every file is copied or current; 1 when any file failed (each is named); 2 when it could
not run: no backup folder configured or it is missing, or ~/.claude/projects is missing.
"""
import argparse
import datetime
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_config

HOME = os.path.expanduser("~")
SRC = os.path.join(HOME, ".claude", "projects")
SKILLS = os.path.join(HOME, ".claude", "skills")
SKILLS_SKIP = {"synced", ".trash"}  # top-level folders under SKILLS that are not backed up
CHUNK = 1 << 20


def backup_root():
    """The configured backup folder (backup.folder), expanded. Raises kit_config.NotConfigured."""
    return os.path.expanduser(kit_config.need("backup", "folder"))


def dest_root():
    return os.path.join(backup_root(), "cc-logs")


def _payload(path):
    """The bytes to back up: the whole file, or a .jsonl up to its last newline."""
    with open(path, "rb") as f:
        data = f.read()
    if path.endswith(".jsonl"):
        data = data[:data.rfind(b"\n") + 1]
    return data


def _is_prefix(old_path, data):
    """True when the copy on file is a byte prefix of the new payload."""
    if os.path.getsize(old_path) > len(data):
        return False
    with open(old_path, "rb") as f:
        pos = 0
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                return True
            if data[pos:pos + len(chunk)] != chunk:
                return False
            pos += len(chunk)


def _write(target, data, mtime):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.utime(tmp, (mtime, mtime))
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _files(base, skip=()):
    """(path, name, path relative to base) for every file under base, in a fixed order, leaving out
    the top-level folders named in skip."""
    for root, dirs, files in os.walk(base):
        if root == base:
            dirs[:] = [d for d in dirs if d not in skip]
        dirs.sort()
        for name in sorted(files):
            if name != ".DS_Store":
                path = os.path.join(root, name)
                yield path, name, os.path.relpath(path, base)


def _jobs(src, skills=None):
    """(path, name, label, output path under dest): src's files, then skills' (ALSO above)."""
    for path, name, rel in _files(src):
        yield path, name, rel, os.path.join("projects", rel)
    if skills and os.path.isdir(skills):
        for path, name, rel in _files(skills, SKILLS_SKIP):
            yield path, name, "skills/" + rel, os.path.join("skills", rel)


def backup(src, dest, stamp, skills=None):
    """Copy src into dest/projects, and `skills` as ALSO above says. Returns (counts, failures)."""
    n = {"files": 0, "new": 0, "appended": 0, "superseded": 0, "current": 0, "skills": 0}
    failed = []
    for path, name, label, out in _jobs(src, skills):
        target = os.path.join(dest, out)
        n["files"] += 1
        n["skills"] += label.startswith("skills/")
        try:
            st = os.stat(path)
            if os.path.exists(target):
                tst = os.stat(target)
                if int(tst.st_mtime) == int(st.st_mtime) and (
                        tst.st_size == st.st_size or name.endswith(".jsonl") and tst.st_size <= st.st_size):
                    n["current"] += 1
                    continue
            data = _payload(path)
            if not os.path.exists(target):
                n["new"] += 1
            elif os.path.getsize(target) == len(data) and _is_prefix(target, data):
                os.utime(target, (st.st_mtime, st.st_mtime))
                n["current"] += 1
                continue
            elif name.endswith(".jsonl") and _is_prefix(target, data):
                n["appended"] += 1
            else:
                keep = os.path.join(dest, "superseded", stamp, out)
                os.makedirs(os.path.dirname(keep), exist_ok=True)
                shutil.copy2(target, keep)
                n["superseded"] += 1
            _write(target, data, st.st_mtime)
        except OSError as e:
            failed.append(f"{label}: {type(e).__name__}: {e.strerror or e}")
    return n, failed


def run(dest=None):
    if dest is None:
        try:
            root = backup_root()
        except kit_config.NotConfigured as e:
            print(f"DID NOT RUN: {e}")
            return 2
        if not os.path.isdir(root):
            print(f"DID NOT RUN: the backup folder {root.replace(HOME, '~')} is missing, so a copy would not sync")
            return 2
        dest = os.path.join(root, "cc-logs")
    elif not os.path.isdir(os.path.dirname(os.path.abspath(dest))):
        print(f"DID NOT RUN: the folder above {dest} is missing")
        return 2
    if not os.path.isdir(SRC):
        print(f"DID NOT RUN: {SRC.replace(HOME, '~')} is missing")
        return 2
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    n, failed = backup(SRC, dest, stamp, SKILLS)
    print(f"{stamp} {n['files']} files under ~/.claude/projects and skills "
          f"({n['skills']} of them skill files): {n['new']} new, "
          f"{n['appended']} appended, "
          f"{n['superseded']} changed (old copy kept), {n['current']} already current, {len(failed)} failed. "
          f"Dest {dest.replace(HOME, '~')}")
    for line in failed:
        print("FAILED", line)
    return 1 if failed else 0


def selftest():
    ok = True

    def check(label, cond):
        nonlocal ok
        ok = ok and cond
        print(("PASS " if cond else "FAIL ") + label)

    def put(path, data, mtime=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        if mtime:
            os.utime(path, (mtime, mtime))

    def read(path):
        with open(path, "rb") as f:
            return f.read()

    u = "0b0b0b0b-1111-2222-3333-444444444444"
    with tempfile.TemporaryDirectory() as t:
        src, dest = os.path.join(t, "projects"), os.path.join(t, "cc-logs")
        main = os.path.join(src, "S", u + ".jsonl")
        sub = os.path.join(src, "S", u, "subagents", "agent-a.jsonl")
        meta = os.path.join(src, "S", u, "subagents", "agent-a.meta.json")
        put(main, b'{"a":1}\n{"b":', 1_000_000)                   # half-written last record
        put(sub, b'{"s":1}\n', 1_000_000)
        put(meta, b'{"m":1}', 1_000_000)
        put(os.path.join(src, "S", ".DS_Store"), b"x")
        n, f = backup(src, dest, "R1")
        bm = os.path.join(dest, "projects", "S", u + ".jsonl")
        check("first run copies 3 files, skips .DS_Store", n["new"] == 3 and n["files"] == 3 and not f)
        check("a half-written record is not copied", read(bm) == b'{"a":1}\n')
        check("copy keeps the source mtime", int(os.stat(bm).st_mtime) == 1_000_000)

        n, f = backup(src, dest, "R2")
        check("second run with no change copies nothing", n["current"] == 3 and n["new"] + n["appended"] == 0)

        put(main, b'{"a":1}\n{"b":2}\n', 1_000_100)
        n, f = backup(src, dest, "R3")
        check("an append overwrites, keeps no old copy",
              n["appended"] == 1 and read(bm) == b'{"a":1}\n{"b":2}\n' and not os.path.exists(os.path.join(dest, "superseded")))

        put(main, b'{"c":3}\n', 1_000_200)
        n, f = backup(src, dest, "R4")
        kept = os.path.join(dest, "superseded", "R4", "projects", "S", u + ".jsonl")
        check("a rewrite keeps the old copy under superseded/",
              n["superseded"] == 1 and read(kept) == b'{"a":1}\n{"b":2}\n' and read(bm) == b'{"c":3}\n')

        os.remove(sub)
        n, f = backup(src, dest, "R5")
        check("a log deleted at the source stays in the backup",
              os.path.exists(os.path.join(dest, "projects", "S", u, "subagents", "agent-a.jsonl")))

        os.chmod(meta, 0)
        put(meta + "2", b"{}", 1_000_000)
        os.utime(meta, (1_000_300, 1_000_300))
        n, f = backup(src, dest, "R6")
        os.chmod(meta, 0o600)
        check("an unreadable file is named as failed, the rest still copy",
              len(f) == 1 and "agent-a.meta.json" in f[0] and os.path.exists(os.path.join(dest, "projects", "S", u, "subagents", "agent-a.meta.json2")))

        # skills (ALSO above): copied beside projects/, synced/ and .trash/ left out, edits versioned.
        src3, sk, dest3 = (os.path.join(t, d) for d in ("p3", "sk", "d3"))
        put(os.path.join(src3, "S", u + ".jsonl"), b'{"a":1}\n', 1_000_000)
        put(os.path.join(sk, "ask", "SKILL.md"), b"v1", 1_000_000)
        put(os.path.join(sk, "ask", "references", "r.md"), b"r", 1_000_000)
        put(os.path.join(sk, "synced", "app", "SKILL.md"), b"app", 1_000_000)
        put(os.path.join(sk, ".trash", "old", "SKILL.md"), b"old", 1_000_000)
        n, f = backup(src3, dest3, "K1", sk)
        check("skills copy to skills/ at the same paths, without synced/ or .trash/",
              n["skills"] == 2 and n["files"] == 3 and not f
              and read(os.path.join(dest3, "skills", "ask", "SKILL.md")) == b"v1"
              and os.path.exists(os.path.join(dest3, "skills", "ask", "references", "r.md"))
              and not os.path.exists(os.path.join(dest3, "skills", "synced"))
              and not os.path.exists(os.path.join(dest3, "skills", ".trash")))
        put(os.path.join(sk, "ask", "SKILL.md"), b"v2", 1_000_100)
        os.remove(os.path.join(sk, "ask", "references", "r.md"))
        n, f = backup(src3, dest3, "K2", sk)
        check("an edited SKILL.md keeps its old copy under superseded/; a deleted skill file stays",
              n["superseded"] == 1 and not f
              and read(os.path.join(dest3, "skills", "ask", "SKILL.md")) == b"v2"
              and read(os.path.join(dest3, "superseded", "K2", "skills", "ask", "SKILL.md")) == b"v1"
              and os.path.exists(os.path.join(dest3, "skills", "ask", "references", "r.md")))
        n, f = backup(src3, dest3, "K3", os.path.join(t, "absent"))
        check("a missing skills folder is skipped", n["skills"] == 0 and n["files"] == 1 and not f)

    print("selftest", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    # A help probe or a typo'd flag must never fall through to a real backup.
    ap = argparse.ArgumentParser(prog="backup_cc_logs.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", help="the cc-logs folder to copy into (default <backup.folder>/cc-logs)")
    ap.add_argument("--selftest", action="store_true",
                    help="planted tree: new, unchanged, appended, rewritten, deleted, skills")
    a = ap.parse_args()
    sys.exit(selftest() if a.selftest else run(a.dest))
