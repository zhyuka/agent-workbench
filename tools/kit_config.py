"""kit_config.py - a project's settings for the agent-workbench tools: its Azure DevOps org and project,
its work-item title convention, its Figma files, and what recall.py searches. Every key, for every
tool, is listed in docs/config.md.

WHY THIS EXISTS. Values like an ADO project or a Figma file key do not belong inside a tool. A tool
that carries one reads from, or files into, that project when run anywhere else. So each project
keeps its own values in a file, and no tool has a project of its own.

WHERE IT LOOKS. Two files, merged key by key, the project's over yours:
  ~/.claude/agent-workbench.json           yours: folders on your machine (notes, transcripts), and the
                                        project to use when you work outside any project folder
  <project>/.claude/agent-workbench.json   the project's, committed with it: the nearest one, walking up
                                        from the folder you run in, the way git finds .git
$AGENT_WORKBENCH_CONFIG names one file to use in place of the project's (a test, or a one-off).
A tool that needs a value neither file has stops, names the key and both places it looked, and exits
2. There is no built-in default project: a missing setting must not quietly read from or file into
somebody else's ADO project.

THE FILE. Every key is optional; a tool asks only for what it uses.
  {"ado":   {"org": "https://dev.azure.com/<org>", "project": "<project>",
             "title_prefix": "<prefix>", "search_stop": ["<word>", ...],
             "frozen_branch": "<branch>", "backlog_iteration": "<iteration name>",
             "phase_suffixes": ["design", "build", ...], "decision_tag": "decision"},
   "figma": {"files": {"<file key>": {"kind": "design", "name": "<label>"}},
             "default_file": "<file key>"},
   "seen_before": {"site": ["<path from the project root>"],
                   "notes": "<folder>", "transcripts": "<folder>"}}

  key                    read by                          what it does
  ado.org                ado.py, ado_rest.py, ado_write.py the organization URL
  ado.project            ado.py, ado_rest.py, ado_write.py the project name
  ado.title_prefix       ado.py (new, lint), ado_rest.py   titles read '<prefix> // {Page - Section} // {what}';
                         (audit title-convention)         unset, any title passes
  ado.search_stop        ado.py (search, new)             extra words the duplicate search ignores, such
                                                          as your product's name, which is in every title
  ado.frozen_branch      ado_rest.py (audit)              a branch nobody may propose merging to; the
                                                          frozen-branch-mentioned check runs only when set
  ado.backlog_iteration  ado_rest.py (audit)              the dateless "someday" iteration; done-in-backlog
                                                          runs only when set
  ado.phase_suffixes     ado_rest.py (audit dup-title)    title endings that mark a planned pair (a "Design"
                                                          and a "Build" item on one stem), not a duplicate
  ado.decision_tag       ado_rest.py (decisions)          the tag that marks an open question; default
                                                          "decision"
  figma.files            figma tools; ado.py (new)        the files a search reads when none is named; kind
                                                          is "design" or "board"
  figma.default_file     figma tools; ado.py (new)        the file a bare node id such as 123:456 refers to
  seen_before.site       recall.py                        files and folders searched as the project's own
                                                          records, e.g. a rulings file
  seen_before.notes      recall.py (fallback)             your notes folder
  seen_before.transcripts recall.py (fallback)            your meeting transcripts folder

TOKENS are never in this file. token("ADO_PAT", ...) reads the first environment variable that is set,
and otherwise, optionally, the OS keychain: on macOS the generic password with service "agent-workbench"
and account "<the variable name>" (security add-generic-password -s agent-workbench -a ADO_PAT -w), on
Linux `secret-tool lookup service agent-workbench account ADO_PAT`. The value is returned, never printed.

  python kit_config.py              where it looked, what each file holds, and the merged settings
  python kit_config.py --selftest   plant both files and break each rule once
"""
import json, os, shutil, subprocess, sys

NAME = "agent-workbench"
FILE = os.path.join(".claude", NAME + ".json")
ENV = "AGENT_WORKBENCH_CONFIG"
MUT = set()   # selftest only: the rule each mutation breaks

_loaded = {}


class NotConfigured(RuntimeError):
    """A tool needed a setting that neither file holds."""


def user_path():
    return os.path.join(os.path.expanduser("~"), FILE)


def project_path(start=None):
    """$AGENT_WORKBENCH_CONFIG, else the nearest <dir>/.claude/agent-workbench.json at or above start (the
    working folder by default), else None. The file in your home folder is yours, not a project's."""
    if os.environ.get(ENV) and "no_env" not in MUT:
        return os.path.abspath(os.path.expanduser(os.environ[ENV]))
    d = os.path.abspath(start or os.getcwd())
    mine = os.path.realpath(user_path())
    while True:
        p = os.path.join(d, FILE)
        if os.path.isfile(p) and os.path.realpath(p) != mine:
            return p
        if "no_walk" in MUT:
            return None
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def _read(path, problems):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        problems.append(f"{path} could not be read: {e}")
        return {}
    if not isinstance(d, dict):
        problems.append(f"{path} holds {type(d).__name__}, not an object")
        return {}
    return d


