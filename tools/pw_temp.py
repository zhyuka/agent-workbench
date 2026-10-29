#!/usr/bin/env python3
"""pw_temp.py - every browser run gets a temp folder of its own, and a killed run's folder is reaped.

WHY THIS EXISTS
  A bare `chromium.launch()` makes its profile (playwright_chromiumdev_profile-*) and an artifacts
  folder (playwright-artifacts-*) in the shared temp dir, and the browser writes its own temp files
  (com.google.chrome.for.testing.*, com.microsoft.edgemac.*, scoped_dir*) there too. A clean close
  deletes them. A run killed by a timeout never gets to, and nothing names the run that made them,
  so nothing can tell an orphan from a live run's folder. Tools that are often killed by timeouts
  leave hundreds of these behind.

  Playwright's driver takes its temp dir from its own environment, once, when it spawns, and the
  browser inherits that environment unless the caller passes `env=`. So `sync_playwright` here sets
  TMPDIR (TEMP and TMP on Windows, and MAC_CHROMIUM_TMPDIR, which Chromium on macOS reads first) to a
  folder named for this process, only while the driver spawns, and deletes the folder when the run
  ends.

  A killed run's folder stays, but its name carries the pid, so `reap` deletes it at the next launch
  once that process is gone. Another tool that names its own browser folders <prefix><pid>-<random>
  can add its prefix to PREFIXES and reap through here too.

USAGE
  from pw_temp import sync_playwright      # in place of playwright.sync_api's; same use
  python tools/pw_temp.py reap [--dry]     # delete the folders of killed runs now
  python tools/pw_temp.py --selftest

  Python's tempfile keeps the real temp dir: it is read before the environment changes, and the
  environment is put back as soon as the driver is up. Another thread that starts a process in
  that moment inherits the folder.

EXIT
  0 done, 1 a selftest case failed, 2 no command given.
"""
import os, re, shutil, subprocess, sys, tempfile, threading, time

PREFIX = "pw-run-"   # pw-run-<pid>-<random>; the random part stops `pkill -f pw-run-123` matching pw-run-1234
# Every folder named <prefix><pid>-: this module's, plus any other tool's that names its browser
# folders the same way (add its prefix here).
PREFIXES = (PREFIX, "edge-run-", "gpushot-")
MIN_AGE = 1800   # a second guard for a live run
# Names Playwright and Chrome for Testing give their temp items, with no pid in them. Only `reap`
# with legacy_age touches these, after checking that no running process names the folder.
LEGACY = ("playwright_chromiumdev_profile-", "playwright-artifacts-", "com.google.chrome.for.testing.")
ENV_KEYS = ("TMPDIR", "TEMP", "TMP", "MAC_CHROMIUM_TMPDIR")
_WIN = os.name == "nt"
_env_lock = threading.Lock()


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True   # alive under another user


