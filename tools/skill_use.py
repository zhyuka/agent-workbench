#!/usr/bin/env python3
"""skill_use.py - did the skill that owns a risky step load before the step, session by session.

    python tools/skill_use.py [--days 14] [-v] [--project DIR]   N of M sessions per skill; -v names the misses
    python tools/skill_use.py --selftest                         planted sessions, each rule broken once

WHY. A skill only helps if it is loaded before the step it governs. A synthetic trigger test measures
the first tool call after a prompt, while these skills load partway through a task, so it says little.
The logs already say what happened. Run it daily, so drift after a description edit or a model upgrade
shows the next morning.

WHAT COUNTS.
  step     figma-writes  use_figma code that creates, removes, moves or sets a node property; or
                         figma_post.py or figma_attach.py run with a write subcommand
           ado           ado.py new, ado_write.py with a write subcommand, `az boards ... create|
                         update|delete`, or an azure-devops MCP *_write tool
           (yours)       any rule in the config key skill_use.rules (kit_config.py):
                           {"<skill>": {"bash": ["<regex over a Bash command>"],
                                        "tool": ["<regex over a tool name>"],
                                        "commit_in": ["<regex over the folder a git commit runs in>"]}}
                         e.g. {"humanize": {"commit_in": ["my-site"]}}: a `git commit` run in a folder
                         matching my-site is a step that needs the humanize skill loaded first
           A command that only reads a tool (sed, grep, --help, --check, --dry-run) is no step.
  loaded   a Skill call for it (a plugin or repo prefix counts, and so does an old name listed in the
           config key skill_use.aliases, {"<old name>": "<skill>"}), a Read of its SKILL.md, or its
           slash command, anywhere earlier in the session
A session counts once per skill: covered when the skill loaded before its first step there.

READS. Main session logs on disk in one project's folder under ~/.claude/projects (--project, default
the current project's), written in the window, through cc_logs.Inventory, after seals.py holds back any
log a .recall-sealed marker names. It prints session ids, dates and counts, never log text.
BLIND TO. Steps run by a subagent (its own log); work done to a skill's rules but without the skill;
use_figma code that writes through a helper the pattern doesn't name; logs only in backups (older than
any window). Exit 0 no miss, 1 a miss, 2 no log read.
"""
import argparse
import collections
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FIGMA_WRITE = re.compile(r'\b(create[A-Z]\w*\(|appendChild|insertChild|\.remove\(\)|setReactionsAsync|swapComponent'
                         r'|setProperties|resize(WithoutConstraints)?\(|\.(characters|fills|strokes|name|x|y|'
                         r'visible|opacity|effects|layoutMode|cornerRadius)\s*=(?!=))')
# A tool counts when it runs a write subcommand, not when a command reads it (sed, grep, --help).
_NOT_HELP = r'(?![^\n;&|]*(?:--help|\s-h\b|--check|--dry-run))'
FIGMA_BASH = re.compile(r'\bpython3?\s+\S*figma_post\.py\s+(?:reply|new|fixnote|delete|batch)\b' + _NOT_HELP
                        + r'|\bpython3?\s+\S*figma_attach\.py\s+(?:attach|edit|mention)\b' + _NOT_HELP)
ADO_BASH = re.compile(r'\bpython3?\s+\S*ado\.py(?:\s+--rest)?\s+new\b' + _NOT_HELP
                      + r'|\bpython3?\s+\S*ado_write\.py\s+(?:state|field|comment|parent|reparent|editcmt|delcmt|'
                        r'attach|batch)\b' + _NOT_HELP
                      + r'|\baz\s+boards\s+(?:\S+\s+){0,2}(?:create|update|delete)\b')
# git's own subcommand, after any -C/-c options; the folder is -C's, else the last `cd` before it.
COMMIT = re.compile(r'\bgit((?:\s+(?:-C\s+\S+|-c\s+\S+|--no-pager))*)\s+commit\b')
CD = re.compile(r'\bcd\s+("[^"]+"|\'[^\']+\'|[^\s;&|)]+)')
SKILL_READ = re.compile(r'/skills/([\w-]+)/SKILL\.md$')
SLASH = re.compile(r'<command-name>/?(?:[\w-]+:)?([\w-]+)</command-name>')
BUILTIN = ('figma-writes', 'ado')

RULES, ALIASES = {}, {}     # set by main() from the config, by selftest() from planted values


def compile_rules(raw):
    """{skill: {"bash": [regex], "tool": [regex], "commit_in": [regex]}}, each compiled."""
    out = {}
    for skill, r in (raw if isinstance(raw, dict) else {}).items():
        r = r if isinstance(r, dict) else {}
        out[skill] = {k: [re.compile(x) for x in (r.get(k) or []) if isinstance(x, str)]
                      for k in ('bash', 'tool', 'commit_in')}
    return out


