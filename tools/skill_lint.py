"""skill_lint.py - lint every SKILL.md under the skills folder.

WHY. Without a lint, a lesson gets appended to whichever SKILL.md was open when it was learned, so
the same lesson ends up in several files as PARAPHRASES, which a text diff can't dedupe and which
drift apart silently until two of them contradict each other.

Checks (a FAIL exits 1; a WARN or a report line does not):
  1. BUDGET + FRONTMATTER - FAIL when a SKILL.md body is over 5k tokens or 500 lines (the Agent
                   Skills guidance), when name or description is missing, or when the description is
                   a bare YAML scalar containing a colon. CRLF line endings WARN.
  2. VERBATIM    - WARN: any 8-gram shared by two SKILL.md files = copy-paste duplication.
  3. TOPIC       - FAIL: a curated registry of facts that have ONE home, the config key
                   skill_lint.topics (kit_config.py): {"<topic>": ["<owner file, relative to the
                   skills folder>", "<regex signature>"]}. Any other SKILL.md matching a topic's
                   signature, in a section that does not cite the owner, is a paraphrase forming;
                   that is the failure mode this exists for, and no generic text measure catches it.
                   With no registry, nothing is checked and the line says so.
  4. TUNABLES    - FAIL: a value a tool's cmd_ default owns (a `def cmd_<name>(x=12)` in a .py beside
                   this one), restated in prose next to that command's name.
  5. REFERENCE FILE SIZES - report only.
  6. INSTRUCTION BUDGETS - FAIL when a skill description passes 80 words, an agent description 50,
                   an agent body 600, an agent carries an HTML comment, or everything that loads
                   at startup (rules files, the memory index, every description) passes the startup
                   ratchet (config key skill_lint.startup_max, default 6200). Set it at your current
                   total and only ever lower it: a fixed budget is what stops rules from only ever
                   being added.
  7. REFERENCES  - with --only: FAIL when a line a staged .md adds cites a file or a `file.md` §N that
                   doesn't exist (workspace_audit.py's cites and xrefs), or a staged .py won't
                   compile or has no --help (its tools check). Files resolve against the config key
                   workspace.root, else the project folder.
  8. NEW SKILLS  - with --only: FAIL when the commit adds a skills/<name>/SKILL.md and no eval suite
                   has a passing receipt for that folder's sha, nor a unit.json that accepts that sha
                   without one (agent_gate_stale.py's suites folder, config key agent_gate.suites; not
                   run when that is unset). agent_gate_stale.py lists the rest.
  9. TAGS        - with --only: FAIL when a staged skill, agent or CLAUDE.md file adds a line with a
                   dated attribution (`(the user, 2026-10-17)`, `user 10/24:`), a star mark (U+2605),
                   or capitals used for emphasis (`NEVER`, `ALWAYS`, `MUST`, `MANDATORY`, `CRITICAL`,
                   `IMPORTANT`); or a staged .py in a folder named tools adds one to its module
                   docstring (its --help), or a tools/README.md to a row. There a date beside a
                   past-tense story verb (asked, said, ruled) counts too, and a section name opening a
                   line (`NEVER DOWNLOADED.`) is not emphasis. Fenced and inline code don't count;
                   lines already there pass until someone edits them.
 10. WORKSPACE MAP - report only, full run: the marked and story lines of the workspace map
                   (config workspace.root and workspace.map, default WORKSPACE.md), which a working
                   folder outside any repo keeps out of every commit check.

Usage:  python tools/skill_lint.py [skills_dir]      exit 1 on any FAIL (default ~/.claude/skills)
        python tools/skill_lint.py --selftest        planted skills and lines: the startup total
                                                     counts a project skill and skips one set
                                                     name-only; topic ownership; the tags

What loads at startup is read from: ~/.claude/agents (SKILL_LINT_AGENTS), the skills folder and the
current project's .claude/skills (SKILL_LINT_PROJECT_SKILLS), each less any skill the project's
.claude/settings*.json skillOverrides set name-only or off (SKILL_LINT_SETTINGS_DIR), and ~/.claude/CLAUDE.md, <project>/CLAUDE.md and the
project's memory index MEMORY.md (SKILL_LINT_ALWAYS, a path list). The project is $CLAUDE_PROJECT_DIR,
else the folder you run in.
"""
import os, re, sys, glob, itertools, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import cc_logs  # noqa: E402  cc_logs.redact: an 8-gram check 2 prints can hold a clone URL's token

# A console that is not UTF-8 would die mid-report with a UnicodeEncodeError on a glyph in a skill,
# which reads like a lint failure and is not one.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Answer -h/--help with this module's own docstring, before the first positional arg is read as a
# skills path.
if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
    print((__doc__ or "").strip() or "No docstring.")
    print("\nusage: skill_lint.py [<skills-dir>] [--only <path>...]   (default: ~/.claude/skills)")
    print("  --only  fail only on what the given paths own (a pre-commit passes its staged paths)")
    raise SystemExit(0)

# --only <paths>: the pre-commit mode. Everything is still measured and printed, but only a failure
# naming a skill, agent or always-loaded file among these paths sets the exit code.
_args = sys.argv[1:]
SELFTEST = "--selftest" in _args
_args = [a for a in _args if a != "--selftest"]
ONLY = _args[_args.index("--only") + 1:] if "--only" in _args else None
_args = _args[:_args.index("--only")] if "--only" in _args else _args
sys.argv[1:] = _args

