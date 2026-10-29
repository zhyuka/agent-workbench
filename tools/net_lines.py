#!/usr/bin/env python3
"""net_lines.py - a repo grows only when a commit names what it retires.

    python tools/net_lines.py --commit-msg <message file>   the commit-msg hook: judge the change being committed
    python tools/net_lines.py --selftest                    planted commits in a scratch repo, through the real hook

WHY. A root fix that starts with a deletion keeps a tools repo from growing. A git hook rather than a
Claude Code hook, so it holds for every committer and where Claude Code hooks are turned off. It is the
commit-msg hook and not pre-commit because it reads the message. Install it as the repo's
.git/hooks/commit-msg (executable):
    #!/bin/sh
    exec python3 "<path to>/tools/net_lines.py" --commit-msg "$1"

THE RULE. Count the change being committed: lines added minus lines deleted over every text file
(`git diff --cached --numstat`; a binary file counts 0). Net 0 or less passes. More than 0 needs a trailer:
    Retires: <path>            that file loses lines in this commit, net, or is deleted
    Retires: <path>:<name>     the def or class <name> in that .py is shorter in this commit, or gone
Several go in one trailer, comma-separated, or in several trailers. Trailers are read as git reads them,
from the message's last paragraph, so put Retires beside Co-Authored-By. Every name must hold, so a
trailer is not a free pass. It does not ask the retired lines to cover the added ones.

WHAT IT JUDGES. `git diff --cached` reads GIT_INDEX_FILE, which git sets for its hooks, so a
`git commit --only <files>` is judged on those files alone. A merge passes (it adds nobody's new lines).
The fact files, config key net_lines.facts (kit_config.py: paths relative to the repo root; default
none), are left out both ways: they are records that grow as facts arrive, and a fact deleted cannot
pay for code.

LIMITS. An --amend is judged against the commit it amends, not that commit's parent. A def is found by its
first line and ends at the next line indented no deeper; a name defined twice uses the first.

Exit 0 pass, 1 refused, 2 the message could not be read. A git command that fails refuses: a check that
did not run is not a pass.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TRAILER = re.compile(r'^Retires:\s*(.+?)\s*$', re.I)


def facts():
    """The config key net_lines.facts: files left out of the count. Empty when unset."""
    try:
        import kit_config
        v = kit_config.get('net_lines', 'facts', default=[]) or []
    except ImportError:
        return ()
    return tuple([v] if isinstance(v, str) else v)


def git(*args, check=True):
    r = subprocess.run(['git', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r


def numstat():
    """{path: (added, deleted)} for the change being committed; binary files count (0, 0)."""
    out = {}
    for line in git('diff', '--cached', '--numstat', '--no-renames').stdout.splitlines():
        a, d, path = line.split('\t', 2)
        out[path] = (int(a) if a != '-' else 0, int(d) if d != '-' else 0)
    return out


def names_in(message):
    """The names in Retires trailers, read as git reads trailers: the message's last paragraph. A body
    line that happens to start "Retires:" is prose."""
    text = '\n'.join(line for line in message.splitlines() if not line.startswith('#'))
    trailers = subprocess.run(['git', 'interpret-trailers', '--parse'], input=text, capture_output=True,
                              text=True, encoding='utf-8', errors='replace')
    if trailers.returncode:
        raise RuntimeError(f"git interpret-trailers: {trailers.stderr.strip()}")
    names = []
    for line in trailers.stdout.splitlines():
        m = TRAILER.match(line)
        if m:
            names += [n.strip() for n in m.group(1).split(',') if n.strip()]
    return names


def def_length(text, name):
    """Lines in the first def or class `name` in Python source `text`, or 0 when there is none."""
    lines = text.splitlines()
    head = re.compile(r'^(\s*)(?:async\s+)?(?:def|class)\s+' + re.escape(name) + r'\b')
    for i, line in enumerate(lines):
        m = head.match(line)
        if not m:
            continue
        indent, n = len(m.group(1)), 1
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            n += 1
        while n > 1 and not lines[i + n - 1].strip():
            n -= 1
        return n
    return 0


def blob(rev_path):
    r = git('show', rev_path, check=False)
    return r.stdout if r.returncode == 0 else ''


def retired(name, stat):
    """'' when `name` lost lines in this change, else why it did not."""
    path, _, func = name.partition(':')
    if not func:
        if path not in stat:
            return f'{path} is not in this commit'
        a, d = stat[path]
        return '' if d > a else f'{path} gains {a - d} net line(s) here ({a} added, {d} deleted)'
    if not path.endswith('.py'):
        return f'{name}: a name inside a file works for a .py only; name the file'
    before = def_length(blob(f'HEAD:{path}'), func)
    if not before:
        return f'{func} is not in HEAD:{path}'
    after = def_length(blob(f':{path}'), func)
    return '' if after < before else f'{func} in {path} is {before} line(s) before and {after} after'


def judge(message):
    """(exit code, lines to print) for the change being committed and its message."""
    if git('rev-parse', '-q', '--verify', 'HEAD', check=False).returncode:
        return 0, []
    if git('rev-parse', '-q', '--verify', 'MERGE_HEAD', check=False).returncode == 0:
        return 0, []
    skip = facts()
    stat = {p: n for p, n in numstat().items() if p not in skip}
    added, deleted = sum(a for a, _ in stat.values()), sum(d for _, d in stat.values())
    net = added - deleted
    if net <= 0:
        return 0, []
    names = names_in(message)
    head = f"net-lines: REFUSED. This commit adds {net} net line(s) to this repo ({added} added, {deleted} deleted)"
    tail = ["Name what this change retires; it must lose lines in this same commit:",
            "  Retires: <path>    or    Retires: <path>:<def or class>",
            "The rule and its limits: net_lines.py --help.",
            "--no-verify only when the user says so."]
    if not names:
        return 1, [head + " and names nothing it retires.", *tail]
    bad = [(n, why) for n in names if (why := retired(n, stat))]
    if not bad:
        return 0, [f"net-lines: +{net} net, retiring {', '.join(names)}."]
    return 1, [head + ", and a name in its Retires trailer lost no lines:",
               *[f"  {n}: {why}" for n, why in bad], *tail]


def commit_msg(path):
    try:
        message = open(path, encoding='utf-8', errors='replace').read()
    except OSError as e:
        print(f"net-lines: could not read the message ({e})")
        return 2
    try:
        code, lines = judge(message)
    except (RuntimeError, ValueError) as e:
        code, lines = 1, [f"net-lines: DID NOT RUN, so the commit is refused: {e}"]
    for line in lines:
        print(line)
    return code


def write(path, text):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def selftest():
    """Real commits in a scratch repo whose commit-msg hook runs this file."""
    me = os.path.abspath(__file__)
    base_py = ("def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(6)) + "\n\n"
               "def g():\n    return 1\n")
    cases = []   # (name, expect 0 or 1, setup(repo), commit args, message)

    def edit(rel, text):
        return lambda repo: write(os.path.join(repo, rel), text)

    def steps(*fs):
        return lambda repo: [f(repo) for f in fs]

    grow = edit('new.py', "".join(f"y{i} = {i}\n" for i in range(10)))
    fact = 'notes/facts.md'
    facts_grow = edit(fact, "".join(f"- fact {i}\n" for i in range(10)))
    shrink_file = edit('b.md', "one\n")
    shrink_f = edit('a.py', base_py.replace("".join(f"    x{i} = {i}\n" for i in range(1, 6)), ""))
    cases += [
        ("net add, no trailer, refuses", 1, steps(grow), [], "Add new.py"),
        ("net add, true file trailer, passes", 0, steps(grow, shrink_file), [], "Add new.py\n\nRetires: b.md"),
        ("net add, false file trailer, refuses", 1, steps(grow), [], "Add new.py\n\nRetires: b.md"),
        ("net add, true def trailer, passes", 0, steps(grow, shrink_f), [], "Add new.py\n\nRetires: a.py:f"),
        ("net add, false def trailer, refuses", 1, steps(grow, shrink_f), [], "Add new.py\n\nRetires: a.py:g"),
        ("one true and one false name, refuses", 1, steps(grow, shrink_f), [], "Add\n\nRetires: a.py:f, b.md"),
        ("a commented-out trailer is not one, refuses", 1, steps(grow, shrink_file), [], "Add\n\n# Retires: b.md"),
        ("a body line starting Retires: is not a trailer, refuses", 1, steps(grow, shrink_file), [],
         "Add new.py\n\nIt is refused unless a\nRetires: b.md line names it.\n\nCo-Authored-By: t <t@t>"),
        ("a trailer beside Co-Authored-By, passes", 0, steps(grow, shrink_file), [],
         "Add new.py\n\nBody.\n\nRetires: b.md\nCo-Authored-By: t <t@t>"),
        ("net zero, passes", 0, edit('b.md', "one\ntwo\nTHREE\n"), [], "Edit b.md"),
        ("a configured fact file grows alone, passes", 0, facts_grow, [], "Add facts"),
        ("README.md grows alone, refuses", 1, edit('README.md', "".join(f"r{i}\n" for i in range(10))), [], "Add"),
        ("facts beside a code addition, refuses", 1, steps(facts_grow, grow), [], "Add facts and new.py"),
        ("a fact deleted cannot retire code, refuses", 1,
         steps(grow, edit(fact, "")), [], f"Add\n\nRetires: {fact}"),
        ("net negative, passes", 0, shrink_file, [], "Trim b.md"),
        ("a deleted file retires, passes", 0,
         steps(grow, lambda repo: os.remove(os.path.join(repo, 'b.md'))), [], "Swap\n\nRetires: b.md"),
        ("--only judges its own files: a net-negative file passes beside a staged big one", 0,
         steps(grow, shrink_file, lambda repo: git_in(repo, 'add', 'new.py')), ['--only', 'b.md'], "Trim b.md"),
        ("--only judges its own files: a net-adding file refuses beside a staged deletion", 1,
         steps(grow, shrink_file, lambda repo: git_in(repo, 'add', 'b.md', 'new.py')), ['--only', 'new.py'],
         "Add new.py"),
    ]
    scratch = tempfile.mkdtemp()
    cfg = os.path.join(scratch, 'config.json')
    with open(cfg, 'w', encoding='utf-8') as f:
        json.dump({'net_lines': {'facts': [fact]}}, f)

    def git_in(repo, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
                   GIT_COMMITTER_EMAIL='t@t', AGENT_WORKBENCH_CONFIG=cfg)
        env.pop('GIT_INDEX_FILE', None)
        return subprocess.run(['git', '-C', repo, '-c', 'core.hooksPath=.git/hooks', '-c', 'commit.gpgsign=false',
                               *args], capture_output=True, text=True, env=env)

    passed = 0
    for name, expect, setup, extra, message in cases:
        with tempfile.TemporaryDirectory() as repo:
            git_in(repo, 'init', '-q')
            write(os.path.join(repo, 'a.py'), base_py)
            write(os.path.join(repo, 'b.md'), "one\ntwo\nthree\n")
            write(os.path.join(repo, fact), "- old 1\n- old 2\n")
            git_in(repo, 'add', '-A')
            git_in(repo, 'commit', '-q', '-m', 'base')
            hook = os.path.join(repo, '.git', 'hooks', 'commit-msg')
            write(hook, f'#!/bin/sh\nexec "{sys.executable}" "{me}" --commit-msg "$1"\n')
            os.chmod(hook, 0o755)
            setup(repo)
            if not extra:
                git_in(repo, 'add', '-A')
            r = git_in(repo, 'commit', '-q', *extra, '-m', message)
            got = 0 if r.returncode == 0 else 1
            # A refusal counts only as the rule's own: a crash also exits 1, as DID NOT RUN.
            ok = got == expect and (expect == 0 or 'net-lines: REFUSED' in r.stdout + r.stderr)
            passed += ok
            print(f"  {'PASS' if ok else 'FAIL'}  {name}  (commit exit {r.returncode})")
            if not ok:
                print('        ' + (r.stdout + r.stderr).strip().replace('\n', '\n        '))
    os.remove(cfg)
    os.rmdir(scratch)
    print(f"net_lines selftest: {passed} of {len(cases)} {'PASSED' if passed == len(cases) else 'FAILED'}")
    return 0 if passed == len(cases) else 1


if __name__ == '__main__':
    if sys.argv[1:2] == ['--commit-msg'] and len(sys.argv) > 2:
        sys.exit(commit_msg(sys.argv[2]))
    if sys.argv[1:2] == ['--selftest']:
        sys.exit(selftest())
    if sys.argv[1:2] in (['-h'], ['--help']):
        print(__doc__)
        sys.exit(0)
    print(__doc__.split('\n\n')[1])
    sys.exit(2)