def load_config():
    """(rules, aliases) from the config keys skill_use.rules and skill_use.aliases; empty when unset."""
    try:
        import kit_config
        raw, aliases = kit_config.get('skill_use', 'rules') or {}, kit_config.get('skill_use', 'aliases') or {}
    except ImportError:
        raw, aliases = {}, {}
    return compile_rules(raw), {str(k): str(v) for k, v in (aliases if isinstance(aliases, dict) else {}).items()}


def commits_in(cmd, cwd, folders):
    """True when a command runs `git commit` in a folder one of the regexes matches."""
    for m in COMMIT.finditer(cmd):
        c = re.search(r'-C\s+(\S+)', m.group(1) or '')
        cds = [d for d in CD.finditer(cmd[:m.start()])]
        where = c.group(1) if c else (cds[-1].group(1) if cds else cwd or '')
        if any(f.search(where) for f in folders):
            return True
    return False


def norm(name):
    name = (name or '').split(':')[-1].split('/')[-1]
    return ALIASES.get(name, name)


def steps_and_loads(rec):
    """([skill whose step this record holds], [skill this record loads]) for one log record."""
    steps, loads = [], []
    msg = rec.get('message') or {}
    content = msg.get('content')
    if rec.get('type') == 'user':
        texts = [content] if isinstance(content, str) else [b.get('text', '') for b in content or []
                                                             if isinstance(b, dict) and b.get('type') == 'text']
        for t in texts:
            loads += [norm(m) for m in SLASH.findall(t or '')]
        return steps, loads
    if rec.get('type') != 'assistant' or not isinstance(content, list):
        return steps, loads
    for b in content:
        if not isinstance(b, dict) or b.get('type') != 'tool_use':
            continue
        name, inp = b.get('name') or '', b.get('input') or {}
        cmd = (inp.get('command') or '') if name == 'Bash' else ''
        if name == 'Skill':
            loads.append(norm(inp.get('skill')))
        elif name == 'Read':
            m = SKILL_READ.search(inp.get('file_path') or '')
            if m:
                loads.append(norm(m.group(1)))
        elif name.endswith('__use_figma'):
            if FIGMA_WRITE.search(inp.get('code') or ''):
                steps.append('figma-writes')
        elif 'azure-devops__' in name and name.endswith('_write'):
            steps.append('ado')
        elif name == 'Bash':
            if FIGMA_BASH.search(cmd):
                steps.append('figma-writes')
            if ADO_BASH.search(cmd):
                steps.append('ado')
        for skill, r in RULES.items():
            if any(x.search(name) for x in r['tool']) or (name == 'Bash' and (
                    any(x.search(cmd) for x in r['bash'])
                    or (r['commit_in'] and commits_in(cmd, rec.get('cwd'), r['commit_in'])))):
                steps.append(skill)
    return steps, loads


def judge(records):
    """{skill: covered?} for one session's records in time order."""
    loaded, out = set(), {}
    for rec in records:
        steps, loads = steps_and_loads(rec)
        for s in steps:          # a load in the same record comes after its own tool calls are written
            out.setdefault(s, s in loaded)
        loaded.update(loads)
    return out


def sessions(days, now=None, inv=None, project=None):
    """{session id: (date, [records in time order])} for one project's main logs written in the window.
    project: its folder under ~/.claude/projects (default: the current project's)."""
    import cc_logs
    import seals as sealing
    from log_extract import seal_dirs
    now = now or datetime.datetime.now().timestamp()
    inv = inv if inv is not None else cc_logs.Inventory()
    folder = os.path.abspath(os.path.expanduser(project or cc_logs.project_dir()))
    copies = [c for c in inv.to_read() if c.kind == 'main' and c.via == 'disk'
              and os.path.dirname(os.path.abspath(c.path)) == folder and os.path.getmtime(c.path) >= now - days * 86400]
    dirs, prune = seal_dirs()
    copies, held, _ = sealing.hold_logs(inv, copies, sealing.find(dirs, prune))
    out = {}
    for c in copies:
        recs, seen = [], set()
        for _, line in cc_logs.iter_lines(c):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get('isSidechain') or r.get('uuid') in seen:
                continue
            seen.add(r.get('uuid'))
            recs.append(r)
        recs.sort(key=lambda r: r.get('timestamp') or '')
        date = (recs[0].get('timestamp') or '')[:10] if recs else ''
        out[c.key[:36]] = (date, recs)
    return out, sum(h.get('log copies', 0) for h in held.values())


def report(found, verbose=False, held=0):
    per = collections.defaultdict(list)
    for sid, (date, recs) in found.items():
        for skill, ok in judge(recs).items():
            per[skill].append((ok, date, sid[:8]))
    print(f'{len(found)} session log(s) read' + (f'; {held} held back by a seal' if held else ''))
    misses = 0
    for skill in list(BUILTIN) + [k for k in RULES if k not in BUILTIN]:
        rows = per.get(skill, [])
        hit = sum(ok for ok, _, _ in rows)
        misses += len(rows) - hit
        print(f'{skill:<13} loaded before its step in {hit} of {len(rows)} session(s)')
        if verbose:
            for ok, date, sid in sorted(rows):
                if not ok:
                    print(f'    missed {date} {sid}')
    return 2 if not found else (1 if misses else 0)


