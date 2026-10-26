#!/usr/bin/env python3
"""agent_gate_stale.py - list each gated agent or skill whose last passing eval no longer matches what runs.

    python tools/agent_gate_stale.py [--suites DIR] [--project DIR]   one line per unit; exit 1 when any needs a re-run
    python tools/agent_gate_stale.py --selftest                       planted units: current, edited, model moved, none

WHY. A unit (an agent or a skill) is gated once by its eval suite. The result goes stale when the
unit's file changes or the model behind it moves. Agent files often say `model: opus`, an alias, so a
model rollout changes what runs with no file edit. Nothing re-runs a suite after either unless
something checks. Run this daily (from a scheduler of your own), so a stale unit shows without anyone
remembering.

WHERE THE SUITES ARE. --suites DIR, else the config key agent_gate.suites (kit_config.py). With
neither, it stops with exit 2 and names the key. Each suite is one folder under it, holding:
  unit.json       {"unit": "cold-reader", "file": "~/.claude/agents/cold-reader.md", "run": "<command>"}
  receipts.jsonl  one line per gate run: {"date", "unit_sha", "model", "pass", "summary", "failed": [...]}
The suite itself (whatever "run" names, e.g. a `claude plugin eval` suite) writes the receipts; this
tool only reads them. The unit's sha is sha1 of its file, first 7 hex digits; a skill is a folder, and
every file in it counts (see file_sha), so a suite runner must stamp receipts with the same hash.

THE LIVE MODEL. A full model id in the unit's frontmatter is the answer. Otherwise the model of that
agent type's newest launch in the project's session logs (--project, default the current project's
folder under ~/.claude/projects), else the most common id of its family across the newest main logs.

It reads only; it runs no suite and spends nothing. Running a stale suite is a separate step.
Exit 0 when every unit has a passing receipt for its current file and the live model; 1 when any
doesn't, or a skill no suite names exists; 2 when it can't tell (no suites folder set, no session
log, an unreadable suite).
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def default_logs(project=None):
    """The project's session-log folder: --project, else cc_logs.project_dir() for the current folder."""
    if project:
        return os.path.abspath(os.path.expanduser(project))
    import cc_logs
    return cc_logs.project_dir()


def default_skills():
    """Where a skill loads from in a session here: your skills, and the current project's own. A repo's
    .claude/skills loads only when work enters that repo, which is run from there."""
    root = os.environ.get('CLAUDE_PROJECT_DIR') or os.getcwd()
    return (os.path.join(os.path.expanduser('~'), '.claude', 'skills'), os.path.join(root, '.claude', 'skills'))


def file_sha(path):
    """sha1, first 7 hex digits, of a unit's file. A skill is a folder: every file in it counts, by
    its path inside the folder, so an edit to a reference file stales the receipt as an edit to
    SKILL.md does."""
    path = os.path.expanduser(path)
    if not os.path.isdir(path):
        with open(path, 'rb') as f:
            return hashlib.sha1(f.read()).hexdigest()[:7]
    h = hashlib.sha1()
    texts = []

    def add(folder, prefix=''):
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if d not in ('__pycache__', '.git'))
            for n in sorted(f for f in files if f != '.DS_Store'):
                p = os.path.join(root, n)
                h.update((prefix + os.path.relpath(p, folder)).encode('utf-8') + b'\0')
                with open(p, 'rb') as f:
                    data = f.read()
                h.update(data + b'\0')
                texts.append(data.decode('utf-8', 'replace'))
    add(path)
    # A shared folder beside the skills (one with no SKILL.md of its own) counts for every skill whose
    # files name it: those skills read it, so an edit there changes them.
    parent = os.path.dirname(path.rstrip('/'))
    for shared in sorted(os.listdir(parent)):
        sp = os.path.join(parent, shared)
        if (os.path.isdir(sp) and sp.rstrip('/') != path.rstrip('/') and shared not in ('synced', '.git')
                and not os.path.exists(os.path.join(sp, 'SKILL.md')) and any(shared + '/' in t for t in texts)):
            add(sp, shared + '/')
    return h.hexdigest()[:7]


