#!/usr/bin/env python3
"""workspace_audit.py - one mechanical end-to-end pass over the whole working set.

WHY. A model cannot reliably read 30 skill files, 100 memory cards and several root docs in one
sitting and report honestly about all of them. This script can. Every check returns N of M with the misses named,
never a bare numerator, so the output is a measurement rather than a claim.

Checks, in order of how often they tend to catch something:

  cites     every file path quoted in a doc or skill exists on disk   <- catches stale references
  secrets   credential-shaped strings in files that are read or shared
  archive   every _archive/ entry is described by a MANIFEST.md
  naming    no dated filename in a live directory (dates belong in frontmatter or _archive)
  tools     every tool in the tools folder compiles and has a --help affordance
  root      every file at the workspace root is on the sanctioned list
  xrefs     every `file.md` §N cross-reference in a skill lands on a real heading
  map       every folder the workspace map names exists, and every live folder has a line
  adostate  no doc calls an ADO item open, pending or tracked once ADO has closed or removed it
  config    ~/.claude allow rules (no cross-list duplicate or dead-path rule), no scratch project
            or empty session-env folder older than 14 days

With no check named, it runs all of them, adostate only when ado.org is set (kit_config.py); name it
to run it anyway. `secrets` prints only file:line, never the matched text.

CONFIG (agent-workbench config, key `workspace`; --root overrides workspace.root):
  root         the working folder to audit. Required.
  docs         doc files, relative to root, that cites/secrets read (default CLAUDE.md, WORKSPACE.md);
               ~/.claude/CLAUDE.md and every skill under ~/.claude/skills are always read
  map          the workspace map (default WORKSPACE.md); map_cover: folders, relative to root,
               whose entries must each appear in it (default [""], the root itself)
  root_files   files allowed at the root (default CLAUDE.md and WORKSPACE.md; macOS screenshots
               are never counted)
  skip         folder names no check walks (added to node_modules, .git, _archive, ...)
  not_ours     top-level folders the archive and naming checks leave alone (other people's
               clones, reference material), e.g. ["repos", "reference"]
  tools        the tools folder (default <root>/tools)
  memory       the memory folder (default ~/.claude/projects/<root as a slug>/memory)
  generated    file names a skill tells you to CREATE: a citation of one is not a miss
  ado_docs     files or folders, relative to root, whose .md files adostate also reads: docs others
               read, such as a repo's README, CLAUDE.md and rules (default none)

Usage
  workspace_audit.py [check ...] [--root DIR] [-v]
  workspace_audit.py --selftest

Exit codes
  0  every check passed
  1  at least one check has misses - all listed
  2  nothing to audit (root missing or not configured), so any claim about it is vacuous
  2  also when a check had nothing to inspect and no other check failed (PARTIAL)
"""

import argparse
import json
import os
import pathlib
import re
import sys
import time

# Findings quote skill text, which carries section marks and emoji. Printing those to a non-UTF-8
# console raises UnicodeEncodeError mid-report, so a real failure reads as a tool bug.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_config

HOME = pathlib.Path.home()
SKILLS = HOME / '.claude' / 'skills'
BASE_SKIP = {'.git', 'node_modules', '.astro', '__pycache__', '.venv', 'dist', '_archive', '.claude'}

# Set by configure() from config or --root.
ROOT = None
SKIP_DIRS = set(BASE_SKIP)
NOT_OURS = set()
MEMORY = None

# ONLY: the commit-time mode. A skill linter can set it to the staged .md files, and then cites
# and xrefs read those files and nothing else, so a commit is refused for a reference it breaks
# rather than one another file broke. None means the daily run over the whole working set.
ONLY = None


def cfg(key, default=None):
    v = kit_config.get('workspace', key)
    return default if v in (None, '', [], {}) else v


def configure(root=None):
    """Set ROOT and the derived settings. Raises kit_config.NotConfigured without a root."""
    global ROOT, SKIP_DIRS, NOT_OURS, MEMORY
    ROOT = pathlib.Path(os.path.expanduser(root or kit_config.need('workspace', 'root')))
    SKIP_DIRS = BASE_SKIP | set(cfg('skip', []))
    NOT_OURS = set(cfg('not_ours', []))
    mem = cfg('memory')
    slug = re.sub(r'[^A-Za-z0-9]', '-', str(ROOT.resolve()) if ROOT.exists() else str(ROOT))
    MEMORY = pathlib.Path(os.path.expanduser(mem)) if mem else HOME / '.claude' / 'projects' / slug / 'memory'


def _scope(default):
    """The files a check reads: the staged ones in commit mode, else its usual set."""
    if ONLY is None:
        return list(default)
    return [pathlib.Path(p) for p in sorted(ONLY) if str(p).endswith('.md') and os.path.exists(p)]


def _rel(md):
    try:
        return md.relative_to(SKILLS).as_posix()
    except ValueError:
        return md.name


def doc_files():
    """Doc surfaces that are read by a person or loaded into context. Not other people's repos.
    SCOPE, so a pass is not over-read: the configured root docs, the global CLAUDE.md and every
    skill. A citation in an unscanned file is not checked and not in the denominator."""
    if ONLY is not None:
        yield from _scope([])
        return
    for n in cfg('docs', ['CLAUDE.md', 'WORKSPACE.md']):
        p = ROOT / n
        if p.exists():
            yield p
    c = HOME / '.claude' / 'CLAUDE.md'
    if c.exists():
        yield c
    for p in sorted(SKILLS.rglob('*.md')) if SKILLS.exists() else []:
        # `.trash` holds deleted skill copies; `synced` holds the desktop app's own skills.
        if 'synced' not in p.parts and '.trash' not in p.parts:
            yield p


