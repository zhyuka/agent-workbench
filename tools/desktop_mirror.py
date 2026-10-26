#!/usr/bin/env python3
"""desktop_mirror.py - keep a browsable one-way copy of your working folder in a backup folder.

    python tools/desktop_mirror.py              mirror now, print one summary line
    python tools/desktop_mirror.py --dry-run    print what would change, change nothing
    python tools/desktop_mirror.py --selftest   planted tree: copy, skip, update, delete, guard

WHY THIS EXISTS. A working folder (often ~/Desktop) that is not inside a cloud drive never leaves the
machine. Real two-way sync is a poor fit for it: it holds hundreds of thousands of rebuildable files
(node_modules, .venv), and git repos and running dev servers should not sit inside a synced folder.
Run this every hour and at login from a scheduler.

CONFIG (agent-workbench config):
    backup.folder         a folder a cloud drive syncs (or an external disk). Required.
    mirror.source         the folder to copy (default: workspace.root, else ~/Desktop)
    mirror.name           the copy's folder name inside backup.folder (default: Mirror-<source name>);
                          git bundles go beside it in <name>-git
    mirror.sentinels      files the source must hold and be readable for a run to start
                          (default ["CLAUDE.md"])
    mirror.worktrees      folders (relative to the source) whose children are git worktrees: not
                          copied file by file; their uncommitted state is archived instead
    mirror.exclude        extra rsync exclude patterns (a leading / anchors at the source root)
    mirror.extra_repos    {"<key>": "<path>"}: git repos outside the source to bundle too (e.g.
                          ~/.claude when it is a repo); keys must not look like a source path

WHAT IT DOES. A one-way rsync of the source into <backup.folder>/<name> with --delete, so the copy
matches the source. It is a copy, not sync: an edit made in the copy is overwritten on the next run.
A file deleted here is deleted there; the cloud drive's recycle bin and version history keep the rest.

GIT. No `.git` and no worktree folder is copied file by file: a repack or a removed worktree makes
thousands of changes at once. Instead, before rsync, each repo under the source is backed up into
<name>-git/<path with / as __>/ (beside the copy, so rsync --delete leaves it alone):
  full.bundle   `git bundle create --all`, rebuilt weekly or when inc.bundle grows past 10% of it
  inc.bundle    every object since full.bundle's refs, rewritten only when the repo's refs change,
                so a commit uploads kilobytes, not the whole repo
  worktrees/<name>.tar.gz  for each worktree under mirror.worktrees: HEAD.txt, changes.patch
                (`git diff HEAD --binary`) and its untracked, non-ignored files, rewritten only when
                they change
To restore: `git clone --mirror full.bundle repo.git`, then `git -C repo.git fetch <path>/inc.bundle
'+refs/*:refs/*'` when there is an inc.bundle, then `git clone repo.git <dir>`; for a worktree,
`git worktree add` at HEAD.txt's commit, `git apply changes.patch`, and copy `untracked/` in. A repo
with no remote has its bundle as its only copy off the machine. A repo gone from the source has its
folder deleted here, and the copy's old `.git` entries and worktree folders are deleted once the git
backup has run clean.

PLACEHOLDERS. A cloud drive can free disk by turning files in the copy into cloud-only placeholders
(`ls -lO` shows `dataless` on macOS). When the source file changes, rsync memory-maps the old copy to
diff against it; inside a background job that can fail on a placeholder ("mmap: Resource deadlock
avoided") and rsync abandons the whole run, every time, unnoticed. So before rsync, each placeholder
whose source file changed (size or mtime) is deleted, and rsync copies that file as new.

NOT COPIED. Rebuildable folders (node_modules, .venv, dist, __pycache__, .astro, .cache,
.pytest_cache), .DS_Store, `.env` files (secrets; `.env.example` is kept), Claude Code settings files
and credentials (they can hold live tokens), desktop.ini and Office `~$` lock files (cloud drives
refuse to upload them), and mirror.exclude.

GUARD. A background job that loses its file-access permission sees an unreadable source, and rsync
--delete would then empty the copy. The run stops before rsync unless the source lists and every
sentinel reads. --max-delete caps one run at 1000 deletions; rsync exits 25 when it hits the cap,
and the next run deletes the next 1000.

VANISHED. A file rsync lists and then cannot open because it is gone (usually a temp file a tool
renamed into place) makes openrsync exit 23. Nothing failed: the file it became is copied on the next
run. So a run whose every error names a source file with "No such file or directory" (or real rsync's
exit 24, "file has vanished") counts as a pass, and the log still names the files.

ALERT. A failed run only writes to the log, and logs go unread. Each run counts failed runs in a row
in <log dir>/DesktopMirror.failstreak and posts a desktop notification from the second one on. A run
that exits 0 resets the count. --dry-run and --selftest never touch it. After each real run it also
calls scheduled_audits.py's watchdog, so each of the two jobs notices the other stopping.

Exit 0 when the copy is current; 1 when rsync or a git backup failed (its message is printed); 2 when
it could not run: the backup folder is not configured or missing, or the source failed the guard.
"""
import argparse
import datetime
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_config