def _merge(low, high):
    out = dict(low)
    for k, v in high.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(start=None):
    """{"settings": merged dict, "user": path, "project": path or None, "problems": [...]}. Never
    raises: a file that will not parse is left out and named under problems, which need() reports."""
    key = (os.path.abspath(start or os.getcwd()), os.environ.get(ENV), user_path())
    if key not in _loaded:
        problems = []
        u, p = user_path(), project_path(start)
        mine, theirs = _read(u, problems), _read(p, problems)
        merged = _merge(theirs, mine) if "user_wins" in MUT else _merge(mine, theirs)
        _loaded[key] = {"settings": merged, "user": u, "project": p, "problems": problems}
    return _loaded[key]


def reset():
    _loaded.clear()


def get(*keys, default=None, start=None):
    v = load(start)["settings"]
    for k in keys:
        if not isinstance(v, dict) or k not in v:
            return default
        v = v[k]
    return v


def need(*keys, start=None):
    """The value, or NotConfigured naming the key, both places looked, and any file that would not
    parse."""
    v = get(*keys, start=start)
    if v not in (None, "", [], {}):
        return v
    c = load(start)
    where = f"{c['project'] or 'no ' + FILE + ' at or above ' + os.path.abspath(start or os.getcwd())}"
    msg = (f"not configured: {'.'.join(keys)} is not set. Looked in {where} and {c['user']}. "
           f"Set it in the project's {FILE} (see kit_config.py for the format).")
    if c["problems"]:
        msg += " " + " ".join(c["problems"])
    raise NotConfigured(msg)


def ado():
    """(org without a trailing slash, project), each None when unset."""
    org = get("ado", "org")
    return (org.rstrip("/") if isinstance(org, str) and org else None), (get("ado", "project") or None)


def figma_files():
    """{file key: (kind, name)} in the order the file lists them."""
    out = {}
    for k, v in (get("figma", "files") or {}).items():
        v = v if isinstance(v, dict) else {}
        out[k] = (v.get("kind") or "design", v.get("name") or k)
    return out


def token(*names):
    """The first of the named environment variables that is set, else the OS keychain entry for the
    first name (service NAME, account <name>), else None. Never printed, logged or written."""
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    if not names or os.environ.get("AGENT_WORKBENCH_NO_KEYCHAIN"):
        return None
    if sys.platform == "darwin" and shutil.which("security"):
        cmd = ["security", "find-generic-password", "-s", NAME, "-a", names[0], "-w"]
    elif shutil.which("secret-tool"):
        cmd = ["secret-tool", "lookup", "service", NAME, "account", names[0]]
    else:
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def run(main):
    """Run a tool's main(); a missing setting prints its message and exits 2 instead of a traceback."""
    try:
        return main()
    except NotConfigured as e:
        print(f"{os.path.basename(sys.argv[0])}: {e}", file=sys.stderr)
        return 2


def show():
    c = load()
    print(f"user file:    {c['user']}{'' if os.path.exists(c['user']) else ' (none)'}")
    print(f"project file: {c['project'] or '(none at or above ' + os.getcwd() + ')'}")
    for p in c["problems"]:
        print(f"PROBLEM: {p}")
    print(json.dumps(c["settings"], indent=1))
    return 0