class Result:
    def __init__(self, name, ok, total, misses, note=''):
        self.name, self.ok, self.total, self.misses, self.note = name, ok, total, misses, note

    @property
    def failed(self):
        return self.ok < self.total

    @property
    def ran(self):
        """A check with no denominator did not run. 0 of 0 is not a pass: a check whose artifact
        is absent must not be counted among the clean ones, or one live check masks all skips."""
        return self.total > 0


# --------------------------------------------------------------------------- cites
# A path quoted in a doc that does not exist is the single most common rot: a file gets renamed or
# archived and every reference to it silently keeps pointing at nothing.
CITE = re.compile(r'`([^`\n]+?\.(?:md|py|astro|css|ts|js|json|html|txt|whl))`')
_NAME_INDEX = None


def name_index():
    """Every filename under the working set, indexed ONCE: walking the tree per candidate turns a
    seconds-long check into minutes. _archive is excluded from most checks but not from this index:
    a doc citing a retired file by its archive path is correct, not rot."""
    global _NAME_INDEX
    if _NAME_INDEX is None:
        idx = set()
        skip = SKIP_DIRS - {'_archive'}
        for base in (ROOT, SKILLS, MEMORY):
            if not base or not base.exists():
                continue
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in skip]
                idx.update(f.lower() for f in files)
        _NAME_INDEX = idx
    return _NAME_INDEX


# A line that cites a file to say it is gone, absent or elsewhere: correct prose, not rot. `branch`
# covers a doc correctly citing a file that lives on ANOTHER branch: this check reads the working tree,
# so it cannot see one.
_PROSE_OK = re.compile(r'(there is no|deliberately no|absent when|create on first|never create|'
                       r'no longer|do not create|does not exist|no such file|\bdead\b|\bstale\b|'
                       r'hallucinat|attachment|\bbranch\b)', re.I)


def _told_gone(line, raw):
    """True when `line` cites `raw` to say it is gone: a _PROSE_OK shape, or "the old `x`" / "the
    retired `x`" just before the citation (read there alone: a README row can be one long line)."""
    return bool(_PROSE_OK.search(line) or re.search(
        r'\b(the old|the retired|retired)\s*`?$', line[:max(line.find(raw), 0)][-40:], re.I))


def check_cites(verbose):
    """KNOWN LIMIT, stated so a green result is not over-read: resolution is by FILENAME, not path.
    A doc citing `START-HERE.md` passes as long as SOME START-HERE.md exists anywhere, even after the
    one it meant was archived. Name-only matching is deliberate: most citations are bare filenames,
    so requiring a full path would flag almost all of them. Read a pass as "the name exists
    somewhere", nothing stronger."""
    names = name_index()
    generated = {g.lower() for g in cfg('generated', [])}
    total = misses = 0
    bad = []
    for f in doc_files():
        text = f.read_text(encoding='utf-8', errors='replace')
        for raw in set(CITE.findall(text)):
            # A leading `@` is CLAUDE.md's import directive, not part of a filename. Strip it after
            # the `~/` strip so `~/@x` is not mangled.
            cand = raw.strip().lstrip('~/').lstrip('@').replace('\\', '/')
            # `public.html` is Apple's pasteboard-type namespace, not a file.
            if any(ch in cand for ch in '<>*?|') or cand.startswith(('http', '$', '{', 'public.')):
                continue
            # A skill that tells you to CREATE a file is showing a template, not citing one.
            leaf = pathlib.Path(cand).name.lower()
            if leaf in generated or cand.startswith(('src/', './')):
                continue
            total += 1
            if leaf in names:
                continue
            # A doc citing a filename to say it should NOT exist, or is absent by design, is correct.
            # Without this the check flags its own rules as rot. Every line the citation sits on is
            # read, not the first alone, so a name cited twice is judged by both lines.
            if all(_told_gone(l, raw) for l in [l for l in text.splitlines() if raw in l] or ['']):
                continue
            if 'xxx' in cand.lower() or cand.startswith('/'):
                continue
            if any((r / cand).exists() for r in (ROOT, HOME, f.parent)):
                continue
            misses += 1
            bad.append(f'{f.parent.name}/{f.name}: `{raw}`')
    return Result('cites', total - misses, total, bad,
                  'paths quoted in docs/skills that resolve to a real file')


# --------------------------------------------------------------------------- secrets
# The pattern must know every token shape the workspace can hold, or a planted `ghp_...` passes
# clean. The last alternative catches ANY long literal assigned to something named like a
# credential. Placeholders are excluded because `<`, `{`, `$` are outside the value class.
SECRET = re.compile(
    r'(gldt-[A-Za-z0-9_\-]{10,}'                       # GitLab deploy token
    r'|glpat-[A-Za-z0-9_\-]{10,}'                      # GitLab PAT
    r'|figd_[A-Za-z0-9_\-]{10,}'                       # Figma
    r'|gh[pousr]_[A-Za-z0-9]{20,}'                     # GitHub classic
    r'|github_pat_[A-Za-z0-9_]{20,}'                   # GitHub fine-grained
    r'|sk-[A-Za-z0-9]{24,}'                            # OpenAI-style
    r'|AKIA[0-9A-Z]{16}'                               # AWS access key id
    r'|xox[baprs]-[A-Za-z0-9\-]{10,}'                  # Slack
    r'|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.'  # JWT
    r'|https?://[^/\s:@]+:[^/\s@]{12,}@'               # credentials in a URL
    r'|(?:pat|token|secret|password|api[_-]?key)\s*[:=]\s*["\']?[A-Za-z0-9_\-]{24,}'
    r')', re.I)


