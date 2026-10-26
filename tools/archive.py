#!/usr/bin/env python3
"""archive.py - archive a path and say why in the same step, so no archive goes undescribed.

    python tools/archive.py move <path>... --why "<what it was and why it left>" [--name SLUG] [--status TEXT] [--dry-run]
    python tools/archive.py note <archived entry> --why "<...>" [--status TEXT] [--dry-run]
    python tools/archive.py --selftest

WHY. workspace_audit's `archive` check wants every archived entry described by a MANIFEST.md.
Sessions that archive by hand (mkdir, mv) skip the description, or write it in a file under another
name. This makes the description part of the move.

THE WORKSPACE is workspace.root in the agent-workbench config (--root overrides it); workspace.not_ours
lists top-level folders this tool never archives from (other people's clones, reference material).

WHERE IT GOES. The nearest `_archive` folder above the first path, up to the workspace's own. `move`
puts the paths in one new folder there: `_archive/<date>-<slug>/` at the workspace's top,
`<slug>-<date>/` in any other archive.
  - <root>/_archive: the new folder gets its own MANIFEST.md (date, what, why).
  - another _archive whose MANIFEST.md has a table: one row is added under it, its columns filled in
    order: the entry, its size (only when the table has 4 or more columns), the why, the status.
  - another _archive with no MANIFEST.md: the folder gets its own.
`note` writes the same record for an entry already in an archive. Either way the record is made only
after the move worked.

REFUSES (exit 2): no --why; a path outside the workspace, under a not_ours folder, or in an _archive
already; a destination that exists; for `note`, an entry not directly inside an _archive, or one
already described; no workspace configured.
"""
import argparse
import datetime
import os
import pathlib
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_config

DESKTOP = None          # the workspace root; main() reads it from config or --root
OFF_LIMITS = ()          # workspace.not_ours


def workspace(root=None):
    global OFF_LIMITS
    OFF_LIMITS = tuple(kit_config.get('workspace', 'not_ours') or ())
    return pathlib.Path(os.path.expanduser(root or kit_config.need('workspace', 'root')))


def atomic_write(path, text):
    tmp = path.with_name(path.name + '.archive-tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def size_of(p):
    files = [p] if p.is_file() else [q for q in p.rglob('*') if q.is_file()]
    mb = sum(q.stat().st_size for q in files) / 1e6
    return f"{len(files)} file{'s' * (len(files) != 1)}, {mb:.1f} MB"


def refuse(msg):
    print(f'refused: {msg}', file=sys.stderr)
    return 2


def check_source(p, desktop, off_limits=None):
    try:
        rel = p.resolve().relative_to(desktop.resolve())
    except ValueError:
        return f'{p} is outside {desktop}'
    if not rel.parts:
        return 'that is the workspace itself'
    if rel.parts[0] in (OFF_LIMITS if off_limits is None else off_limits):
        return f'{rel} is under {rel.parts[0]}/, which this tool does not archive'
    if '_archive' in rel.parts:
        return f'{rel} is already in an archive'
    if not p.exists():
        return f'{rel} does not exist'
    return None


def archive_for(p, desktop):
    """The nearest _archive above p, stopping at the workspace's own."""
    d = p.resolve().parent
    top = desktop.resolve()
    while d != top and top in d.parents:
        if (d / '_archive').is_dir():
            return d / '_archive'
        d = d.parent
    return top / '_archive'


def table_header(text):
    """(index of the first table line, column count) of the first markdown table, or None."""
    lines = text.split('\n')
    for i, line in enumerate(lines[:-1]):
        if line.startswith('|') and re.match(r'^\|(\s*:?-+:?\s*\|)+\s*$', lines[i + 1]):
            return i, line.strip().strip('|').count('|') + 1
    return None


def record(entry, arch, why, status, what, desktop, date):
    """Describe `entry` (a path directly in `arch`) where the archive check looks. Returns the file written."""
    index = arch / 'MANIFEST.md'
    name = f'`{entry.name}/`' if entry.is_dir() else f'`{entry.name}`'
    head = table_header(index.read_text(encoding='utf-8')) if index.exists() else None
    if arch.resolve() != (desktop / '_archive').resolve() and head:
        text = index.read_text(encoding='utf-8')
        lines = text.split('\n')
        start, cols = head
        end = start + 2
        while end < len(lines) and lines[end].startswith('|'):
            end += 1
        cells = [name] + ([size_of(entry)] if cols >= 4 else []) + [why.replace('|', '/')]
        cells = cells[:cols - 1] + [''] * max(0, cols - 1 - len(cells)) + [status]
        lines.insert(end, '| ' + ' | '.join(cells) + ' |')
        atomic_write(index, '\n'.join(lines))
        return index
    if not entry.is_dir():
        # A loose file in an archive with no table: start the index.
        atomic_write(index, f'# Archive index\n\n| Entry | What it was | Status |\n|---|---|---|\n'
                            f'| {name} | {why.replace("|", "/")} | {status} |\n')
        return index
    own = entry / 'MANIFEST.md'
    atomic_write(own, f'---\ndate: {date}\nwhat: {what}\nwhy: {why}\nstatus: {status}\n---\n')
    return own


def described(entry, arch):
    index = arch / 'MANIFEST.md'
    text = index.read_text(encoding='utf-8', errors='replace') if index.exists() else ''
    return (entry.is_dir() and (entry / 'MANIFEST.md').exists()) or f'`{entry.name}`' in text \
        or f'`{entry.name}/`' in text


def slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:48] or 'archived'