def _running_args():
    """Every running process's command line, joined, or None when ps could not run."""
    try:
        return subprocess.run(["ps", "-axww", "-o", "args="], capture_output=True, text=True,
                              timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def kill_pattern(path):
    """The pkill -f pattern for the processes that name this folder: its name as a whole path component.
    A bare name is a substring match: playwright_chromiumdev_profile-f would also match a live browser
    on .../playwright_chromiumdev_profile-fAb123/ (the selftest plants that decoy)."""
    name = "".join("\\" + c if c in ".[]()*+?{}|^$\\" else c for c in os.path.basename(path))
    return "/" + name + "(/|[[:space:]]|$)"


def remove(path):
    # A browser that outlived its killed run still holds the folder: stop it first. The folder is in that
    # browser's --user-data-dir, so pkill -f finds the re-parented renderer as well.
    if not _WIN:
        try:
            subprocess.run(["pkill", "-9", "-f", kill_pattern(path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
        except (OSError, subprocess.SubprocessError):
            pass
    for _ in range(12):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
        if not os.path.lexists(path):
            return True
        time.sleep(0.5)
    return False


def reap(root=None, dry=False, now=None, alive=_alive, legacy_age=None, args=None):
    """Delete the folders of runs that are gone; return their paths (with dry, delete nothing).

    A folder named <prefix><pid>- goes once that pid is gone and the folder is MIN_AGE old. With
    legacy_age (seconds), a LEGACY item that old also goes, unless a running process's command line
    names it; args stands in for ps in the selftest. Windows is skipped: os.kill(pid, 0) sends
    CTRL_C_EVENT there, not measured."""
    if _WIN:
        return []
    root = root or tempfile.gettempdir()
    now = time.time() if now is None else now
    gone = []
    try:
        names = os.listdir(root)
    except OSError:
        return gone
    if legacy_age is not None and args is None and any(n.startswith(LEGACY) for n in names):
        args = _running_args()
        if args is None:
            legacy_age = None   # cannot see what is running, so leave the pid-less items alone
    for name in sorted(names):
        path = os.path.join(root, name)
        try:
            age = now - os.lstat(path).st_mtime
        except OSError:
            continue   # it went meanwhile
        prefix = next((p for p in PREFIXES if name.startswith(p)), None)
        if prefix:
            m = re.match(r"(\d+)-", name[len(prefix):])
            if not m or not os.path.isdir(path) or age < MIN_AGE or alive(int(m.group(1))):
                continue
        elif legacy_age is not None and name.startswith(LEGACY):
            if age < legacy_age or name in args:
                continue
        else:
            continue
        if dry or remove(path):
            gone.append(path)
    return gone


def new_folder(root=None):
    return tempfile.mkdtemp(prefix=f"{PREFIX}{os.getpid()}-", dir=root or tempfile.gettempdir())


class _Run:
    """playwright.sync_api.sync_playwright(), with the driver started on a temp folder of its own."""

    def __init__(self, real, root=None):
        self._real, self._root = real, root   # root: the selftest's own, so it never reaps the real temp dir
        self._cm = None
        self.folder = None

    def __enter__(self):
        tempfile.gettempdir()   # fix Python's own temp dir before the environment changes
        reap(self._root)
        self.folder = new_folder(self._root)
        try:
            with _env_lock:
                saved = {k: os.environ.get(k) for k in ENV_KEYS}
                os.environ.update({k: self.folder for k in ENV_KEYS})
                try:
                    self._cm = self._real()
                    pw = self._cm.__enter__()
                finally:
                    for k, v in saved.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
        except BaseException:
            remove(self.folder)
            raise
        pw.stop = self.__exit__   # the real one points stop at its own __exit__
        return pw

    def start(self):
        return self.__enter__()

    def __exit__(self, *exc):
        try:
            if self._cm is not None:
                self._cm.__exit__(*exc)
        finally:
            if self.folder and os.path.lexists(self.folder):
                remove(self.folder)


def __getattr__(name):
    # `from pw_temp import sync_playwright` raises ImportError at the caller's import when Playwright is
    # missing, exactly as `from playwright.sync_api import sync_playwright` does, so the callers' own
    # `except ImportError` still reports could-not-run. reap needs no Playwright, so a tool that
    # drives a browser without it can import this module freely.
    if name == "sync_playwright":
        from playwright.sync_api import sync_playwright as real
        return lambda: _Run(real)
    raise AttributeError(name)


def selftest():
    fails, cases = [], []

    def check(label, ok, detail=""):
        print(("ok   " if ok else "FAIL ") + label + ("" if ok else f"  ({detail})"))
        cases.append(label)
        if not ok:
            fails.append(label)

    # Planted names that no real folder can share a start with, so a pkill of one stops no real browser
    # even in a broken build: pid 999999 is above macOS's and Linux's default pid limit, and the random
    # part of a real Playwright or Chrome temp name is letters and digits, never "_".
    dead, me = 999999, os.getpid()
    # A live process standing in for another run's browser, whose folder name starts with a planted name.
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                              "--user-data-dir=/x/playwright_chromiumdev_profile-_fAb123/p",
                              "--user-data-dir=/x/pw-run-999999-_aXy/q"])
    with tempfile.TemporaryDirectory() as root:
        old = time.time() - 2 * MIN_AGE
        plant = {
            f"pw-run-{dead}-_a": old, f"pw-run-{me}-_b": old, f"pw-run-{dead}-_c": time.time(),
            f"edge-run-{dead}-_d": old, "pw-run-abc-_e": old, "playwright_chromiumdev_profile-_f": old,
            "playwright-artifacts-_g": old, "playwright-artifacts-_h": old, "com.google.chrome.for.testing._i": old,
            "playwright_chromiumdev_profile-_j": time.time(), "unrelated-_k": old,
        }
        for n, t in plant.items():
            if n.startswith("com.google"):   # Chrome for Testing leaves files as well as folders
                open(os.path.join(root, n), "w").close()
            else:
                os.makedirs(os.path.join(root, n, "sub"))
            os.utime(os.path.join(root, n), (t, t))
        names = lambda paths: sorted(os.path.basename(p) for p in paths)
        dry = reap(root, dry=True)
        check("dry: names a dead pid's folders", names(dry) == [f"edge-run-{dead}-_d", f"pw-run-{dead}-_a"], names(dry))
        check("dry: deletes nothing", all(os.path.lexists(os.path.join(root, n)) for n in plant))
        gone = names(reap(root))
        check("deletes a dead pid's old folder", f"pw-run-{dead}-_a" in gone and not os.path.exists(os.path.join(root, f"pw-run-{dead}-_a")), gone)
        check("deletes a dead pid's folder under another listed prefix", f"edge-run-{dead}-_d" in gone, gone)
        check("keeps a live pid's folder", os.path.isdir(os.path.join(root, f"pw-run-{me}-_b")))
        check("keeps a dead pid's folder younger than MIN_AGE", os.path.isdir(os.path.join(root, f"pw-run-{dead}-_c")))
        check("keeps a name with no pid", os.path.isdir(os.path.join(root, "pw-run-abc-_e")))
        check("leaves legacy names alone without legacy_age", os.path.isdir(os.path.join(root, "playwright-artifacts-_g")))
        legacy = names(reap(root, legacy_age=MIN_AGE, args="... --user-data-dir=/x/playwright-artifacts-_h ..."))
        check("legacy_age: deletes old pid-less items",
              legacy == ["com.google.chrome.for.testing._i", "playwright-artifacts-_g", "playwright_chromiumdev_profile-_f"], legacy)
        check("legacy_age: keeps one a running process names", os.path.isdir(os.path.join(root, "playwright-artifacts-_h")))
        check("legacy_age: keeps a young one", os.path.isdir(os.path.join(root, "playwright_chromiumdev_profile-_j")))
        check("never touches an unrelated name", os.path.isdir(os.path.join(root, "unrelated-_k")))
        check("a live process whose folder only starts with a reaped name survives", decoy.poll() is None)
        pat = kill_pattern("/t/com.google.chrome.for.testing._i")
        check("the kill pattern matches the folder itself, dots escaped",
              re.search(pat.replace("[[:space:]]", r"\s"), "x --dir=/t/com.google.chrome.for.testing._i/a")
              and not re.search(pat.replace("[[:space:]]", r"\s"), "x --dir=/t/comXgoogle.chrome.for.testing._i/a"), pat)
    decoy.kill()

    # The wrapper, on a stub driver: the folder is in the driver's environment only while it starts,
    # Python's temp dir never moves, and the folder goes on exit, on stop() and on a failed start.
    real_tmp = tempfile.gettempdir()
    seen = {}

    class Stub:
        def __init__(self, fail=False):
            self.fail = fail

        def __enter__(self):
            seen["env"] = {k: os.environ.get(k) for k in ENV_KEYS}
            if self.fail:
                raise RuntimeError("driver did not start")
            pw = type("PW", (), {})()
            pw.stop = self.__exit__   # as the real one does
            return pw

        def __exit__(self, *a):
            seen["exited"] = True

    before = {k: os.environ.get(k) for k in ENV_KEYS}
    with tempfile.TemporaryDirectory() as scratch:   # never the real temp dir: a broken build reaps here only
        killed = os.path.join(scratch, f"pw-run-{dead}-_z")
        os.makedirs(os.path.join(killed, "playwright_chromiumdev_profile-_z"))
        os.utime(killed, (time.time() - 2 * MIN_AGE,) * 2)
        run = _Run(Stub, scratch)
        with run as pw:
            folder = run.folder
            check("the next launch reaps a killed run's folder", not os.path.exists(killed))
            check("driver starts with every temp variable on the run's folder",
                  all(v == folder for v in seen["env"].values()), seen["env"])
            check("the folder is named for this pid", os.path.basename(folder).startswith(f"{PREFIX}{me}-"), folder)
            check("the environment is put back once the driver is up",
                  {k: os.environ.get(k) for k in ENV_KEYS} == before)
            check("Python's temp dir does not move", tempfile.gettempdir() == real_tmp)
        check("the folder goes when the run ends", not os.path.exists(folder) and seen.get("exited"))
        run = _Run(Stub, scratch)
        pw = run.start()
        folder = run.folder
        pw.stop()
        check("start() then stop() deletes the folder", not os.path.exists(folder))
        run = _Run(lambda: Stub(fail=True), scratch)
        try:
            run.__enter__()
            check("a failed start raises", False)
        except RuntimeError:
            check("a failed start deletes the folder and puts the environment back",
                  not os.path.exists(run.folder) and {k: os.environ.get(k) for k in ENV_KEYS} == before)

    print(f"\n{'PASS' if not fails else 'FAIL'}: {len(cases) - len(fails)} of {len(cases)} cases passed")
    return 1 if fails else 0


if __name__ == "__main__":
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        sys.exit(0)
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if sys.argv[1:2] == ["reap"]:
        for p in reap(dry="--dry" in sys.argv):
            print(p)
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