HOME = os.path.expanduser("~")
RSYNC = shutil.which("rsync") or "/usr/bin/rsync"
GIT = shutil.which("git") or "/usr/bin/git"
LOG_DIR = (os.path.join(HOME, "Library", "Logs") if sys.platform == "darwin"
           else os.path.join(HOME, ".local", "state", "agent-workbench"))
FULL_EVERY_DAYS = 7
INC_SHARE = 0.10  # rebuild full.bundle once inc.bundle passes this share of it
UNTRACKED_MAX = 50 * 1024 * 1024  # per worktree; a bigger untracked file is listed, not packed
# Folders the repo search never enters: rebuildable, or (the worktrees) backed up through their repo.
NO_WALK = {"node_modules", ".venv", "dist", "__pycache__", ".astro", ".cache", ".pytest_cache", ".git"}
SENTINELS = ("CLAUDE.md",)
MAX_DELETE = 1000
WORKTREES = ()     # relative folders whose children are worktrees (mirror.worktrees)
STREAK = os.path.join(LOG_DIR, "DesktopMirror.failstreak")
ALERT_AFTER = 2
SF_DATALESS = 0x40000000  # sys/stat.h: the file's data is only in the cloud
# First match wins, so the .env.example include comes before the .env excludes.
# A leading / anchors a pattern at the source root; a trailing / matches folders only.
BASE_FILTERS = [
    "--include=.env.example",
    "--exclude=.env",
    "--exclude=.env.*",
    "--exclude=node_modules/",
    "--exclude=.venv/",
    "--exclude=dist/",
    "--exclude=__pycache__/",
    "--exclude=.astro/",
    "--exclude=.cache/",
    "--exclude=.pytest_cache/",
    "--exclude=.DS_Store",
    "--exclude=desktop.ini",
    "--exclude=Desktop.ini",
    "--exclude=~$*",
    # Claude Code settings can hold live tokens in `env`, the way .env files do.
    "--exclude=.claude/settings*.json",
    "--exclude=remote-settings.json",
    "--exclude=.credentials.json",
    # git goes to <name>-git/ as bundles (GIT, above). `.git` is a folder in a repo and a file in a
    # worktree; this matches both.
    "--exclude=.git",
]
FILTERS = list(BASE_FILTERS)


def configure(worktrees=None, exclude=None, sentinels=None):
    """Set the module's worktree folders, extra excludes and sentinels (from config, or a test)."""
    global WORKTREES, FILTERS, SENTINELS
    WORKTREES = tuple(w.strip("/") for w in (worktrees or ()))
    FILTERS = (BASE_FILTERS + [f"--exclude={e}" for e in (exclude or ())]
               + [f"--exclude=/{w}/" for w in WORKTREES])
    if sentinels:
        SENTINELS = tuple(sentinels)


def settings():
    """(source, dest, git dest, backup folder, extra repos) from config. Raises NotConfigured."""
    backup = os.path.expanduser(kit_config.need("backup", "folder"))
    source = kit_config.get("mirror", "source") or kit_config.get("workspace", "root") or "~/Desktop"
    source = os.path.expanduser(source)
    name = kit_config.get("mirror", "name") or "Mirror-" + os.path.basename(source.rstrip("/"))
    extra = tuple((k, os.path.expanduser(v)) for k, v in (kit_config.get("mirror", "extra_repos") or {}).items())
    configure(kit_config.get("mirror", "worktrees"), kit_config.get("mirror", "exclude"),
              kit_config.get("mirror", "sentinels"))
    return source, os.path.join(backup, name), os.path.join(backup, name + "-git"), backup, extra


def guard(src):
    """None when src looks like the real source folder, else the reason it does not."""
    try:
        os.listdir(src)
    except OSError as e:
        return f"cannot list {src}: {e.strerror}"
    for name in SENTINELS:
        path = os.path.join(src, name)
        try:
            with open(path, "rb") as f:
                f.read(1)
        except OSError as e:
            return f"cannot read {path}: {e.strerror}"
    return None


def drop_stale_placeholders(src, dest, dry_run=False,
                            cloud_only=lambda path, st: st.st_flags & SF_DATALESS):
    """Delete each cloud-only copy in dest whose src file changed, so rsync copies it whole.

    Returns how many it deleted (would delete, with dry_run)."""
    dropped = 0
    for root, _dirs, files in os.walk(dest):
        for name in files:
            d = os.path.join(root, name)
            try:
                st = os.lstat(d)
                if not cloud_only(d, st):
                    continue
                s = os.lstat(os.path.join(src, os.path.relpath(d, dest)))
            except OSError:
                continue
            if s.st_size != st.st_size or int(s.st_mtime) != int(st.st_mtime):
                if not dry_run:
                    os.remove(d)
                dropped += 1
    return dropped