def _upstream_owned(text, p):
    """True when the file overwrites ITSELF from an upstream clone (a `cp .../<this file> ...
    .claude/skills/` line): a literal in it is upstream's to fix, and any local edit is lost on the
    next update. Detected from the copy target, not allowlisted by name."""
    return re.search(r'cp\s+\S+/' + re.escape(p.name) + r'\s+\S*\.claude/skills/', text) is not None


def check_secrets(verbose):
    """Locations only, never the value - that rule is the whole point of this check."""
    files = list(doc_files())
    bad, upstream = [], []
    for p in files:
        text = p.read_text(encoding='utf-8', errors='replace')
        hits = [i for i, line in enumerate(text.splitlines(), 1) if SECRET.search(line)]
        if not hits:
            continue
        where = upstream if _upstream_owned(text, p) else bad
        where.extend(f'{p.parent.name}/{p.name}:{i}' for i in hits)
    note = 'doc/skill files with no credential-shaped literal'
    if upstream:
        note += f' ({len(upstream)} upstream-owned, listed but not counted: ' + ', '.join(upstream) + ')'
    # Counting a failure nobody here can fix trains the reader to ignore the check.
    return Result('secrets', len(files) - len(bad), len(files), bad, note)


# --------------------------------------------------------------------------- archive
def archive_check(root, not_ours=None):
    """(ok, total, bad). In <root>/_archive each folder carries its own MANIFEST.md. In any other
    _archive an entry counts when it is a folder with its own MANIFEST.md or the _archive's
    MANIFEST.md names it in backticks. Not walked: not_ours folders, and anything inside an archive."""
    skip = (SKIP_DIRS - {'_archive'}) | set(NOT_OURS if not_ours is None else not_ours)
    total = ok = 0
    bad = []
    top = root / '_archive'
    for d in (sorted(x for x in top.iterdir() if x.is_dir()) if top.exists() else []):
        total += 1
        if (d / 'MANIFEST.md').exists():
            ok += 1
        else:
            bad.append(f'no MANIFEST.md: _archive/{d.name}')
    for here_s, dirs, _ in os.walk(root):
        here = pathlib.Path(here_s)
        dirs[:] = sorted(x for x in dirs if x not in skip and not (here == root and x == '_archive'))
        if '_archive' not in dirs:
            continue
        dirs.remove('_archive')
        arch = here / '_archive'
        index = arch / 'MANIFEST.md'
        text = index.read_text(encoding='utf-8', errors='replace') if index.exists() else ''
        rel = arch.relative_to(root)
        unnamed = []
        for e in sorted(p for p in arch.iterdir() if not p.name.startswith('.') and p.name != 'MANIFEST.md'):
            total += 1
            if (e.is_dir() and (e / 'MANIFEST.md').exists()) or f'`{e.name}`' in text or f'`{e.name}/`' in text:
                ok += 1
            else:
                unnamed.append(e.name)
        if unnamed and not text:
            bad.append(f'no MANIFEST.md: {rel}/ ({len(unnamed)} entries)')
        else:
            bad += [f'{rel}/{n}: not named in {rel}/MANIFEST.md' for n in unnamed]
    return ok, total, bad


def check_archive(verbose):
    ok, total, bad = archive_check(ROOT)
    return Result('archive', ok, total, bad,
                  'archived entries a MANIFEST.md describes (their own, or their _archive folder\'s)')


