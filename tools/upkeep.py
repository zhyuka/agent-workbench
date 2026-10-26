#!/usr/bin/env python3
"""upkeep.py - the cleanups that are safe without a person, for the daily run.

    python tools/upkeep.py cleanup [--dry-run] [--json FILE]   remove what is regenerable, merged or empty
    python tools/upkeep.py settings [--user] [--dry-run]       the settings-allow step alone
    python tools/upkeep.py --selftest                          planted trees: each cleanup takes its bad case only

WHY THIS EXISTS. Cleanups that need no judgement get asked for by hand again and again, while a
daily audit log of red results goes unread. scheduled_audits.py runs `cleanup` first every day, so
the audits see the cleaned state, and today.md lists what each step removed and what it kept.

CLEANUP. Each step prints `<step>: removed N of M` and names what it kept and why. --dry-run removes
nothing and says what it would. A step with nothing configured has nothing to do (0 of 0).
  worktree-prune     `git worktree prune` in every clone: a registration whose folder is gone
  merged-branches    `git branch -d` of a local branch whose commits are all in the clone's integration
                     branch (the first of upkeep.integration that exists; default origin/main,
                     origin/master, main, master), that no worktree has checked out, and that was not
                     cut or moved in the last day (a branch cut a minute ago has no commits yet, so it
                     reads as merged). Never -D
  idle-worktrees     a worktree under a configured root whose branch is in its clone's integration
                     branch, whose tree is clean, that no process sits in and that git has not touched
                     for 24 hours: its listed symlinks, the worktree and its branch
  scratch-projects   a ~/.claude/projects folder for a scratch path (/private/tmp, /private/var/folders)
                     whose source path is gone, untouched for a day, and whose every file has a
                     same-size copy in the log backup (backup_cc_logs.py's <backup.folder>/cc-logs/projects)
  orphan-pyc         a .pyc in a tools folder's __pycache__ whose module has no source
  old-dist           a dist-* build folder older than 7 days under a configured parent, that no
                     process holds open, sits in or names on its command line
  uv-cache           `uv cache prune`
  signed-in-profiles a signed_in_browser.py profile whose run is gone (its pid is in the name)
  browser-runs       a pw_temp.py run folder whose run is gone; and a Playwright profile, artifacts
                     folder or Chrome for Testing file with no pid in its name, a day old, that no
                     running process names on its command line
  settings-allow     an allow rule that does nothing (a local copy of a user rule, or a path that is
                     gone; workspace_audit's config rule) in <workspace.root>/.claude/settings.local.json.
                     The same rules in ~/.claude/settings.json are only named: that file can hold
                     tokens. `settings --user`, run by you, prunes them
  setup-commit       commits, in each configured instruction repo (e.g. ~/.claude when it is a git
                     repo), each changed file that has sat unchanged for an hour and passes
                     `skill_lint.py --only`, through the repo's own hook. A file still changing or over
                     a budget is kept and named. It never reverts. (Here "removed" means committed.)
NEVER: a dirty, unmerged or recently used worktree or branch; a remote; a session log that is not
backed up.

CONFIG (agent-workbench config, key `upkeep`; all optional):
  repos         clones to prune and clean (default: every clone directly under <workspace.root>/repos,
                plus this tools folder when it is a clone)
  integration   integration refs, first that exists wins
  worktrees     [{"clone": "<path>", "root": "<folder of its worktrees>", "links": ["<relative symlink>"]}]
  dist_parents  folders whose dist-* subfolders are builds
  pyc_dirs      more tools folders for orphan-pyc
  setup_repos   instruction repos for setup-commit

Exit: 0 every step ran; 2 a step did not run (named).
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kit_config  # noqa: E402

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
SCRATCH_PREFIXES = ("-private-tmp-", "-private-var-folders-")
DAY = 86400


def _cfg(key, default=None):
    v = kit_config.get("upkeep", key)
    return default if v in (None, "", [], {}) else v


def _x(p):
    return os.path.expanduser(p)


def workspace_root():
    r = kit_config.get("workspace", "root")
    return _x(r) if r else None


INTEGRATION = tuple(_cfg("integration", ["origin/main", "origin/master", "main", "master"]))


def backup_projects():
    """backup_cc_logs.py's copy of ~/.claude/projects, or None when no backup folder is configured."""
    b = kit_config.get("backup", "folder")
    return os.path.join(_x(b), "cc-logs", "projects") if b else None


