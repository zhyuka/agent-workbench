#!/usr/bin/env python3
"""memory_check.py - the index/card gate for a Claude Code memory folder.

Sibling to class_check.py. That one answers "how many files in the class got the change?";
this one answers the equivalent question one layer up:

    "MEMORY.md is re-sent on every turn. How many of its lines still describe their card?"

MEMORY.md is the only part of memory that is always resident, so it is the only part that
steers recall. A hook that has drifted from its card does not merely waste tokens - it
produces confident wrong recall, which is the more expensive failure: a hook that says PARKED
over a card with an open thread, or SHIPPED over an unreconciled correction.

Like class_check.py this is deliberately stateless and it refuses a bare numerator: every
run prints N of M or it prints nothing useful.

THE MEMORY FOLDER is any ~/.claude/projects/<project>/memory: --memory DIR names one, else the
current project's (the folder Claude Code keeps for the working directory, or $CLAUDE_PROJECT_DIR).

ABSORBED CARDS. When cards are consolidated, the old ones are often kept in an archive folder and
links to them are left in place on purpose. Name those folders with --absorbed DIR (repeatable) or
the `memory.absorbed` list in kit_config; a link that resolves there is reported as ABSORBED, not
DEAD, so the dead count can reach zero.

Usage
  memory_check.py [drift|budget|links|clusters|all] [--memory DIR] [--budget N] [--absorbed DIR] [-v]

Examples
  memory_check.py                      every check, one report
  memory_check.py drift -v             which index lines contradict their card
  memory_check.py budget --budget 100  which hooks are over the length budget, worst first
  memory_check.py clusters             cards whose names suggest they cover one fact between them

Exit codes
  0  every card indexed and no drift found
  1  drift, orphans, or over-budget hooks - all listed
  2  nothing to check: no MEMORY.md, or it has no index lines, so any claim is vacuous
"""

import argparse
import collections
import os
import pathlib
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# A Windows console is cp1252 and this file prints a glyph outside it. Without this the run dies
# with UnicodeEncodeError, often at the LAST print, throwing away completed work.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def default_root():
    import cc_logs
    return pathlib.Path(cc_logs.memory_dir())


# A card saying one of these about itself, with an index line that does not, is the drift that
# matters: the hook reads live and the card reads finished, or the hook reads finished and the
# card still has a loop open.
DONE_IN_CARD = re.compile(
    r'\b(SUPERSEDED|RULED OUT|RETIRED|WITHDRAWN|DEPRECATED|OBSOLETE|NO LONGER)\b')
OPEN_IN_CARD = re.compile(
    r'(still open|still outstanding|still pending|unanswered|unreconciled'
    # A bare `reconcile` is not here: the verb is common in prose (a rule about how to word a claim
    # reads as an open loop). Only the forms that assert THIS card has something unreconciled count.
    r'|not yet (done|shipped|built|run|live)|remains open'
    r'|needs? reconciling|to be reconciled|still to reconcile|TODO)', re.I)
DONE_IN_HOOK = re.compile(
    r'\b(SHIPPED|CLOSED|DONE|PARKED|SUNSET|RETIRED|COMPLETE|SUPERSEDED|DEFERRED'
    r'|DECIDED OUT|RULED OUT|ARCHIVED|SAFE TO)\b', re.I)
# A hook that already flags the loop is not drifted, it is doing its job. Without this the check
# fires on its own fix: "NOT parked, one thread still open" matches PARKED and reads as drift.
# re.I is load-bearing: a hook beginning a sentence with "Open items remain..." must match \bopen\b.
HOOK_FLAGS_OPEN = re.compile(
    r'(⚠|\bNOT\b|\bstill\b|\bopen\b|\boutstanding\b|\bpending\b|\bre-check\b|\bunless\b)', re.I)

INDEX_LINE = re.compile(r'^\s*-\s*\[(?P<title>[^\]]+)\]\((?P<card>[^)]+\.md)\)(?P<hook>.*)$')


def load(root):
    """Return (index_lines, cards). index_lines maps card stem -> (raw line, hook text)."""
    idx = root / 'MEMORY.md'
    if not idx.exists():
        return None, None
    lines = {}
    for raw in idx.read_text(encoding='utf-8').splitlines():
        m = INDEX_LINE.match(raw)
        if m:
            lines[pathlib.Path(m.group('card')).stem] = (raw, m.group('hook').strip())
    cards = {p.stem: p for p in sorted(root.glob('*.md')) if p.name != 'MEMORY.md'}
    return lines, cards


