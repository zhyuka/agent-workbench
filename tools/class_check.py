#!/usr/bin/env python3
"""class_check.py - the numerator/denominator gate for a whole-class change.

It answers one question in one shot, with no state to maintain:

    "I changed a shared file. How many files in the class actually got the change?"

A common failure is a shared-file change verified on the two or three examples someone named,
then reported as done. The fix is not remembering
to check - it is never being able to state a numerator without also stating a denominator.
This tool will not print one without the other, and it exits nonzero when the class is not
uniform, so a build step or a skill can gate on it.

Deliberately stateless. Gates that refuse at a decision point and keep no bookkeeping get used;
gates that need a file maintained between runs get abandoned.

Usage
  class_check.py <pattern> [--glob G] [--exclude G] [--root R] [--expect all|none|N] [--fixed] [-i] [-v]

Examples
  # every page should carry the new footer partial
  class_check.py 'SiteFooter' --glob '**/*.astro' --expect all

  # nothing should still reference the retired token
  class_check.py '#FF00FF' --glob '**/*.{astro,css,ts}' --expect none

  # what is the blast radius of this component, before touching it
  class_check.py 'HeroBanner' --glob '**/*.astro'

  # check the BUILD OUTPUT, not the source - the denominator that actually ships
  class_check.py 'aria-label' --root dist --glob '**/*.html' --expect all

Skipped directories
  Installs and build output (.git, node_modules, .venv, __pycache__ and others) are not walked, and
  _archive is not counted. The header counts every pruned dir and -v names each one. A glob that
  names one as a literal path segment enters it: 'repos/x/node_modules/pkg/*.md' counts those
  files, '**/*.md' does not. Files in a pruned _archive that the glob matches are listed as NOT
  COUNTED beside the N of M. .claude, .github and any dot dir not named here are walked.

Exit codes
  0  the class is uniform in the way --expect demanded (or no --expect was given)
  1  the class is NOT uniform - misses are listed
  2  could not run: the class is empty (the glob matched nothing, so any claim about it is
     vacuous), --root is not a directory, the pattern is not a valid regex, or --expect is not
     all, none or an integer
"""

import argparse
import os
import re
import sys

# The header echoes the pattern, so a non-ASCII pattern on a stdout that is not UTF-8 (the Windows
# console, or PYTHONIOENCODING=ascii) dies with UnicodeEncodeError before the count prints.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Pruned from the walk, unless a --glob names one as a literal path segment. Pruning one a glob
# names made the denominator silently short (a two-file glob into .claude/ passed as "1 of 1").
# Every prune is counted and printed, so the class never shrinks without saying so.
#
# .claude is not on either list. It holds skills, hooks and settings people edit. Skipping it let
# '**/skills/<name>/SKILL.md' miss a copy kept in a repo's .claude folder, and the check passed as
# "1 of 1" when the class was 2.
GENERATED = {                   # installs and build output: never walked
    '.git', 'node_modules', '.astro', '.svelte-kit', '.next', '.cache',
    '__pycache__', '.venv', 'venv', 'coverage',
}
ARCHIVED = {'_archive'}         # copies kept out of the class: not counted, but looked inside
SKIP_DIRS = GENERATED | ARCHIVED


def brace_expand(pat):
    """Expand one level of {a,b,c} so --glob '**/*.{astro,css}' behaves as expected."""
    m = re.search(r'\{([^{}]*)\}', pat)
    if not m:
        return [pat]
    out = []
    for piece in m.group(1).split(','):
        out.extend(brace_expand(pat[:m.start()] + piece + pat[m.end():]))
    return out


def glob_to_re(pat):
    """Translate a glob to a regex. ** crosses separators, * does not."""
    pat = pat.replace('\\', '/')
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith('**/', i):
            out.append('(?:.*/)?')
            i += 3
        elif pat.startswith('**', i):
            out.append('.*')
            i += 2
        elif c == '*':
            out.append('[^/]*')
            i += 1
        elif c == '?':
            out.append('[^/]')
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile('^' + ''.join(out) + '$')


def named_skip_dirs(pat):
    """Regexes for each SKIP_DIRS entry that pat names as a literal directory segment.

    'repos/x/.claude/t.md' and '**/.claude/*.md' ask for files inside .claude, so the walk must
    enter it. Only a literal segment opens one: '**/*.py' and '.*/HEAD' still prune .git.
    """
    segs = pat.replace('\\', '/').split('/')
    return [glob_to_re('/'.join(segs[:k + 1]))
            for k, seg in enumerate(segs[:-1]) if seg in SKIP_DIRS]


def enumerate_class(root, globs, excludes=()):
    """The denominator. Walked from the filesystem, never from memory.

    Returns (files, entered, pruned, uncounted): the class, the skip dirs a glob named and the walk
    entered, the path of every skip dir it did not enter, and the files inside a pruned ARCHIVED
    dir that the glob matches. Those are named, never counted.
    """
    pats, opened = [], []
    for g in globs:
        for expanded in brace_expand(g):
            pats.append(glob_to_re(expanded))
            opened.extend(named_skip_dirs(expanded))
    skips = []
    for g in excludes:
        for expanded in brace_expand(g):
            skips.append(glob_to_re(expanded))

    def wanted(rel):
        return any(p.match(rel) for p in pats) and not any(s.match(rel) for s in skips)

    found, entered, pruned = [], [], []
    for dirpath, dirnames, filenames in os.walk(root):
        here = os.path.relpath(dirpath, root).replace('\\', '/')
        keep = []
        for d in dirnames:
            if d not in SKIP_DIRS:
                keep.append(d)
                continue
            rel = d if here == '.' else here + '/' + d
            if any(o.match(rel) for o in opened):
                keep.append(d)
                entered.append(rel)
            else:
                pruned.append(rel)
        dirnames[:] = keep
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace('\\', '/')
            if wanted(rel):
                found.append((rel, full))
    uncounted = []
    for top in pruned:
        if top.rsplit('/', 1)[-1] not in ARCHIVED:
            continue
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, top)):
            dirnames[:] = [d for d in dirnames if d not in GENERATED]
            for fn in filenames:
                rel = os.path.relpath(os.path.join(dirpath, fn), root).replace('\\', '/')
                if wanted(rel):
                    uncounted.append(rel)
    return sorted(found), sorted(entered), sorted(pruned), sorted(uncounted)