def selftest_archive():
    """Plant every shape of archive once. A top-level-only rule sees 2 of the 8."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        d = pathlib.Path(t) / 'Work'

        def put(rel, text='x'):
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        put('_archive/a/MANIFEST.md')                      # own manifest: ok
        put('_archive/b/notes.md')                         # none: bad
        put('_archive/b/_archive/inner.md')                # inside an archive: not walked
        put('proj/_archive/MANIFEST.md', '| `x/` | ... |\n| `y.md` | ... |\n')
        put('proj/_archive/x/f.md')                        # named in the table: ok
        put('proj/_archive/x/_archive/deep.md')            # an archive inside an archive: not walked
        put('proj/_archive/y.md')                          # named: ok
        put('proj/_archive/z.md')                          # not named: bad
        put('proj/_archive/w/MANIFEST.md')                 # own manifest: ok
        put('shareouts/s/_archive/one.md')                 # no manifest at all: bad, one line
        put('shareouts/s/_archive/two.md')
        put('repos/r/_archive/q.md')                       # not ours: not walked
        put('proj/node_modules/pkg/_archive/c.md')         # a skipped folder: not walked
        ok, total, bad = archive_check(d, not_ours={'repos'})
    want = (4, 8, ['no MANIFEST.md: _archive/b', 'proj/_archive/z.md: not named in proj/_archive/MANIFEST.md',
                   'no MANIFEST.md: shareouts/s/_archive/ (2 entries)'])
    good = (ok, total, bad) == want
    print(('PASS ' if good else 'FAIL ') + f'archive: {ok} of {total}, {bad}')
    print('archive selftest', 'PASSED' if good else 'FAILED')
    return 0 if good else 1


# --------------------------------------------------------------------------- naming
DATED = re.compile(r'\d{4}-\d{2}-\d{2}|\d{8}')


def check_naming(verbose):
    total, bad = 0, []
    for p in ROOT.rglob('*.md'):
        rel = p.relative_to(ROOT).parts
        if any(part in SKIP_DIRS or part == 'archive' for part in rel):
            continue
        if len(rel) > 1 and rel[0] in NOT_OURS:      # other people's clones are not ours to rename
            continue
        total += 1
        if DATED.search(p.stem):
            bad.append(str(p.relative_to(ROOT)))
    return Result('naming', total - len(bad), total, bad, 'live .md files with no date in the filename')


# --------------------------------------------------------------------------- tools
def check_tools(verbose):
    """A tool with no discoverable interface is a tool nobody else can use, including you six weeks
    from now. Static on purpose: running `--help` on every tool takes minutes and some block on a
    missing argv; a help affordance in the source is the cheap proxy."""
    d = pathlib.Path(os.path.expanduser(cfg('tools', str(ROOT / 'tools'))))
    tools = sorted(d.glob('*.py')) if d.exists() else []
    if ONLY is not None:        # commit mode: the staged tools only
        tools = [p for p in tools if str(p) in {os.path.realpath(q) for q in ONLY} | set(map(str, ONLY))]
    if not tools:
        return Result('tools', 0, 0, [], f'no tools in {d}' if ONLY is None else 'no staged tool')
    bad, modules = [], 0
    for p in tools:
        src = p.read_text(encoding='utf-8', errors='replace')
        try:
            compile(src, str(p), 'exec')
        except SyntaxError as e:
            bad.append(f'{p.name}: SyntaxError line {e.lineno}')
            continue
        # A module other tools import and nothing runs has no command line to document.
        if '__main__' not in src and 'sys.argv' not in src:
            modules += 1
            continue
        # argparse is the common case but not the only one; look for any help affordance.
        if not any(k in src for k in ('argparse', 'ArgumentParser', '--help', 'usage:', 'Usage',
                                      'print(__doc__)')):
            bad.append(f'{p.name}: no --help affordance of any kind')
    return Result('tools', len(tools) - len(bad), len(tools), bad,
                  f'tools that compile and expose a --help interface ({modules} import-only, help not asked)')


# --------------------------------------------------------------------------- root
# macOS saves screenshots and screen recordings to the Desktop. Matched by the names macOS gives
# them, so a hand-named file still counts.
ROOT_CAPTURE = re.compile(r'(Screenshot|Screen Recording) \d{4}-\d\d-\d\d at .+\.(png|mov)')


def check_root(verbose):
    allowed = set(cfg('root_files', ['CLAUDE.md', 'WORKSPACE.md']))
    files = [p for p in ROOT.iterdir() if p.is_file() and not p.name.startswith('.')]
    captures = [p for p in files if ROOT_CAPTURE.fullmatch(p.name)]
    files = [p for p in files if p not in captures]
    bad = [f'{p.name} does not belong at root' for p in files if p.name not in allowed]
    return Result('root', len(files) - len(bad), len(files), bad,
                  f'root files on the sanctioned list ({len(captures)} screen captures not counted)')


# --------------------------------------------------------------------------- xrefs
def check_xrefs(verbose):
    """A `file.md` §N citation must resolve to a heading in that file, "## N." or "## §N.". A section
    renumbered once leaves every copy of the citation pointing nowhere, and nothing notices, because a
    cross-reference is prose and prose is not checked."""
    if not SKILLS.exists():
        return Result('xrefs', 0, 0, [], 'no skills dir')
    heads = {}

    def headings(p):
        if p not in heads:
            try:
                heads[p] = set(re.findall(r'^#+\s*§?\s*(\d+(?:\.\d+)*)\.?\s', p.read_text(
                    encoding='utf-8', errors='replace'), re.M))
            except OSError:
                heads[p] = set()
        return heads[p]

    bad, total = [], 0
    for md in _scope(SKILLS.rglob('*.md')):
        if 'synced' in md.parts or '.trash' in md.parts:
            continue
        text = md.read_text(encoding='utf-8', errors='replace')
        rel = _rel(md)
        for m in re.finditer(r'`([A-Za-z0-9_./-]+\.md)`\s*§\s*(\d+(?:\.\d+)*)', text):
            target, sec = m.group(1), m.group(2)
            cand = [SKILLS / target] + list(SKILLS.rglob(target.split('/')[-1]))
            hit = next((c for c in cand if c.exists()), None)
            if not hit:
                continue
            total += 1
            if sec not in headings(hit):
                bad.append(f'{rel} cites {target} §{sec}; that file has no such heading')
    return Result('xrefs', total - len(bad), total, bad,
                  'section cross-references that resolve to a real heading')


# --------------------------------------------------------------------------- map
# The workspace map (WORKSPACE.md) names each folder. `cites` reads it too, but by file NAME and only
# for names with an extension, so a folder that is gone, or a live folder with no line, passes.
#
# Forward: each folder the map names (a backticked token ending in `/`) and each file or folder a
# bullet opens with must exist. A relative token resolves against the folder its parent bullet opens
# with, the root, and any folder the same bullet names (and that folder's parents up to the root).
# "`x/` is empty" must be an empty folder, "`x/` is gone" must not exist, and "the old `x/`" is
# history and is not checked.
# Reverse: each entry in each map_cover folder must appear in some backticked token.
MAP_IGNORE = {'.DS_Store', '.localized', '__pycache__', '.git', 'Icon\r'}
MAP_EXT = re.compile(r'\.(md|py|html|jsonc?|tsv|txt|m?js|ya?ml|xlsx|pdf)$', re.I)
TICK = re.compile(r'`([^`\n]+)`')


def _map_bullets(text):
    """[line number, depth, text] per bullet, continuation lines joined; other lines have depth -1."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r'^(\s*)- (.*)$', line)
        if m:
            out.append([n, len(m.group(1)), m.group(2)])
        elif line.strip() and out and out[-1][1] >= 0 and line.startswith(' ') and not line.lstrip().startswith('#'):
            out[-1][2] += ' ' + line.strip()
        elif line.strip():
            out.append([n, -1, line.strip()])
    return out