def git(repo, *args):
    """Run git in repo. Returns the CompletedProcess, output as bytes."""
    return subprocess.run([GIT, "-C", repo, *args], capture_output=True)


def find_repos(src):
    """Relative paths of every git repo (a folder holding a .git folder) under src, sorted."""
    repos = []
    # The folders FILTERS excludes by path (the worktree folders, mirror.exclude's anchored folders).
    skip = {os.path.join(src, f[len("--exclude=/"):].rstrip("/"))
            for f in FILTERS if f.startswith("--exclude=/") and f.endswith("/")}
    for root, dirs, _files in os.walk(src):
        if ".git" in dirs:
            repos.append(os.path.relpath(root, src))
        dirs[:] = [d for d in dirs if d not in NO_WALK and os.path.join(root, d) not in skip]
    return sorted(repos)


def repo_key(rel):
    return "_desktop" if rel == "." else rel.replace(os.sep, "__")


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def put_file(path, data):
    """Write bytes or text to path through a temp file beside it, then os.replace."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def make_bundle(repo, path, revs):
    """git bundle create into a temp file, moved to path. None on success, 'empty' when there is
    nothing to bundle, else git's error."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=".tmp")
    os.close(fd)
    os.remove(tmp)  # git refuses to write over an existing file
    p = git(repo, "bundle", "create", "-q", tmp, *revs)
    if p.returncode:
        if os.path.exists(tmp):
            os.remove(tmp)
        err = p.stderr.decode("utf-8", "replace").strip()
        return "empty" if "empty bundle" in err else (err or f"git bundle exit {p.returncode}")
    os.replace(tmp, path)
    return None


def backup_repo(repo, out, dry_run=False, now=None):
    """Bring out/ (full.bundle, inc.bundle, full.refs, refs.txt) up to date with repo.

    Returns (what it did: 'full', 'inc', 'same' or 'empty'; error or None)."""
    p = git(repo, "for-each-ref", "--format=%(objectname) %(refname)")
    if p.returncode:
        return None, p.stderr.decode("utf-8", "replace").strip() or "git for-each-ref failed"
    refs = p.stdout.decode("utf-8", "replace")
    if not refs.strip():
        return "empty", None
    full, inc = os.path.join(out, "full.bundle"), os.path.join(out, "inc.bundle")
    full_refs = read_text(os.path.join(out, "full.refs"))
    now = time.time() if now is None else now
    need_full = (full_refs is None or not os.path.exists(full)
                 or now - os.path.getmtime(full) > FULL_EVERY_DAYS * 86400)
    if not need_full and read_text(os.path.join(out, "refs.txt")) == refs:
        return "same", None
    if dry_run:
        return ("full" if need_full else "inc"), None
    os.makedirs(out, exist_ok=True)
    if not need_full:
        # Everything reachable now and not from full.bundle's refs. A base object git has since
        # pruned can't be named, so it is left out of the --not list.
        base = sorted({l.split()[0] for l in full_refs.splitlines() if l.strip()})
        have = [s for s in base if git(repo, "cat-file", "-e", s).returncode == 0]
        err = make_bundle(repo, inc, ["--all", *("^" + s for s in have)])
        if err == "empty":
            # Every ref points into full.bundle; refs.txt still says where each one points.
            if os.path.exists(inc):
                os.remove(inc)
        elif err:
            return None, err
        elif os.path.getsize(inc) > INC_SHARE * os.path.getsize(full):
            need_full = True
        if not need_full:
            put_file(os.path.join(out, "refs.txt"), refs)
            return "inc", None
    err = make_bundle(repo, full, ["--all"])
    if err:
        return None, err
    put_file(os.path.join(out, "full.refs"), refs)
    put_file(os.path.join(out, "refs.txt"), refs)
    if os.path.exists(inc):
        os.remove(inc)
    return "full", None


def worktree_snapshot(wt):
    """(fingerprint, HEAD.txt text, patch bytes, untracked paths) for a worktree, or an error string."""
    head = git(wt, "rev-parse", "HEAD")
    branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
    patch = git(wt, "diff", "HEAD", "--binary")
    others = git(wt, "ls-files", "--others", "--exclude-standard", "-z")
    for p in (head, branch, patch, others):
        if p.returncode:
            return p.stderr.decode("utf-8", "replace").strip() or "git failed"
    head_txt = f"{head.stdout.decode().strip()} {branch.stdout.decode().strip()}\n"
    paths = [x for x in others.stdout.decode("utf-8", "replace").split("\0") if x]
    h = hashlib.sha256(head_txt.encode() + patch.stdout)
    for rel in paths:
        try:
            st = os.lstat(os.path.join(wt, rel))
            h.update(f"\0{rel}\0{st.st_size}\0{st.st_mtime_ns}".encode())
        except OSError:
            pass
    return h.hexdigest(), head_txt, patch.stdout, paths


