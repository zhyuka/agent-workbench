#!/usr/bin/env python3
"""tool_time.py - how long sessions wait on each homemade tool, read from the Claude Code session logs.

    python tools/tool_time.py                   each tool over the last 7 days against its typical week of the 4 before
    python tools/tool_time.py --from A --to B   totals by run start in [A, B), ISO dates: the figure to cite
    python tools/tool_time.py --audit           the daily audit: exit 1 only when a tool is newly slow
    python tools/tool_time.py --at DATE ...     any of the above as of DATE instead of now
    python tools/tool_time.py --selftest        planted logs: runs, mentions, heredocs, prompts, background runs

WHY. Log search and other homemade tools can quietly get slow (a cache bug that makes every run re-read
its inputs, say) and cost hours of foreground waiting. Each session sees only its own runs, so nothing
flags a tool that has been slow for weeks. Run --audit daily (from a scheduler of your own); it reports a
newly slow tool once.

A RUN. A Bash call whose command runs `python <tool>.py`, for a .py in this folder or in a folder named
by --tools or the config key tool_time.tools (a list of folders), or `npm run <script>`. It has to be the first word of a command segment, after env
assignments and timeout, env, nice, time, nohup or exec, or inside `zsh -lc '...'` or `bash -c "..."`. A tool
named in an echo, a grep pattern or a heredoc body is a mention, not a run. A command that runs several tools
charges its whole time to the first. A call copied into a second log (a forked session) counts once.

ITS TIME. From the call to its result. A call that ran in the background, or was moved there, ends at its
<task-notification> (completed or failed), which names the call's tool-use id. A call moved there (its
Bash timeout ran out, or someone pressed to background it) was waited on until the move, and that wait
counts as foreground, and for a slow tool it can be a large share of the waiting. One with no notification
(stopped, or its session ended) is counted as unpaired, not timed. A call where someone answered a
permission prompt (permissionDecision.source user_*) is left out, because its time includes the wait for
the answer. Older logs don't record that, so a prompted call there still counts. A
call refused by a rule is not a run. A gap over 6 h is a session left open, not a run, and is dropped.
Foreground time is waiting. Background time is not, since the session goes on working, so every report
gives the two apart; the slow test uses both, since a background run is as slow as a foreground one.

An `npm run` in the background is a server, not a wait, and is left out.

SLOW. Over the last 7 days the tool ran 5+ times, its median run is at least 2x its typical week (the median
of its weekly medians over the 4 weeks before, weeks with 5+ runs), and its runs add up to 1 h or more.
Judged against the best of the 4 weeks instead, it flags many more tools, since one fast week sets the
bar; the typical week does not. --audit keeps the slow tools in
~/.cache/tool_time_flags.json and exits 1 only for a tool that was not slow at its last run, so a slow tool
is reported once, not every day; one that recovers leaves the file, and is reported again if it slows again.

BLIND. Bash calls in session logs under ~/.claude*/projects on this machine only. A tool that another tool
or a scheduler starts is not seen, and a tool whose name changed counts as two tools. A tool whose commands
differ in cost (a quick check mode and a slow proof mode) is judged on its mix, so its typical week can
swing when the mix shifts.
"""
import argparse
import collections
import datetime
import glob
import json
import os
import re
import shlex
import statistics
import sys
import tempfile

HOME = os.path.expanduser('~')
TOOLS = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HOME, '.claude*', 'projects', '**', '*.jsonl')
STATE = os.path.join(HOME, '.cache', 'tool_time_flags.json')
WEEK = 7 * 86400
MIN_RUNS, RATIO, MIN_WAIT = 5, 2.0, 3600
LONGEST = 6 * 3600
WRAP = {'timeout', 'gtimeout', 'env', 'nice', 'time', 'nohup', 'exec', 'command'}
SHELLS = {'zsh', 'bash', 'sh'}
SEP = set(';&|()')
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)(\w+)\1")
NOTE = re.compile(r'<tool-use-id>([^<]+)</tool-use-id>.*?<status>(\w+)</status>', re.S)


def configured_folders():
    """This folder, and the folders the config key tool_time.tools lists (kit_config.py), if any."""
    extra = []
    try:
        if TOOLS not in sys.path:
            sys.path.insert(0, TOOLS)
        import kit_config
        v = kit_config.get('tool_time', 'tools')
        extra = [v] if isinstance(v, str) else list(v or [])
    except ImportError:
        pass
    return [TOOLS] + [os.path.expanduser(d) for d in extra if isinstance(d, str) and d]


