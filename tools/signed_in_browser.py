#!/usr/bin/env python3
"""signed_in_browser.py - one browser (Edge or Chrome) for a whole batch of steps on a signed-in site,
then gone.

    serve <url> [--minutes N] [--port P] [--channel C]
                                           keep one signed-in browser up for several short scripts.
                                           Prints the address to connect_over_cdp to. Closes and
                                           deletes its profile after N minutes (default 90, at most
                                           240), on Ctrl-C or kill, when the window is closed, or on
                                           `stop`.
    stop                                   close the browser `serve` started, and check its profile is gone
    status                                 is a `serve` browser up, and until when
    selftest                               planted cases, no browser

In Python, for a batch that runs in one process:

    with signed_in_browser.session(url, ready=check) as s:
        ... s.page ... s.goto(next_url) ...

WHY. A tool that launches a fresh browser for every step pays a sign-in each time, and a browser with
force-installed extensions can unpack hundreds of MB per launch. A tool that keeps its signed-in
profile for reuse leaves session cookies on disk. Every tool that drives a signed-in site gets its
browser here instead, once per batch, and the profile goes when the batch ends.

CONFIG (agent-workbench config, all optional):
    signed_in_browser.channel            "msedge" (default) or "chrome": the installed browser to drive
    signed_in_browser.no_profile_under   folders a profile must never sit under, because they are
                                         synced to a cloud drive or backed up (default ~/Desktop and
                                         ~/Documents)
--channel on the command line overrides the config.

RULES IT KEEPS
  - The profile is a fresh folder in the system temp dir, never under a synced folder. It refuses to
    start when the temp dir resolves under one. No signed-in profile is kept between sessions: the
    folder is deleted when the batch ends, and a run that was killed is reaped at the next launch,
    by its pid in the folder's name.
  - `serve` has a hard limit (default 90 minutes, at most 240) so a forgotten browser does not
    outlive the session that needed it.
  - The browser's temp files go inside the same folder (MAC_CHROMIUM_TMPDIR), so they go with it.
  - It types nothing into any page. Signing in is the caller's `ready` function. `sso_ready` here
    only waits while single sign-on finishes on its own, and hands the window to the user on a
    password, code, account picker or consent screen.

EXIT 0 done, 1 refused, 2 did not run (no Playwright or browser, the sign-in was not finished).

BLIND TO what the steps do; it counts launches (LAUNCHES), it does not check them. A `serve` browser
answers any local process on its port while it is up. A folder reaped by pid could in principle
belong to a new process that reused the pid; it is then only reaped later.
"""
import contextlib, json, os, re, shutil, signal, sys, tempfile, time, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import kit_config
except ImportError:
    kit_config = None

PREFIX = "signed_in_"          # folder name: signed_in_<pid>_<random>
SERVE_DEFAULT, SERVE_MAX = 90, 240
LAUNCHES = 0                   # browser launches in this process; the selftests and batch callers read it


def _cfg(key, default):
    v = kit_config.get("signed_in_browser", key) if kit_config else None
    return default if v in (None, "", []) else v


def channel():
    return _cfg("channel", "msedge")


def no_profile_under():
    roots = _cfg("no_profile_under", ["~/Desktop", "~/Documents"])
    return [os.path.realpath(os.path.expanduser(r)) for r in roots]


def temp_root():
    """The per-user temp dir. On macOS read from the system, so a scheduled job without TMPDIR (which
    falls back to /tmp) looks where a shell's run put the profile."""
    if sys.platform == "darwin":
        try:
            import subprocess
            d = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True,
                               timeout=20).stdout.strip()
            if d and os.path.isdir(d):
                return os.path.realpath(d)
        except (OSError, subprocess.SubprocessError):
            pass
    return os.path.realpath(tempfile.gettempdir())


SERVE_STATE = os.path.join(temp_root(), "signed_in_serve.json")