def move(paths, why, name=None, status=None, dry=False, desktop=None, today=None):
    date = today or datetime.date.today().isoformat()
    srcs = [pathlib.Path(p) for p in paths]
    for p in srcs:
        bad = check_source(p, desktop)
        if bad:
            return refuse(bad)
    arch = archive_for(srcs[0], desktop)
    slug = slugify(name or srcs[0].stem)
    top = arch.resolve() == (desktop / '_archive').resolve()
    dest = arch / (f'{date}-{slug}' if top else f'{slug}-{date}')
    if dest.exists():
        return refuse(f'{dest} exists; pass another --name')
    rel = lambda p: str(p.resolve().relative_to(desktop.resolve()))
    what = ', '.join(rel(p) for p in srcs)
    print(f"{'would move' if dry else 'moved'} {what} -> {rel(dest)}/")
    if dry:
        return 0
    dest.mkdir(parents=True)
    for p in srcs:
        shutil.move(str(p), str(dest / p.name))
    out = record(dest, arch, why, status or f'ARCHIVED {date}', f'{what}, moved here {date}', desktop, date)
    print(f'described in {rel(out)}')
    return 0


def note(entry, why, status=None, dry=False, desktop=None, today=None):
    date = today or datetime.date.today().isoformat()
    e = pathlib.Path(entry)
    if not e.exists() or e.parent.name != '_archive':
        return refuse(f'{entry} is not an entry directly inside an _archive folder')
    if described(e, e.parent):
        return refuse(f'{entry} is already described')
    if dry:
        print(f'would describe {e.name} in {e.parent}')
        return 0
    out = record(e, e.parent, why, status or f'ARCHIVED, recorded {date}', f'{e.name} ({size_of(e)})', desktop, date)
    print(f'described in {out}')
    return 0


