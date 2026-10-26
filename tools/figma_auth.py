#!/usr/bin/env python3
"""Resolve a Figma personal access token without keeping a second copy of the secret.

WHY THIS EXISTS
---------------
A token that lives only in one shell's environment works when that shell runs a tool and fails the
moment the same command runs from a process that did not load it: a GUI app, a git hook, a scheduled
job. A check run in the wrong process ("the token is set") is not evidence about the right one. So
every Figma tool resolves the token here, from the same places, and `--source` says which one
answered.

The tempting fix, exporting the token into one more place (a user-level variable, a dotfile, a
config file), copies a live credential. Two copies are two things to rotate and one to forget. This
module reads the copy that already exists and never writes one.

RESOLUTION ORDER, first hit wins:
    1. FIGMA_TOKEN / FIGMA_PAT / FIGMA_ACCESS_TOKEN in the process environment
    2. the OS keychain, the same convention kit_config.token() uses: service $FIGMA_KEYCHAIN_SERVICE
       (default "agent-workbench"), account "FIGMA_TOKEN".
         macOS:  security add-generic-password -s agent-workbench -a FIGMA_TOKEN -w
         Linux:  secret-tool store --label=figma service agent-workbench account FIGMA_TOKEN
       The value is read into this process and never written anywhere else.
       FIGMA_AUTH_NO_KEYCHAIN=1 (or AGENT_WORKBENCH_NO_KEYCHAIN=1) turns this source off, for a test of
       the no-token path.

On macOS, whether `security` asks before it answers depends on the item's access list; an item
created with `security` itself lists `security`, so a logged-in session reads it without a prompt. A
read that takes longer than KEYCHAIN_TIMEOUT seconds, which is what a prompt nobody answers looks
like, counts as a miss, and `--source` says so.

SECURITY
--------
The token value is NEVER printed, logged, or written anywhere by this module. `describe()` returns
only the NAME of the source it came from. `main()` prints a source name and a live/dead verdict.
If you add a code path here, it prints the source, never the secret.
"""

import argparse
import os
import shutil
import subprocess
import sys

ENV_KEYS = ("FIGMA_TOKEN", "FIGMA_PAT", "FIGMA_ACCESS_TOKEN")
SECURITY = "/usr/bin/security"
KEYCHAIN_TIMEOUT = 10
DEFAULT_SERVICE = "agent-workbench"
ACCOUNT = "FIGMA_TOKEN"
# Why the last Keychain read missed, for --source. Never holds the value.
_keychain_note = None

# check()'s one call, made through figma_client.py beside this file like every other Figma call.
API = "me"
HERE = os.path.dirname(os.path.abspath(__file__))


def _service():
    return os.environ.get("FIGMA_KEYCHAIN_SERVICE") or DEFAULT_SERVICE


def _keychain_cmd(svc, account):
    """The command that prints the item's value, or (None, why not)."""
    if sys.platform == "darwin":
        if not os.path.exists(SECURITY):
            return None, "no %s" % SECURITY
        return [SECURITY, "find-generic-password", "-s", svc, "-a", account, "-w"], None
    tool = shutil.which("secret-tool")
    if tool:
        return [tool, "lookup", "service", svc, "account", account], None
    return None, None


def _from_keychain():
    """(token, source_name) out of the OS keychain, or (None, None).

    The value comes back on the child's stdout, which only this process reads. Never raises: no
    keychain tool, no item, a locked keychain or a read that hangs on a prompt is a miss.
    """
    global _keychain_note
    _keychain_note = None
    if os.environ.get("FIGMA_AUTH_NO_KEYCHAIN") == "1" or os.environ.get("AGENT_WORKBENCH_NO_KEYCHAIN"):
        _keychain_note = "off"
        return None, None
    svc = _service()
    cmd, why = _keychain_cmd(svc, ACCOUNT)
    if not cmd:
        _keychain_note = why
        return None, None
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=KEYCHAIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        _keychain_note = "the read of %s/%s took over %ds (a prompt nobody answered?)" % (svc, ACCOUNT, KEYCHAIN_TIMEOUT)
        return None, None
    except OSError as e:
        _keychain_note = "could not run %s (%s)" % (cmd[0], e.__class__.__name__)
        return None, None
    v = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
    if v:
        return v, "keychain:%s/%s" % (svc, ACCOUNT)
    _keychain_note = "no %s/%s item" % (svc, ACCOUNT)
    return None, None


