"""leak_scan.py - refuse a commit that would publish a secret or a term you keep private.

    python scripts/leak_scan.py --deny FILE [--allow FILE] [--staged | PATH ...]
    python scripts/leak_scan.py --selftest

WHY THIS EXISTS. This repo is a public copy of a working setup that lives somewhere private. Each
port from the private side can carry a token, a colleague's name, an internal host or a file id.
A reviewer misses those; a list doesn't.

WHAT IT CHECKS, in every text file given (or every staged file with --staged):
  - secrets: GitHub, GitLab, Figma, Slack, AWS, Anthropic and OpenAI token shapes, private key
    blocks, Azure DevOps PATs assigned to a variable, and "password = ..." style assignments
  - every term in the --deny file: one per line, matched case-insensitively as a whole word; a line
    starting "re:" is a Python regex; "#" starts a comment. Keep that file OUT of the repo, since
    it names what it protects. $LEAK_SCAN_DENY names it when --deny is not given.
  - the --allow file (optional) lists "<path>\t<term>" pairs that may appear, e.g. the author's
    name in LICENSE.

EXIT CODES. 0 clean, 1 findings (each printed as path:line: kind: the matched text, masked for a
secret), 2 could not run (no deny file, unreadable input). Never 0 when it could not run.
"""
import argparse, os, re, subprocess, sys, tempfile

SECRETS = [
    ("github-token", r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    ("gitlab-token", r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    ("figma-token", r"\bfig[dou]_[A-Za-z0-9_-]{30,}\b"),
    ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ("aws-key", r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"),
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    ("openai-key", r"\bsk-(proj-)?[A-Za-z0-9]{32,}\b"),
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("ado-pat", r"(?i)\b(pat|ado_pat|azure_devops_pat)\s*[=:]\s*['\"][a-z0-9]{52}['\"]"),
    ("password", r"(?i)\b(password|passwd|secret)\s*[=:]\s*['\"][^'\"\s]{8,}['\"]"),
]


def load_deny(path):
    terms = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("re:"):
                terms.append((line, re.compile(line[3:], re.I)))
            else:
                terms.append((line, re.compile(r"(?<![A-Za-z0-9])" + re.escape(line) + r"(?![A-Za-z0-9])", re.I)))
    return terms


def load_allow(path):
    allow = set()
    if path:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                if raw.strip() and not raw.startswith("#") and "\t" in raw:
                    p, t = raw.rstrip("\n").split("\t", 1)
                    allow.add((p.strip(), t.strip()))
    return allow


def staged_files(root):
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
                         cwd=root, capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p]


def staged_text(root, path):
    r = subprocess.run(["git", "show", ":" + path], cwd=root, capture_output=True)
    return r.stdout


def mask(s):
    return s[:4] + "..." + str(len(s)) + " chars" if len(s) > 8 else "***"


def scan_text(rel, data, deny, allow):
    if b"\0" in data[:4096]:
        return []
    text = data.decode("utf-8", errors="replace")
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, pat in SECRETS:
            for m in re.finditer(pat, line):
                hits.append(f"{rel}:{n}: secret {kind}: {mask(m.group(0))}")
        for label, rx in deny:
            if (rel, label) in allow:
                continue
            for m in rx.finditer(line):
                hits.append(f"{rel}:{n}: private term: {m.group(0)!r}")
    # A term split across a path also counts.
    for label, rx in deny:
        if (rel, label) not in allow and rx.search(rel):
            hits.append(f"{rel}:0: private term in the path: {label!r}")
    return hits


def run(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--deny", default=os.environ.get("LEAK_SCAN_DENY"))
    ap.add_argument("--allow", default=os.environ.get("LEAK_SCAN_ALLOW"))
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("paths", nargs="*")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.deny or not os.path.isfile(a.deny):
        print("leak_scan: COULD NOT RUN: no deny file (pass --deny or set LEAK_SCAN_DENY)", file=sys.stderr)
        return 2
    deny, allow = load_deny(a.deny), load_allow(a.allow)
    root = os.getcwd()
    items = []
    try:
        if a.staged:
            items = [(p, staged_text(root, p)) for p in staged_files(root)]
        else:
            for p in a.paths or ["."]:
                if os.path.isdir(p):
                    for d, dirs, files in os.walk(p):
                        dirs[:] = [x for x in dirs if x not in (".git", "__pycache__", "node_modules")]
                        for f in files:
                            fp = os.path.join(d, f)
                            items.append((os.path.relpath(fp, root), open(fp, "rb").read()))
                else:
                    items.append((os.path.relpath(p, root), open(p, "rb").read()))
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"leak_scan: COULD NOT RUN: {e}", file=sys.stderr)
        return 2
    hits = []
    for rel, data in items:
        hits += scan_text(rel, data, deny, allow)
    for h in hits:
        print(h)
    print(f"leak_scan: {len(hits)} finding(s) in {len(items)} file(s), {len(deny)} private terms", file=sys.stderr)
    return 1 if hits else 0


def selftest():
    with tempfile.TemporaryDirectory() as d:
        deny = os.path.join(d, "deny.txt")
        with open(deny, "w", encoding="utf-8") as f:
            f.write("# c\nAcme Corp\nre:\\bjdoe\\b\n")
        allow = os.path.join(d, "allow.txt")
        with open(allow, "w", encoding="utf-8") as f:
            f.write("LICENSE\tAcme Corp\n")
        cases = [
            ("good.md", "Plain text about acmecorporation and jdoes.\n", 0),
            ("term.md", "Built at acme corp last year.\n", 1),
            ("regex.md", "ask jdoe\n", 1),
            ("tok.py", "T = 'ghp_" + "a" * 36 + "'\n", 1),
            ("key.txt", "-----BEGIN RSA " + "PRIVATE KEY-----\n", 1),
            ("LICENSE", "Copyright Acme Corp\n", 0),
        ]
        ok = 0
        for name, body, want in cases:
            p = os.path.join(d, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
            cwd = os.getcwd()
            os.chdir(d)
            try:
                import io, contextlib
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    got = run(["--deny", deny, "--allow", allow, name])
            finally:
                os.chdir(cwd)
            print(("PASS" if got == want else "FAIL"), name, "want", want, "got", got)
            ok += got == want
        with contextlib.redirect_stderr(io.StringIO()):
            nodeny = run(["--deny", os.path.join(d, "missing.txt"), "x"])
        print(("PASS" if nodeny == 2 else "FAIL"), "missing deny file exits 2, got", nodeny)
        ok += nodeny == 2
        total = len(cases) + 1
        print(f"selftest: {ok} of {total}")
        return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