def marked_none(path):
    """True when the card's frontmatter says `index: none`: memory_index.py leaves it out of
    MEMORY.md on purpose (a card reached through a hub card), so it is not a finding. The line is
    found by memory_index.card_index, so both tools read it the same way, at the top level or under
    `metadata:`. A card whose frontmatter does not parse is not marked."""
    from memory_index import Refuse, card_index
    try:
        return card_index(path.read_text(encoding='utf-8'), path.name) == 'none'
    except Refuse:
        return False


def check_coverage(lines, cards):
    """Both directions. A card with no index line is never recalled; the reverse is a dead link.
    A card marked `index: none` is left out on purpose and counts as neither."""
    unindexed = sorted(s for s in set(cards) - set(lines) if not marked_none(cards[s]))
    dangling = sorted(set(lines) - set(cards))
    return unindexed, dangling


def check_drift(lines, cards):
    """(drifted, exempt, nocard) -- three buckets, so a line that was never compared is never
    counted in the numerator as agreeing. An earlier version returned only the drifted list, and
    every exempt or card-less line read as "agrees"."""
    out, exempt, nocard = [], [], []
    for stem, (raw, hook) in sorted(lines.items()):
        p = cards.get(stem)
        if not p:
            nocard.append(stem)
            continue
        body = p.read_text(encoding='utf-8')
        card_open = OPEN_IN_CARD.search(body)
        hook_done = DONE_IN_HOOK.search(hook)
        hook_open = HOOK_FLAGS_OPEN.search(hook)
        # The exemption has ONE job: a hook that says "SHIPPED, but one thread still open" must not
        # read as drift against a card with an open loop, because the hook already said so. That
        # needs BOTH markers. A hook with an open-flag and no done-marker is checked like any other;
        # exempting on a bare ⚠ or the word "open" skipped the lines that carry the most weight.
        if hook_done and hook_open:
            exempt.append(stem)
            continue
        if hook_done and card_open:
            out.append((stem, 'hook reads finished, card has an open loop', card_open.group(0)))
        # THE OTHER DIRECTION (hook reads live, card reads finished) IS DELIBERATELY NOT IMPLEMENTED.
        # Matching DONE_IN_CARD in the body, or only in the description, was tried: nearly every hit
        # was a word about the card's SUBJECT ("the retired tree"), not its status. A card's own
        # status is not separable from its subject by keyword, and a gate with mostly false
        # positives stops being read. This direction needs a real status field in the frontmatter.
    return out, exempt, nocard


def check_internal_reversals(lines, cards):
    """Advisory only - never fails a run. A card containing SUPERSEDED/RETIRED is usually a card
    doing its job: recording that some framing *inside it* was overturned. Treated as drift it was
    wrong every time it was checked. Kept as a worklist because a wholly-superseded card looks
    identical."""
    out = []
    for stem, (raw, hook) in sorted(lines.items()):
        p = cards.get(stem)
        if p and DONE_IN_CARD.search(p.read_text(encoding='utf-8')) \
                and not DONE_IN_HOOK.search(hook):
            out.append(stem)
    return out


PREFIXES = ('reference_', 'project_', 'feedback_', 'user_', 'people_', 'env_')


def resolve(target, cards):
    """Cards are cited three ways: exact stem, hyphens for underscores, and with the type prefix
    dropped ([[repo-notes]] for project_repo_notes). All three resolve. Anything left really is
    dangling."""
    def norm(s):
        return s.strip().replace('-', '_').lower()

    want = norm(target)
    by_norm = {norm(s): s for s in cards}
    if want in by_norm:
        return by_norm[want]
    for pre in PREFIXES:
        if pre + want in by_norm:
            return by_norm[pre + want]
    for n, stem in by_norm.items():
        if n.split('_', 1)[-1] == want:
            return stem
    return None


# A [[link]] inside a code span is PROSE ABOUT the mechanism, not a use of it: a card explaining
# why it writes names plain "because `[[wikilinks]]` here would dangle" must not be flagged.
CODE_SPAN = re.compile(r'`[^`\n]*`')
SKILLS_DIR = pathlib.Path.home() / '.claude' / 'skills'