def root_refusal(root, synced=None):
    """Refusal text or None: the profile must not sit under a synced folder."""
    real = os.path.realpath(root)
    for s in (no_profile_under() if synced is None else [os.path.realpath(x) for x in synced]):
        if real == s or real.startswith(s + os.sep):
            return (f"REFUSED: the temp dir {real} is under {s}, which is synced or backed up; a "
                    "signed-in profile may not go there. Unset TMPDIR or point it elsewhere.")
    return None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap(root=None, now=None, alive=_alive, dry=False):
    """Delete profile folders left by runs that were killed (their pid is gone). Returns the paths
    deleted (with dry, the ones it would delete). Runs at every launch, and daily from upkeep.py."""
    root = root or temp_root()
    gone = []
    try:
        names = os.listdir(root)
    except OSError:
        return gone
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        m = re.match(re.escape(PREFIX) + r"(\d+)_", name)
        if not m:
            continue
        pid = int(m.group(1))
        if pid == os.getpid() or alive(pid):
            continue
        if dry:
            gone.append(path)
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            gone.append(path)
    if not dry and root == temp_root():
        st = _read_state()
        if st and not _alive(st.get("pid", 0)) and not os.path.exists(st.get("folder", "")):
            try:
                os.remove(SERVE_STATE)   # a killed serve's record, its folder already reaped
            except OSError:
                pass
    return gone


def new_folder(root=None, synced=None):
    root = root or temp_root()
    why = root_refusal(root, synced)
    if why:
        raise SystemExit(why)
    return tempfile.mkdtemp(prefix=f"{PREFIX}{os.getpid()}_", dir=root)


def sso_ready(host):
    """A `ready` function for a site on `host` behind single sign-on: 'ready' once the tab is on that
    host with content and no sign-in form, 'needs-user' on a password, code, account picker or
    consent screen, or an identity provider's email field, else 'wait'. It never types."""
    js = """() => ({url: location.href,
      password: !!document.querySelector('input[type=password]'),
      code: !!document.querySelector('input[autocomplete="one-time-code"],input[name=otc]'),
      email: !!document.querySelector('input[type=email],input[name=loginfmt],input[name=identifier]'),
      text: document.body ? document.body.innerText.slice(0, 3000) : ''})"""

    def ready(page):
        try:
            info = page.evaluate(js)
        except Exception:
            return "wait"
        return classify(info, host)
    return ready


def classify(info, host):
    at = urllib.parse.urlparse(info.get("url") or "").netloc
    on_site = at == host or at.endswith("." + host)
    if info.get("password") or info.get("code") or re.search(
            r"pick an account|choose an account|permissions requested|consent", info.get("text") or "", re.I):
        return "needs-user"
    if not on_site and info.get("email"):
        return "needs-user"      # the identity provider asks who you are: the user's to answer
    if on_site:
        return "ready" if len(info.get("text") or "") > 200 else "wait"
    return "wait"


def wait_ready(page, ready, wait_user, settle=90, now=time.time, sleep=time.sleep):
    """Poll ready(page) until 'ready'. After `settle` seconds of waiting, or at once on 'needs-user',
    the window is the user's for `wait_user` seconds. 'ready', or why not."""
    start, user_since = now(), None
    while True:
        state = ready(page)
        if state == "ready":
            return "ready"
        if state not in ("wait", "needs-user"):
            return state             # the caller's own reason
        if user_since is None and (state == "needs-user" or now() - start > settle):
            user_since = now()
            print(f"  SIGN-IN NEEDS YOU: finish it in the browser window that opened. Waiting {wait_user}s.")
        if user_since is not None and now() - user_since > wait_user:
            return f"the sign-in was not finished within {wait_user}s"
        sleep(1)


class Session:
    def __init__(self, page, ctx, folder):
        self.page, self.ctx, self.folder = page, ctx, folder

    def goto(self, url, timeout=90000):
        """Open url, once more if the first load after idle drops the connection."""
        from playwright.sync_api import Error as PlaywrightError
        for attempt in (1, 2):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                return
            except PlaywrightError:
                if attempt == 2:
                    raise


def _launch(p, folder, args=(), chan=None):
    """Start the browser on the folder's profile. The playwright handle `p` is the caller's."""
    global LAUNCHES
    env = dict(os.environ, MAC_CHROMIUM_TMPDIR=os.path.join(folder, "t"))
    os.makedirs(env["MAC_CHROMIUM_TMPDIR"], exist_ok=True)
    LAUNCHES += 1
    return p.chromium.launch_persistent_context(
        os.path.join(folder, "profile"), channel=chan or channel(), headless=False, env=env,
        viewport={"width": 1280, "height": 900},
        args=["--no-first-run", "--no-default-browser-check", *args])


class NotReady(Exception):
    """The browser or the sign-in did not get far enough; str() says why."""