def tool_names(folders=None):
    folders = configured_folders() if folders is None else folders
    return {os.path.basename(p)[:-3] for d in folders for p in glob.glob(os.path.join(d, '*.py'))}


def strip_heredocs(cmd):
    """The command without its heredoc bodies: a tool named inside one is text fed to a program, not a run."""
    lines, out, i = cmd.split('\n'), [], 0
    while i < len(lines):
        out.append(lines[i])
        ends = [m.group(2) for m in HEREDOC.finditer(lines[i])]
        i += 1
        for end in ends:
            while i < len(lines) and lines[i].strip() != end:
                i += 1
            i += 1
    return '\n'.join(out)


def _unquoted_newlines(cmd):
    """Newlines outside quotes become ';', so each line is its own segment."""
    out, q, esc = [], None, False
    for ch in cmd:
        if esc:
            esc = False
        elif ch == '\\' and q != "'":
            esc = True
        elif q:
            q = None if ch == q else q
        elif ch in '\'"':
            q = ch
        elif ch == '\n':
            ch = ';'
        out.append(ch)
    return ''.join(out)


def segments(cmd):
    text = _unquoted_newlines(strip_heredocs(cmd))
    try:
        lx = shlex.shlex(text, posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        toks = list(lx)
    except ValueError:                                  # an unbalanced quote: split on spaces instead
        toks = re.split(r'\s+|([;&|()]+)', text)
        toks = [t for t in toks if t]
    seg = []
    for t in toks:
        if set(t) <= SEP:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg


def runs_in(cmd, names, depth=0):
    """The tools a command runs, in order (see A RUN in the docstring)."""
    found = []
    for toks in segments(cmd):
        while toks:
            if re.fullmatch(r'[A-Za-z_]\w*=.*', toks[0]):
                toks = toks[1:]
            elif toks[0] in WRAP:
                toks = toks[1:]
                while toks and (toks[0].startswith('-') or re.fullmatch(r'[\d.]+[smhd]?', toks[0])):
                    toks = toks[1:]
            else:
                break
        if not toks:
            continue
        head = os.path.basename(toks[0])
        if head in SHELLS:
            for i, a in enumerate(toks[1:-1], 1):
                if re.fullmatch(r'-\w*c\w*', a):
                    if depth < 2:
                        found += runs_in(toks[i + 1], names, depth + 1)
                    break
        elif re.fullmatch(r'python(\d+(\.\d+)*)?', head):
            args = toks[1:]
            while args and args[0].startswith('-') and args[0] not in ('-', '-c', '-m'):
                args = args[2:] if args[0] in ('-X', '-W') else args[1:]
            if args and args[0].endswith('.py') and os.path.basename(args[0])[:-3] in names:
                found.append(os.path.basename(args[0])[:-3])
        elif head == 'npm' and len(toks) > 2 and toks[1] in ('run', 'run-script'):
            found.append('npm run ' + toks[2])
    return found


def _ts(s):
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def _note(r):
    """(tool-use id, status) if r is the record that queued a background task's notification."""
    if r.get('type') == 'queue-operation' and r.get('operation') == 'enqueue':
        text = r.get('content')
    elif r.get('type') == 'attachment' and (r.get('attachment') or {}).get('type') == 'queued_command':
        text = r['attachment'].get('prompt')
    elif r.get('type') == 'user' and isinstance((r.get('message') or {}).get('content'), str):
        text = r['message']['content']
    else:
        return None
    if not isinstance(text, str) or not text.lstrip().startswith('<task-notification>'):
        return None
    m = NOTE.search(text)
    return (m.group(1), m.group(2)) if m else None


def read_logs(start, names, pattern=LOGS):
    """Every run in a log modified since start, as (runs, logs read). A run is {tool, t0: the call, res: its
    result, t1: its end (None: unpaired), launched_bg: started in the background, bg: ran there, prompted}."""
    runs, seen, n = [], set(), 0
    for f in glob.glob(pattern, recursive=True):
        try:
            if os.path.getmtime(f) < start:
                continue
            fh = open(f, encoding='utf-8', errors='replace')
        except OSError:
            continue
        n += 1
        uses, done, notes = {}, [], {}
        with fh:
            for line in fh:
                if '"tool_use' not in line and 'task-notification' not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or not r.get('timestamp'):
                    continue
                note = _note(r)
                if note:
                    if note[1] in ('completed', 'failed'):
                        notes[note[0]] = min(notes.get(note[0], 9e18), _ts(r['timestamp']))
                    continue
                content = (r.get('message') or {}).get('content')
                if not isinstance(content, list):
                    continue
                for x in content:
                    if not isinstance(x, dict):
                        continue
                    if x.get('type') == 'tool_use' and x.get('name') == 'Bash' and x.get('id') not in seen:
                        inp = x.get('input') or {}
                        hit = runs_in(inp.get('command') or '', names)
                        if hit and hit[0].startswith('npm run ') and inp.get('run_in_background'):
                            continue                    # a server, not a wait
                        if hit:
                            seen.add(x['id'])
                            uses[x['id']] = (hit[0], _ts(r['timestamp']), bool(inp.get('run_in_background')))
                    elif x.get('type') == 'tool_result' and x.get('tool_use_id') in uses:
                        tool, t0, bg = uses.pop(x['tool_use_id'])
                        pd = r.get('permissionDecision') or {}
                        if pd.get('decision') == 'reject' or r.get('toolDenialKind'):
                            continue
                        res = r.get('toolUseResult') if isinstance(r.get('toolUseResult'), dict) else {}
                        body = x.get('content')
                        body = body if isinstance(body, str) else json.dumps(body)[:2000]
                        moved = bg or bool(res.get('backgroundTaskId')) or bool(
                            re.search(r'running in background with ID|moved to the background', body))
                        done.append({'tool': tool, 't0': t0, 'res': _ts(r['timestamp']), 'id': x['tool_use_id'],
                                     'launched_bg': bg, 'bg': moved,
                                     'prompted': str(pd.get('source', '')).startswith('user')})
        for d in done:
            d['t1'] = notes.get(d.pop('id')) if d['bg'] else d['res']
            runs.append(d)
    return runs, n


def table(runs, lo, hi):
    """{tool: {'d': [every timed run], 'fg': [what the session waited: call to result, for a call not
    started in the background, so a call moved there counts its wait until the move], 'bg': n timed in the
    background, 'unpaired': n, 'prompted': n}} for runs starting in [lo, hi)"""
    by = collections.defaultdict(lambda: {'d': [], 'fg': [], 'bgd': [], 'bg': 0, 'unpaired': 0, 'prompted': 0})
    for r in runs:
        if not lo <= r['t0'] < hi:
            continue
        b = by[r['tool']]
        if r['prompted']:
            b['prompted'] += 1
            continue
        if not r['launched_bg'] and 0 <= r['res'] - r['t0'] <= LONGEST:
            b['fg'].append(r['res'] - r['t0'])
        if r['t1'] is None:
            b['unpaired'] += 1
        elif 0 <= r['t1'] - r['t0'] <= LONGEST:
            b['d'].append(r['t1'] - r['t0'])
            if r['bg']:
                b['bg'] += 1
                b['bgd'].append(r['t1'] - r['t0'])
    return by


def weeks(runs, now):
    """Each tool's last 7 days, with its typical week (the median of its weekly medians over the 4 weeks
    before) and whether it is slow."""
    w = [table(runs, now - (i + 1) * WEEK, now - i * WEEK) for i in range(5)]
    out = []
    for tool, cur in w[0].items():
        ds = cur['d']
        if not ds:
            continue
        base = [statistics.median(x[tool]['d']) for x in w[1:] if len(x.get(tool, {}).get('d', [])) >= MIN_RUNS]
        typical = statistics.median(base) if base else None
        med, total = statistics.median(ds), sum(ds)
        slow = bool(len(ds) >= MIN_RUNS and typical is not None and med >= RATIO * typical and total >= MIN_WAIT)
        out.append({'tool': tool, 'runs': len(ds), 'bg': cur['bg'], 'median': med, 'total': total, 'typical': typical,
                    'waited': sum(cur['fg']), 'unpaired': cur['unpaired'], 'prompted': cur['prompted'], 'slow': slow})
    return sorted(out, key=lambda r: -r['total'])


def _local(t):
    return f'{datetime.datetime.fromtimestamp(t).astimezone():%Y-%m-%d %H:%M %Z}'


def _h(s):
    return f'{s / 3600:.1f} h' if s >= 360 else f'{s / 60:.0f} min'


def describe(r):
    typical = (f'{r["typical"]:.0f}s in a typical week ({r["median"] / r["typical"]:.1f}x)' if r['typical']
            else 'no earlier week with 5+ runs')
    return (f'{r["tool"]}: {r["runs"]} runs ({r["bg"]} in the background), {_h(r["total"])} of run time '
            f'({_h(r["waited"])} waited on in the foreground), median {r["median"]:.0f}s against {typical}')


def atomic_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def audit(rows, state_path, now):
    """(lines, exit code): FAIL for a tool newly slow, a note for one still slow; the state file is rewritten."""
    try:
        before = json.load(open(state_path, encoding='utf-8'))
    except (OSError, ValueError):
        before = {}
    slow = [r for r in rows if r['slow']]
    since = datetime.datetime.fromtimestamp(now).strftime('%Y-%m-%d')
    atomic_json(state_path, {r['tool']: before.get(r['tool'], since) for r in slow})
    new = [r for r in slow if r['tool'] not in before]
    lines = [f'FAIL newly slow: {describe(r)}' for r in new]
    lines += [f'still slow since {before[r["tool"]]}: {describe(r)}' for r in slow if r['tool'] in before]
    counted = sum(1 for r in rows if r['runs'] >= MIN_RUNS)
    lines.append(f'{len(slow)} of {counted} tools with {MIN_RUNS}+ runs in the last 7 days are slow, {len(new)} newly;'
                 f' slow = median at least {RATIO:g}x a typical week of the 4 before, and {MIN_WAIT // 3600} h+ of run time')
    return lines, (1 if new else 0)


def coverage(runs, n_logs, lo, hi):
    inside = [r for r in runs if lo <= r['t0'] < hi]
    return (f'COVERAGE {n_logs} session logs read; {len(inside)} runs from '
            f'{_local(lo)} to {_local(hi)}:'
            f' {sum(1 for r in inside if r["prompted"])} left out (a prompt was answered),'
            f' {sum(1 for r in inside if r["t1"] is None and not r["prompted"])} background runs unpaired (not timed)')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--audit', action='store_true')
    ap.add_argument('--from', dest='frm')
    ap.add_argument('--to')
    ap.add_argument('--at')
    ap.add_argument('--state', default=STATE)
    ap.add_argument('--logs', default=LOGS)
    ap.add_argument('--tools', nargs='*', help='folders whose *.py count as tools (default: this folder and the config key tool_time.tools)')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    names = tool_names(a.tools) if a.tools else tool_names()
    now = _ts(a.at) if a.at else datetime.datetime.now().timestamp()
    if a.audit and a.at and a.state == STATE:
        print('did not run: --audit --at needs its own --state; the real state file is for today only')
        return 2
    if a.frm:
        lo, hi = _ts(a.frm), (_ts(a.to) if a.to else now)
        runs, n = read_logs(lo, names, a.logs)
        if not n:
            print(f'did not run: no session log modified since {a.frm} under {a.logs}')
            return 2
        rows = table(runs, lo, hi)
        fg = sum(sum(b['fg']) for b in rows.values()) or 1
        bg = sum(sum(b['bgd']) for b in rows.values())
        print(f'{a.frm} to {a.to or "now"}: sessions waited {_h(fg)} on {sum(len(b["fg"]) for b in rows.values())} calls'
              f' (a call moved to the background counts until the move); background runs took {_h(bg)} more over'
              f' {sum(b["bg"] for b in rows.values())} runs, while sessions went on working. % is of the waiting')
        for tool, b in sorted(rows.items(), key=lambda kv: (-sum(kv[1]['fg']), -sum(kv[1]['bgd']))):
            if b['fg'] or b['bgd']:
                med = f'{statistics.median(b["fg"]):6.1f}s' if b['fg'] else '     -'
                print(f'  {tool:28s} waited {_h(sum(b["fg"])):>9s} on {len(b["fg"]):5d} calls  median {med}'
                      f'  longest {max(b["fg"] or [0]):6.0f}s  {100 * sum(b["fg"]) / fg:4.1f}%'
                      f'  | background {_h(sum(b["bgd"]))} in {b["bg"]} runs')
        print(coverage(runs, n, lo, hi))
        return 0
    runs, n = read_logs(now - 5 * WEEK, names, a.logs)
    if not n:                                  # nothing read is "did not run", never "nothing slow"
        print(f'did not run: no session log from the last 5 weeks under {a.logs}')
        return 2
    rows = weeks(runs, now)
    if a.audit:
        lines, code = audit(rows, a.state, now)
        print('\n'.join(lines))
        print(coverage(runs, n, now - WEEK, now))
        return code
    print(f'last 7 days to {_local(now)}, against each tool\'s typical week of the 4 before')
    for r in rows:
        print(('SLOW ' if r['slow'] else '     ') + describe(r))
    print(coverage(runs, n, now - WEEK, now))
    return 0


def selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(('PASS ' if cond else 'FAIL ') + name)

    names = {'slowtool', 'steadytool', 'burst', 'jitter', 'recall', 'mover'}
    run = lambda c: runs_in(c, names)
    check('a run: after cd, timeout, an env assignment and python options',
          run("cd ~/work && X=1 timeout 300 python -u -X utf8 tools/slowtool.py --a 'b c' | head") == ['slowtool'])
    check('a run inside zsh -lc, and npm run', run("zsh -lc 'python3 tools/recall.py x'; npm run check") == ['recall', 'npm run check'])
    check('a run on its own line of a multi-line command', run('cd x\npython tools/burst.py') == ['burst'])
    check('a mention is not a run: echo, grep pattern, a quoted string',
          run('echo "python tools/slowtool.py"; grep -n "python tools/recall.py" f; git commit -m "python burst.py"') == [])
    check('a heredoc body is not a run, and the command after it is',
          run("python - <<'EOF'\npython tools/slowtool.py\nEOF\npython tools/recall.py") == ['recall'])
    check('python -c and an unknown script are not runs', run('python -c "import x"; python other.py') == [])

    with tempfile.TemporaryDirectory() as tmp:
        now = _ts('2026-10-25T12:00:00Z')
        iso = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        logs = os.path.join(tmp, '.claude', 'projects', 'p')
        os.makedirs(logs)
        n = [0]

        def call(fh, t, cmd, dur, bg=False, note=None, prompted=False, denied=False, tid=None, moved=None):
            n[0] += 1
            tid = tid or f'toolu_{n[0]}'
            fh.write(json.dumps({'type': 'assistant', 'timestamp': iso(t), 'message': {'content': [
                {'type': 'tool_use', 'id': tid, 'name': 'Bash', 'input': {'command': cmd, 'run_in_background': bg}}]}}) + '\n')
            text = ('Command running in background with ID: b1.' if bg else
                    'Command did not complete within its 600s timeout and was moved to the background (ID: b2).'
                    if moved else 'ok')
            rec = {'type': 'user', 'timestamp': iso(t + (1 if bg else moved or dur)), 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': tid, 'content': text}]}}
            if prompted:
                rec['permissionDecision'] = {'decision': 'accept', 'source': 'user_temporary'}
            if denied:
                rec['permissionDecision'] = {'decision': 'reject', 'source': 'config'}
            fh.write(json.dumps(rec) + '\n')
            if note:
                fh.write(json.dumps({'type': 'queue-operation', 'operation': 'enqueue', 'timestamp': iso(t + dur),
                                     'content': f'<task-notification>\n<task-id>b1</task-id>\n<tool-use-id>{tid}'
                                                f'</tool-use-id>\n<status>{note}</status>\n</task-notification>'}) + '\n')

        with open(os.path.join(logs, 'a.jsonl'), 'w', encoding='utf-8') as fh:
            for wk in range(1, 5):                       # four earlier weeks: every tool at its usual pace
                for i in range(6):
                    t = now - wk * WEEK - (i + 1) * 3600
                    call(fh, t, 'python tools/slowtool.py', 300)
                    call(fh, t + 60, 'python tools/steadytool.py', 700)
                    call(fh, t + 120, 'python tools/burst.py', 60)
                    call(fh, t + 180, 'python tools/jitter.py', 100 if wk == 1 else 300)   # one fast week
            for i in range(6):                           # this week: slowtool 2.3x, steadytool the same, burst 2x but 12 min,
                t = now - (i + 1) * 3600                 # jitter 5x its fast week but 1.7x its typical one
                call(fh, t, 'python tools/slowtool.py', 700)
                call(fh, t + 60, 'python tools/steadytool.py', 700)
                call(fh, t + 120, 'python tools/burst.py', 120)
                call(fh, t + 180, 'python tools/jitter.py', 1600 if i == 0 else 500)
            call(fh, now - 700, 'npm run preview:prod', 5 * 3600, bg=True, note='completed')   # a server
            call(fh, now - 800, 'python tools/mover.py', 1500, moved=600, note='completed')      # waited, then moved
            call(fh, now - 600, 'echo "python tools/steadytool.py"', 5000)                # a mention
            call(fh, now - 500, 'python tools/steadytool.py', 5000, prompted=True)        # waited on a prompt
            call(fh, now - 450, 'python tools/steadytool.py', 5000, denied=True)          # refused by a rule
            call(fh, now - 400, 'python tools/recall.py', 900, bg=True, note='completed', tid='toolu_bg')
            call(fh, now - 300, 'python tools/recall.py', 900, bg=True, tid='toolu_quiet')  # never notified
            call(fh, now - 250, 'grep -n task-notification log', 1)    # its output quotes a notification: not one
            fh.write(json.dumps({'type': 'user', 'timestamp': iso(now - 249), 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_grep', 'content': '<task-notification>\n<tool-use-id>'
                 'toolu_quiet</tool-use-id>\n<status>completed</status>\n</task-notification>'}]}}) + '\n')
            call(fh, now - 200, 'python tools/recall.py', 900, bg=True, note='killed')    # stopped: not a finish
        with open(os.path.join(logs, 'fork.jsonl'), 'w', encoding='utf-8') as fh:
            call(fh, now - 400, 'python tools/recall.py', 900, bg=True, note='completed', tid='toolu_bg')
        pattern = os.path.join(tmp, '.claude*', 'projects', '**', '*.jsonl')
        runs, nl = read_logs(now - 5 * WEEK, names, pattern)
        rows = {r['tool']: r for r in weeks(runs, now)}
        check('a tool at 2.3x its typical week with over 1 h of run time is slow', rows['slowtool']['slow'])
        check('a tool at its usual pace is not slow, however long it runs', not rows['steadytool']['slow']
              and rows['steadytool']['total'] == 6 * 700)
        check('a tool at 2x with under 1 h of run time is not slow', not rows['burst']['slow'])
        check('a tool is judged against its typical week, not its one fast week',
              not rows['jitter']['slow'] and rows['jitter']['typical'] == 300 and rows['jitter']['total'] >= MIN_WAIT)
        check('an npm run in the background is a server, not timed', 'npm run preview:prod' not in rows)
        mv = rows.get('mover', {})
        check('a call moved to the background counts its wait until the move as waiting, and runs to its notification',
              mv.get('waited') == 600 and mv.get('total') == 1500 and mv.get('bg') == 1)
        check('a mention, a prompted call and a refused call are not timed; the prompted one is counted apart',
              rows['steadytool']['runs'] == 6 and rows['steadytool']['prompted'] == 1)
        rec = rows['recall']
        check('a background run is timed to its notification, once across a forked copy, and one with no finish is '
              'unpaired; it is run time, not waiting', rec['runs'] == 1 and rec['total'] == 900 and rec['bg'] == 1
              and rec['unpaired'] == 2 and rec['waited'] == 0 and rows['steadytool']['waited'] == 6 * 700)
        state = os.path.join(tmp, 'flags.json')
        first, c1 = audit(list(rows.values()), state, now)
        second, c2 = audit(list(rows.values()), state, now + 86400)
        check('--audit fails once for a newly slow tool, then notes it as still slow and exits 0',
              c1 == 1 and any(l.startswith('FAIL newly slow: slowtool') for l in first)
              and c2 == 0 and any(l.startswith('still slow since 2026-10-25: slowtool') for l in second))
        well = [dict(r, slow=False) for r in rows.values()]
        audit(well, state, now + 2 * 86400)
        third, c3 = audit(list(rows.values()), state, now + 3 * 86400)
        check('a tool that recovers leaves the state, and fails again when it slows again', c3 == 1)
        check('the audit line counts tools in N of M form', any(
            re.search(r'\b1 of 4 tools with 5\+ runs', l) for l in first))
        empty = os.path.join(tmp, 'none', '**', '*.jsonl')
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            codes = (main(['--audit', '--logs', empty, '--state', state]),
                     main(['--from', '2026-10-01', '--logs', empty]), main(['--audit', '--at', '2026-10-01']))
        check('no session log read, or --at against the real state file, is did not run (exit 2), never clean',
              codes == (2, 2, 2))
    print('ALL PASS' if ok else 'SOME FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