def save_worktree(wt, out_tar, dry_run=False):
    """Write out_tar (HEAD.txt, changes.patch, untracked/) unless its .sha says it is current.

    Returns ('saved' or 'same', error or None)."""
    snap = worktree_snapshot(wt)
    if isinstance(snap, str):
        return None, snap
    digest, head_txt, patch, paths = snap
    sha_path = out_tar[:-len(".tar.gz")] + ".sha"
    if read_text(sha_path) == digest + "\n" and os.path.exists(out_tar):
        return "same", None
    if dry_run:
        return "saved", None
    os.makedirs(os.path.dirname(out_tar), exist_ok=True)
    buf = io.BytesIO()
    left, skipped = UNTRACKED_MAX, []
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, data):
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), int(time.time())
            tar.addfile(info, io.BytesIO(data))
        add("HEAD.txt", head_txt.encode())
        add("changes.patch", patch)
        for rel in paths:
            full = os.path.join(wt, rel)
            try:
                size = os.lstat(full).st_size
            except OSError:
                continue
            if size > left:
                skipped.append(f"{size} {rel}")
                continue
            left -= size
            tar.add(full, arcname="untracked/" + rel, recursive=False)
        if skipped:
            add("skipped.txt", ("over the size cap, not saved:\n" + "\n".join(skipped) + "\n").encode())
    put_file(out_tar, buf.getvalue())
    put_file(sha_path, digest + "\n")
    return "saved", None


def backup_git(src, gitdest, dry_run=False, home_repos=()):
    """Back up every repo under src, every worktree under WORKTREES and each (key, path) in
    home_repos into gitdest. Returns (counts, errors)."""
    counts = {"full": 0, "inc": 0, "same": 0, "empty": 0, "saved": 0, "kept": 0, "dropped": 0}
    errors = []
    repos = find_repos(src)
    keys = {repo_key(r): r for r in repos}
    for key, rel in keys.items():
        did, err = backup_repo(os.path.join(src, rel), os.path.join(gitdest, key), dry_run)
        if err:
            errors.append(f"{rel}: {err}")
        else:
            counts[did] += 1
    for key, path in home_repos:
        if not os.path.isdir(os.path.join(path, ".git")):
            errors.append(f"{path}: not a git repo")
            continue
        keys[key] = path       # listed, so the cleanup below keeps its backup
        did, err = backup_repo(path, os.path.join(gitdest, key), dry_run)
        if err:
            errors.append(f"{path}: {err}")
        else:
            counts[did] += 1
    # Worktrees: their commits are in the main repo's bundle; this keeps what is uncommitted.
    present = {}
    listed = True
    for wroot in WORKTREES:
        try:
            wts = sorted(os.listdir(os.path.join(src, wroot)))
        except OSError:
            wts, listed = [], False
        for name in wts:
            wt = os.path.join(src, wroot, name)
            if not os.path.isfile(os.path.join(wt, ".git")):
                continue
            p = git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")
            main = os.path.dirname(p.stdout.decode().strip()) if p.returncode == 0 else ""
            real = os.path.realpath(src)
            rel = os.path.relpath(os.path.realpath(main), real) if main else None
            if rel not in repos:
                errors.append(f"{wroot}/{name}: its repo is not under the source ({main or 'unknown'})")
                continue
            key = repo_key(rel)
            present.setdefault(key, set()).add(name)
            did, err = save_worktree(wt, os.path.join(gitdest, key, "worktrees", name + ".tar.gz"), dry_run)
            if err:
                errors.append(f"{wroot}/{name}: {err}")
            else:
                counts["saved" if did == "saved" else "kept"] += 1
    # A repo or worktree gone from the source: drop its backup, but only after a search that found
    # repos and worktree folders that listed, so an unreadable source deletes nothing.
    if repos and listed and os.path.isdir(gitdest) and not errors:
        for key in os.listdir(gitdest):
            path = os.path.join(gitdest, key)
            if not os.path.isdir(path):
                continue
            if key not in keys:
                counts["dropped"] += 1
                if not dry_run:
                    shutil.rmtree(path, ignore_errors=True)
                continue
            wdir = os.path.join(path, "worktrees")
            for f in os.listdir(wdir) if os.path.isdir(wdir) else []:
                name = f[:-len(".tar.gz")] if f.endswith(".tar.gz") else f[:-len(".sha")] if f.endswith(".sha") else None
                if name is not None and name not in present.get(key, set()):
                    if f.endswith(".tar.gz"):
                        counts["dropped"] += 1
                    if not dry_run:
                        os.remove(os.path.join(wdir, f))
    return counts, errors