def selftest():
    global RULES, ALIASES
    saved = RULES, ALIASES
    RULES = compile_rules({'humanize': {'commit_in': ['my-site']}, 'release': {'bash': [r'\bmake\s+release\b']}})
    ALIASES = {'oldname': 'humanize'}

    def tu(name, **inp):
        return {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': name, 'input': inp}]}}

    def user(text):
        return {'type': 'user', 'message': {'content': text}}
    write = tu('mcp__figma__use_figma', code='const f = figma.createFrame(); f.name = "x";')
    read = tu('mcp__figma__use_figma', code='return figma.currentPage.children.map(n => n.name === "x")')
    cases = [
        ('figma write after Skill(figma-writes)', [tu('Skill', skill='figma-writes'), write], {'figma-writes': True}),
        ('figma write with no load', [write], {'figma-writes': False}),
        ('the load after the write is too late', [write, tu('Skill', skill='figma-writes')], {'figma-writes': False}),
        ('a use_figma read is no step', [read], {}),
        ('figma_post.py after a Read of the SKILL.md', [tu('Read', file_path='/home/x/.claude/skills/figma-writes/SKILL.md'),
                                                         tu('Bash', command='python tools/figma_post.py batch')], {'figma-writes': True}),
        ('ado.py new after Skill(plugin:ado)', [tu('Skill', skill='agent-workbench:ado'),
                                               tu('Bash', command='zsh -lc "python ~/kit/tools/ado.py new --type Bug"')], {'ado': True}),
        ('an ADO MCP write with no load', [tu('mcp__azure-devops__wit_work_item_write', id=1)], {'ado': False}),
        ('ado.py search is no step', [tu('Bash', command='python tools/ado.py search hero')], {}),
        ('a configured commit rule, covered by an old name from skill_use.aliases',
         [tu('Skill', skill='oldname'), tu('Bash', command='cd ~/repos/my-site-wt/a && git commit -F m.txt')], {'humanize': True}),
        ('a configured commit rule after /humanize typed', [user('<command-name>/humanize</command-name>'),
                                                           tu('Bash', command='git -C /home/x/repos/my-site commit -m x')], {'humanize': True}),
        ('a commit elsewhere is no step, even naming the folder', [tu('Bash', command='cd ~/kit/tools && git commit -m "my-site note"')], {}),
        ('a configured commit rule by cwd, no load', [dict(tu('Bash', command='git commit -m x'), cwd='/home/x/repos/my-site')],
         {'humanize': False}),
        ('git log and status in the folder are no step', [tu('Bash', command='cd ~/repos/my-site && git log -1 --format="%h commit" && git status')], {}),
        ('reading a write tool is no step', [tu('Bash', command="sed -n '1,60p' ~/kit/tools/figma_post.py; grep -n new tools/ado_write.py")], {}),
        ('--help and --check are no step', [tu('Bash', command='python tools/ado.py --rest new --help; python tools/figma_post.py batch x.json --check')], {}),
        ('ado_write.py field with no load', [tu('Bash', command='zsh -lc "python ~/kit/tools/ado_write.py field 12 System.Title x"')], {'ado': False}),
        ('a configured commit rule with -c options after a cd', [tu('Bash', command='cd /home/x/repos/my-site-wt/b && git -c user.name=t commit -m x')],
         {'humanize': False}),
        ('a configured bash rule after its skill', [tu('Skill', skill='release'), tu('Bash', command='make release')], {'release': True}),
    ]
    ok_all = True
    for label, recs, want in cases:
        got = judge(recs)
        ok_all &= got == want
        print(('PASS ' if got == want else 'FAIL ') + f'{label}: {got}')
    RULES = {}
    got = judge([tu('Bash', command='cd ~/repos/my-site && git commit -m x')])
    ok_all &= got == {}
    print(('PASS ' if got == {} else 'FAIL ') + f'with no rules configured, a commit is no step: {got}')
    RULES, ALIASES = saved
    print(f'skill_use selftest {"PASSED" if ok_all else "FAILED"}')
    return 0 if ok_all else 1


def main():
    global RULES, ALIASES
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--days', type=int, default=14)
    ap.add_argument('-v', '--verbose', action='store_true')
    ap.add_argument('--project', help="the project's log folder under ~/.claude/projects (default: the current project's)")
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    RULES, ALIASES = load_config()
    found, held = sessions(a.days, project=a.project)
    return report(found, a.verbose, held)


if __name__ == '__main__':
    sys.exit(main())