def absorbed_dirs(given):
    """The archive folders of consolidated cards: --absorbed, else kit_config `memory.absorbed`."""
    if given:
        return [pathlib.Path(os.path.expanduser(d)) for d in given]
    try:
        import kit_config
        got = kit_config.get('memory', 'absorbed') or []
    except ImportError:
        got = []
    return [pathlib.Path(os.path.expanduser(d)) for d in ([got] if isinstance(got, str) else got)]


def check_links(lines, cards, absorbed_from=()):
    """[[wikilinks]] pointing at a card that does not exist, split three ways because the three
    want three different actions. DEAD is the only one that is a defect: a card link with no card
    anywhere. ABSORBED resolves into an archive of consolidated cards and must be left alone.
    ELSEWHERE names a skill or a file - a real target in another namespace, not a broken card.
    Resolution into an archive is by file stem, and resolve()'s prefix-drop rule bridges a stem
    (reference_some_topic) and a slug ([[some-topic]])."""
    lower = {s.lower(): s for s in cards}
    absorbed = {p.stem for d in absorbed_from if d.is_dir() for p in d.glob('*.md')
                if p.name != 'MEMORY.md'}
    dead, gone, elsewhere = [], [], []
    for stem, p in sorted(cards.items()):
        body = CODE_SPAN.sub('', p.read_text(encoding='utf-8'))
        for target in sorted(set(re.findall(r'\[\[([^\]]+)\]\]', body))):
            if resolve(target, lower):
                continue
            if absorbed and resolve(target, absorbed):
                gone.append((stem, target))
            elif target.endswith('.md') or (SKILLS_DIR / target).is_dir():
                elsewhere.append((stem, target))
            else:
                dead.append((stem, target))
    return dead, gone, elsewhere


HOOK_SEP = re.compile(r'^(?:[—–-]\s+)+')


def check_budget(lines, budget):
    """Budget the HOOK, not the whole line. Much of every line is markdown scaffolding
    - '- [Title](reference_some_long_name.md) - ' - which no rewriting shortens. Budgeting the
    line measures the filename and hides whether the hook itself got tighter.

    The ' — ' separator is scaffolding too, but INDEX_LINE leaves it on the hook, so every hook
    would measure 2 long. Strip it, doubled or not."""
    sizes = ((len(HOOK_SEP.sub('', hook)), stem) for stem, (_, hook) in lines.items())
    return sorted((s for s in sizes if s[0] > budget), reverse=True)