def resolve():
    """(token, source_name), or (None, None). Does not exit."""
    for k in ENV_KEYS:
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip(), "env:%s" % k
    return _from_keychain()


def describe():
    """The NAME of the source the token came from, or None. Never the value."""
    return resolve()[1]


def keychain_where():
    """Where source 2 looks, in words, with why it missed when it did."""
    if sys.platform != "darwin" and not shutil.which("secret-tool"):
        return "the OS keychain (no macOS `security` or `secret-tool` here)"
    where = "the keychain item %s/%s" % (_service(), ACCOUNT)
    return where + (" (%s)" % _keychain_note if _keychain_note else "")


def token(required=True):
    """The token string. Exits with an actionable message when required and absent."""
    tok, _ = resolve()
    if tok:
        return tok
    if not required:
        return None
    sys.exit(
        "No Figma token found.\n"
        "  Looked in: %s\n"
        "        and: %s\n"
        "Set one of %s in the environment, or store it once in the OS keychain\n"
        "(see figma_auth.py's docstring). Run `python figma_auth.py` to check."
        % (", ".join(ENV_KEYS), keychain_where(), "/".join(ENV_KEYS))
    )


def headers(required=True):
    """The auth header, for code that cannot go through figma_client.py. A tool makes its calls
    through figma_client.py, which paces them; `figma_client.py --audit` fails a tool that uses this."""
    return {"X-Figma-Token": token(required=required)}  # figma-client: allow (the resolver's own helper)


def check():
    """Resolve AND prove the token authenticates. Returns (ok, source, detail).

    Resolving is not the same claim as working. An expired PAT resolves fine and then 403s on the
    first real call, which reads as a tool bug rather than an auth problem.
    """
    tok, src = resolve()
    if not tok:
        return False, None, "no token found"
    import urllib.error
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import figma_client

    try:
        who = figma_client.get(API, token=tok, tool="figma_auth", timeout=20, deadline=60)
        return True, src, "authenticated as %s" % who.get("email", "?")
    except figma_client.RateLimited as e:
        return False, src, "rate limited past the deadline on /v1/%s (%s); the token was not tested" % (API, e.msg)
    except urllib.error.HTTPError as e:
        return False, src, "HTTP %s from /v1/%s (token resolved but rejected)" % (e.code, API)
    except Exception as e:  # network, DNS, proxy
        return False, src, "could not reach /v1/%s: %s" % (API, e.__class__.__name__)


def main():
    a = argparse.ArgumentParser(
        prog="figma_auth.py",
        description="Report where the Figma token resolves from, and whether it still works. "
                    "Prints the SOURCE NAME and a live/dead verdict -- never the token itself.",
        epilog="Exit 0 = a token resolved and authenticated (with --source: resolved); 1 = it did not.",
    )
    a.add_argument("--source", action="store_true",
                   help="only say where a token resolves from, with no network call")
    args = a.parse_args()
    if args.source:
        src = describe()
        print("source: %s" % (src or "NONE"))
        if not src:
            print("  looked in: %s; %s" % (", ".join(ENV_KEYS), keychain_where()))
        return 0 if src else 1
    ok, src, detail = check()
    print("source: %s" % (src or "NONE"))
    print("status: %s - %s" % ("OK" if ok else "FAIL", detail))
    if not ok and src:
        print("\nThe token was found but did not work. Rotate it in Figma and update the source above.")
    elif not ok:
        print("\nNothing to resolve. See the module docstring for where this looks.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