def scan(files, rx):
    hit, miss, unreadable = [], [], []
    for rel, full in files:
        try:
            with open(full, encoding='utf-8', errors='replace') as fh:
                body = fh.read()
        except OSError as exc:
            unreadable.append((rel, str(exc)))
            continue
        n = len(rx.findall(body))
        (hit if n else miss).append((rel, n))
    return hit, miss, unreadable


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pattern')
    ap.add_argument('--glob', action='append', default=None,
                    help="file glob defining the class; repeatable. default '**/*'")
    ap.add_argument('--exclude', action='append', default=None,
                    help='glob to remove from the class; repeatable')
    ap.add_argument('--root', default='.')
    ap.add_argument('--expect', default=None,
                    help="all | none | an integer count the class must match")
    ap.add_argument('--fixed', '-F', action='store_true', help='literal pattern, not regex')
    ap.add_argument('-i', '--ignore-case', action='store_true')
    # MULTILINE is always on: patterns are written per-line ('^name:'), not per-file.
    ap.add_argument('-v', '--verbose', action='store_true', help='list every file, not the first 20')
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print('root is not a directory: %s' % root, file=sys.stderr)
        return 2

    globs = args.glob or ['**/*']
    pat = re.escape(args.pattern) if args.fixed else args.pattern
    try:
        rx = re.compile(pat, re.M | (re.I if args.ignore_case else 0))
    except re.error as exc:
        print('bad pattern: %s' % exc, file=sys.stderr)
        return 2

    files, entered, pruned, uncounted = enumerate_class(root, globs, args.exclude or [])
    total = len(files)

    print('pattern   %s%s' % (args.pattern, '  [literal]' if args.fixed else ''))
    print('root      %s' % root)
    print('glob      %s' % ', '.join(globs))
    if args.exclude:
        print('exclude   %s' % ', '.join(args.exclude))
    if entered:
        shown = entered if args.verbose else entered[:5]
        more = len(entered) - len(shown)
        print('entered   %s%s  (skip dirs a glob names)'
              % (', '.join(shown), ', ... %d more (-v for all)' % more if more else ''))
    if pruned:
        kinds = {}
        for p in pruned:
            kinds[p.rsplit('/', 1)[-1]] = kinds.get(p.rsplit('/', 1)[-1], 0) + 1
        print('pruned    %s  (not walked; name one in --glob to count its files%s)'
              % (', '.join('%s x%d' % kv for kv in sorted(kinds.items())),
                 '' if args.verbose else '; -v names each'))
        if args.verbose:
            for p in pruned:
                print('            %s' % p)
    print()

    cap = None if args.verbose else 20

    def show(label, rows, fmt):
        if not rows:
            return
        print('%s (%d):' % (label, len(rows)))
        for row in rows[:cap]:
            print('   ' + fmt(row))
        if cap is not None and len(rows) > cap:
            print('   ... %d more (-v for all)' % (len(rows) - cap))
        print()

    not_counted = ('NOT COUNTED: %d file(s) in a pruned %s match the glob (listed below)'
                   % (len(uncounted), '/'.join(sorted(ARCHIVED))))

    if total == 0:
        print('CLASS IS EMPTY - the glob matched no files.')
        print('Any "N of M" claim about this class is vacuous. Fix the glob or the root.')
        if uncounted:
            print(not_counted)
            print()
            show('NOT COUNTED', uncounted, lambda r: r)
        return 2

    hit, miss, unreadable = scan(files, rx)
    print('%d of %d files match.  (%d do not)' % (len(hit), total, len(miss)))
    if uncounted:
        print(not_counted)
    if unreadable:
        print('%d unreadable and counted in neither' % len(unreadable))
    print()

    show('MATCH', hit, lambda r: '%-4d %s' % (r[1], r[0]))
    show('MISS', miss, lambda r: r[0])
    show('UNREADABLE', unreadable, lambda r: '%s  (%s)' % (r[0], r[1]))
    show('NOT COUNTED', uncounted, lambda r: r)

    if args.expect is None:
        print('No --expect given, so this is a measurement, not a gate.')
        print('Quote it as "%d of %d". Never quote the numerator alone.' % (len(hit), total))
        return 0

    want = args.expect.strip().lower()
    if want == 'all':
        ok, why = len(hit) == total, 'every file in the class must match'
    elif want == 'none':
        ok, why = len(hit) == 0, 'no file in the class may match'
    else:
        try:
            n = int(want)
        except ValueError:
            print('--expect takes all, none, or an integer', file=sys.stderr)
            return 2
        ok, why = len(hit) == n, 'exactly %d files must match' % n

    print('GATE: %s' % why)
    print('%s - %d of %d' % ('PASS' if ok else 'FAIL', len(hit), total))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