def selftest():
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    base, results = [], []

    def check(name, ok, detail=""):
        base.append((name, bool(ok)))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if not ok and detail else ""))

    def write(path, d):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(d if isinstance(d, str) else json.dumps(d))

    def tool(root, cwd, code, env_extra=None):
        env = dict(os.environ, HOME=os.path.join(root, "home"), PYTHONPATH=here,
                   AGENT_WORKBENCH_NO_KEYCHAIN="1")
        env.pop(ENV, None)
        env.update(env_extra or {})
        for k in ("ADO_PAT", "AZURE_DEVOPS_EXT_PAT"):
            env.pop(k, None)
        return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=120)

    have_fc = os.path.exists(os.path.join(here, "figma_comments.py"))

    def checks(root):
        home, proj = os.path.join(root, "home"), os.path.join(root, "proj")
        deep, bare = os.path.join(proj, "a", "b"), os.path.join(root, "elsewhere")
        for d in (deep, bare):
            os.makedirs(d, exist_ok=True)
        old = os.environ.get("HOME"), os.environ.pop(ENV, None)
        os.environ["HOME"] = home
        try:
            reset()
            try:
                need("ado", "org", start=bare)
                raised = ""
            except NotConfigured as e:
                raised = str(e)
            check("no file anywhere: get() is None and need() refuses, naming the key and both places",
                  get("ado", "org", start=bare) is None and "ado.org" in raised and user_path() in raised)
            write(user_path(), {"ado": {"org": "https://dev.azure.com/UserOrg/", "project": "UserProj"},
                                "seen_before": {"notes": "~/notes"}})
            write(os.path.join(proj, FILE), {"ado": {"project": "Theirs", "title_prefix": "T"},
                                             "figma": {"files": {"KEY1": {"kind": "board", "name": "B"}}}})
            reset()
            check("outside any project the user file answers",
                  get("ado", "project", start=bare) == "UserProj" and get("ado", "org", start=bare)
                  == "https://dev.azure.com/UserOrg/")
            check("the project file is found by walking up from a folder below it",
                  load(deep)["project"] == os.path.join(proj, FILE))
            check("the project's value wins key by key; a key only the user file has is kept",
                  get("ado", "project", start=deep) == "Theirs" and get("ado", "org", start=deep)
                  == "https://dev.azure.com/UserOrg/" and get("seen_before", "notes", start=deep) == "~/notes")
            write(os.path.join(root, "one.json"), {"ado": {"project": "Env"}})
            os.environ[ENV] = os.path.join(root, "one.json")
            reset()
            check(f"${ENV} replaces the project file", get("ado", "project", start=deep) == "Env")
            os.environ.pop(ENV)
            write(os.path.join(bare, FILE), "{not json")
            reset()
            try:
                need("ado", "title_prefix", start=bare)
                raised = ""
            except NotConfigured as e:
                raised = str(e)
            check("a file that will not parse is left out, and need() names it",
                  get("ado", "project", start=bare) == "UserProj" and "could not be read" in raised
                  and os.path.join(bare, FILE) in raised)
            os.remove(os.path.join(bare, FILE))
        finally:
            os.environ["HOME"] = old[0] if old[0] is not None else os.environ.pop("HOME", "")
            if old[1] is not None:
                os.environ[ENV] = old[1]
            reset()

        # The tools read it: run them as a separate process from inside the planted project.
        r = tool(root, deep, "import ado_rest, ado_write, ado\n"
                             "print(ado_rest.ORG, ado_rest.PROJECT, ado_write.ORG, ado_write.PROJECT, "
                             "ado.ORG_DEFAULT, ado.PROJ_DEFAULT)\n"
                             "print(bool(ado.TITLE_RE.match('T // Home - Hero // x')), "
                             "bool(ado.TITLE_RE.match('Other // Home - Hero // x')))")
        want = ("https://dev.azure.com/UserOrg Theirs https://dev.azure.com/UserOrg Theirs "
                "https://dev.azure.com/UserOrg Theirs\nTrue False")
        check("ado.py, ado_rest.py and ado_write.py take the org, project and title prefix from it",
              r.stdout.strip() == want, (r.stdout + r.stderr).strip()[-300:])
        if have_fc:
            r = tool(root, deep, "import figma_comments as fc\nprint(list(fc.KNOWN_FILES))")
            check("figma_comments.py takes the files from it", r.stdout.strip() == "['KEY1']",
                  (r.stdout + r.stderr).strip()[-300:])
        os.remove(os.path.join(home, FILE))  # the planted one: HOME is the real one again here
        r = tool(root, bare, "import ado_rest\nprint(ado_rest.ORG, ado_rest.PROJECT)")
        check("with no file they still import, with nothing set", r.returncode == 0 and r.stdout.strip()
              == "None None", (r.stdout + r.stderr).strip()[-300:])
        r = tool(root, bare, "import sys, ado_rest\nsys.argv = ['ado_rest.py', 'pull']\n"
                             "import kit_config\nsys.exit(kit_config.run(ado_rest.main))")
        check("a tool that needs ADO with nothing set exits 2 and says what to set, before any call",
              r.returncode == 2 and "not configured: ado.org" in r.stderr, f"exit {r.returncode}: "
              + r.stderr.strip()[-300:])
        r = tool(root, bare, "import ado\nprint(bool(ado.TITLE_RE.match('anything at all')))")
        check("with no title_prefix set, any title passes the convention", r.stdout.strip() == "True",
              (r.stdout + r.stderr).strip()[-300:])
        r = tool(root, bare, "import kit_config\nprint(kit_config.token('KC_SELFTEST_A', 'KC_SELFTEST_B'))",
                 {"KC_SELFTEST_B": "from-env"})
        check("token() reads the first variable that is set", r.stdout.strip() == "from-env",
              (r.stdout + r.stderr).strip()[-300:])

    with tempfile.TemporaryDirectory() as root:
        checks(root)
    ok_base = all(ok for _, ok in base)
    if not have_fc:
        print("  (figma_comments.py is not installed beside this file, so its check did not run)")
    print("mutations: each rule broken on purpose must fail a check above")
    for m in ("no_walk", "user_wins", "no_env"):
        MUT.clear()
        MUT.add(m)
        n = len(base)
        with tempfile.TemporaryDirectory() as root:
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                checks(root)
        caught = not all(ok for _, ok in base[n:])
        del base[n:]
        results.append(caught)
        print(f"  {'CAUGHT' if caught else 'MISSED'}  {m}")
    MUT.clear()
    reset()
    print(f"{sum(ok for _, ok in base)} of {len(base)} checks passed; {sum(results)} of {len(results)} "
          "mutations caught")
    return 0 if ok_base and all(results) else 1


if __name__ == "__main__":
    if sys.argv[1:2] in (["-h"], ["--help"]):
        print(__doc__)
        sys.exit(0)
    sys.exit(selftest() if sys.argv[1:2] == ["--selftest"] else show())