def models_in(path, tail_bytes=2_000_000):
    """Counter of the model ids assistant messages used in one log (its last tail_bytes)."""
    seen = collections.Counter()
    with open(path, 'rb') as f:
        f.seek(max(0, os.path.getsize(path) - tail_bytes))
        lines = f.read().decode('utf-8', 'replace').splitlines()[1:]
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        m = (o.get('message') or {}).get('model') if o.get('type') == 'assistant' else None
        if m and m.startswith('claude-'):
            seen[m] += 1
    return seen


def frontmatter_model(path):
    path = os.path.expanduser(path)
    if os.path.isdir(path):     # a skill: it runs on the main session's model unless it names one
        path = os.path.join(path, 'SKILL.md')
    text = open(path, encoding='utf-8').read()
    m = re.search(r'^model:\s*(\S+)', text.split('\n---', 1)[0], re.M)
    return m.group(1) if m else 'inherit'


def live_model(unit=None, alias='opus', logs=None, sessions=60, mains=30):
    """The model id a unit runs on today, or None.

    A full id in the unit's frontmatter is the answer. Otherwise: the model of the newest launch of
    that agent type (subagents/*.meta.json names the type, its .jsonl the model). With no launch
    on record, the most common id of that family (opus, sonnet, haiku) across the newest main logs.
    One log is never enough: a single session run on another model would read as a rollout."""
    if alias.startswith('claude-'):
        return alias
    logs = logs or default_logs()
    dirs = sorted((d for d in glob.glob(os.path.join(logs, '*', 'subagents')) if os.path.isdir(d)),
                  key=os.path.getmtime, reverse=True)[:sessions]
    if unit:
        launches = []
        for d in dirs:
            for meta in glob.glob(os.path.join(d, '*.meta.json')):
                try:
                    if json.load(open(meta)).get('agentType') == unit:
                        launches.append(meta)
                except (OSError, ValueError):
                    continue
        for meta in sorted(launches, key=os.path.getmtime, reverse=True)[:3]:
            log = meta[:-len('.meta.json')] + '.jsonl'
            seen = models_in(log) if os.path.exists(log) else None
            if seen:
                return seen.most_common(1)[0][0]
    family = alias if alias in ('opus', 'sonnet', 'haiku') else ''
    seen = collections.Counter()
    for path in sorted(glob.glob(os.path.join(logs, '*.jsonl')), key=os.path.getmtime, reverse=True)[:mains]:
        seen.update({m: n for m, n in models_in(path, 500_000).items() if family in m})
    return seen.most_common(1)[0][0] if seen else None