SKILLS = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/.claude/skills")
# Every directory holding a SKILL.md is in scope, derived from disk: a hardcoded list lets new skills
# ship unlinted.
SCOPE = tuple(sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(SKILLS, "*", "SKILL.md"))))
MAX_TOK, MAX_LINES = 5000, 500


def _config(*keys, default=None):
    try:
        import kit_config
        return kit_config.get(*keys, default=default)
    except ImportError:
        return default


PROJECT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _memory_index():
    try:
        import cc_logs
        return os.path.join(cc_logs.memory_dir(cwd=PROJECT), "MEMORY.md")
    except (ImportError, AttributeError):
        return os.path.join(os.path.expanduser("~"), ".claude", "projects",
                            re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(PROJECT)), "memory", "MEMORY.md")


# Section 6's limits. Paths can be pointed elsewhere for a planted test.
SKILL_DESC_MAX, AGENT_DESC_MAX, AGENT_BODY_MAX = 80, 50, 600
STARTUP_MAX = int(_config("skill_lint", "startup_max", default=6200) or 6200)
AGENTS = os.environ.get("SKILL_LINT_AGENTS", os.path.expanduser("~/.claude/agents"))
# The project's own skills load in every session there too, so their descriptions count toward the
# startup total. A repo's .claude/skills deeper down loads only when work enters that repo, so it is
# not read.
PROJECT_SKILLS = os.environ.get("SKILL_LINT_PROJECT_SKILLS", os.path.join(PROJECT, ".claude", "skills"))
SETTINGS_DIR = os.environ.get("SKILL_LINT_SETTINGS_DIR", PROJECT)
HIDDEN = ("name-only", "off", "user-invocable-only")
ALWAYS = os.environ.get("SKILL_LINT_ALWAYS", os.pathsep.join([
    os.path.expanduser("~/.claude/CLAUDE.md"),
    os.path.join(PROJECT, "CLAUDE.md"),
    _memory_index()])).split(os.pathsep)

# fact -> (owner file, signature regex). Owner is where the fact is ALLOWED to live in prose.
TOPICS = {str(k): (v[0], v[1]) for k, v in (_config("skill_lint", "topics", default={}) or {}).items()
          if isinstance(v, (list, tuple)) and len(v) == 2}


def read(p):
    return open(p, encoding="utf-8", errors="replace").read()


def body_of(t):
    m = re.match(r"^---\n.*?\n---\n", t, re.S)
    return t[m.end():] if m else t


def owners_of(paths):
    """Regexes matching the failures that the given repo paths own."""
    rxs = []
    for p in paths:
        parts = p.replace(os.sep, "/").split("/")
        if "skills" in parts and parts.index("skills") + 1 < len(parts):
            n = parts[parts.index("skills") + 1]
            rxs.append(rf"(^|[\s'(,]){re.escape(n)}([:,)\s/]|$)")
        elif "agents" in parts and p.endswith(".md"):
            rxs.append(rf"^agent {re.escape(parts[-1][:-3])}:")
        elif parts[-1] in ("CLAUDE.md", "MEMORY.md") or "memory" in parts:
            rxs.append(r"^startup-loaded")
        if ("skills" in parts or "agents" in parts) and p.endswith(".md"):
            rxs.append(r"^startup-loaded")   # a description counts toward the startup total
    return rxs


def installed_descriptions():
    """(plugin words, synced words shown, synced skills hidden): the descriptions of installed plugin
    skills and of skills synced from claude.ai, which load in every session too. A synced skill set to
    name-only or off in the project's settings loads no description. skillOverrides doesn't reach
    plugin skills. Blind to: overrides in ~/.claude/settings.json, which this never reads, and a plugin
    installed but turned off."""
    import json
    overrides = skill_overrides()
    plugin = 0
    try:
        installed = json.load(open(os.path.expanduser("~/.claude/plugins/installed_plugins.json")))
        for entries in (installed.get("plugins") or {}).values():
            for e in entries if isinstance(entries, list) else [entries]:
                for s in glob.glob(os.path.join(e.get("installPath", ""), "skills", "*", "SKILL.md")):
                    plugin += desc_words(read(s))
    except (OSError, ValueError):
        pass
    synced = hidden = 0
    for s in glob.glob(os.path.join(SKILLS, "synced", "*", "*", "SKILL.md")):
        if overrides.get(os.path.basename(os.path.dirname(s))) in HIDDEN:
            hidden += 1
        else:
            synced += desc_words(read(s))
    return plugin, synced, hidden


def skill_overrides():
    """skillOverrides from the project's settings files, keyed by bare skill name."""
    import json
    overrides = {}
    for f in (".claude/settings.json", ".claude/settings.local.json"):
        try:
            overrides.update(json.load(open(os.path.join(SETTINGS_DIR, f), encoding="utf-8")).get("skillOverrides") or {})
        except (OSError, ValueError):
            pass
    return overrides


def project_skills():
    """{name: SKILL.md path} for each project skill whose description loads (not set name-only or
    off). The project folder is not read twice when it is the skills folder being linted."""
    if os.path.realpath(PROJECT_SKILLS) == os.path.realpath(SKILLS):
        return {}
    overrides = skill_overrides()
    found = {os.path.basename(os.path.dirname(p)): p
             for p in sorted(glob.glob(os.path.join(PROJECT_SKILLS, "*", "SKILL.md")))}
    return {n: p for n, p in found.items() if overrides.get(n) not in HIDDEN}