def git(repo, *args, timeout=60, env=None):
    p = subprocess.run(["git", "-C", repo, "--no-optional-locks", *args], capture_output=True, text=True,
                       timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def clones(repos=None, extra=(HERE,)):
    """Every clone with its own .git folder (worktrees share their clone's): upkeep.repos, else each
    clone directly under <workspace.root>/repos, plus `extra` when it is a clone."""
    listed = [_x(p) for p in _cfg("repos", [])]
    if repos is None and not listed and workspace_root():
        repos = os.path.join(workspace_root(), "repos")
    out = list(listed)
    if repos and os.path.isdir(repos):
        out += [os.path.join(repos, d) for d in sorted(os.listdir(repos)) if os.path.isdir(os.path.join(repos, d, ".git"))]
    return [o for o in out if os.path.isdir(os.path.join(o, ".git"))] + \
        [e for e in extra if os.path.isdir(os.path.join(e, ".git")) and e not in out]


def integration(repo):
    for ref in INTEGRATION:
        if git(repo, "rev-parse", "-q", "--verify", ref + "^{commit}")[0] == 0:
            return ref
    return None


def worktrees(repo):
    """[{path, branch}] from `git worktree list --porcelain`, the main checkout first."""
    rc, out, _ = git(repo, "worktree", "list", "--porcelain")
    rows, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                rows.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[9:]
        elif line.startswith("branch refs/heads/"):
            cur["branch"] = line[18:]
    return rows


def processes():
    """(cwd paths, open file paths, command lines) of this user's processes; empty on failure."""
    def lines(argv):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            return []
    user = os.environ.get("USER") or ""
    cwd = [l[1:] for l in lines(["lsof", "-a", "-d", "cwd", "-Fn", "-u", user]) if l.startswith("n")]
    files = [l[1:] for l in lines(["lsof", "-Fn", "-u", user]) if l.startswith("n/")]
    cmds = lines(["ps", "-axo", "command="])
    return cwd, files, cmds


def under(path, paths):
    p = path.rstrip("/") + "/"
    return any(x == path or x.startswith(p) for x in paths)


def newest_mtime(path):
    newest = os.lstat(path).st_mtime
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            try:
                newest = max(newest, os.lstat(os.path.join(root, n)).st_mtime)
            except OSError:
                pass
    return newest


class Step:
    def __init__(self, name, dry):
        self.name, self.dry, self.removed, self.of, self.kept, self.error = name, dry, [], 0, [], None

    def take(self, what, fn):
        """Remove `what` with fn() unless this is a dry run; a failure keeps it, with the reason."""
        if self.dry:
            self.removed.append(what)
            return
        try:
            fn()
            self.removed.append(what)
        except Exception as e:
            self.kept.append(f"{what} ({type(e).__name__}: {str(e)[:80]})")

    def line(self):
        verb = "would remove" if self.dry else "removed"
        if self.error:
            return f"{self.name}: DID NOT RUN ({self.error})"
        s = f"{self.name}: {verb} {len(self.removed)} of {self.of}"
        return s + (f"; kept {len(self.kept)}" if self.kept else "")

    def as_dict(self):
        return {"step": self.name, "removed": self.removed, "of": self.of, "kept": self.kept,
                "error": self.error, "dry_run": self.dry, "line": self.line()}


# ------------------------------------------------------------------ cleanup steps

def worktree_prune(dry, repos):
    s = Step("worktree-prune", dry)
    for r in repos:
        rc, out, err = git(r, "worktree", "prune", "--dry-run", "-v")
        found = [l for l in (out + "\n" + err).splitlines() if l.startswith("Removing worktrees/")]
        s.of += len(worktrees(r)) - 1 + len(found)
        for l in found:
            s.take(f"{os.path.basename(r)}: {l[len('Removing '):].split(':')[0]}", lambda r=r: git(r, "worktree", "prune"))
    return s


def lint_ok(path):
    """skill_lint.py --only <path> exits 0: the file is within the instruction budgets."""
    return subprocess.run([sys.executable, os.path.join(HERE, "skill_lint.py"), "--only", path],
                          capture_output=True).returncode == 0


def setup_commit(dry, repos, now, settle=3600, lint=lint_ok):
    """Commit each changed file in the instruction repos that has sat unchanged for `settle` seconds
    and passes skill_lint, so every session's edit lands in history. A file still changing, or over
    budget, stays uncommitted and is named; nothing is restored or reverted."""
    s = Step("setup-commit", dry)
    for r in repos:
        if not os.path.isdir(os.path.join(r, ".git")):
            continue
        name = os.path.basename(r) or r
        # -z, read raw: git() strips its output, which eats the first entry's leading status space.
        z = subprocess.run(["git", "-C", r, "status", "--porcelain", "-z", "-uall"],
                           capture_output=True, text=True).stdout.split("\0")
        paths, i = [], 0
        while i < len(z):
            e = z[i]
            if len(e) > 3:
                paths.append(e[3:])
                i += 1 if e[0] not in "RC" else 2   # a rename's source path follows it
            else:
                i += 1
        s.of += len(paths)
        ready = []
        for p in paths:
            full = os.path.join(r, p)
            if os.path.exists(full) and now - os.path.getmtime(full) < settle:
                s.kept.append(f"{name}: {p} (changed in the last hour)")
            elif os.path.exists(full) and p.endswith(".md") and not lint(full):
                s.kept.append(f"{name}: {p} (over a budget: skill_lint.py --only {full})")
            else:
                ready.append(p)
        if not ready:
            continue
        if dry:
            s.removed += [f"{name}: {p}" for p in ready]
            continue
        msg = (f"upkeep: commit {len(ready)} settled instruction file(s)\n\n"
               "Unchanged for an hour and within skill_lint's budgets.\n\n" + "\n".join(ready))
        a = subprocess.run(["git", "-C", r, "add", "-A", "--", *ready], capture_output=True, text=True)
        c = a.returncode == 0 and subprocess.run(["git", "-C", r, "commit", "-q", "-m", msg, "--", *ready],
                                                 capture_output=True, text=True)
        if c and c.returncode == 0:
            s.removed += [f"{name}: {p}" for p in ready]
        else:
            err = (a.stderr if a.returncode else c.stderr).strip().splitlines()
            s.kept += [f"{name}: {p} (commit refused: {(err or ['?'])[-1][:80]})" for p in ready]
    return s


def merged_branches(dry, repos, now):
    s = Step("merged-branches", dry)
    for r in repos:
        integ = integration(r)
        if not integ:
            s.kept.append(f"{os.path.basename(r)}: no integration branch")
            continue
        busy = {w.get("branch") for w in worktrees(r)}
        local = integ.split("/", 1)[1] if integ.startswith("origin/") else integ
        names = git(r, "for-each-ref", "--format=%(refname:short)", "refs/heads")[1].splitlines()
        for b in names:
            if b == local:
                continue
            s.of += 1
            if b in busy:
                continue
            if git(r, "merge-base", "--is-ancestor", b, integ)[0] != 0:
                continue
            reflog = os.path.join(r, ".git", "logs", "refs", "heads", b)
            if os.path.exists(reflog) and now - os.path.getmtime(reflog) < DAY:
                s.kept.append(f"{os.path.basename(r)}: {b} (cut or moved in the last day)")
                continue

            def delete(r=r, b=b):
                rc, _, err = git(r, "branch", "-d", b)
                if rc:
                    raise RuntimeError(err.splitlines()[-1] if err else f"exit {rc}")
            s.take(f"{os.path.basename(r)}: {b}", delete)
    return s


def idle_worktrees(dry, clone, wt_root, procs, now, links=()):
    s = Step("idle-worktrees", dry)
    if not os.path.isdir(os.path.join(clone, ".git")):
        s.error = f"no clone at {clone}"
        return s
    integ = integration(clone)
    if not integ:
        s.error = f"no integration branch in {clone}"
        return s
    cwd = procs[0]
    for w in worktrees(clone):
        path, b = w.get("path", ""), w.get("branch")
        if not path.startswith(wt_root.rstrip("/") + "/"):
            continue
        s.of += 1
        name = os.path.basename(path)
        why = None
        gd = git(path, "rev-parse", "--absolute-git-dir")[1]
        touched = max([os.path.getmtime(os.path.join(gd, f)) for f in ("HEAD", "index", "logs/HEAD")
                       if os.path.exists(os.path.join(gd, f))] or [now])
        if not b:
            why = "detached HEAD"
        elif git(clone, "merge-base", "--is-ancestor", b, integ)[0] != 0:
            why = f"branch not in {integ}"
        elif git(path, "status", "--porcelain")[1]:
            why = "uncommitted changes"
        elif under(path, cwd):
            why = "a process sits in it"
        elif now - touched < DAY:
            why = "git touched it in the last 24 hours"
        if why:
            s.kept.append(f"{name} ({why})")
            continue

        def remove(path=path, b=b):
            for rel in links:
                link = os.path.join(path, rel)
                if os.path.islink(link):
                    os.unlink(link)
            rc, _, err = git(clone, "worktree", "remove", path)
            if rc:
                raise RuntimeError(err.splitlines()[-1] if err else f"exit {rc}")
            git(clone, "branch", "-d", b)
        s.take(f"{name} ({b})", remove)
    return s


def scratch_projects(dry, projects, backup, now):
    s = Step("scratch-projects", dry)
    if not os.path.isdir(projects):
        s.error = f"no {projects}"
        return s
    if not backup:
        s.kept.append("no backup.folder configured, so nothing is known to be backed up")
        return s
    for name in sorted(os.listdir(projects)):
        folder = os.path.join(projects, name)
        if not (os.path.isdir(folder) and name.startswith(SCRATCH_PREFIXES)):
            continue
        s.of += 1
        source = "/" + name[1:].replace("-", "/")
        cwds = set()
        for root, _, files in os.walk(folder):
            for f in files:
                if f.endswith(".jsonl"):
                    try:
                        with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                            for line in fh:
                                if '"cwd"' in line:
                                    cwds.add(json.loads(line).get("cwd") or "")
                                    break
                    except (OSError, ValueError):
                        pass
        why = None
        if any(c and os.path.exists(c) for c in cwds) or os.path.exists(source):
            why = "its source path still exists"
        elif now - newest_mtime(folder) < DAY:
            why = "touched in the last day"
        else:
            for root, _, files in os.walk(folder):
                for f in files:
                    p = os.path.join(root, f)
                    copy = os.path.join(backup, os.path.relpath(p, projects))
                    if not (os.path.isfile(copy) and os.path.getsize(copy) == os.path.getsize(p)):
                        why = f"{os.path.relpath(p, folder)} has no same-size backup copy"
                        break
                if why:
                    break
        if why:
            s.kept.append(f"{name} ({why})")
            continue
        s.take(name, lambda folder=folder: shutil.rmtree(folder))
    return s


def orphan_pyc(dry, dirs):
    s = Step("orphan-pyc", dry)
    for d in dirs:
        cache = os.path.join(d, "__pycache__")
        if not os.path.isdir(cache):
            continue
        for f in sorted(os.listdir(cache)):
            if not f.endswith(".pyc"):
                continue
            s.of += 1
            if not os.path.exists(os.path.join(d, f.split(".")[0] + ".py")):
                s.take(os.path.relpath(os.path.join(cache, f), HOME), lambda p=os.path.join(cache, f): os.remove(p))
    return s


def old_dist(dry, parents, procs, now, days=7):
    s = Step("old-dist", dry)
    cwd, files, cmds = procs
    for site in parents:
        if not os.path.isdir(site):
            continue
        for name in sorted(os.listdir(site)):
            d = os.path.join(site, name)
            if not (name.startswith("dist-") and os.path.isdir(d) and not os.path.islink(d)):
                continue
            s.of += 1
            rel = os.path.relpath(d, HOME)
            if now - newest_mtime(d) < days * DAY:
                continue
            if under(d, cwd) or under(d, files) or any(name in c for c in cmds):
                s.kept.append(f"{rel} (a process uses or names it)")
                continue
            s.take(rel, lambda d=d: shutil.rmtree(d))
    return s


def uv_cache(dry):
    s = Step("uv-cache", dry)
    uv = shutil.which("uv") or ("/opt/homebrew/bin/uv" if os.path.exists("/opt/homebrew/bin/uv") else None)
    if not uv:
        return s
    s.of = 1
    if dry:
        s.removed.append("uv cache prune (not run in a dry run)")
        return s
    p = subprocess.run([uv, "cache", "prune"], capture_output=True, text=True, timeout=300)
    if p.returncode:
        s.error = (p.stderr.strip().splitlines() or [f"exit {p.returncode}"])[-1][:100]
    else:
        s.removed.append(((p.stderr or p.stdout).strip().splitlines() or ["uv cache prune"])[-1][:100])
    return s


def signed_in_profiles(dry, root=None):
    """Signed-in browser profiles that a killed run of signed_in_browser.py left in the temp dir. They hold
    session cookies, and the next launch reaps them, so this daily pass covers days with no launch."""
    s = Step("signed-in-profiles", dry)
    sys.path.insert(0, HERE)
    import signed_in_browser
    found = signed_in_browser.reap(root, dry=True)
    s.of = len(found)
    for p in found:
        s.take(os.path.basename(p), lambda p=p: shutil.rmtree(p))
    return s


def browser_runs(dry, roots=None):
    """Temp folders of browser runs killed before they could delete them. The pid-named ones (pw_temp.py's
    pw-run-) are reaped at the next launch too; this pass covers days with no launch. The pid-less ones
    are what a bare Playwright launch leaves. Under a scheduler TMPDIR can be unset and tempfile falls
    back to /tmp, so both dirs are read."""
    s = Step("browser-runs", dry)
    sys.path.insert(0, HERE)
    import pw_temp, signed_in_browser
    if roots is None:
        roots = sorted({signed_in_browser.temp_root(), os.path.realpath(tempfile.gettempdir())})
    found = [p for r in roots for p in pw_temp.reap(r, dry=True, legacy_age=24 * 3600)]
    s.of = len(found)
    for p in found:
        def rm(p=p):
            if not pw_temp.remove(p):
                raise OSError("still there after 12 tries")
        s.take(os.path.basename(p), rm)
    return s


def settings_allow(dry, home=HOME, desktop=None, user=False):
    """Allow rules that do nothing, by workspace_audit's config rule: a local copy of a user rule, or a
    rule naming a path that is gone (a removed worktree, a tool that moved). Each "always allow" click
    adds a literal rule, so they pile up. It prunes the workspace's settings.local.json. The user file
    (~/.claude/settings.json) can hold live tokens and Claude Code writes it too, so the daily run only
    names its rules; `upkeep.py settings --user`, run by you, prunes them. A rule prints masked and cut
    to 80 characters, never whole."""
    s = Step("settings-allow", dry)
    desktop = desktop or workspace_root()
    if not desktop:
        s.error = "workspace.root is not set"
        return s
    sys.path.insert(0, HERE)
    import pathlib
    import workspace_audit as wa
    home, desktop = pathlib.Path(home), pathlib.Path(desktop)
    lists, flags = wa.allow_rule_flags(home, desktop)
    s.of = len(flags)
    for label, path in wa.settings_files(home, desktop).items():
        mine = [(r, why) for lab, r, why in flags if lab == label]
        if not mine:
            continue
        if label == "user" and not user:
            s.kept += [f"user: {why} (you prune it: upkeep.py settings --user)" for _, why in mine]
            continue
        if dry:
            s.removed += [f"{label}: {why}" for _, why in mine]
            continue
        try:
            # Read again just before writing: Claude Code may have added a rule since the check.
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            drop = {r for r, _ in mine}
            allow = data["permissions"]["allow"]
            data["permissions"]["allow"] = [r for r in allow if r not in drop]
            text = json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if raw.endswith("\n") else "")
            tmp = f"{path}.upkeep-tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
            os.replace(tmp, path)
            s.removed += [f"{label}: {why}" for _, why in mine]
        except Exception as e:
            s.kept += [f"{label}: {why} ({type(e).__name__}: {str(e)[:80]})" for _, why in mine]
    return s


