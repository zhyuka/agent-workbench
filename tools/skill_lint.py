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
  7. NEW SKILLS  - with --only: FAIL when the commit adds a skills/<name>/SKILL.md and no eval suite
                   has a passing receipt for that folder's sha (agent_gate_stale.py's suites folder,
                   config key agent_gate.suites; not run when that is unset).

Usage:  python tools/skill_lint.py [skills_dir]      exit 1 on any FAIL (default ~/.claude/skills)
        python tools/skill_lint.py --selftest        planted skills: the startup total counts a
                                                     project skill and skips one set name-only

What loads at startup is read from: ~/.claude/agents (SKILL_LINT_AGENTS), the current project's
.claude/skills (SKILL_LINT_PROJECT_SKILLS) less any skill its .claude/settings*.json skillOverrides set
name-only or off (SKILL_LINT_SETTINGS_DIR), and ~/.claude/CLAUDE.md, <project>/CLAUDE.md and the
project's memory index MEMORY.md (SKILL_LINT_ALWAYS, a path list). The project is $CLAUDE_PROJECT_DIR,
else the folder you run in.
"""
import os, re, sys, glob, itertools, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

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


SUITES = os.environ.get("SKILL_LINT_SUITES") or os.path.expanduser(_config("agent_gate", "suites", default="") or "")


def new_skill_gate(staged):
    """7. NEW SKILLS, commit mode: a SKILL.md this commit adds is refused unless a suite in SUITES
    names its folder and holds a passing receipt for the folder's current sha (agent_gate_stale.py's
    hash). An existing skill is listed STALE or UNTESTED by agent_gate_stale.py instead, and is not
    refused here."""
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
    print("\n7. NEW SKILLS (a skill this commit adds needs a suite with a passing receipt)")
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
                print(f"         \"{' '.join(g)}\"")
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
    startup = 0
    for n, p in paths.items():
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
        fails += new_skill_gate(ONLY)

    print("\n" + "=" * 70)
    for f in fails:
        print("FAIL  " + f)
    if warns and not fails:
        for w in warns:
            print("WARN  " + w)
    print(f"{len(fails)} failures, {len(warns)} warnings")
    return 1 if fails else 0


def selftest():
    """Run this file on planted folders and read its output. A user skill (5 words), a project skill (7)
    and a project skill set name-only (9): the startup total must be 12. Reading the user folder alone
    gives 5; ignoring the override gives 21. A registered topic restated in a second skill must FAIL,
    and the same text in a section that cites the owner must not."""
    import json, tempfile
    ok_all = True
    with tempfile.TemporaryDirectory() as tmp:
        def skill(root, name, words, body="body\n"):
            os.makedirs(os.path.join(root, name))
            with open(os.path.join(root, name, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(f"---\nname: {name}\ndescription: {' '.join(['word'] * words)}\n---\n{body}")
        user, proj = os.path.join(tmp, "user"), os.path.join(tmp, "proj", ".claude", "skills")
        skill(user, "u-skill", 5)
        skill(proj, "p-skill", 7)
        skill(proj, "q-skill", 9)
        os.makedirs(os.path.join(tmp, "agents"))
        with open(os.path.join(tmp, "proj", ".claude", "settings.json"), "w") as f:
            json.dump({"skillOverrides": {"q-skill": "name-only"}}, f)
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
        print(("PASS" if ok else "FAIL") + f" startup total counts the project skill and skips the name-only one: "
              f"got {got}, want 12 (user folder alone 5; override ignored 21)")

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
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(selftest() if SELFTEST else main())