def map_check(doc, root, home, cover=('',)):
    """(ok, total, misses) for the map at `doc` against the tree at `root`."""
    text = doc.read_text(encoding='utf-8', errors='replace')
    total, bad, stack = 0, [], []          # stack: (depth, folder the bullet opens with)

    def resolve(tok, bases):
        if tok.startswith('~/'):
            return [home / tok[2:]]
        if tok.split('/')[0] == root.name:
            return [root.parent / tok]
        return [b / tok for b in bases]

    for n, depth, body in _map_bullets(text):
        if depth < 0:
            stack = []
        else:
            while stack and stack[-1][0] >= depth:
                stack.pop()
        parent = next((d for _, d in reversed(stack) if d), root)
        head_part = body.split(' - ', 1)[0] if body.startswith('`') else ''
        heads = set(TICK.findall(head_part)) if ' - ' in body else set(TICK.findall(head_part)[:1])
        toks = [(m.group(1), m.start(), m.end()) for m in TICK.finditer(body)]
        named = []                          # folders this bullet names, to resolve its other tokens
        for tok, _, _ in toks:
            if not any(c in tok for c in '<>*|:$#'):
                for p in resolve(tok, [parent, root]):
                    if p.is_dir():
                        named += [p] + [a for a in p.parents if root in a.parents or a == root]
        bases = [parent, root] + named
        opens = None
        for tok, s, e in toks:
            if any(c in tok for c in '<>*|:$#') or tok.startswith(('http', '--')):
                continue
            if ' ' in tok and not tok.endswith('/'):
                continue
            is_dir = tok.endswith('/')
            if not (is_dir or (tok in heads and (MAP_EXT.search(tok) or tok.startswith('.')))):
                continue
            before, after = body[:s], body[e:]
            if re.search(r'\bthe old\s*$', before, re.I):
                continue
            here = resolve(tok, bases)
            total += 1
            found = next((p for p in here if p.exists()), None)
            if re.match(r'\s*(is gone|does not exist|no longer exists)', after):
                if found:
                    bad.append(f'line {n}: `{tok}` is said to be gone and exists at {found}')
            elif not found:
                bad.append(f'line {n}: `{tok}` does not exist')
            elif re.match(r'\s*is empty', after) and found.is_dir() and any(found.iterdir()):
                bad.append(f'line {n}: `{tok}` is said to be empty and is not')
            if tok in heads and is_dir and found and found.is_dir() and opens is None:
                opens = found
        if depth >= 0:
            stack.append((depth, opens))

    said, shapes = set(), []
    for tok in TICK.findall(text):
        t = tok.strip().rstrip('/')
        said.add(t)
        said.update(t.split('/'))
        # One line for a family a tool writes: `view@<worktree>.html` names each of them. A shape
        # needs 4 letters of its own: a bare `<name>` (as in `wt/<name>`) would name everything.
        last = t.split('/')[-1]
        parts = re.split(r'<[^<>]+>', last)
        if len(parts) > 1 and len(re.sub(r'[^A-Za-z]', '', ''.join(parts))) >= 4:
            shapes.append(re.compile('.+'.join(map(re.escape, parts))))
    for sub in cover:
        folder = root / sub if sub else root
        if not folder.is_dir():
            bad.append(f'`{sub}/` does not exist, so its entries were not checked')
            continue
        for p in sorted(folder.iterdir()):
            if p.name in MAP_IGNORE or (not sub and ROOT_CAPTURE.fullmatch(p.name)):
                continue
            total += 1
            if p.name not in said and not any(s.fullmatch(p.name) for s in shapes):
                bad.append(f'{(sub + "/") if sub else ""}{p.name}{"/" if p.is_dir() else ""} has no line in the map')
    return total - len(bad), total, bad


def check_map(verbose):
    doc = ROOT / cfg('map', 'WORKSPACE.md')
    if not doc.exists():
        return Result('map', 0, 0, [], f'no {doc.name}')
    ok, total, bad = map_check(doc, ROOT, HOME, tuple(cfg('map_cover', [''])))
    return Result('map', ok, total, bad, f'{doc.name} folders that exist, and covered entries the map names')