@contextlib.contextmanager
def session(url, ready=None, wait_user=180, chan=None):
    """One browser for the whole `with` block: launched once, `url` opened, `ready(page)` waited for,
    then closed and its profile deleted however the block ends. Raises NotReady when the browser did
    not start, `url` did not load, or the sign-in was not finished."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from pw_temp import sync_playwright   # the driver's own temp folder goes with the run too
    except ImportError:
        raise NotReady("Playwright is not installed for this Python (pip install playwright)")
    reap()
    folder = new_folder()
    try:
        with sync_playwright() as p:
            try:
                ctx = _launch(p, folder, chan=chan)
            except PlaywrightError as e:
                raise NotReady(f"the browser did not start: {str(e).splitlines()[0]}")
            try:
                pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                s = Session(pg, ctx, folder)
                try:
                    s.goto(url)
                except PlaywrightError as e:
                    raise NotReady(f"{url[:60]} did not load: {str(e).splitlines()[0]}")
                if ready is not None:
                    state = wait_ready(pg, ready, wait_user)
                    if state != "ready":
                        raise NotReady(state)
                yield s
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    finally:
        shutil.rmtree(folder, ignore_errors=True)   # the profile held the session cookies


# --- serve: one browser for several scripts, with a hard limit ----------------------------------

def _read_state():
    try:
        return json.load(open(SERVE_STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(d):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SERVE_STATE))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SERVE_STATE)


def serve(url, minutes=SERVE_DEFAULT, port=9333, wait_user=600, chan=None):
    try:
        from playwright.sync_api import Error as PlaywrightError
        from pw_temp import sync_playwright
    except ImportError:
        print("DID NOT RUN: Playwright is not installed for this Python (pip install playwright).")
        return 2
    minutes = max(1, min(int(minutes), SERVE_MAX))
    old = _read_state()
    if old and _alive(old.get("pid", 0)):
        print(f"REFUSED: a serve browser is already up (pid {old['pid']}, port {old['port']}, until "
              f"{time.strftime('%H:%M', time.localtime(old['deadline']))}). Use it, or `stop` it first.")
        return 1
    reap()
    folder = new_folder()
    stop = {"now": False}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.update(now=True))
    host = urllib.parse.urlparse(url).netloc
    why = "did not start"
    try:
        with sync_playwright() as p:
            try:
                ctx = _launch(p, folder, [f"--remote-debugging-port={port}"], chan)
            except PlaywrightError as e:
                print(f"DID NOT RUN: the browser did not start: {str(e).splitlines()[0]}")
                return 2
            closed = {"yes": False}
            ctx.on("close", lambda *_: closed.update(yes=True))
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                Session(pg, ctx, folder).goto(url)
            except PlaywrightError as e:
                print(f"DID NOT RUN: {url[:60]} did not load: {str(e).splitlines()[0]}")
                ctx.close()
                return 2
            deadline = time.time() + minutes * 60
            _write_state({"pid": os.getpid(), "port": port, "folder": folder, "url": url,
                          "started": time.time(), "deadline": deadline})
            state = wait_ready(pg, sso_ready(host), wait_user)
            print(f"SERVING http://127.0.0.1:{port} ({state}) until {time.strftime('%H:%M', time.localtime(deadline))}."
                  " Connect with playwright chromium.connect_over_cdp; never call browser.close() there.",
                  flush=True)
            while not stop["now"] and not closed["yes"] and time.time() < deadline:
                try:
                    pg.wait_for_timeout(1000)
                except PlaywrightError:
                    break              # the window or the tab was closed
            why = "stopped" if stop["now"] else "window closed" if closed["yes"] else f"{minutes} minutes up"
            try:
                ctx.close()
            except Exception:
                pass
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        st = _read_state()
        if st and st.get("pid") == os.getpid():
            os.remove(SERVE_STATE)
    print(f"CLOSED ({why}); profile deleted: {not os.path.exists(folder)}")
    return 0


def stop_serve(wait=90):          # a loaded machine can take well over 30 s to close the browser
    st = _read_state()
    if not st:
        print("Nothing to stop: no serve browser is recorded.")
        return 0
    pid, folder = st.get("pid", 0), st.get("folder", "")
    if _alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(wait):
            if not _alive(pid):
                break
            time.sleep(1)
    if _alive(pid):
        print(f"DID NOT RUN: pid {pid} is still up after {wait}s.")
        return 2
    if folder and os.path.exists(folder):
        shutil.rmtree(folder, ignore_errors=True)
    if os.path.exists(SERVE_STATE):
        os.remove(SERVE_STATE)
    print(f"Stopped pid {pid}; profile folder gone: {not os.path.exists(folder)}")
    return 0


def status():
    st = _read_state()
    if not st or not _alive(st.get("pid", 0)):
        print("No serve browser is up.")
        return 0
    print(f"Serve browser up: pid {st['pid']}, http://127.0.0.1:{st['port']}, {st['url'][:60]}, until "
          f"{time.strftime('%H:%M', time.localtime(st['deadline']))}.")
    return 0


def selftest():
    results = []

    def case(name, ok):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    td = tempfile.mkdtemp(prefix="sib-selftest-")
    try:
        live, dead = os.path.join(td, f"{PREFIX}111_a"), os.path.join(td, f"{PREFIX}222_b")
        other = os.path.join(td, "playwright_chromiumdev_profile-z")
        for d in (live, dead, other):
            os.makedirs(os.path.join(d, "profile"))
        would = reap(td, alive=lambda pid: pid == 111, dry=True)
        case("reap --dry: names the dead pid's folder, deletes nothing",
             would == [dead] and os.path.exists(dead))
        gone = reap(td, alive=lambda pid: pid == 111)
        case("reap: a profile whose pid is gone is deleted", dead in gone and not os.path.exists(dead))
        case("reap: a profile whose pid is alive is kept", os.path.exists(live))
        case("reap: a folder no tool of ours made is kept", os.path.exists(other))
        synced = os.path.join(td, "Synced")
        case("a temp dir under a synced folder is refused", root_refusal(os.path.join(synced, "x"), [synced]) is not None)
        case("... and the synced folder itself", root_refusal(synced, [synced]) is not None)
        case("a sibling whose name only starts the same passes", root_refusal(synced + "-other", [synced]) is None)
        case("the default list holds the home Desktop", os.path.realpath(os.path.expanduser("~/Desktop"))
             in no_profile_under() or kit_config and kit_config.get("signed_in_browser", "no_profile_under"))
        f = new_folder(td, [])
        case("a new profile folder carries this pid", os.path.basename(f).startswith(f"{PREFIX}{os.getpid()}_"))
        case("... and reap leaves this process's own folder alone", f not in reap(td) and os.path.exists(f))
        site = "app.example.com"
        case("on the site with content: ready",
             classify({"url": "https://app.example.com/home", "text": "x" * 300}, site) == "ready")
        case("on the site, still blank: wait",
             classify({"url": "https://app.example.com/home", "text": ""}, site) == "wait")
        case("a blank redirect through the identity provider: wait",
             classify({"url": "https://idp.example.net/saml2", "text": ""}, site) == "wait")
        case("the identity provider's account field: the user's, never typed into",
             classify({"url": "https://idp.example.net/x", "email": True}, site) == "needs-user")
        case("a password: the user's",
             classify({"url": "https://idp.example.net/x", "password": True}, site) == "needs-user")
        case("an account picker: the user's",
             classify({"url": "https://idp.example.net/x", "text": "Pick an account"}, site) == "needs-user")
        case("a look-alike host is not the site",
             classify({"url": "https://app.example.com.evil.example/", "text": "x" * 300}, site) == "wait")
        clock = [0.0]
        tick = lambda: clock[0]

        def sleep(_):
            clock[0] += 1
        states = iter(["wait", "wait", "ready"])
        case("wait_ready: waits out the redirect and reports ready",
             wait_ready(None, lambda _: next(states), 5, now=tick, sleep=sleep) == "ready")
        clock[0] = 0.0
        got = wait_ready(None, lambda _: "needs-user", 3, now=tick, sleep=sleep)
        case("wait_ready: a sign-in the user never finishes gives up after the user's wait", "not finished" in got)
        case("wait_ready: a caller's own reason comes back as is",
             wait_ready(None, lambda _: "the account is someone else's", 3, now=tick, sleep=sleep)
             == "the account is someone else's")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"{sum(results)} of {len(results)} planted cases behave as expected.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if args[0] in ("selftest", "--selftest"):
        sys.exit(selftest())
    if args[0] == "stop":
        sys.exit(stop_serve())
    if args[0] == "status":
        sys.exit(status())
    if args[0] == "serve" and len(args) >= 2:
        opts = {"--minutes": SERVE_DEFAULT, "--port": 9333, "--channel": None}
        rest = list(args[1:])
        for flag in opts:
            if flag in rest:
                i = rest.index(flag)
                opts[flag] = rest[i + 1] if flag == "--channel" else int(rest[i + 1])
                del rest[i:i + 2]
        sys.exit(serve(rest[0], opts["--minutes"], opts["--port"], chan=opts["--channel"]))
    print(__doc__)
    sys.exit(2)