def cleanup(dry):
    now = time.time()
    procs = processes()
    repos = clones()
    steps = []
    wts = _cfg("worktrees", [])
    pyc = [HERE] + [_x(p) for p in _cfg("pyc_dirs", [])]
    dists = [_x(p) for p in _cfg("dist_parents", [])]
    setup = [_x(p) for p in _cfg("setup_repos", [])]

    def idle():
        st = Step("idle-worktrees", dry)
        for w in wts:
            one = idle_worktrees(dry, _x(w["clone"]), _x(w["root"]), procs, now, w.get("links") or ())
            st.of += one.of
            st.removed += one.removed
            st.kept += one.kept
            st.error = st.error or one.error
        return st
    for name, fn in (("worktree-prune", lambda: worktree_prune(dry, repos)),
                     ("merged-branches", lambda: merged_branches(dry, repos, now)),
                     ("idle-worktrees", idle),
                     ("scratch-projects", lambda: scratch_projects(dry, PROJECTS, backup_projects(), now)),
                     ("orphan-pyc", lambda: orphan_pyc(dry, pyc)),
                     ("old-dist", lambda: old_dist(dry, dists, procs, now)),
                     ("uv-cache", lambda: uv_cache(dry)),
                     ("signed-in-profiles", lambda: signed_in_profiles(dry)),
                     ("browser-runs", lambda: browser_runs(dry)),
                     ("settings-allow", lambda: settings_allow(dry)),
                     ("setup-commit", lambda: setup_commit(dry, setup, now))):
        try:
            steps.append(fn())
        except Exception as e:
            st = Step(name, dry)
            st.error = f"{type(e).__name__}: {str(e)[:100]}"
            steps.append(st)
    return steps


