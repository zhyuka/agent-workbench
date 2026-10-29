#!/usr/bin/env python3
"""ask_rules.py - do the settings ask rules prompt on every outward write, and on no read?

Reads the `permissions.ask` list of each settings file and tests it against two lists of commands:
WRITES, this plugin's commands that post, edit or delete something outside your machine (figma_post,
figma_attach, ado_write, ado.py new), which must each match an ask rule; and READS, which only read
or test and must match none. A read that prompts is the defect: a rule as broad as
`Bash(*figma_post*)` fires on --help, selftest and grep too, and once prompts fire on reads they get
clicked through, writes included.

    python tools/ask_rules.py [settings.json ...]   exit 1 on a write with no rule or a read with one
    python tools/ask_rules.py --selftest            planted rule lists, each break caught

WHICH FILES. The paths given, else the config key ask_rules.settings (kit_config.py): a list of
settings files, such as a project's .claude/settings.json; ~ is expanded, and a relative path is read
from the folder you run in. With neither, it stops with exit 2 and names the key. A file named and
missing fails.

A rule is `Bash(<pattern>)`, `*` matching any run of characters, checked against the whole command
and against each part between `&&`, `||`, `;` and `|`, as Claude Code checks a compound command.

Blind to: a write whose command line names no tool (a script file that imports one, or the REST API
called directly with a token); rules in managed settings, which it doesn't read; a subcommand added
to a tool and not to WRITES.

Exit 0 when every write prompts and no read does; 1 on a miss or a missing file; 2 when no settings
file is named or configured.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Every subcommand that writes, from each tool's own dispatch, in the shapes a command takes: a
# relative path, the plugin root, a login shell, a cd first, an import.
WRITES = [
    "python tools/figma_post.py new FILEKEY 1:2 hello --picture a.jpg",
    "python tools/figma_post.py reply FILEKEY 123 hello",
    "python tools/figma_post.py fixnote FILEKEY 123 hello",
    "python tools/figma_post.py delete FILEKEY 123",
    "python tools/figma_post.py batch posts.json",
    'python "${CLAUDE_PLUGIN_ROOT}/tools/figma_attach.py" attach FILEKEY 123 a.jpg',
    'python "${CLAUDE_PLUGIN_ROOT}/tools/figma_attach.py" edit FILEKEY 123 --text-file t.txt',
    'python "${CLAUDE_PLUGIN_ROOT}/tools/figma_attach.py" mention FILEKEY 123',
    "zsh -lc 'python ~/tools/ado_write.py state 101 Resolved'",
    "python ado_write.py field 101 System.Title x",
    "python ado_write.py comment 101 hello",
    "python ado_write.py editcmt 101 9 hello",
    "python ado_write.py delcmt 101 9",
    "python ado_write.py parent 101 100",
    "python ado_write.py reparent 101 100",
    "python ado_write.py attach 101 a.png",
    "python ado_write.py batch b.json",
    "cd tools && python ado.py --rest new --type Task --title x",
    "python -c 'import figma_post; figma_post.new(1, 2, 3)'",
    "python -c 'from ado_write import state'",
]
# Reads and tests that mention the same tools and must not prompt.
READS = [
    "python tools/figma_post.py --help",
    "python tools/figma_post.py selftest",
    'python "${CLAUDE_PLUGIN_ROOT}/tools/figma_attach.py" selftest',
    "python ado_write.py --help",
    "python ado_write.py comments 101",
    "python ado_write.py --selftest",
    "grep -n figma_post drafts.md",
    "sed -n 1,40p tools/ado_write.py",
    "git -C tools diff figma_post.py",
    "python ado.py search hero",
]


def _parts(cmd):
    return [cmd] + [p.strip() for p in re.split(r"&&|\|\||;|\|", cmd) if p.strip()]


def matches(rule, cmd):
    m = re.fullmatch(r"Bash\((.*)\)", rule, re.S)
    if not m:
        return False
    rx = ".*".join(re.escape(s) for s in m.group(1).split("*"))
    return any(re.fullmatch(rx, p, re.S) for p in _parts(cmd))


def problems(ask):
    out = [f"a write does not prompt: {c}" for c in WRITES if not any(matches(r, c) for r in ask)]
    out += [f"a read prompts ({next(r for r in ask if matches(r, c))}): {c}"
            for c in READS if any(matches(r, c) for r in ask)]
    return out


def ask_of(path):
    with open(path, encoding="utf-8") as f:
        return ((json.load(f).get("permissions") or {}).get("ask")) or []


def settings_files(paths=None):
    """The paths given, else ask_rules.settings; NotConfigured (exit 2 through kit_config.run) when
    neither names one."""
    if not paths:
        import kit_config
        paths = kit_config.need("ask_rules", "settings")
        paths = [paths] if isinstance(paths, str) else list(paths)
    return [os.path.abspath(os.path.expanduser(p)) for p in paths]


def report(files):
    bad = 0
    for p in files:
        if not os.path.isfile(p):
            print(f"MISSING {p}")
            bad += 1
            continue
        ask = ask_of(p)
        found = problems(ask)
        print(f"{p}: {len(ask)} ask rule(s); {len(WRITES) - sum(f.startswith('a write') for f in found)} of "
              f"{len(WRITES)} writes prompt, {sum(f.startswith('a read') for f in found)} of {len(READS)} reads prompt")
        for f in found:
            print(f"  FAIL {f}")
        bad += len(found)
    return 1 if bad else 0


def selftest():
    good = ["Bash(*figma_post* new *)", "Bash(*figma_post* reply *)", "Bash(*figma_post* fixnote *)",
            "Bash(*figma_post* delete *)", "Bash(*figma_post* batch *)",
            "Bash(*figma_attach* attach *)", "Bash(*figma_attach* edit *)", "Bash(*figma_attach* mention *)",
            "Bash(*ado_write* state *)", "Bash(*ado_write* field *)", "Bash(*ado_write* comment *)",
            "Bash(*ado_write* editcmt *)", "Bash(*ado_write* delcmt *)", "Bash(*ado_write* parent *)",
            "Bash(*ado_write* reparent *)", "Bash(*ado_write* attach *)", "Bash(*ado_write* batch *)",
            "Bash(*ado.py*new*)",
            "Bash(*import figma_post*)", "Bash(*import figma_attach*)", "Bash(*import ado_write*)",
            "Bash(*from figma_post*)", "Bash(*from figma_attach*)", "Bash(*from ado_write*)"]
    broad = ["Bash(*figma_post*)", "Bash(*figma_attach*)", "Bash(*ado.py*new*)", "Bash(*ado_write*)"]
    cases = [
        ("the narrowed rules: every write prompts, no read does", good, 0),
        ("one broad rule per tool: reads prompt too", broad, None),
        ("a write subcommand dropped (ado_write state)", [r for r in good if "state" not in r], 1),
        ("the import rules dropped: python -c writes don't prompt", [r for r in good if "import" not in r and "from" not in r], 2),
        ("'comment' without its space after: 'comments' prompts", [r.replace("comment *", "comment*") for r in good], 1),
        ("no rules at all: every write fails", [], len(WRITES)),
    ]
    fails = 0
    for name, ask, want in cases:
        got = problems(ask)
        writes = sum(g.startswith("a write") for g in got)
        reads = sum(g.startswith("a read") for g in got)
        ok = (not got) if want == 0 else (reads > 0 and writes == 0) if want is None else len(got) == want
        print(f"  {'PASS' if ok else 'MISS'}  {name}: {writes} write(s) unprompted, {reads} read(s) prompted")
        fails += not ok
    # The files: a planted settings file read end to end, a missing one, and none configured.
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for name, rules in (("good.json", good), ("broad.json", broad)):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                json.dump({"permissions": {"ask": rules}}, f)
        env = dict(os.environ, HOME=os.path.join(tmp, "home"), PYTHONPATH=HERE)
        env.pop("AGENT_WORKBENCH_CONFIG", None)
        runs = [("a planted file whose rules hold: exit 0", ["good.json"], 0, ""),
                ("a planted file with broad rules: exit 1", ["broad.json"], 1, ""),
                ("a named file that is missing: exit 1", ["none.json"], 1, ""),
                ("no file named or configured: exit 2, naming the key", [], 2, "ask_rules.settings")]
        for name, args, want, says in runs:
            r = subprocess.run([sys.executable, os.path.abspath(__file__), *args], cwd=tmp, env=env,
                               capture_output=True, text=True)
            ok = r.returncode == want and says in r.stderr
            print(f"  {'PASS' if ok else 'MISS'}  {name} (got {r.returncode})")
            fails += not ok
        cases += runs
    print(f"{len(cases) - fails} of {len(cases)} planted cases behave as expected.")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="settings files (default: the config key ask_rules.settings)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    import kit_config
    return kit_config.run(lambda: report(settings_files(a.files)))


if __name__ == "__main__":
    sys.exit(main())