def selftest_map():
    """Plant a tree and a map, then break each rule once. Each break must fail by that rule."""
    import tempfile
    results = []
    with tempfile.TemporaryDirectory() as t:
        t = pathlib.Path(t)
        root, home = t / 'Work', t / 'home'
        for d in ('proj/reviews', 'proj/empty', 'repos/site/app/public', 'tools'):
            (root / d).mkdir(parents=True)
        (home / '.claude').mkdir(parents=True)
        (root / 'proj' / 'notes.md').write_text('x')
        (root / 'CLAUDE.md').write_text('x')
        good = ('# Map\n\n- `CLAUDE.md` - rules. `WORKSPACE.md` - this map.\n- `proj/` - work.\n'
                '  - `reviews/` - drafts.\n  - `notes.md` - notes. `empty/` is empty.\n- `repos/` - clones.\n'
                '  - `site/` - the site. In it the project is `app/public/`.\n- `tools/` - tools.\n'
                '- `~/.claude/tools/` is gone. The old `kit/` folder moved.\n')
        doc = root / 'WORKSPACE.md'
        cover = ('', 'proj', 'repos')

        def run(label, text, expect):
            doc.write_text(text)
            ok, total, bad = map_check(doc, root, home, cover)
            fired = any(expect in b for b in bad) if expect else not bad
            results.append((fired, label, f'{ok} of {total}', bad[:2]))

        run('known-good map passes', good, None)
        run('a folder that does not exist fails', good + '- `ghost/` - not on disk.\n', '`ghost/` does not exist')
        (root / 'proj' / 'empty' / 'f').write_text('x')
        run('"is empty" on a folder with a file fails', good, 'said to be empty')
        (root / 'proj' / 'empty' / 'f').unlink()
        (home / '.claude' / 'tools').mkdir()
        run('"is gone" on a folder that exists fails', good, 'said to be gone')
        (home / '.claude' / 'tools').rmdir()
        (root / 'repos' / 'unmapped-worktree').mkdir()
        run('a live folder with no line fails', good, 'unmapped-worktree/ has no line')
        (root / 'repos' / 'unmapped-worktree').rmdir()
        (root / 'proj' / 'view@wt1.html').write_text('x')
        run('a file a pattern line names passes', good + '- `proj/view@<worktree>.html` - views.\n', None)
        run('a file the pattern does not name fails', good + '- `proj/other@<worktree>.html` - views.\n',
            'view@wt1.html has no line')
        run('a bare placeholder names nothing', good + '- each worktree is on `wt/<name>`.\n',
            'view@wt1.html has no line')
        (root / 'proj' / 'view@wt1.html').unlink()
        run('known-good map passes again', good, None)
    for fired, label, frac, bad in results:
        print(('PASS ' if fired else 'FAIL ') + f'{label} ({frac}) {bad}')
    ok = all(r[0] for r in results)
    print('map selftest', 'PASSED' if ok else 'FAILED')
    return 0 if ok else 1


# --------------------------------------------------------------------------- config
# The Claude Code config under ~/.claude drifts with nothing watching it: each "always allow" click
# adds a literal rule, rules outlive the files they name, a project-local list copies user rules,
# and scratch project folders and empty session-env folders pile up. This check reads the allow
# lists only, never `env`, and prints a rule cut to 80 characters with long tokens masked.
# Blind spot: scheduled-task registration lives in the app, not on disk.
STALE_DAYS = 14


def _mask(rule):
    return re.sub(r'[A-Za-z0-9_\-]{32,}', '<...>', rule)[:80]


def settings_files(home, root):
    return {'user': home / '.claude' / 'settings.json',
            'local': root / '.claude' / 'settings.local.json'}


def allow_rule_flags(home, root):
    """({label: allow list, or None when the file is not JSON}, [(label, rule, why)]) for the allow
    rules that do nothing: a local copy of a user rule, or one naming a path that is gone.
    upkeep.py's settings step prunes by this same list."""
    import json
    # Paths under the home being audited; `~/` is expanded first. Other absolute paths (/tmp/x, a
    # URL's /path) are not checked, so a dead /tmp rule passes.
    abs_path = re.compile(re.escape(str(home)) + r'/[^\s"\'()*]+')
    lists = {}
    for label, p in settings_files(home, root).items():
        if p.exists():
            try:
                lists[label] = json.loads(p.read_text(encoding='utf-8')).get('permissions', {}).get('allow', [])
            except ValueError:
                lists[label] = None
    flags = []
    for label, rules in lists.items():
        for r in rules or []:
            if label == 'local' and r in (lists.get('user') or []):
                flags.append((label, r, f'duplicates a user rule {_mask(r)}'))
            else:
                expanded = r.replace('~/', str(home) + '/').replace('//' + str(home).lstrip('/'), str(home))
                dead = next((m for m in abs_path.findall(expanded)
                             if not pathlib.Path(m.split('*')[0].rstrip('/') or '/').exists()), None)
                if dead:
                    flags.append((label, r, f'rule names a path that does not exist ({dead[:60]})'))
    return lists, flags


def config_check(home, root, now=None):
    import time
    now = now or time.time()
    claude = home / '.claude'
    lists, flags = allow_rule_flags(home, root)
    total = sum(1 if rules is None else len(rules) for rules in lists.values())
    bad = [f'{label} settings file is not valid JSON' for label, rules in lists.items() if rules is None]
    bad += [f'{label}: {why}' for label, _, why in flags]
    cutoff = now - STALE_DAYS * 86400
    projects, senv = claude / 'projects', claude / 'session-env'
    for d in (sorted(projects.glob('-private-tmp-*')) + sorted(projects.glob('-tmp-*')) if projects.exists() else []):
        total += 1
        if d.stat().st_mtime < cutoff:
            bad.append(f'scratch project folder older than {STALE_DAYS} days: projects/{d.name[:60]}')
    stale_empty = 0
    for d in (senv.iterdir() if senv.exists() else []):
        if d.is_dir():
            total += 1
            if not any(d.iterdir()) and d.stat().st_mtime < cutoff:
                stale_empty += 1
    if stale_empty:
        bad.append(f'{stale_empty} empty session-env folder(s) older than {STALE_DAYS} days')
    return total - len(bad) - max(stale_empty - 1, 0), total, bad


def check_config(verbose):
    ok, total, bad = config_check(HOME, ROOT)
    return Result('config', ok, total, bad, 'allow rules and scratch folders under ~/.claude')