def prune_excluded(dest, dry_run=False):
    """Delete the copy's .git entries and worktree folders, which rsync no longer updates.

    Returns how many it deleted (would delete, with dry_run)."""
    n = 0
    wts = {os.path.join(dest, w) for w in WORKTREES}
    for wt in sorted(wts):
        if os.path.isdir(wt):
            n += 1
            if not dry_run:
                shutil.rmtree(wt, ignore_errors=True)
    for root, dirs, files in os.walk(dest):
        dirs[:] = [d for d in dirs if os.path.join(root, d) not in wts]
        if ".git" in dirs:
            dirs.remove(".git")
            n += 1
            if not dry_run:
                shutil.rmtree(os.path.join(root, ".git"), ignore_errors=True)
        if ".git" in files:
            n += 1
            if not dry_run:
                os.remove(os.path.join(root, ".git"))
    return n


def mirror(src, dest, dry_run=False):
    """Run rsync. Returns (exit code, counts dict, stderr)."""
    # --omit-dir-times: -a alone sets every copy folder's time back to the source's on each run, so
    # nearly every folder reads as changed every hour. File times are still kept.
    cmd = [RSYNC, "-a", "--omit-dir-times", "--delete", f"--max-delete={MAX_DELETE}",
           "--itemize-changes", *FILTERS]
    if dry_run:
        cmd.append("--dry-run")
    cmd += [src.rstrip("/") + "/", dest]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    counts = {"new": 0, "updated": 0, "deleted": 0}
    # openrsync prints the cap message on stdout, so it is read from there too.
    notes = []
    for line in p.stdout.splitlines():
        if line.startswith("*deleting"):
            counts["deleted"] += 1
        elif line.startswith(">f+++"):
            counts["new"] += 1
        elif line.startswith(">f"):
            counts["updated"] += 1
        elif line.startswith("Deletions stopped"):
            notes.append(line)
        if dry_run:
            print(line)
    return p.returncode, counts, "\n".join([p.stderr.strip(), *notes]).strip()


def only_vanished(rc, err, src):
    """True when rsync's only errors are source files that were gone by the time it opened them."""
    lines = [l for l in err.splitlines() if l.strip()]
    prefix = src.rstrip("/") + "/"
    return (rc in (23, 24) and bool(lines)
            and all(prefix in l and re.search(r"No such file or directory|file has vanished", l)
                    for l in lines))


def run(src, dest, backup, dry_run=False, gitdest=None, home_repos=()):
    if not os.path.isdir(backup):
        print(f"backup folder missing, nothing would sync: {backup}")
        return 2
    reason = guard(src)
    if reason:
        print(f"stopped before rsync, the source failed the guard: {reason}")
        return 2
    gitdest = gitdest or dest.rstrip("/") + "-git"
    os.makedirs(dest, exist_ok=True)
    os.makedirs(gitdest, exist_ok=True)
    g, git_errors = backup_git(src, gitdest, dry_run, home_repos)
    pruned = 0 if git_errors else prune_excluded(dest, dry_run)
    dropped = drop_stale_placeholders(src, dest, dry_run)
    rc, c, err = mirror(src, dest, dry_run)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    verb = " (would be)" if dry_run else ""
    print(f"{stamp} {c['new']} new, {c['updated']} updated, {c['deleted']} deleted, "
          f"{dropped} cloud-only re-copied, {pruned} old .git copies deleted{verb}, rsync exit {rc}. "
          f"git: {g['full']} full + {g['inc']} inc bundles, {g['same']} unchanged, "
          f"{g['saved']} worktrees saved, {g['kept']} unchanged, {g['dropped']} dropped, "
          f"{len(git_errors)} failed. Dest {dest.replace(HOME, '~')}")
    for e in git_errors:
        print(f"git backup failed: {e}")
    if only_vanished(rc, err, src):
        print(err)
        print("counted as a pass: those files were gone before rsync opened them")
        return 1 if git_errors else 0
    if rc:
        print(err or "(rsync printed nothing)")
        return 1
    return 1 if git_errors else 0