def selftest():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import workspace_audit as wa
    global OFF_LIMITS
    OFF_LIMITS = ('repos',)
    results = []

    def check(label, ok, detail=''):
        results.append(bool(ok))
        print(('PASS ' if ok else 'FAIL ') + label + (f'  {detail}' if detail and not ok else ''))

    import contextlib
    import io
    quiet = lambda: contextlib.redirect_stderr(io.StringIO())
    with tempfile.TemporaryDirectory() as t:
        d = pathlib.Path(t) / 'Work'

        def put(rel, text='x'):
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            return p
        (d / '_archive').mkdir(parents=True)
        put('tools/old.md')
        put('proj/_archive/MANIFEST.md', '# Index\n\n| Dir | Contents | What it was | Status |\n|---|---|---|---|\n'
                                             '| `a/` | 1 file | A | SUPERSEDED |\n\nA note after the table.\n')
        put('proj/_archive/a/f.md')
        put('proj/reviews/draft.md')
        put('shareouts/s/_archive/MANIFEST.md', '# S\n\n| File | What it was | Status |\n|---|---|---|\n')
        put('shareouts/s/deck/old.html')
        put('shareouts/s/_archive/loose-2026-10-01/x.md')
        put('flows/_archive/kept/y.md')
        put('flows/a.md')
        put('repos/r/x.md')
        day = '2026-10-25'
        with quiet():
            check('no --why is refused by the CLI', main(['move', str(d / 'tools/old.md')], d) == 2)
            check('a path under repos/ is refused', move([d / 'repos/r/x.md'], 'w', desktop=d, today=day) == 2)
            check('a path already in an archive is refused',
                  move([d / 'proj/_archive/a/f.md'], 'w', desktop=d, today=day) == 2)
        check('move to the top archive makes a dated folder with its own MANIFEST.md',
              move([d / 'tools/old.md'], 'tool notes', desktop=d, today=day) == 0
              and (d / '_archive/2026-10-25-old/old.md').exists()
              and 'why: tool notes' in (d / '_archive/2026-10-25-old/MANIFEST.md').read_text()
              and not (d / 'tools/old.md').exists())
        check('move to an archive with a 4-column table adds one row under the table, before the note',
              move([d / 'proj/reviews/draft.md'], 'the old draft', name='draft', desktop=d, today=day) == 0
              and '| `a/` | 1 file | A | SUPERSEDED |\n| `draft-2026-10-25/` | 1 file, 0.0 MB | the old draft | '
                  'ARCHIVED 2026-10-25 |\n\nA note' in (d / 'proj/_archive/MANIFEST.md').read_text(),
              (d / 'proj/_archive/MANIFEST.md').read_text())
        check('move to an archive with a 3-column table fills entry, why, status',
              move([d / 'shareouts/s/deck'], 'the deck before the cut', name='pre-cut', status='SUPERSEDED by deck.html',
                   desktop=d, today=day) == 0
              and '| `pre-cut-2026-10-25/` | the deck before the cut | SUPERSEDED by deck.html |'
                  in (d / 'shareouts/s/_archive/MANIFEST.md').read_text())
        check('note records an entry already archived', note(d / 'shareouts/s/_archive/loose-2026-10-01', 'loose',
                                                             desktop=d, today=day) == 0)
        with quiet():
            check('note refuses an entry already described',
                  note(d / 'shareouts/s/_archive/loose-2026-10-01', 'again', desktop=d, today=day) == 2)
        check('move to an archive with no index gives the folder its own MANIFEST.md',
              move([d / 'flows/a.md'], 'a', desktop=d, today=day) == 0
              and (d / 'flows/_archive/a-2026-10-25/MANIFEST.md').exists())
        ok, total, bad = wa.archive_check(d, not_ours={'repos'})
        check('the archive check reads every recorded entry as described, the one left alone as not',
              (ok, total, bad) == (6, 7, ['no MANIFEST.md: flows/_archive/ (1 entries)']), f'{ok} of {total} {bad}')
    print(f'archive selftest: {sum(results)} of {len(results)} checks passed')
    return 0 if all(results) else 1


def main(argv=None, desktop=None):
    ap = argparse.ArgumentParser(prog='archive.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', nargs='?', choices=('move', 'note'))
    ap.add_argument('paths', nargs='*')
    ap.add_argument('--why')
    ap.add_argument('--name')
    ap.add_argument('--status')
    ap.add_argument('--root', help='the workspace (default: workspace.root in config)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.cmd or not a.paths:
        ap.print_help()
        return 2
    if not (a.why or '').strip():
        return refuse('--why is required: what it was, and why it left')
    if desktop is None:
        try:
            desktop = workspace(a.root)
        except kit_config.NotConfigured as e:
            return refuse(str(e))
    if a.cmd == 'move':
        return move(a.paths, a.why.strip(), a.name, a.status, a.dry_run, desktop)
    if len(a.paths) != 1:
        return refuse('note takes one entry')
    return note(a.paths[0], a.why.strip(), a.status, a.dry_run, desktop)


if __name__ == '__main__':
    sys.exit(main())