def desc_words(t):
    """Words in the frontmatter description, quoted or bare, one line or folded."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", t, re.S)
    d = re.search(r"^description:\s*(.*?)(?=^[\w-]+:|\Z)", m.group(1) if m else "", re.M | re.S)
    return len(d.group(1).split()) if d else 0


def _restates(body, rx, owner):
    """True only if the topic appears in a section that does NOT cite its owner.

    Naming a topic while pointing at its home is the whole design ("states: refs/pull.md §3").
    Naming it and then explaining it again is the drift this lints for.
    """
    secs = re.split(r"^(?=#{2,3} )", body, flags=re.M)
    cites = (os.path.basename(owner), os.path.dirname(owner) + "/") if os.path.dirname(owner) else (os.path.basename(owner),)
    return any(re.search(rx, s, re.I) and not any(c in s for c in cites) for s in secs)


def staged_references(staged):
    """7. REFERENCES, commit mode only: workspace_audit.py's cites and xrefs checks, run on the staged
    .md files alone, and its tools check on the staged .py files. The daily run reads the whole
    working set but reports later; here a file can't be committed with a path, a tool or a section
    that isn't there. A reference fails only when it is in a line this commit adds: a doc may name a
    retired file on purpose, and judging the whole file would refuse every edit to it. Each check's
    own blind spots hold: cites matches by file name, not full path."""
    md = [os.path.abspath(p) for p in staged if p.endswith(".md")]
    py = [os.path.abspath(p) for p in staged if p.endswith(".py")]
    print("\n7. REFERENCES (staged .md files: cited files and § sections exist;"
          " staged .py files: compile, and a runnable one answers --help)")
    if not md and not py:
        print("   (no staged .md or .py file)")
        return []
    import workspace_audit as wa
    wa.configure(_config("workspace", "root") or PROJECT)
    wa.ONLY = set(md + py)
    out = []
    added = "\n".join(added_lines(p) for p in md)
    checks = ((wa.check_cites, wa.check_xrefs) if md else ()) + ((wa.check_tools,) if py else ())
    for check in checks:
        r = check(False)
        if not r.total:
            # 0 of 0 is not a pass: nothing in the staged files was there to check.
            print(f"   NOT RUN  {r.name:<9} 0 of 0, nothing to check")
            continue
        new = [m for m in r.misses if check is wa.check_tools or (miss_key(m) or "\0") in added
               or miss_key(m) is None]
        old = len(r.misses) - len(new)
        print(f"   {'FAIL' if new else 'PASS'}  {r.name:<9} {r.ok} of {r.total}"
              + (f"; {old} in lines this commit does not touch, not judged" if old else ""))
        out += [f"refs {r.name}: {m}" for m in new]
    return out


SUITES = os.environ.get("SKILL_LINT_SUITES") or os.path.expanduser(_config("agent_gate", "suites", default="") or "")


def new_skill_gate(staged):
    """8. NEW SKILLS, commit mode: a SKILL.md this commit adds is refused unless a suite in SUITES
    names its folder and holds a passing receipt for the folder's current sha (agent_gate_stale.py's
    hash), or a unit.json accepts that sha without a suite (agent_gate_stale.py's ACCEPTED). An
    existing skill is listed STALE or UNTESTED by agent_gate_stale.py instead, and is not refused
    here."""
    new = []
    for p in staged:
        p = os.path.abspath(p)
        if os.path.basename(p) != "SKILL.md" or "skills" not in p.split(os.sep):
            continue
        r = subprocess.run(["git", "-C", os.path.dirname(p), "diff", "--cached", "--name-only", "--diff-filter=A",
                            "--", p], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            new.append(os.path.dirname(p))
    if not new:
        return []
    print("\n8. NEW SKILLS (a skill this commit adds needs a suite with a passing receipt)")
    if not SUITES:
        print("   NOT RUN  no suites folder: set the config key agent_gate.suites to gate new skills")
        return []
    from agent_gate_stale import file_sha
    import json
    out = []
    for folder in new:
        sha, ok = file_sha(folder), False
        for unit_json in glob.glob(os.path.join(SUITES, "*", "unit.json")):
            try:
                unit = json.load(open(unit_json, encoding="utf-8"))
                if os.path.realpath(os.path.expanduser(unit["file"])) != os.path.realpath(folder):
                    continue
                ok |= (unit.get("accepted") or {}).get("sha") == sha
                rec = os.path.join(os.path.dirname(unit_json), "receipts.jsonl")
                ok |= any(json.loads(l).get("unit_sha") == sha and json.loads(l).get("pass")
                          for l in open(rec, encoding="utf-8") if l.strip())
            except (OSError, ValueError, KeyError):
                continue
        name = os.path.basename(folder)
        print(f"   {'PASS' if ok else 'FAIL'}  {name} (folder sha {sha})")
        if not ok:
            out.append(f"new skill {name}: no suite in {SUITES} names its folder with a passing receipt for "
                       f"sha {sha}. Build a suite for it, run it, then commit")
    return out


def added_lines(path):
    """The text a commit adds to `path` (`git diff --cached` in its repo), or the whole file when
    nothing is staged for it: a new file outside git, or a run by hand."""
    r = subprocess.run(["git", "-C", os.path.dirname(path), "diff", "--cached", "-U0", "--", path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not r.stdout.strip():
        return open(path, encoding="utf-8", errors="replace").read()
    return "\n".join(l[1:] for l in r.stdout.splitlines() if l.startswith("+") and not l.startswith("+++"))


# 9. TAGS. A dated attribution is who said it and when: "(<who>, <date>)" with anything before the
# who inside the brackets, or "<who> <date>:" opening a quote. <who> is any one word, a name or a
# role, so the pattern needs no list of names; a word that marks a measurement ("measured
# 2026-10-26: 182 runs") is not a who.
_DATE = r"(?:20\d\d-\d\d-\d\d|\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
_WHO = r"(?!(?:measured|re-verified|verified|checked|tested|counted|read|of)\b)[A-Za-z][\w'.-]*"
TAGS = [
    ("a dated attribution", re.compile(r"\([^()]{0,60}?\b" + _WHO + r",\s*" + _DATE + r"\b[^()]{0,40}\)"
                                       + r"|\b" + _WHO + r",? " + _DATE + r"\s*:")),
    ("a star", re.compile("★")),
    ("capitals for emphasis", re.compile(r"\b(?:NEVER|ALWAYS|MUST|MANDATORY|CRITICAL|IMPORTANT)\b")),
]


# A tool docstring's section name: an all-caps label opening the line and ending in . or :
_HEADING = re.compile(r"^\s*[A-Z][A-Z0-9 '/-]*[.:](?=\s|$)")


def tag_lines(text, headings=False):
    """[(tag, line)] for the lines of `text` that carry a TAGS mark outside fenced and inline code. With
    headings, a line's opening section name ("NEVER DOWNLOADED.") is not read as emphasis."""
    out, fence = [], False
    for ln in text.split("\n"):
        if ln.lstrip().startswith("```"):
            fence = not fence
            continue
        prose = "" if fence else re.sub(r"`[^`]*`", "", ln)
        if headings:
            prose = _HEADING.sub("", prose, count=1)
        out += [(name, ln.strip()) for name, rx in TAGS if rx.search(prose)]
    return out


# A past-tense story verb, unless a present passive ("is no longer asked") makes it a mechanism. A README
# passage marked *Measured*, to the end of its cell, is kept word for word, so its verbs aren't read.
_STORY_VERB = re.compile(r"\b(?:asked|said|ruled|told|wanted|chose|picked|decided|complained|noticed|insisted)\b", re.I)
_PRESENT_PASSIVE = re.compile(r"\b(?:is|are|be|being)\b(?:\W+\w+){0,2}?\W+$", re.I)
_MEASURED = re.compile(r"\*[Mm]easured\*(?:[^|]|(?<=\\)\|)*")


def story_lines(text):
    """[(tag, line)]: check 9's marks, plus a date on the same line as a past-tense story verb (asked,
    said, ruled). A date alone passes, so a measured line keeps its date, and so does a name beside one."""
    out, fence = list(tag_lines(text, headings=True)), False
    for ln in text.split("\n"):
        if ln.lstrip().startswith("```"):
            fence = not fence
            continue
        prose = "" if fence else re.sub(r"`[^`]*`", "", _MEASURED.sub("", ln))
        story = any(not _PRESENT_PASSIVE.search(prose[:m.start()]) for m in _STORY_VERB.finditer(prose))
        if re.search(_DATE, prose) and story and not any(line == ln.strip() for _, line in out):
            out.append(("a date beside a story verb", ln.strip()))
    return out


def docstring_added(path):
    """The lines a commit adds inside a staged .py file's module docstring, its --help text; the whole
    docstring when nothing is staged for it."""
    import ast
    src = open(path, encoding="utf-8", errors="replace").read()
    try:
        first = ast.parse(src).body[:1]
    except SyntaxError:
        return ""
    if not (first and isinstance(first[0], ast.Expr) and isinstance(getattr(first[0].value, "value", None), str)):
        return ""
    lo, hi, lines = first[0].lineno, first[0].end_lineno, src.split("\n")
    r = subprocess.run(["git", "-C", os.path.dirname(path), "diff", "--cached", "-U0", "--", path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not r.stdout.strip():
        return "\n".join(lines[lo - 1:hi])
    keep = set()
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", r.stdout, re.M):
        s, c = int(m.group(1)), int(m.group(2) or 1)
        keep |= {n for n in range(s, s + c) if lo <= n <= hi}
    return "\n".join(lines[n - 1] for n in sorted(keep))


def instruction_file(path):
    """A skill or agent file (any .md under a skills or agents folder) or a CLAUDE.md."""
    parts = path.replace(os.sep, "/").split("/")
    return path.endswith(".md") and ("skills" in parts or "agents" in parts or parts[-1] == "CLAUDE.md")


def tool_help_file(path):
    """A .py or README.md directly inside a folder named tools: what they hold is a tool's help."""
    parts = path.replace(os.sep, "/").split("/")
    return len(parts) > 1 and parts[-2] == "tools" and (parts[-1].endswith(".py") or parts[-1] == "README.md")


def added_tags(staged):
    """9. TAGS, commit mode: a staged instruction file may not add a line with a dated attribution, a
    star or capitals for emphasis (templates/writing-instructions.md, 'What an instruction looks
    like'). A staged tool may not add one to its module docstring (its --help), nor tools/README.md to
    a row, and there a date beside a past-tense story verb counts too: the help says what the tool does
    and why, as a mechanism; the story goes in the commit message."""
    out = []
    for p in staged:
        q = os.path.abspath(p)
        if not os.path.exists(q):
            continue
        if instruction_file(q):
            out += [f"{p}: adds {tag}: \"{line[:90]}\". Say the rule plainly; the date and who said it go "
                    f"in the record" for tag, line in tag_lines(added_lines(q))]
        elif tool_help_file(q):
            text = docstring_added(q) if q.endswith(".py") else added_lines(q)
            out += [f"{p}: adds {tag} to its help: \"{line[:90]}\". Say what it does and why; the story goes "
                    f"in the commit message" for tag, line in story_lines(text)]
    return out


def workspace_story():
    """10. WORKSPACE MAP: a working folder outside any repo has no commit to refuse a line in its map,
    so the full run reports the map's marked and story lines instead. Not run without workspace.root."""
    root = _config("workspace", "root")
    if ONLY is not None or not root:
        return
    path = os.path.join(os.path.expanduser(root), _config("workspace", "map") or "WORKSPACE.md")
    if not os.path.exists(path):
        return
    found = story_lines(read(path))
    print(f"\n10. WORKSPACE MAP ({os.path.basename(path)}: reported, not refused)\n   {'PASS' if not found else 'INFO'}  "
          f"{len(found)} line(s) with a mark, or a date beside a story verb")
    for tag, line in found[:20]:
        print(f"         {tag}: {line[:110]}")


def miss_key(miss):
    """The text a reference miss quotes from its line: a cite's backticked token or an xref's §N.
    None when it can't be told, and then the miss is kept."""
    k = re.findall(r"`([^`]+)`", miss)
    if k:
        return k[-1]
    k = re.search(r" cites \S+ (§\S+);", miss)
    return k.group(1) if k else None


def shared_docs():
    """Reference files: each skill's references/*.md, and the .md files in a folder beside the skills
    that has no SKILL.md of its own (a folder several skills share)."""
    out = glob.glob(os.path.join(SKILLS, "*", "references", "*.md"))
    for d in sorted(glob.glob(os.path.join(SKILLS, "*", ""))):
        if not os.path.exists(os.path.join(d, "SKILL.md")) and os.path.basename(d.rstrip(os.sep)) != "synced":
            out += glob.glob(os.path.join(d, "*.md"))
    return sorted(set(out))


def main():
    fails, warns = [], []
    paths = {n: os.path.join(SKILLS, n, "SKILL.md") for n in SCOPE}
    paths = {n: p for n, p in paths.items() if os.path.exists(p)}
    if not paths:
        print(f"no skills under {SKILLS}"); return 1

    print("1. BUDGET + FRONTMATTER")
    for n, p in paths.items():
        raw = open(p, "rb").read()
        t = raw.decode("utf-8", "replace")
        b = body_of(t)
        tok, lines = len(b) // 4, b.count("\n") + 1
        ok = tok <= MAX_TOK and lines <= MAX_LINES
        if not ok:
            fails.append(f"{n}: body ~{tok} tok / {lines} lines (max {MAX_TOK} / {MAX_LINES})")
        m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", t, re.S)
        fm = m.group(1) if m else ""
        if not re.search(r"^name:", fm, re.M) or not re.search(r"^description:", fm, re.M):
            fails.append(f"{n}: frontmatter missing name or description")

        # THE check that matters. A bare YAML scalar containing a colon does not parse, and the
        # picker silently falls back to the H1 title, so the model never sees the trigger text.
        # Quote the value to fix it.
        dm = re.search(r"^description:[ \t]*(.*)$", fm, re.M)
        if dm:
            val = dm.group(1).strip()
            if not val.startswith(('"', "'")) and ":" in val:
                fails.append(f"{n}: description is a bare YAML scalar containing a colon, so it "
                             f"does not parse and the picker shows the H1 title instead. Wrap it "
                             f"in double quotes.")
        # CRLF does not break the picker, so this is hygiene, not a failure.
        if b"\r\n" in raw:
            warns.append(f"{n}: CRLF line endings (cosmetic; LF preferred)")
        print(f"   {'PASS' if ok else 'FAIL'}  {n:<12} ~{tok:>5} tok  {lines:>4} lines")

    print("\n2. VERBATIM DUPLICATION (8-grams shared by two SKILL.md files)")
    grams = {}
    for n, p in paths.items():
        w = re.sub(r"\s+", " ", body_of(read(p))).lower().split()
        grams[n] = set(tuple(w[i:i + 8]) for i in range(len(w) - 7))
    found = 0
    for a, b in itertools.combinations(paths, 2):
        sh = grams[a] & grams[b]
        if sh:
            found += len(sh)
            warns.append(f"{a} <-> {b}: {len(sh)} shared 8-grams")
            print(f"   WARN  {a} <-> {b}  {len(sh)} shared 8-grams")
            for g in list(sh)[:3]:
                print(f"         \"{cc_logs.redact(' '.join(g))}\"")
    if not found:
        print("   PASS  no verbatim overlap between any two SKILL.md files")

    print("\n3. TOPIC OWNERSHIP (a fact restated outside its home = a paraphrase forming)")
    bad = 0
    for topic, (owner, rx) in TOPICS.items():
        owner_path = os.path.join(SKILLS, owner)
        if not os.path.exists(owner_path):
            fails.append(f"topic '{topic}': owner {owner} does not exist"); bad += 1; continue
        if not re.search(rx, read(owner_path), re.I):
            fails.append(f"topic '{topic}': NOT found in its owner {owner}"); bad += 1; continue
        strays = [n for n, p in paths.items()
                  if os.path.realpath(p) != os.path.realpath(owner_path) and _restates(body_of(read(p)), rx, owner)]
        if strays:
            bad += 1
            fails.append(f"topic '{topic}' restated in {', '.join(strays)} (home: {owner})")
            print(f"   FAIL  {topic:<32} also in {', '.join(strays)}")
    if not TOPICS:
        print("   NOT RUN  no topics registered (config key skill_lint.topics)")
    elif not bad:
        print(f"   PASS  all {len(TOPICS)} registered facts live only in their owner file")

    print("\n4. TUNABLES (a value the code owns, restated in prose, WILL go stale)")
    tools = glob.glob(os.path.join(HERE, "*.py"))
    owned = {}   # command name -> {literal values it owns}
    for tp in tools:
        src = read(tp)
        for m in re.finditer(r"^def cmd_(\w+)\(([^)]*)\)", src, re.M):
            vals = set(re.findall(r"=\s*(\d+\.\d+|\d{2,})", m.group(2)))
            if vals:
                owned.setdefault(m.group(1), set()).update(vals)
    docs = shared_docs() + list(paths.values())
    stale = 0
    for d in docs:
        txt = read(d)
        rel = os.path.relpath(d, SKILLS).replace(os.sep, "/")
        for para in re.split(r"\n\s*\n", txt):
            for cmd, vals in owned.items():
                if not re.search(rf"\b{cmd}\b", para):
                    continue
                for v in sorted(vals):
                    if re.search(rf"(?<![\w.]){re.escape(v)}(?![\w.])", para):
                        stale += 1
                        fails.append(f"{rel}: restates `{cmd}`'s tunable {v} - the code owns it, "
                                     f"cite the command instead")
                        print(f"   FAIL  {rel}: `{cmd}` tunable {v} restated in prose")
    if not stale:
        print(f"   PASS  no doc restates a tunable owned by {len(owned)} command(s)")

    print("\n5. REFERENCE FILE SIZES (on-demand, so no hard cap, but watch the drift)")
    for d in shared_docs():
        n = len(read(d))
        rel = os.path.relpath(d, SKILLS).replace(os.sep, "/")
        print(f"   {rel:<44} {n:>6} b  ~{n//4:>5} tok")

    print("\n6. INSTRUCTION BUDGETS")
    before = len(fails)
    startup, overrides = 0, skill_overrides()   # a skill set name-only or off loads no description
    for n, p in ((n, p) for n, p in paths.items() if overrides.get(n) not in HIDDEN):
        w = desc_words(read(p))
        startup += w
        if w > SKILL_DESC_MAX:
            fails.append(f"{n}: description {w} words (max {SKILL_DESC_MAX})")
    project = project_skills()
    for n, p in project.items():
        w = desc_words(read(p))
        startup += w
        if w > SKILL_DESC_MAX:
            fails.append(f"project skill {n}: description {w} words (max {SKILL_DESC_MAX})")
    for p in sorted(glob.glob(os.path.join(AGENTS, "*.md"))):
        n, t = os.path.basename(p)[:-3], read(p)
        w, bw = desc_words(t), len(body_of(t).split())
        startup += w
        if w > AGENT_DESC_MAX:
            fails.append(f"agent {n}: description {w} words (max {AGENT_DESC_MAX})")
        if bw > AGENT_BODY_MAX:
            fails.append(f"agent {n}: body {bw} words (max {AGENT_BODY_MAX})")
        if "<!--" in t:
            fails.append(f"agent {n}: HTML comment in the prompt; provenance goes in the record")
    always = sum(len(read(p).split()) for p in ALWAYS if os.path.exists(p))
    startup += always
    if startup > STARTUP_MAX:
        fails.append(f"startup-loaded {startup} words (max {STARTUP_MAX}): rules and memory index "
                     f"{always}, descriptions {startup - always}. Replace a line instead of adding one")
    over = len(fails) - before - (startup > STARTUP_MAX)
    print(f"   {'PASS' if startup <= STARTUP_MAX else 'FAIL'}  startup-loaded {startup} of {STARTUP_MAX} "
          f"words (rules and memory index {always}, descriptions {startup - always}, of which "
          f"{len(project)} project skill(s) in {PROJECT_SKILLS.replace(os.path.expanduser('~'), '~')})")
    print(f"   {'PASS' if not over else 'FAIL'}  descriptions and agent bodies: {over} over budget")
    plugin, synced, hidden = installed_descriptions()
    print(f"   INFO  installed skills also load {plugin + synced} description words, so the startup total "
          f"is {startup + plugin + synced}: plugin skills {plugin} (skillOverrides can't reach them; only "
          f"turning the plugin off does), synced skills {synced} ({hidden} set name-only or off). Reported, "
          f"not capped: a plugin update would fail every commit")

    if ONLY is not None:
        owned = owners_of(ONLY)
        others = [f for f in fails if not any(re.search(rx, f) for rx in owned)]
        fails = [f for f in fails if f not in others]
        for f in others:
            warns.append(f"(not staged) {f}")
        fails += staged_references(ONLY)
        fails += new_skill_gate(ONLY)
        fails += added_tags(ONLY)
    workspace_story()

    print("\n" + "=" * 70)
    for f in fails:
        print("FAIL  " + f)
    if warns and not fails:
        for w in warns:
            print("WARN  " + w)
    print(f"{len(fails)} failures, {len(warns)} warnings")
    return 1 if fails else 0


def selftest():
    """Run this file on planted folders and read its output. User skills of 5 words and 11 set name-only,
    project skills of 7 and 9 set name-only: the startup total must be 12. The user folder alone gives
    5; ignoring the project override gives 21, and ignoring the user one 23. A registered topic restated in a second skill must FAIL,
    and the same text in a section that cites the owner must not. Check 9 must catch each planted bad
    line, pass each good one, and read only the added line of a staged edit."""
    import json, tempfile
    ok_all = True
    with tempfile.TemporaryDirectory() as tmp:
        def skill(root, name, words, body="body\n"):
            os.makedirs(os.path.join(root, name))
            with open(os.path.join(root, name, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(f"---\nname: {name}\ndescription: {' '.join(['word'] * words)}\n---\n{body}")
        user, proj = os.path.join(tmp, "user"), os.path.join(tmp, "proj", ".claude", "skills")
        skill(user, "u-skill", 5)
        skill(user, "v-skill", 11)
        skill(proj, "p-skill", 7)
        skill(proj, "q-skill", 9)
        os.makedirs(os.path.join(tmp, "agents"))
        with open(os.path.join(tmp, "proj", ".claude", "settings.json"), "w") as f:
            json.dump({"skillOverrides": {"q-skill": "name-only", "v-skill": "name-only"}}, f)
        open(os.path.join(tmp, "empty.md"), "w").close()
        env = dict(os.environ, SKILL_LINT_PROJECT_SKILLS=proj, SKILL_LINT_SETTINGS_DIR=os.path.join(tmp, "proj"),
                   SKILL_LINT_AGENTS=os.path.join(tmp, "agents"), SKILL_LINT_ALWAYS=os.path.join(tmp, "empty.md"),
                   SKILL_LINT_SUITES=os.path.join(tmp, "suites"), AGENT_WORKBENCH_CONFIG=os.path.join(tmp, "none.json"))
        out = subprocess.run([sys.executable, os.path.abspath(__file__), user], env=env,
                             capture_output=True, text=True, encoding="utf-8").stdout
        m = re.search(r"startup-loaded (\d+) of", out)
        got = int(m.group(1)) if m else None
        ok = got == 12
        ok_all &= ok
        print(("PASS" if ok else "FAIL") + f" startup total counts the project skill and skips the name-only ones: "
              f"got {got}, want 12 (user folder alone 5; project override ignored 21; user override ignored 23)")

        # Check 2 on two skills that share a clone line holding a token, built here so no file holds one.
        two, fake = os.path.join(tmp, "two"), "gldt-" + "Zq7x" * 6
        for n in ("r-skill", "s-skill"):
            skill(two, n, 1, f"run git clone --depth 1 https://deploy-token-1:{fake}@host.example/x.git "
                             "into the tools folder now\n")
        out2 = subprocess.run([sys.executable, os.path.abspath(__file__), two], env=env, capture_output=True,
                              text=True, encoding="utf-8").stdout
        ok = "[redacted]" in out2 and fake.lower() not in out2.lower() and "shared 8-grams" in out2
        ok_all &= ok
        print(("PASS" if ok else "FAIL") + " check 2 prints a shared 8-gram that holds a token with the token redacted")

        topics = os.path.join(tmp, "topics")
        os.makedirs(os.path.join(topics, "refs"))
        with open(os.path.join(topics, "refs", "pull.md"), "w") as f:
            f.write("Every variant of a component set is pulled.\n")
        skill(topics, "a-skill", 5, "## Pull\nPull every variant before building.\n")
        skill(topics, "b-skill", 5, "## Pull\nStates: every variant, see refs/pull.md.\n")
        with open(os.path.join(tmp, "cfg.json"), "w") as f:
            json.dump({"skill_lint": {"topics": {"component variants": ["refs/pull.md", r"every variant"]}}}, f)
        env["AGENT_WORKBENCH_CONFIG"] = os.path.join(tmp, "cfg.json")
        r = subprocess.run([sys.executable, os.path.abspath(__file__), topics], env=env,
                           capture_output=True, text=True, encoding="utf-8")
        ok = (r.returncode == 1 and "topic 'component variants' restated in a-skill (home: refs/pull.md)" in r.stdout
              and "b-skill (home" not in r.stdout)
        ok_all &= ok
        print(("PASS" if ok else "FAIL") + " a registered topic restated without citing its owner fails; one that "
              "cites the owner does not")

        # Check 9: each bad line caught, each good one passed, and only the added line of a staged
        # edit read.
        bad = ["- **Design changes (the user, 2026-10-17):** a frame change is a thread.", "- ★ Read it first.",
               "Priority runs highest first (per the lead, 10/14).", "Drafts wait (no timed card; the user, 2026-10-25).",
               "You MUST search before a create.", 'user 10/24: "can you do this now"', "NEVER: push to main."]
        good = ["Run `ping.py --since 2026-10-07T23:25`.", "A run that did not start is DID NOT RUN.",
                "See `_archive/open-threads-2026-10-20/`.", "Each finding is a BLOCKER or POLISH.",
                "Crop to the top half of the frame (about 1/2 of its height).", "```\nNEVER inside a fence\n```"]
        caught = [b for b in bad if tag_lines(b)]
        passed = [g for g in good if not tag_lines(g)]
        repo = os.path.join(tmp, "repo")
        f = os.path.join(repo, ".claude", "skills", "x", "SKILL.md")
        os.makedirs(os.path.dirname(f))
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("You MUST read it (the user, 2026-10-01).\n")
        # The planted repo ignores your own git hooks and signing, so the self-test commit can't block.
        g = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@localhost",
             "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}"]
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "old line"], check=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("A new ★ line.\n")
        subprocess.run(g + ["add", "-A"], check=True)
        staged = added_tags([f])
        ok = len(caught) == len(bad) and len(passed) == len(good) and len(staged) == 1 and "a star" in staged[0]
        ok_all &= ok
        print(("PASS" if ok else "FAIL") + f" tags: {len(caught)} of {len(bad)} bad lines caught, {len(passed)} of "
              f"{len(good)} good lines passed, staged edit gives {len(staged)} finding(s), want 1 (the added star)")

        # Check 8: a new skill passes when a unit.json accepts its folder's sha, and is refused otherwise.
        from agent_gate_stale import file_sha
        r2 = os.path.join(tmp, "r2")
        y = os.path.join(r2, "skills", "y")
        os.makedirs(y)
        with open(os.path.join(y, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: y\n---\nbody\n")
        subprocess.run(["git", "init", "-q", r2], check=True)
        subprocess.run(["git", "-C", r2, "add", "-A"], check=True)
        s8 = os.path.join(tmp, "suites8", "y")
        os.makedirs(s8)
        global SUITES
        real = SUITES
        SUITES = os.path.dirname(s8)
        try:
            results8 = []
            for sha in (file_sha(y), "0000000"):
                with open(os.path.join(s8, "unit.json"), "w") as fh:
                    json.dump({"unit": "y", "file": y, "accepted": {"sha": sha}}, fh)
                results8.append(len(new_skill_gate([os.path.join(y, "SKILL.md")])))
        finally:
            SUITES = real
        ok = results8 == [0, 1]
        ok_all &= ok
        print(("PASS" if ok else "FAIL") + f" new skill: accepted at its sha gives {results8[0]} finding(s), want 0; "
              f"accepted at another sha gives {results8[1]}, want 1")

        # Check 9 on tool help: story lines caught, measured and command lines passed, and in a staged tool
        # only the lines it adds to its module docstring are read.
        hbad = ["WHY. On 10/24 the user asked for one line per tool.",
                "The lead ruled 2026-10-20 that every row names its owner.",
                "It runs first (the lead, 10/14).", "The help said nothing until 10/22, when it was rewritten.",
                "★ Read the README first.", "You MUST pass --since.", "It was decided on 2026-10-14 to drop it.",
                "GUARD. You MUST pass --since.", "The token value is NEVER printed, logged, or written anywhere.",
                "| `t.py` | the user asked for it on 10/24. *Measured* 2026-10-22: 3 of 3 | z |"]
        # Lines of the shapes an earlier version of this rule flagged: a measurement with its date, a
        # date opening a wrapped line, and a section name in capitals.
        hgood = ["Exit 0 when every unit passes; measured 2026-10-26: 182 runs, 2 limit hits.",
                 "python tools/ping.py --since 2026-10-07T23:25", "A run before 10/26 has no digest.",
                 "Reads ADO and prints N of M.", "Each finding is a BLOCKER or POLISH.",
                 "                 2026-10-18 (grade() says why the gate is no longer asked). There is no small-gap",
                 "(2026-10-14). What can be read end to end is the user-authored subset, roughly 0.4% of the bytes",
                 "2026-10-18. grade() explains why the gate is no longer asked. There is no small-gap",
                 "NEVER DOWNLOADED. The run sets this process tree's I/O policy so opening a cloud placeholder fails",
                 "NEVER: a dirty, unmerged or recently used worktree or branch; a remote; a session log that is not",
                 "THE TOKEN IS NEVER WRITTEN INTO THE HTML. It is read server-side only (the environment,",
                 "| `t.py` | edits. *Measured* 2026-10-22: the stored version was picked as the closest \\| ok | z |"]
        hc = [b for b in hbad if story_lines(b)]
        hp = [g for g in hgood if not story_lines(g)]
        tdir = os.path.join(tmp, "work", "tools")
        os.makedirs(tdir)
        tool = os.path.join(tdir, "t.py")
        with open(tool, "w", encoding="utf-8") as fh:
            fh.write('"""t.py - prints N of M.\n\nExit 0 when done.\n"""\nimport sys\n')
        gt = ["git", "-C", tdir, "-c", "user.name=t", "-c", "user.email=t@localhost",
              "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}"]
        subprocess.run(["git", "init", "-q", tdir], check=True)
        subprocess.run(gt + ["add", "-A"], check=True)
        subprocess.run(gt + ["commit", "-qm", "t"], check=True)
        with open(tool, "w", encoding="utf-8") as fh:
            fh.write('"""t.py - prints N of M.\n\nThe user asked for this on 10/24.\nExit 0 when done.\n"""\n'
                     'import sys\n# The user asked for this on 10/24.\n')
        subprocess.run(gt + ["add", "-A"], check=True)
        tool_found = added_tags([tool])
        ok = len(hc) == len(hbad) and len(hp) == len(hgood) and len(tool_found) == 1 and "to its help" in tool_found[0]
        ok_all &= ok
        print(("PASS" if ok else "FAIL") + f" tool help: {len(hc)} of {len(hbad)} story lines caught, {len(hp)} of "
              f"{len(hgood)} measured lines passed; a staged tool gives {len(tool_found)} finding(s), want 1 (the "
              f"docstring line, not the same words in a comment)")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(selftest() if SELFTEST else main())