def check(suites, model_of=None, skills=None, logs=None):
    """[(unit, status, detail)]; status is current, stale, failed, untested or unknown.

    Each (file sha, model) pair is judged by its own receipts: a pass is CURRENT; a fail is FAILED,
    which a person reads, and nothing re-runs; no receipt is STALE, the only state a re-run clears.
    So one change costs at most one run, however many days it stays red. A skill folder that no
    unit names is UNTESTED: a skill is live the moment its folder exists, commit or not, so a commit
    hook alone can't keep an untested one out. `skills` is one folder or several; a skill outside the
    first is named with its folder."""
    model_of = model_of or (lambda unit: live_model(unit['unit'], frontmatter_model(unit['file']), logs=logs))
    skills = default_skills() if skills is None else skills
    rows = []
    covered = set()
    for unit_json in glob.glob(os.path.join(suites, '*', 'unit.json')):
        try:
            covered.add(os.path.realpath(os.path.expanduser(json.load(open(unit_json, encoding='utf-8'))['file'])))
        except (OSError, ValueError, KeyError):
            pass
    roots = (skills,) if isinstance(skills, str) else tuple(skills)
    for i, root in enumerate(roots):
        for skill_md in sorted(glob.glob(os.path.join(root, '*', 'SKILL.md'))):
            folder = os.path.dirname(skill_md)
            if os.path.realpath(folder) not in covered:
                where = '' if i == 0 else f' ({root.replace(os.path.expanduser("~"), "~")})'
                rows.append((os.path.basename(folder), 'untested', 'no suite names this skill folder' + where))
    for unit_json in sorted(glob.glob(os.path.join(suites, '*', 'unit.json'))):
        folder = os.path.dirname(unit_json)
        try:
            unit = json.load(open(unit_json, encoding='utf-8'))
            sha = file_sha(unit['file'])
            model = model_of(unit)
        except (OSError, ValueError, KeyError) as e:
            rows.append((os.path.basename(folder), 'unknown', f'unreadable: {e}'))
            continue
        name, run = unit['unit'], unit.get('run', '?')
        if not model:
            rows.append((name, 'unknown', 'no session log names the model it runs on'))
            continue
        receipts = []
        path = os.path.join(folder, 'receipts.jsonl')
        if os.path.exists(path):
            receipts = [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]
        pair = [r for r in receipts if r.get('unit_sha') == sha and r.get('model') == model]
        if any(r.get('pass') for r in pair):
            r = [r for r in pair if r.get('pass')][-1]
            rows.append((name, 'current', f'{sha} passed on {r.get("date")} with {model}: {r.get("summary", "")}'))
        elif pair:
            r = pair[-1]
            rows.append((name, 'failed', f'{sha} on {model} failed on {r.get("date")} ({", ".join(r.get("failed", []))}); '
                                         f'a person decides; nothing re-runs it'))
        else:
            last = receipts[-1] if receipts else {}
            was = f'last receipt {last.get("unit_sha")} on {last.get("model")}' if last else 'no receipt'
            rows.append((name, 'stale', f'{sha} on {model} has never run ({was}); run: {run}'))
    return rows


def report(rows):
    print(f'{len(rows)} gated unit(s)')
    for unit, status, detail in rows:
        print(f'{status.upper():8} {unit}: {detail}')
    if any(s == 'unknown' for _, s, _ in rows):
        return 2
    return 1 if any(s in ('stale', 'failed', 'untested') for _, s, _ in rows) else 0


def suites_folder(arg=None):
    """--suites, else the config key agent_gate.suites; NotConfigured (exit 2 through kit_config.run)
    when neither is set."""
    if arg:
        return os.path.abspath(os.path.expanduser(arg))
    import kit_config
    return os.path.abspath(os.path.expanduser(kit_config.need('agent_gate', 'suites')))