def selftest_config():
    """Plant a ~/.claude, then break each config rule once. Each break must fail by that rule."""
    import json
    import tempfile
    import time
    results = []
    with tempfile.TemporaryDirectory() as t:
        t = pathlib.Path(t)
        home, root = t / 'home', t / 'home' / 'Work'
        claude = home / '.claude'
        for d in (claude / 'projects', claude / 'session-env', root / '.claude'):
            d.mkdir(parents=True)
        real = home / 'real.txt'
        real.write_text('x')

        def write(user_rules, local_rules):
            (claude / 'settings.json').write_text(json.dumps({'permissions': {'allow': user_rules}}))
            (root / '.claude' / 'settings.local.json').write_text(json.dumps({'permissions': {'allow': local_rules}}))

        def run(label, expect):
            ok, total, bad = config_check(home, root)
            fired = any(expect in b for b in bad) if expect else not bad
            results.append((fired, label, f'{ok} of {total}', bad[:2]))

        good_user, good_local = ['Bash(ls *)', f'Read({real})'], ['Bash(wc *)']
        write(good_user, good_local)
        run('known-good config passes', None)
        write(good_user, good_local + ['Bash(ls *)'])
        run('a local rule duplicating a user rule fails', 'duplicates a user rule')
        write(good_user + [f'Bash(python3 {home}/gone.py *)'], good_local)
        run('a rule for a missing file fails', 'does not exist')
        write(good_user, good_local)
        old = time.time() - (STALE_DAYS + 1) * 86400
        scratch = claude / 'projects' / '-private-tmp-bench'
        scratch.mkdir()
        os.utime(scratch, (old, old))
        run('an old scratch project folder fails', 'scratch project folder')
        scratch.rmdir()
        empty = claude / 'session-env' / 'abc'
        empty.mkdir()
        os.utime(empty, (old, old))
        run('an old empty session-env folder fails', 'empty session-env')
        empty.rmdir()
        (claude / 'projects' / '-private-tmp-fresh').mkdir()
        run('a fresh scratch folder passes', None)
    for fired, label, frac, bad in results:
        print(('PASS ' if fired else 'FAIL ') + f'{label} ({frac}) {bad}')
    ok = all(r[0] for r in results)
    print('config selftest', 'PASSED' if ok else 'FAILED')
    return 0 if ok else 1


def selftest_secrets():
    """A planted token of each common shape is caught; a placeholder is not."""
    planted = ['ghp_' + 'A' * 36, 'glpat-' + 'b' * 20, 'figd_' + 'c' * 30, 'AKIA' + 'D' * 16,
               'token = "' + 'e' * 30 + '"']
    clean = ['token = "<your token>"', 'export FIGMA_TOKEN=$FIGMA_TOKEN', 'see `ghp_...` shapes']
    good = all(SECRET.search(s) for s in planted) and not any(SECRET.search(s) for s in clean)
    print(('PASS ' if good else 'FAIL ') + f'secrets: {len(planted)} planted caught, {len(clean)} placeholders pass')
    return 0 if good else 1


# --------------------------------------------------------------------------- adostate
# A doc line that names an ADO item as open, pending or tracked after ADO closed or removed it goes
# false without anyone editing it: a handoff doc keeps saying a change is coming, "tracked as #N", long
# after #N closed without it. The states come from ado_rest.py's cache, as fresh as its last pull: an
# item closed since reads open (a miss, never a false alarm), and the note gives the cache's age. A line
# that says closed, done or removed next to the number is already telling the truth and is left alone.
ADO_WORD = (r"(?:still |stays |remains? |is )?(?:open|pending|upcoming|in progress|tracked(?: as| in| by| on)?"
            r"|waiting|owed|to do|not yet (?:done|fixed|closed)|being (?:revised|worked|fixed)|blocked on)")
ADO_NUM = r"(?:\*\*)?(?:ADO )?(?:#|_workitems/edit/)(\d{3,5})(?:\*\*)?"
ADO_CLAIM = re.compile(rf"\b{ADO_WORD}\b[^.;|]{{0,25}}?{ADO_NUM}|{ADO_NUM}[^.;|]{{0,25}}?\b{ADO_WORD}\b", re.I)
ADO_SHUT = re.compile(r"\b(closed|resolved|done|removed|shipped|completed|moot|won't fix)\b", re.I)


def adostate_files():
    """doc_files(), plus the .md files under each workspace.ado_docs entry (a file or a folder,
    relative to root): docs others read that cites and secrets don't."""
    yield from doc_files()
    if ONLY is not None:
        return
    for rel in cfg('ado_docs', []):
        p = ROOT / os.path.expanduser(rel)
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from sorted(q for q in p.rglob('*.md') if 'node_modules' not in q.parts)


def adostate_findings(files, states):
    """(claims, misses): lines that give an ADO item a state, and those whose item is closed or removed."""
    claims, misses = 0, []
    for f in files:
        try:
            lines = pathlib.Path(f).read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines, 1):
            for m in ADO_CLAIM.finditer(ln):
                n = int(m.group(1) or m.group(2))
                claims += 1
                st, when = states.get(n, (None, ''))
                if st in ('Closed', 'Removed') and not ADO_SHUT.search(ln[max(0, m.start() - 80):m.end() + 80]):
                    where = str(f).replace(str(HOME), '~')
                    misses.append(f"{where}:{i} calls #{n} {m.group(0).strip()!r}; ADO has it {st} since {when}")
                    break
    return claims, misses