def notify(message, title="Desktop mirror"):
    """Post a desktop notification (osascript on macOS, notify-send elsewhere when present). The text
    goes in as arguments, so no AppleScript quoting. scheduled_audits.py posts through this too."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/osascript", "-e", "on run argv",
                            "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                            "-e", "end run", message, title], capture_output=True)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], capture_output=True)
    except OSError:
        pass


def record(rc, path=STREAK, alert=notify):
    """Count failed runs in a row in `path`, alert from the ALERT_AFTER-th on. Returns the count."""
    try:
        with open(path, encoding="utf-8") as f:
            streak = int(f.read().strip() or 0)
    except (OSError, ValueError):
        streak = 0
    streak = streak + 1 if rc else 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{streak}\n")
    if streak >= ALERT_AFTER:
        alert(f"The mirror of the working folder has failed {streak} runs in a row (exit {rc}). "
              f"See its log in {LOG_DIR}")
        print(f"alerted: {streak} failed runs in a row")
    return streak


def selftest():
    global INC_SHARE, MAX_DELETE, WORKTREES, FILTERS, SENTINELS
    root = tempfile.mkdtemp(prefix="desktop_mirror_")
    src, od = os.path.join(root, "Work"), os.path.join(root, "Cloud")
    dest = os.path.join(od, "Mirror-Work")
    saved = (WORKTREES, FILTERS, SENTINELS)
    configure(["repos/wt"], ["/private-archive/", ".render-cache/"], ["CLAUDE.md", "WORKSPACE.md"])
    try:
        def put(rel, text="x"):
            path = os.path.join(src, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

        def sh(cwd, *args):
            p = subprocess.run([GIT, "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t",
                                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                               capture_output=True, text=True)
            assert p.returncode == 0, (args, p.stderr)
            return p.stdout.strip()

        def commit(repo, rel, text):
            put(os.path.join(repo, rel), text)
            sh(os.path.join(src, repo), "add", "-A")
            sh(os.path.join(src, repo), "commit", "-q", "-m", rel)

        for repo in ("tools", "repos/site"):
            os.makedirs(os.path.join(src, repo))
            sh(os.path.join(src, repo), "init", "-q", "-b", "main")
            commit(repo, "a.py", "1")

        for rel in ("CLAUDE.md", "WORKSPACE.md", "notes/notes.md", ".env.example",
                    ".env", "app/.env.local", "repos/site/node_modules/x.js", "repos/site/dist/i.html",
                    "py/.venv/bin/python", "tools/__pycache__/a.pyc", "personal/p.txt",
                    "notes/.DS_Store", "notes/~$deck.pptx",
                    "_archive/old/desktop.ini",
                    "private-archive/claude-config/CLAUDE.md",
                    # a broken repo in an excluded folder: the repo search must not enter it
                    "private-archive/claude-config/plugins/tmp/.git/HEAD",
                    "_archive/old/notes.md", ".claude/settings.local.json",
                    "_archive/v1/stage/.claude/settings.json", "_archive/v1/remote-settings.json",
                    "_archive/v1/.credentials.json", ".claude/launch.json",
                    "repos/site/.render-cache/a.png"):
            put(rel)
        os.makedirs(dest)
        with open(os.path.join(dest, "stale.txt"), "w") as f:
            f.write("gone from the source")

        results = []

        def check(label, ok):
            print(f"{'ok  ' if ok else 'FAIL'} {label}")
            results.append(ok)

        def exists(rel):
            return os.path.lexists(os.path.join(dest, rel))

        # A worktree of repos/site with a commit, a staged edit and an untracked file of its own.
        WT = WORKTREES[0]
        wt = os.path.join(src, WT, "live")
        sh(os.path.join(src, "repos/site"), "worktree", "add", "-q", "-b", "wt/live", wt)
        commit(os.path.join(WT, "live"), "b.py", "wt commit")
        put(os.path.join(WT, "live", "a.py"), "uncommitted edit")
        sh(wt, "add", "a.py")
        put(os.path.join(WT, "live", "new.md"), "untracked")
        # Copies an older version of this script left: a .git folder and a worktree folder.
        for rel in ("tools/.git/HEAD", WT + "/gone/.git", "repos/site/.git/config"):
            os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
            with open(os.path.join(dest, rel), "w") as f:
                f.write("old copy")

        gd = dest + "-git"
        check("run 1 exits 0", run(src, dest, od) == 0)
        for rel in ("CLAUDE.md", "notes/notes.md", ".env.example", "tools/a.py",
                    "personal/p.txt", "_archive/old/notes.md", ".claude/launch.json"):
            check(f"copied {rel}", exists(rel))
        for rel in ("tools/.git", "repos/site/.git", WT):
            check(f"skipped, and the old copy deleted: {rel}", not exists(rel))
        for key in ("tools", "repos__site"):
            check(f"git: {key} has full.bundle and refs.txt, no inc.bundle",
                  all(os.path.exists(os.path.join(gd, key, f)) for f in ("full.bundle", "refs.txt"))
                  and not os.path.exists(os.path.join(gd, key, "inc.bundle")))
        tar_path = os.path.join(gd, "repos__site", "worktrees", "live.tar.gz")
        with tarfile.open(tar_path) as t:
            names = set(t.getnames())
            patch = t.extractfile("changes.patch").read().decode()
            head = t.extractfile("HEAD.txt").read().decode()
        check("worktree: the archive holds HEAD, the staged edit and the untracked file",
              {"HEAD.txt", "changes.patch", "untracked/new.md"} <= names
              and "uncommitted edit" in patch and "wt/live" in head)

        def restore(key):
            out = os.path.join(root, "restore-" + key)
            shutil.rmtree(out, ignore_errors=True)
            sh(root, "clone", "-q", "--mirror", os.path.join(gd, key, "full.bundle"), out)
            inc = os.path.join(gd, key, "inc.bundle")
            if os.path.exists(inc):
                sh(out, "fetch", "-q", inc, "+refs/*:refs/*")
            return out

        check("git: the worktree's commit restores from the repo's bundle",
              sh(restore("repos__site"), "log", "-1", "--format=%s", "refs/heads/wt/live") == "b.py")
        full = os.path.join(gd, "tools", "full.bundle")
        before = os.path.getmtime(full)
        g, errs = backup_git(src, gd)
        check("git: nothing changed, nothing rewritten",
              not errs and g["same"] == 2 and g["kept"] == 1 and os.path.getmtime(full) == before)
        share, INC_SHARE = INC_SHARE, 10  # these repos are tiny, so any inc.bundle is a big share
        commit("tools", "c.py", "2")
        g, errs = backup_git(src, gd)
        INC_SHARE = share
        check("git: a commit writes inc.bundle and leaves full.bundle",
              not errs and g["inc"] == 1 and os.path.getmtime(full) == before
              and os.path.exists(os.path.join(gd, "tools", "inc.bundle")))
        check("git: full + inc restores the new commit",
              sh(restore("tools"), "log", "-1", "--format=%s", "refs/heads/main") == "c.py")
        share, INC_SHARE = INC_SHARE, 0
        commit("tools", "d.py", "3")
        g, errs = backup_git(src, gd)
        INC_SHARE = share
        check("git: inc.bundle past its share of full rebuilds full and drops inc",
              not errs and g["full"] == 1 and not os.path.exists(os.path.join(gd, "tools", "inc.bundle"))
              and sh(restore("tools"), "log", "-1", "--format=%s", "refs/heads/main") == "d.py")
        os.utime(full, (0, 0))
        g, _ = backup_git(src, gd)
        check("git: a full.bundle older than a week is rebuilt", g["full"] == 1)
        home = os.path.join(root, "home-repo")      # a setup repo outside the source, like ~/.claude
        os.makedirs(home)
        sh(home, "init", "-q", "-b", "main")
        with open(os.path.join(home, "CLAUDE.md"), "w") as f:
            f.write("rules")
        sh(home, "add", "-A")
        sh(home, "commit", "-q", "-m", "home")
        g, errs = backup_git(src, gd, home_repos=(("_home__x", home),))
        check("git: a repo outside the source is bundled under its key",
              not errs and sh(restore("_home__x"), "log", "-1", "--format=%s", "refs/heads/main") == "home")
        g, errs = backup_git(src, gd, home_repos=(("_home__x", home),))
        check("git: the next run keeps it, not dropped as gone from the source",
              not errs and g["dropped"] == 0 and os.path.exists(os.path.join(gd, "_home__x", "full.bundle")))
        g, errs = backup_git(src, gd, home_repos=(("_home__y", os.path.join(root, "nope")),))
        check("git: a missing extra repo is an error, not a silent skip",
              any("not a git repo" in e for e in errs))
        shutil.rmtree(os.path.join(gd, "_home__x"), ignore_errors=True)
        sh(os.path.join(src, "repos/site"), "worktree", "remove", "--force", wt)
        g, errs = backup_git(src, gd)
        check("worktree: a removed worktree's archive is dropped",
              not errs and g["dropped"] == 1 and not os.path.exists(tar_path))
        os.makedirs(os.path.join(gd, "repos__moved"))
        away = os.path.join(src, "repos", "wt-away")
        os.makedirs(os.path.join(src, WT), exist_ok=True)
        os.rename(os.path.join(src, WT), away)
        backup_git(src, gd)
        check("git: no worktree folder to list drops nothing", os.path.isdir(os.path.join(gd, "repos__moved")))
        os.rename(away, os.path.join(src, WT))
        g, _ = backup_git(src, gd)
        check("git: a repo gone from the source has its folder dropped",
              g["dropped"] == 1 and not os.path.exists(os.path.join(gd, "repos__moved")))
        os.makedirs(os.path.join(src, "broken", ".git"))
        os.makedirs(os.path.join(dest, "keep"))
        with open(os.path.join(dest, "keep", ".git"), "w") as f:
            f.write("old copy")
        check("git: a failed backup fails the run", run(src, dest, od) == 1)
        check("git: a failed backup deletes no old copy", exists("keep/.git"))
        shutil.rmtree(os.path.join(src, "broken"))
        for planted in ("keep", "broken"):
            shutil.rmtree(os.path.join(dest, planted))

        for rel in (".env", "app/.env.local", "repos/site/node_modules", "repos/site/dist",
                    "py/.venv", "tools/__pycache__", "notes/.DS_Store",
                    "notes/~$deck.pptx", "_archive/old/desktop.ini",
                    "private-archive", ".claude/settings.local.json",
                    "_archive/v1/stage/.claude/settings.json", "_archive/v1/remote-settings.json",
                    "_archive/v1/.credentials.json", "repos/site/.render-cache"):
            check(f"skipped {rel}", not exists(rel))
        check("deleted stale.txt", not exists("stale.txt"))

        put("notes/notes.md", "changed, longer")
        old = 1767225600  # a copy folder's time that drifted from the source's
        os.utime(os.path.join(dest, "personal"), (old, old))  # nothing in it changes this run
        rc, c, _ = mirror(src, dest)
        check(f"run 2 sees exactly 1 updated, 0 new, 0 deleted (saw {c})",
              rc == 0 and c == {"new": 0, "updated": 1, "deleted": 0})
        check("run 2 leaves a copy folder's time alone",
              int(os.stat(os.path.join(dest, "personal")).st_mtime) == old)

        os.remove(os.path.join(src, "CLAUDE.md"))
        check("guard: a missing sentinel exits 2", run(src, dest, od) == 2)
        check("guard: the copy is untouched", exists("CLAUDE.md") and exists("notes/notes.md"))
        put("CLAUDE.md")
        check("missing backup folder exits 2", run(src, dest, os.path.join(root, "nope")) == 2)

        # A placeholder can't be planted in a temp folder, so the test names which copies count as one.
        put("notes/same.md", "unchanged")
        mirror(src, dest)
        put("notes/notes.md", "changed again, and longer still")
        fake = {os.path.join(dest, "notes", n) for n in ("notes.md", "same.md")}
        n = drop_stale_placeholders(src, dest, cloud_only=lambda path, st: path in fake)
        check("placeholders: only the changed one is deleted",
              n == 1 and not exists("notes/notes.md") and exists("notes/same.md"))
        rc, c, _ = mirror(src, dest)
        check("placeholders: rsync copies it back as new",
              rc == 0 and c["new"] == 1 and exists("notes/notes.md"))

        cap, MAX_DELETE = MAX_DELETE, 2
        for i in range(4):
            with open(os.path.join(dest, f"extra{i}.txt"), "w") as f:
                f.write("gone from the source")
        rc, _, err = mirror(src, dest)
        MAX_DELETE = cap
        check("cap: exit 25 says it hit the cap", rc == 25 and "max-delete" in err.lower())

        # The shape openrsync prints for files renamed away mid-run, under the planted source.
        gone = "\n".join(f"rsync(11768): error: {src}/notes/{n}/build.png.new: "
                         "open (2) in /: No such file or directory" for n in ("a", "b"))
        check("vanished: exit 23 naming only gone source files is a pass", only_vanished(23, gone, src))
        check("vanished: real rsync's exit 24 is a pass",
              only_vanished(24, f'file has vanished: "{src}/a.png.tmp"', src))
        check("vanished: one other error with them is a failure",
              not only_vanished(23, gone + f"\nrsync: error: {src}/b.md: open (13): Permission denied", src))
        check("vanished: a missing backup path is a failure",
              not only_vanished(23, f"rsync: error: {dest}/x: mkdir (2): No such file or directory", src))
        check("vanished: exit 23 with no message is a failure", not only_vanished(23, "", src))
        check("vanished: the cap is still a failure", not only_vanished(25, gone, src))

        sent = []
        streak = os.path.join(root, "failstreak")
        check("alert: 1st failure is quiet", record(1, streak, sent.append) == 1 and not sent)
        check("alert: 2nd failure in a row alerts", record(2, streak, sent.append) == 2 and len(sent) == 1)
        check("alert: a clean run resets the count", record(0, streak, sent.append) == 0 and len(sent) == 1)

        print(f"selftest: {sum(results)} of {len(results)} checks passed")
        return 0 if all(results) else 1
    finally:
        WORKTREES, FILTERS, SENTINELS = saved
        shutil.rmtree(root, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    try:
        source, dest, gitdest, backup, extra = settings()
    except kit_config.NotConfigured as e:
        print(f"DID NOT RUN: {e}")
        sys.exit(2)
    rc = run(source, dest, backup, a.dry_run, gitdest, home_repos=extra)
    if not a.dry_run:
        record(rc)
        # This job runs every hour on its own clock, so it is the one that can notice the daily
        # audits or another scheduled job stopping (scheduled_audits.watchdog).
        try:
            from scheduled_audits import watchdog_alert
            for problem in watchdog_alert(alert=lambda m: notify(m, "Workspace watchdog")).values():
                print(f"watchdog: {problem}")
        except Exception as e:
            print(f"watchdog: DID NOT RUN ({type(e).__name__}: {e})")
    sys.exit(rc)


if __name__ == "__main__":
    main()