def selftest():
    """Plant one unit per state and check each is read right, both ways."""
    with tempfile.TemporaryDirectory() as tmp:
        def unit(name, body, receipts):
            folder = os.path.join(tmp, 'suites', name)
            os.makedirs(folder)
            agent = os.path.join(tmp, name + '.md')
            open(agent, 'w', encoding='utf-8').write(body)
            json.dump({'unit': name, 'file': agent, 'run': 'x'}, open(os.path.join(folder, 'unit.json'), 'w'))
            with open(os.path.join(folder, 'receipts.jsonl'), 'w') as f:
                for r in receipts:
                    f.write(json.dumps(r) + '\n')
            return hashlib.sha1(body.encode()).hexdigest()[:7]

        good = unit('good', 'A', [])
        with open(os.path.join(tmp, 'suites', 'good', 'receipts.jsonl'), 'w') as f:
            f.write(json.dumps({'unit_sha': good, 'model': 'm1', 'pass': True, 'date': 'd'}) + '\n')
        unit('edited', 'B', [{'unit_sha': 'old', 'model': 'm1', 'pass': True, 'date': 'd'}])
        moved = unit('moved', 'C', [])
        with open(os.path.join(tmp, 'suites', 'moved', 'receipts.jsonl'), 'w') as f:
            f.write(json.dumps({'unit_sha': moved, 'model': 'm0', 'pass': True, 'date': 'd'}) + '\n')
        failed = unit('failed', 'D', [])
        with open(os.path.join(tmp, 'suites', 'failed', 'receipts.jsonl'), 'w') as f:
            f.write(json.dumps({'unit_sha': failed, 'model': 'm1', 'pass': False, 'date': 'd'}) + '\n')
        unit('none', 'E', [])
        # A skill is a folder: its receipt is for the whole folder, and an edit to a reference stales it.
        # A shared folder it names (no SKILL.md of its own) counts too. A skill no suite names is UNTESTED.
        skills = os.path.join(tmp, 'skills')
        skill = os.path.join(skills, 'skill-x')
        os.makedirs(os.path.join(skill, 'references'))
        os.makedirs(os.path.join(skills, 'shared-refs'))
        os.makedirs(os.path.join(skills, 'skill-y'))
        open(os.path.join(skill, 'SKILL.md'), 'w').write('---\nname: x\n---\nRead shared-refs/s.md first.\n')
        open(os.path.join(skill, 'references', 'r.md'), 'w').write('one')
        open(os.path.join(skills, 'shared-refs', 's.md'), 'w').write('one')
        open(os.path.join(skills, 'skill-y', 'SKILL.md'), 'w').write('---\nname: y\n---\nno suite\n')
        for name in ('skill-kept', 'skill-edited'):
            os.makedirs(os.path.join(tmp, 'suites', name))
            json.dump({'unit': name, 'file': skill, 'run': 'x'}, open(os.path.join(tmp, 'suites', name, 'unit.json'), 'w'))
            with open(os.path.join(tmp, 'suites', name, 'receipts.jsonl'), 'w') as f:
                f.write(json.dumps({'unit_sha': file_sha(skill), 'model': 'm1', 'pass': True, 'date': 'd'}) + '\n')
        run = lambda: {u: s for u, s, _ in check(os.path.join(tmp, 'suites'), model_of=lambda unit: 'm1', skills=skills)}
        got1 = run()
        y_before = file_sha(os.path.join(skills, 'skill-y'))
        open(os.path.join(skills, 'shared-refs', 's.md'), 'w').write('two')
        got_shared = run()
        y_after = file_sha(os.path.join(skills, 'skill-y'))
        open(os.path.join(skills, 'shared-refs', 's.md'), 'w').write('one')
        open(os.path.join(skill, 'references', 'r.md'), 'w').write('two')
        got2 = run()
        got = {k: v for k, v in got1.items() if not k.startswith('skill-')}
        want = {'good': 'current', 'edited': 'stale', 'moved': 'stale', 'failed': 'failed', 'none': 'stale'}
        ok = got == want
        print(('PASS' if ok else 'FAIL') + f' states: got {got}')
        sk_ok = got1.get('skill-kept') == 'current' and got2.get('skill-edited') == 'stale'
        print(('PASS' if sk_ok else 'FAIL') + f' a skill folder: current {got1.get("skill-kept")}, '
              f'stale after a reference edit {got2.get("skill-edited")}')
        sh_ok = got_shared.get('skill-kept') == 'stale' and y_before == y_after
        print(('PASS' if sh_ok else 'FAIL') + f' a shared folder it names: stale after an edit there '
              f'{got_shared.get("skill-kept")}; a skill that does not name it unchanged {y_before == y_after}')
        un_ok = got1.get('skill-y') == 'untested' and 'skill-x' not in got1 and 'shared-refs' not in got1
        print(('PASS' if un_ok else 'FAIL') + f' a skill no suite names is untested: {got1.get("skill-y")}; '
              f'a covered skill and a shared folder are not listed')
        # A project skill (the project's .claude/skills) is listed too; reading your folder alone misses it.
        proj = os.path.join(tmp, 'project', '.claude', 'skills')
        os.makedirs(os.path.join(proj, 'skill-p'))
        open(os.path.join(proj, 'skill-p', 'SKILL.md'), 'w').write('---\nname: p\n---\nproject skill\n')
        both = {u: (s, d) for u, s, d in check(os.path.join(tmp, 'suites'), model_of=lambda unit: 'm1', skills=(skills, proj))}
        one = {u for u, _, _ in check(os.path.join(tmp, 'suites'), model_of=lambda unit: 'm1', skills=skills)}
        pr_ok = both.get('skill-p', ('',))[0] == 'untested' and '/project/.claude/skills' in both['skill-p'][1] and 'skill-p' not in one
        print(('PASS' if pr_ok else 'FAIL') + f' a project skill is untested: {both.get("skill-p", ("missing",))[0]}; '
              f'your folder alone misses it: {"skill-p" not in one}')
        sk_ok &= frontmatter_model(skill) == 'inherit' and sh_ok and un_ok and pr_ok
        ok &= sk_ok

        # The model a unit runs on: its own newest launch first; else the family's most common id
        # across main logs, so one session on another model doesn't read as a rollout; a full id is the answer.
        logs = os.path.join(tmp, 'logs')
        sub = os.path.join(logs, 'sess1', 'subagents')
        os.makedirs(sub)

        def log(path, models):
            with open(path, 'w') as f:
                f.write('{"first line may be cut"}\n')
                for m in models:
                    f.write(json.dumps({'type': 'assistant', 'message': {'model': m}}) + '\n')
        log(os.path.join(logs, 'a.jsonl'), ['claude-opus-9', 'claude-opus-9'])
        log(os.path.join(logs, 'b.jsonl'), ['claude-opus-9', '<synthetic>'])
        log(os.path.join(logs, 'c.jsonl'), ['claude-haiku-1', 'claude-haiku-1', 'claude-haiku-1'])
        json.dump({'agentType': 'reader'}, open(os.path.join(sub, 'agent-x.meta.json'), 'w'))
        log(os.path.join(sub, 'agent-x.jsonl'), ['claude-opus-10'])
        cases = [(('reader', 'opus'), 'claude-opus-10'), (('nobody', 'opus'), 'claude-opus-9'),
                 (('nobody', 'claude-opus-7'), 'claude-opus-7'), (('nobody', 'haiku'), 'claude-haiku-1')]
        lm_ok = True
        for (u, alias), want_m in cases:
            got_m = live_model(u, alias, logs=logs)
            lm_ok &= got_m == want_m
            print(('PASS' if got_m == want_m else 'FAIL') + f' live model {u}/{alias}: got {got_m}, want {want_m}')

        # No suites folder set anywhere: exit 2 naming the key, never a clean 0.
        import subprocess
        env = dict(os.environ, HOME=os.path.join(tmp, 'home'), PYTHONPATH=HERE + os.pathsep + os.environ.get('PYTHONPATH', ''))
        for k in ('AGENT_WORKBENCH_CONFIG', 'BUILD_KIT_CONFIG'):
            env.pop(k, None)
        bare = os.path.join(tmp, 'bare')
        os.makedirs(bare)
        r = subprocess.run([sys.executable, os.path.abspath(__file__)], cwd=bare, env=env, capture_output=True, text=True)
        nc_ok = r.returncode == 2 and 'agent_gate.suites' in r.stderr
        print(('PASS' if nc_ok else 'FAIL') + f' no suites folder configured: exit {r.returncode}, names the key: '
              f'{"agent_gate.suites" in r.stderr}')
        return 0 if ok and lm_ok and nc_ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--suites', help='the suites folder (default: the config key agent_gate.suites)')
    ap.add_argument('--project', help="the project's session-log folder under ~/.claude/projects (default: the current project's)")
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    import kit_config
    return kit_config.run(lambda: report(check(suites_folder(a.suites), logs=default_logs(a.project))))


if __name__ == '__main__':
    sys.exit(main())