def check_adostate(verbose):
    try:
        import ado_rest
        with open(ado_rest.CACHE, encoding='utf-8') as fh:
            cache = json.load(fh)
    except Exception as e:      # no cache, no config: the check did not run, never "nothing stale"
        return Result('adostate', 0, 0, [], f'DID NOT RUN: no ADO cache ({type(e).__name__}); run ado_rest.py pull')
    states = {int(k): (v['fields'].get('System.State'),
                       (v['fields'].get('Microsoft.VSTS.Common.StateChangeDate') or '')[:10])
              for k, v in (cache.get('items') or {}).items()}
    claims, misses = adostate_findings(adostate_files(), states)
    age = (time.time() - cache.get('pulled', 0)) / 3600
    return Result('adostate', claims - len(misses), claims, misses,
                  f'lines that give an ADO item a state, read against the cache pulled {age:.0f} h ago')


def selftest_adostate():
    """Planted lines: three ways of calling a closed item open are each caught; a line that says
    closed, an open item, an unrelated "open" and an item ADO doesn't hold are not."""
    states = {1101: ('Closed', '2026-08-29'), 1102: ('Closed', '2026-08-31'), 2201: ('Active', '2026-10-22')}
    import tempfile as _t
    with _t.TemporaryDirectory() as d:
        bad, good = pathlib.Path(d, 'bad.md'), pathlib.Path(d, 'good.md')
        bad.write_text("- **To do (open, tracked - ADO #1101)**: captions.\n"
                       "and ADO #1101 stays open until someone closes it.\n"
                       "a one-line change, tracked as **ADO #1102**. More text.\n", encoding='utf-8')
        good.write_text("tracked as **ADO #1102** (closed without the change)\n"
                        "ADO #2201 is still open.\n"
                        "Open them: requirements are often a screenshot (#1101 holds 67 MB).\n"
                        "tracked in #9999, which ADO doesn't hold.\n"
                        'Open, unrelated: #1101 "Cross-page // Token cleanup and more" was closed.\n',
                        encoding='utf-8')
        cb, mb = adostate_findings([bad], states)
        cg, mg = adostate_findings([good], states)
    results = [('adostate: three ways of calling a closed item open are each caught', len(mb) == 3 and cb == 3),
               ('adostate: a line saying closed, an open item, a stray "open" and an unknown item are not',
                mg == [] and cg == 4)]
    for name, ok in results:
        print(('PASS ' if ok else 'FAIL ') + name)
    return 0 if all(r[1] for r in results) else 1


CHECKS = {'cites': check_cites, 'secrets': check_secrets, 'archive': check_archive,
          'naming': check_naming, 'tools': check_tools, 'root': check_root,
          'xrefs': check_xrefs, 'map': check_map, 'adostate': check_adostate, 'config': check_config}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # No `default=`: argparse validates a list default against `choices` as ONE value, so a bare run
    # would die before any check ran. An empty list means all.
    ap.add_argument('checks', nargs='*', choices=list(CHECKS))
    ap.add_argument('--root', help='the working folder to audit (default: workspace.root in config)')
    ap.add_argument('-v', '--verbose', action='store_true')
    ap.add_argument('--selftest', action='store_true',
                    help='plant a tree and break each map, config, archive, secrets and adostate rule once')
    a = ap.parse_args()
    if a.selftest:
        global SKIP_DIRS
        SKIP_DIRS = set(BASE_SKIP)
        return max(selftest_map(), selftest_config(), selftest_archive(), selftest_secrets(), selftest_adostate())
    try:
        configure(a.root)
    except kit_config.NotConfigured as e:
        print(f'workspace_audit.py: {e}', file=sys.stderr)
        return 2
    # Without an ADO org the adostate check has nothing to read, so a bare run leaves it out rather
    # than report every run PARTIAL.
    checks = a.checks or [c for c in CHECKS if c != 'adostate' or kit_config.get('ado', 'org')]
    if not ROOT.exists():
        print(f'no workspace at {ROOT}', file=sys.stderr)
        return 2

    failed = False
    width = max(len(c) for c in checks)
    results = []
    for name in checks:
        r = CHECKS[name](a.verbose)
        results.append(r)
        # A check that did not run prints SKIP, never `ok`: the per-line mark is what a reader scans.
        mark = 'ok  ' if not r.failed else 'FAIL'
        if not r.ran:
            mark = 'SKIP'
        print(f'{mark} {r.name:<{width}}  {r.ok} of {r.total}   {r.note}')
        cap = None if a.verbose else 10
        for m in r.misses[:cap]:
            print(f'       - {m}')
        if r.misses and cap and len(r.misses) > cap:
            print(f'       ... {len(r.misses) - cap} more (-v)')
        failed |= r.failed

    skipped = [r for r in results if not r.ran]
    ran = [r for r in results if r.ran]
    clean = sum(1 for r in ran if not r.failed)
    print(f'\n{clean} of {len(ran)} checks clean'
          + (f'  ({len(skipped)} of {len(results)} DID NOT RUN)' if skipped else ''))
    for r in skipped:
        print(f'  DID NOT RUN  {r.name:<12} {r.note}')
    if not ran:
        print('nothing was actually inspected - treat this run as vacuous', file=sys.stderr)
        return 2
    if skipped:
        # Partial coverage is not a clean workspace.
        print(f'PARTIAL: {len(skipped)} check(s) had no artifact to inspect. '
              'Coverage is incomplete; this is not an all-clear.', file=sys.stderr)
        return 2 if not failed else 1
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