# ------------------------------------------------------------------ selftest

def selftest():
    results = []

    def check(label, ok, detail=""):
        ok = bool(ok)
        results.append(ok)
        print(("PASS " if ok else "FAIL ") + label + (f"  {detail}" if detail and not ok else ""))

    now = time.time()
    old = now - 3 * DAY
    with tempfile.TemporaryDirectory() as t:
        t = os.path.realpath(t)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")

        def g(repo, *a):
            subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, env=env)

        repos, wt_root = os.path.join(t, "repos"), os.path.join(t, "repos", "site-wt")
        site = os.path.join(repos, "site")
        os.makedirs(site)
        g(site, "init", "-q", "-b", "master")
        open(os.path.join(site, "a.txt"), "w").write("a")
        g(site, "add", "a.txt")
        g(site, "commit", "-q", "-m", "a")
        g(site, "update-ref", "refs/remotes/origin/main", "HEAD")
        g(site, "branch", "merged")
        g(site, "branch", "own")
        g(site, "checkout", "-q", "own")
        open(os.path.join(site, "b.txt"), "w").write("b")
        g(site, "add", "b.txt")
        g(site, "commit", "-q", "-m", "own work")
        g(site, "checkout", "-q", "master")
        for name in ("idle", "dirty", "fresh", "inuse", "gone"):
            g(site, "worktree", "add", "-q", "-b", f"wt/{name}", os.path.join(wt_root, name), "origin/main")
        open(os.path.join(wt_root, "dirty", "a.txt"), "w").write("edited")
        shutil.rmtree(os.path.join(wt_root, "gone"))
        for name in ("idle", "dirty", "inuse"):
            gd = git(os.path.join(wt_root, name), "rev-parse", "--absolute-git-dir")[1]
            for f in ("HEAD", "index", "logs/HEAD"):
                if os.path.exists(os.path.join(gd, f)):
                    os.utime(os.path.join(gd, f), (old, old))

        s = worktree_prune(False, [site])
        check("worktree-prune removes the registration whose folder is gone, 1 of 6",
              len(s.removed) == 1 and s.of == 6 and "gone" in s.removed[0], s.line())
        for b in ("merged", "wt/gone"):
            os.utime(os.path.join(site, ".git", "logs", "refs", "heads", b), (old, old))
        g(site, "branch", "justcut")
        s = merged_branches(False, [site], now)
        left = git(site, "for-each-ref", "--format=%(refname:short)", "refs/heads")[1].split()
        check("merged-branches deletes the merged branch and the pruned worktree's, keeps unmerged and checked-out ones",
              "merged" not in left and "wt/gone" not in left and {"own", "wt/idle", "wt/dirty", "wt/fresh", "master"} <= set(left),
              f"{s.line()} left={left}")
        check("merged-branches keeps a branch cut in the last day", "justcut" in left
              and [k for k in s.kept if "last day" in k] == ["site: justcut (cut or moved in the last day)"], f"kept={s.kept}")
        # git refuses -d on a checked-out branch anyway; the step must not even try.
        check("merged-branches never asks git to delete a checked-out branch",
              not [k for k in s.kept if "last day" not in k], f"kept={s.kept}")
        procs = ([os.path.join(wt_root, "inuse")], [], [])
        s = idle_worktrees(False, site, wt_root, procs, now)
        kept = " ".join(s.kept)
        check("idle-worktrees removes the merged, clean, idle one only",
              [r.split(" ")[0] for r in s.removed] == ["idle"] and not os.path.exists(os.path.join(wt_root, "idle"))
              and "uncommitted" in kept and "process" in kept and "24 hours" in kept, f"{s.line()} kept={s.kept}")
        check("idle-worktrees deletes the removed worktree's branch",
              "wt/idle" not in git(site, "for-each-ref", "--format=%(refname:short)", "refs/heads")[1].split())
        dry = idle_worktrees(True, site, wt_root, ([], [], []), now + 2 * DAY)
        check("a dry run removes nothing", os.path.isdir(os.path.join(wt_root, "fresh")) and dry.removed, dry.line())

        projects, backup = os.path.join(t, "projects"), os.path.join(t, "backup")

        def plant(rel, text, back=True, size_off=0):
            p = os.path.join(projects, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(text)
            os.utime(p, (old, old))
            os.utime(os.path.dirname(p), (old, old))
            if back:
                b = os.path.join(backup, rel)
                os.makedirs(os.path.dirname(b), exist_ok=True)
                open(b, "w").write(text + "x" * size_off)
        gone_cwd = json.dumps({"cwd": os.path.join(t, "no-such-dir")})
        plant("-private-tmp-gone/s.jsonl", gone_cwd + "\n")
        plant("-private-tmp-unbacked/s.jsonl", gone_cwd + "\n", back=False)
        plant("-private-tmp-differs/s.jsonl", gone_cwd + "\n", size_off=3)
        plant("-private-tmp-live/s.jsonl", json.dumps({"cwd": t}) + "\n")
        plant("-Users-someone-Work/s.jsonl", gone_cwd + "\n")
        plant("-private-tmp-fresh/s.jsonl", gone_cwd + "\n")
        os.utime(os.path.join(projects, "-private-tmp-fresh", "s.jsonl"), (now, now))
        none = scratch_projects(False, projects, None, now)
        check("scratch-projects with no backup configured removes nothing", not none.removed and none.kept)
        s = scratch_projects(False, projects, backup, now)
        check("scratch-projects removes the backed-up scratch folder whose source is gone, and only it",
              s.removed == ["-private-tmp-gone"] and s.of == 5
              and os.path.isdir(os.path.join(projects, "-Users-someone-Work")), f"{s.line()} kept={s.kept}")


        tools = os.path.join(t, "tools")
        os.makedirs(os.path.join(tools, "__pycache__"))
        open(os.path.join(tools, "a.py"), "w").write("")
        for f in ("a.cpython-313.pyc", "b.cpython-313.pyc"):
            open(os.path.join(tools, "__pycache__", f), "w").write("")
        s = orphan_pyc(False, [tools])
        check("orphan-pyc removes the .pyc whose module is gone", len(s.removed) == 1 and "b.cpython" in s.removed[0]
              and os.path.exists(os.path.join(tools, "__pycache__", "a.cpython-313.pyc")), s.line())

        pub = os.path.join(t, "public")
        for n, when in (("dist-old", old - 7 * DAY), ("dist-new", now), ("dist-held", old - 7 * DAY), ("dist", old - 7 * DAY)):
            os.makedirs(os.path.join(pub, n))
            open(os.path.join(pub, n, "i.html"), "w").write("x")
            for p in (os.path.join(pub, n, "i.html"), os.path.join(pub, n)):
                os.utime(p, (when, when))
        s = old_dist(False, [pub], ([], [], ["python3 -m http.server --directory dist-held"]), now)
        check("old-dist removes the week-old build nobody names; keeps a new one, a named one and dist itself",
              [os.path.basename(r) for r in s.removed] == ["dist-old"] and os.path.isdir(os.path.join(pub, "dist"))
              and os.path.isdir(os.path.join(pub, "dist-held")), f"{s.line()} kept={s.kept}")

        prof = os.path.join(t, "tmpdir")
        dead_p, mine_p = os.path.join(prof, "signed_in_999999_x"), os.path.join(prof, f"signed_in_{os.getpid()}_y")
        for d in (dead_p, mine_p, os.path.join(prof, "unrelated")):
            os.makedirs(os.path.join(d, "profile"))
        st = signed_in_profiles(False, prof)
        check("signed-in-profiles: a killed run's profile is removed; this process's and an unrelated folder stay",
              st.of == 1 and not os.path.exists(dead_p) and os.path.exists(mine_p)
              and os.path.exists(os.path.join(prof, "unrelated")), st.line())

        runs = os.path.join(t, "runs")
        two_days, hour = time.time() - 2 * 86400, time.time() - 3600
        # "_" never starts the random part of a real temp name, so removing these stops no real browser.
        plant = {"pw-run-999999-_a": two_days, f"pw-run-{os.getpid()}-_b": two_days,
                 "playwright_chromiumdev_profile-_c": two_days, "playwright-artifacts-_d": hour, "unrelated": two_days}
        for n, when in plant.items():
            os.makedirs(os.path.join(runs, n, "x"))
            os.utime(os.path.join(runs, n), (when, when))
        st = browser_runs(False, [runs])
        left = sorted(os.listdir(runs))
        check("browser-runs: a dead pid's run folder and a 2-day-old pid-less profile go; this process's, "
              "an hour-old artifacts folder and an unrelated folder stay",
              st.of == 2 and left == ["playwright-artifacts-_d", f"pw-run-{os.getpid()}-_b", "unrelated"],
              f"{st.line()}; left {left}")

        sh = os.path.join(t, "sethome")
        sd = os.path.join(sh, "Desktop")
        os.makedirs(os.path.join(sh, ".claude"))
        os.makedirs(os.path.join(sd, ".claude"))
        open(os.path.join(sh, "real.py"), "w").write("x")
        ufile, lfile = os.path.join(sh, ".claude", "settings.json"), os.path.join(sd, ".claude", "settings.local.json")
        live, gone = f"Bash(python {sh}/real.py *)", f"Bash(python {sh}/gone.py *)"
        user_text = json.dumps({"env": {"K": "v"}, "permissions": {"allow": ["Bash(ls *)", gone]}}, indent=2)
        open(ufile, "w").write(user_text)
        open(lfile, "w").write(json.dumps({"permissions": {"allow": [live, gone, "Bash(ls *)"],
                                                           "deny": ["Bash(curl *)"]}}, indent=2) + "\n")
        st = settings_allow(False, sh, sd)
        local = json.load(open(lfile))
        check("settings-allow: the local file keeps its live rule and its deny list, and loses the dead "
              "and duplicate rules",
              local["permissions"] == {"allow": [live], "deny": ["Bash(curl *)"]} and open(lfile).read().endswith("\n")
              and len(st.removed) == 2, f"{st.line()} {local}")
        check("settings-allow: the user file is byte-for-byte untouched and its dead rule is named, not removed",
              open(ufile).read() == user_text and len(st.kept) == 1 and "--user" in st.kept[0], f"{st.kept}")
        st = settings_allow(False, sh, sd, user=True)
        check("settings-allow --user prunes the user file's dead rule and keeps its other keys",
              json.load(open(ufile)) == {"env": {"K": "v"}, "permissions": {"allow": ["Bash(ls *)"]}}, st.line())
    # setup-commit: settled good file and a deletion commit; a fresh file, an over-budget file and
    # another session's staged file stay out of the commit.
    with tempfile.TemporaryDirectory() as t:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        r = os.path.join(os.path.realpath(t), "setup")
        os.makedirs(r)
        run = lambda *a: subprocess.run(["git", "-C", r, *a], check=True, capture_output=True, env=env)
        run("init", "-q")
        run("config", "user.name", "t"); run("config", "user.email", "t@t")
        for f in ("gone.md", "staged.md"):
            open(os.path.join(r, f), "w").write("x")
        run("add", "."); run("commit", "-q", "-m", "base")
        for f, body in (("good.md", "fine"), ("bad.md", "over"), ("fresh.md", "new"), ("staged.md", "y")):
            open(os.path.join(r, f), "w").write(body)
        os.remove(os.path.join(r, "gone.md"))
        old = time.time() - 2 * 3600
        for f in ("good.md", "bad.md"):
            os.utime(os.path.join(r, f), (old, old))
        run("add", "staged.md")   # another session's fresh edit, already staged
        dry = setup_commit(True, [r], time.time(), lint=lambda p: not p.endswith("bad.md"))
        check("setup-commit dry run names settled files and commits nothing",
              len(dry.removed) == 2 and run("rev-list", "--count", "HEAD").stdout.strip() == b"1", dry.line())
        s = setup_commit(False, [r], time.time(), lint=lambda p: not p.endswith("bad.md"))
        last = run("show", "--name-status", "--format=", "HEAD").stdout.decode()
        check("setup-commit commits the settled good file and the deletion",
              "good.md" in last and "D\tgone.md" in last, f"{s.line()} last={last!r}")
        check("setup-commit keeps the fresh and the over-budget file out, and says why",
              "fresh.md" not in last and "bad.md" not in last
              and any("fresh.md (changed in the last hour)" in k for k in s.kept)
              and any("bad.md (over a budget" in k for k in s.kept), f"kept={s.kept}")
        check("setup-commit leaves another session's staged, fresh file out of its commit",
              "staged.md" not in last and b"staged.md" in run("diff", "--cached", "--name-only").stdout,
              f"last={last!r}")
        check("setup-commit leaves the working tree as it was",
              open(os.path.join(r, "bad.md")).read() == "over" and os.path.exists(os.path.join(r, "fresh.md")))

    print(f"selftest: {sum(results)} of {len(results)} checks passed")
    return 0 if all(results) else 1


def main():
    ap = argparse.ArgumentParser(prog="upkeep.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=("cleanup", "settings"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--user", action="store_true", help="settings: prune ~/.claude/settings.json too")
    ap.add_argument("--json")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.cmd not in ("cleanup", "settings"):
        ap.print_help()
        return 2
    steps = cleanup(a.dry_run) if a.cmd == "cleanup" else [settings_allow(a.dry_run, user=a.user)]
    for st in steps:
        print(st.line())
        for k in st.kept[:12]:
            print(f"    kept {k}")
    data = [st.as_dict() for st in steps]
    failed = [st.name for st in steps if st.error]
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    if failed:
        print("did not run: " + ", ".join(failed))
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