def check_clusters(lines, floor=3):
    """Cards sharing a topic word are candidates for covering one fact between them. This is a
    prompt to look, never a verdict - whether to merge is a judgement the tool cannot make."""
    buckets = collections.defaultdict(list)
    for stem in lines:
        for word in set(re.findall(r'[a-z]+', stem.lower())):
            if len(word) > 3 and word not in ('reference', 'project', 'feedback', 'user'):
                buckets[word].append(stem)
    return {w: sorted(v) for w, v in buckets.items() if len(v) >= floor}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', nargs='?', default='all',
                    choices=['drift', 'budget', 'links', 'clusters', 'all'])
    ap.add_argument('--memory', '--root', dest='root', default=None,
                    help='the memory folder (default: the current project\'s)')
    ap.add_argument('--budget', type=int, default=100,
                    help='max characters per index-line hook, after the separator (default 100)')
    ap.add_argument('--absorbed', action='append', default=[],
                    help='an archive folder of consolidated cards (repeatable; default kit_config memory.absorbed)')
    ap.add_argument('-v', '--verbose', action='store_true', help='list every hit, not the first 15')
    a = ap.parse_args()

    root = pathlib.Path(os.path.expanduser(a.root)) if a.root else default_root()
    lines, cards = load(root)
    if lines is None:
        print(f'no MEMORY.md under {root}', file=sys.stderr)
        return 2
    if not lines:
        print('MEMORY.md has no index lines - nothing to check, so any claim about it is vacuous',
              file=sys.stderr)
        return 2

    cap = None if a.verbose else 15
    print(f'root      {root}')
    print(f'index     {len(lines)} lines')
    print(f'cards     {len(cards)} files\n')

    bad = False
    unindexed, dangling = check_coverage(lines, cards)
    matched = len(set(lines) & set(cards))
    unlisted = len(set(cards) - set(lines)) - len(unindexed)
    print(f'{matched} of {len(cards)} cards are indexed.  ({len(unindexed)} unindexed, '
          f'{unlisted} marked `index: none`, {len(dangling)} index lines point at no card)')
    for stem in (unindexed + dangling)[:cap]:
        print(f'   {"UNINDEXED" if stem in unindexed else "DANGLING "}  {stem}')
    bad |= bool(unindexed or dangling)
    print()

    if a.mode in ('drift', 'all'):
        d, exempt, nocard = check_drift(lines, cards)
        checked = len(lines) - len(exempt) - len(nocard)
        print(f'{checked - len(d)} of {checked} CHECKED index lines agree with their card.  '
              f'({len(d)} drifted)')
        if exempt or nocard:
            print(f'   of {len(lines)} index lines total: {checked} checked, '
                  f'{len(exempt)} exempted by the open-flag rule (NOT checked), '
                  f'{len(nocard)} have no card.')
            print('   ⚠ The exemption fires ONLY where a hook states a decision AND flags a '
                  'loop ("SHIPPED, one thread still open") -- self-qualifying, so drift cannot be '
                  'read off it. They are UNVERIFIED, not clean.')
        for stem in nocard[:cap]:
            print(f'   NO CARD  {stem}')
        for stem, why, ev in d[:cap]:
            print(f'   DRIFT  {stem}\n          {why} -> {ev!r}')
        bad |= bool(d)
        adv = check_internal_reversals(lines, cards)
        print(f'   (advisory, does not fail: {len(adv)} cards record an internal reversal - '
              f'usually healthy)')
        print()

    if a.mode in ('budget', 'all'):
        over = check_budget(lines, a.budget)
        print(f'{len(lines) - len(over)} of {len(lines)} index line HOOKS are within {a.budget} chars.  '
              f'({len(over)} over)')
        for n, stem in over[:cap]:
            print(f'   {n:5}  {stem}')
        bad |= bool(over)
        # Claude Code loads only the first 200 lines / 25 KB of MEMORY.md and drops the rest without
        # an error; memory_index.py refuses to grow it past the same cap.
        import memory_index
        text = (root / 'MEMORY.md').read_text(encoding='utf-8')
        past = memory_index.over_cap(text)
        print(f'MEMORY.md is {text.count(chr(10))} lines, {len(text.encode("utf-8"))} bytes: '
              + (f'OVER the load cap ({", ".join(past)})' if past else
                 f'within the load cap ({memory_index.LINE_CAP} lines, {memory_index.BYTE_CAP} bytes)'))
        bad |= bool(past)
        print()

    if a.mode in ('links', 'all'):
        dead, gone, elsewhere = check_links(lines, cards, absorbed_dirs(a.absorbed))
        total = len(dead) + len(gone) + len(elsewhere)
        print(f'{len(dead)} of {total} [[wikilinks]] with no card are genuinely DEAD.')
        for stem, target in dead[:cap]:
            print(f'   {stem} -> [[{target}]]')
        if gone:
            print(f'   + {len(gone)} ABSORBED by a consolidation - the card existed and was merged '
                  f'away on purpose.')
            print('     DO NOT RECREATE them. Correct action: none, unless the successor names a '
                  'single live replacement. -v to list.')
            if a.verbose:
                for stem, target in gone:
                    print(f'       {stem} -> [[{target}]]')
        if elsewhere:
            print(f'   + {len(elsewhere)} naming a SKILL or FILE, not a card - real target, '
                  f'other namespace. -v to list.')
            if a.verbose:
                for stem, target in elsewhere:
                    print(f'       {stem} -> [[{target}]]')
        bad |= bool(dead)
        print()

    if a.mode in ('clusters', 'all'):
        cl = check_clusters(lines)
        print(f'{len(cl)} topic words appear in 3+ card names (merge candidates, not verdicts)')
        for w, members in sorted(cl.items(), key=lambda kv: -len(kv[1]))[:cap]:
            tail = ' ...' if len(members) > 4 else ''
            print(f'   {len(members):2}  {w:14} {", ".join(members[:4])}{tail}')
        print()

    if cap:
        print('(lists truncated at 15; -v for all)')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
