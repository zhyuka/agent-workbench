#!/usr/bin/env python3
"""
ado.py - a deterministic guard rail around Azure DevOps work-item writes.

WHY THIS EXISTS
  It prevents two recurring, expensive failures: opening a DUPLICATE work item
  (skipping "search ADO first") and shipping an UNDER-FORMALIZED ticket (content in
  the wrong field, empty Acceptance Criteria, no Parent link, project-root iteration,
  off-convention title).

  Instructions to search first and to formalize are usually already written down.
  Duplicates still happen because of COMPLIANCE under multi-task load, not missing
  guidance, so more prose does nothing. This wrapper makes the search a hard
  PRECONDITION of `new`, and the formalization rules MECHANICAL checks: you cannot
  create an un-searched or un-formalized item through it, and you can `lint` anything
  you touch.

  RULE: route every work-item create through `ado.py new`, never a bare
  `az boards work-item create`. `ado.py lint <id>` any item you edit.

CONFIG  The org, project and title prefix come from kit_config.py (ado.org, ado.project,
        ado.title_prefix; ado.search_stop adds words the duplicate search ignores). With the org
        or project unset, every command stops with exit 2 and names the key.

AUTH  `--rest` before the subcommand sends the same calls over `ADO_PAT` (or the OS keychain, see
      kit_config.token) and python urllib, with no `az`. Without it, uses the `az` CLI (run
      `az login` once). az is invoked with shell=False + PYTHONIOENCODING=utf-8, which also
      sidesteps Windows PowerShell mangling embedded double quotes. It does not stop
      `az --fields` cutting a value at its first newline; `--rest` avoids that (see the
      body-truncation lint).

COMMANDS
  search <keywords...>          Title search across open+closed (the "search first"
                                primitive). Run this before you ever create. Each
                                keyword matches as a PHRASE and word-by-word; results
                                are ranked by how many distinct terms hit, floored at
                                2 shared terms (1 for a one-word query; a phrase hit
                                always counts; --min-terms overrides the floor) and
                                capped at --top.
  new    ...                    Create - only after an automatic dup-search passes
                                (or --force) AND the formalization gate passes AND,
                                when the body cites a Figma node, the Figma
                                cross-check passes: every open thread in that
                                section that shares wording with the body is linked
                                in it or named in --figma-ok. It also refuses on an
                                open thread on the same layer as a cited node in any
                                copy of its component (figma_family.py), whatever
                                its wording. Exit 3 for either kind of duplicate, 2
                                when Figma could not be read (including when
                                figma_comments.py / figma_family.py are not installed).
  lint   <id> [<id> ...]        Formalization audit of existing item(s). Exit != 0
                                if any item fails a hard check.

  Run `ado.py <command> -h` for options.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

# The Windows console is cp1252 and this file prints glyphs outside it. Without this the run can
# die with UnicodeEncodeError, often at the LAST print, throwing away completed work.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_config

# The project's .claude/agent-workbench.json (kit_config.py); None when unset, and main() then stops.
ORG_DEFAULT, PROJ_DEFAULT = kit_config.ado()

# Title house style: "<prefix> // {Page - Section} // {short description}", the prefix from
# ado.title_prefix. With no prefix set, any title passes.
TITLE_PREFIX = kit_config.get("ado", "title_prefix")
_P = re.escape(TITLE_PREFIX) if TITLE_PREFIX else None
TITLE_RE = re.compile(rf"^{_P} // .+ - .+ // .+" if _P else r".+")
# The hard check is the looser form (page only, no "- Section"); missing the "- Section" is a WARN.
TITLE_RE_LOOSE = re.compile(rf"^{_P} // .+ // .+" if _P else r".+")
TITLE_FORM = f"{TITLE_PREFIX} // {{Page - Section}} // {{desc}}" if TITLE_PREFIX else "any title"
# What the hard check accepts. Its messages name this form, not TITLE_FORM, so a refusal never
# demands a stricter form than the check actually enforces.
TITLE_FORM_LOOSE = f"{TITLE_PREFIX} // {{Page}} // {{desc}}" if TITLE_PREFIX else "any title"

TYPE_ALIASES = {"story": "User Story", "user story": "User Story",
                "bug": "Bug", "task": "Task"}
# Where each type's *body* belongs. Bugs render ReproSteps, not Description.
BODY_FIELD = {"Bug": "Microsoft.VSTS.TCM.ReproSteps",
              "User Story": "System.Description",
              "Task": "System.Description"}
AC_FIELD = "Microsoft.VSTS.Common.AcceptanceCriteria"
# Only the User Story form shows AcceptanceCriteria in the Agile process. The Bug and Task types do
# not put the field on their form, but ADO stores whatever is written to it, so AC sent there is
# saved and never seen. So the AC goes to a field the form does show: a Task's joins its
# Description, and a Bug's goes to System Info.
AC_OFF_FORM = {"Bug": "Microsoft.VSTS.TCM.SystemInfo", "Task": "System.Description"}

# A parent in its child's own backlog category (a Story under a Story, a Bug under a Task) makes
# ADO hide the parent from that backlog and turn off reordering, behind a "same category hierarchy"
# banner. The board's backlog configuration says which category each type is in. Bug has no level
# of its own: the team's bugsBehavior puts it with the Tasks ("asTasks") or the Stories, and only
# a named-team call lists it, so it is placed from bugsBehavior here. Leaving Bug out lets a Bug
# filed under a Task slip past every check. CATEGORY is the fallback when the configuration cannot
# be read.
CATEGORY = {"Epic": "Microsoft.EpicCategory", "Feature": "Microsoft.FeatureCategory",
            "User Story": "Microsoft.RequirementCategory",
            "Task": "Microsoft.TaskCategory", "Bug": "Microsoft.TaskCategory"}
BUGS_BEHAVIOR = {"asTasks": "Microsoft.TaskCategory", "asRequirements": "Microsoft.RequirementCategory"}


def categories_from(bc):
    """{work item type: backlog category} from a /work/backlogconfiguration response; CATEGORY
    when there is none. With bugsBehavior "off" a Bug is on no backlog, so it is left out."""
    if not bc:
        return dict(CATEGORY)
    out = {}
    for lvl in (bc.get("portfolioBacklogs") or []) + [bc.get("requirementBacklog") or {},
                                                       bc.get("taskBacklog") or {}]:
        for w in lvl.get("workItemTypes") or []:
            out[w["name"]] = lvl.get("id")
    if BUGS_BEHAVIOR.get(bc.get("bugsBehavior")):
        out.setdefault("Bug", BUGS_BEHAVIOR[bc["bugsBehavior"]])
    else:
        out.pop("Bug", None)
    return out


def same_category(child_type, parent_type, cats):
    """The category both share when parenting child_type to parent_type trips the banner, else None."""
    c = cats.get(child_type)
    return c if c and c == cats.get(parent_type) else None


def same_category_reason(who, child_type, pid, parent_type, cat):
    return (f"{who} is a {child_type} and #{pid} is a {parent_type}; both are in "
            f"{cat.replace('Microsoft.', '')}, so ADO would hide #{pid} from the backlog and turn "
            f"off reordering. Parent it one level up (the item above #{pid}) and link #{pid} as "
            f"related instead.")


def _fold_ac(wtype, body, ac):
    """(body, ac, {field: value}) to write. For a type whose form has no AC field, the AC moves,
    under its heading, to the field AC_OFF_FORM names: the body itself, or a field of its own."""
    if not (ac and wtype in AC_OFF_FORM):
        return body, ac, {}
    section = "<p><b>Acceptance criteria</b></p>" + ac
    home = AC_OFF_FORM[wtype]
    if home == BODY_FIELD.get(wtype, "System.Description"):
        return body + section, None, {}
    return body, None, {home: section}


def _hidden_ac_check(wtype, fields):
    """A lint check: FAIL when a Bug or Task keeps text in the AC field its form does not show."""
    hidden = _strip(fields.get(AC_FIELD, "") or "")
    bf = AC_OFF_FORM[wtype].split(".")[-1]
    return ("FAIL", not hidden,
            f"no AC hidden in AcceptanceCriteria (the {wtype} form does not show it)" if not hidden
            else f"{len(hidden)} chars of AC hidden in AcceptanceCriteria, which the {wtype} form "
                 f"does not show. Move them into {bf}")

AZ = shutil.which("az") or shutil.which("az.cmd") or "az"
_TAG_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------- transport
# TWO TRANSPORTS, ONE GATE. Where `az` is blocked (an enterprise permission prompt nobody can click
# from a background run), "az is blocked" becomes a reason to not file a ticket at all, which is
# worse than either transport. ado_write.py's PAT + urllib path works, but it deliberately refuses
# to CREATE, because creation is where duplicates happen and it carries no dup-search.
#
# So the TRANSPORT moves, not the gate. Everything above this line - the formalization checks, the
# dup search, the Gherkin refusal, lint - runs identically either way. `--rest` only changes how
# the same argv reaches the server. The shim below understands exactly the five call shapes this
# file makes and raises on anything else, so it can never half-work silently: an unrecognised
# command is a crash, not a no-op.
USE_REST = False


def _env():
    return dict(os.environ, PYTHONIOENCODING="utf-8")


def _pat():
    p = kit_config.token("ADO_PAT", "AZURE_DEVOPS_EXT_PAT")
    if not p:
        raise RuntimeError("--rest needs ADO_PAT (or AZURE_DEVOPS_EXT_PAT) in the environment, or "
                           "in the OS keychain (see kit_config.py)")
    return p


def _rest(method, url, payload=None, ctype="application/json"):
    """One REST call. The PAT is read here and never printed, logged or returned."""
    import base64
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f":{_pat()}".encode()).decode())
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = " :: " + e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
        raise RuntimeError(f"{method} {url.split('?')[0]} failed: {e}{detail}")


def _flag(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def _rest_dispatch(args):
    """Serve the same argv `az` would have taken, over PAT + urllib.

    Deliberately exhaustive rather than general: the five shapes this file actually emits, and a
    hard failure on anything else. A permissive shim that shrugged at an unknown verb would let a
    future call look like it succeeded."""
    org = (_flag(args, "--org") or ORG_DEFAULT).rstrip("/")
    proj = _flag(args, "--project") or PROJ_DEFAULT
    from urllib.parse import quote
    pq = quote(proj, safe="")
    A = "api-version=7.0"

    if args[:2] == ["boards", "query"]:
        r = _rest("POST", f"{org}/{pq}/_apis/wit/wiql?{A}", {"query": _flag(args, "--wiql")})
        ids = [w["id"] for w in r.get("workItems", [])][:200]
        if not ids:
            return []
        fields = ("System.Id,System.Title,System.State,System.WorkItemType,System.ChangedDate")
        got = _rest("GET", f"{org}/_apis/wit/workitems?ids={','.join(map(str, ids))}"
                           f"&fields={fields}&{A}")
        by_id = {w["fields"]["System.Id"]: w for w in got.get("value", [])}
        return [by_id[i] for i in ids if i in by_id]   # keep the WIQL's ORDER BY

    if args[:3] == ["boards", "work-item", "create"]:
        ops = [{"op": "add", "path": "/fields/System.Title", "value": _flag(args, "--title")}]
        for flag, ref in (("--iteration", "System.IterationPath"),
                          ("--area", "System.AreaPath"),
                          ("--assigned-to", "System.AssignedTo")):
            if _flag(args, flag):
                ops.append({"op": "add", "path": f"/fields/{ref}", "value": _flag(args, flag)})
        wtype = quote("$" + _flag(args, "--type"), safe="$")
        return _rest("POST", f"{org}/{pq}/_apis/wit/workitems/{wtype}?{A}", ops,
                     "application/json-patch+json")

    if args[:3] == ["boards", "work-item", "update"]:
        # `--fields` takes MULTIPLE "k=v" after ONE flag, same contract as az.
        i = args.index("--fields") + 1
        ops = []
        while i < len(args) and not args[i].startswith("--") and args[i] != "-o":
            ref, _, val = args[i].partition("=")
            ops.append({"op": "add", "path": f"/fields/{ref}", "value": val})
            i += 1
        return _rest("PATCH", f"{org}/_apis/wit/workitems/{_flag(args, '--id')}?{A}", ops,
                     "application/json-patch+json")

    if args[:4] == ["boards", "work-item", "relation", "add"]:
        rel = {"Parent": "System.LinkTypes.Hierarchy-Reverse",
               "Child": "System.LinkTypes.Hierarchy-Forward",
               "Related": "System.LinkTypes.Related"}[_flag(args, "--relation-type")]
        ops = [{"op": "add", "path": "/relations/-", "value": {
            "rel": rel, "url": f"{org}/_apis/wit/workItems/{_flag(args, '--target-id')}"}}]
        return _rest("PATCH", f"{org}/_apis/wit/workitems/{_flag(args, '--id')}?{A}", ops,
                     "application/json-patch+json")

    if args[:3] == ["boards", "work-item", "show"]:
        exp = "&$expand=relations" if "--expand" in args else ""
        return _rest("GET", f"{org}/_apis/wit/workitems/{_flag(args, '--id')}?{A}{exp}")

    raise RuntimeError(f"--rest does not implement: {' '.join(args[:4])}")


def az(args):
    """Run az with shell=False (no quoting hazards). Returns CompletedProcess.
    Under --rest this returns a CompletedProcess-shaped stand-in so the callers that only check
    returncode keep working unchanged."""
    if USE_REST:
        _rest_dispatch(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    return subprocess.run([AZ, *args], capture_output=True, text=True, env=_env())


def az_json(args):
    if USE_REST:
        return _rest_dispatch(args)
    p = az(args)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip())
    out = p.stdout.strip()
    if not out:
        return []  # `az boards query` emits EMPTY stdout (not []) for a zero-result set
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError("az returned non-JSON output (known for some items)")


def _wiql_lit(s):
    """Escape a string for a WIQL single-quoted literal."""
    return s.replace("'", "''")


def _strip(html):
    return _TAG_RE.sub("", html or "").replace("&nbsp;", " ").strip()


def _to_html(text):
    """A plain-text body -> minimal HTML so line breaks survive ADO's HTML render.
    If it already looks like HTML, pass through untouched."""
    if not text:
        return ""
    if "<" in text and ">" in text:
        return text
    return "<br>".join(line.strip() for line in text.strip().splitlines())


# ---------------------------------------------------------------- search
# MATCHING. A naive matcher passes each keyword through to WIQL whole:
# `[System.Title] CONTAINS '<keyword>'`, OR'd. CONTAINS on System.Title (a String field) is a
# substring LIKE, so a multi-word keyword only matches a title carrying that exact phrase verbatim.
# A long phrase therefore returns ZERO rows and the dup gate opens - it FAILS OPEN, the one
# direction a gate must never fail.
#
# So every keyword is expanded into TERMS - the phrase itself plus its individual words - the WIQL
# ORs over the words, and the rows come back RANKED by how many distinct terms hit. A phrase hit is
# worth more than a word hit because it is much stronger evidence. Recall comes from the OR;
# precision comes from the score floor and the cap, not from a narrow query.
#
# Add your product's own name (which sits in every title) and the like through ado.search_stop.
SEARCH_STOP = {"the", "and", "for", "not", "with", "from", "that", "this",
               "does", "into", "when", "page", "pages", "are", "was", "its", "our", "all",
               "per", "use", "via", "add", "update", "fix", "make", "should", "than",
               "cross", "section", "sections", "across",
               # short words that are ALSO substrings of common title words - "one" is
               # inside "comp-one-nt", "are" inside "area". CONTAINS is a substring
               # match, so these manufacture false hits.
               "one", "two", "now", "new", "old", "out", "off", "only", "same", "each",
               "some", "more", "most", "just", "then", "over", "top", "end", "one-"}
SEARCH_STOP |= {str(w).lower() for w in (kit_config.get("ado", "search_stop") or [])}
if TITLE_PREFIX:
    SEARCH_STOP |= {w.lower() for w in re.findall(r"[A-Za-z0-9-]+", TITLE_PREFIX)}
MIN_TERM = 3      # a 1-2 char substring matches everything; drop it
MAX_TERMS = 24    # keep the WIQL sane
TOP_DEFAULT = 15  # rank, then cap - an uncapped dump gets skimmed, not read


def _tokenize(s):
    return [w for w in re.findall(r"[a-z0-9][a-z0-9-]{%d,}" % (MIN_TERM - 1), (s or "").lower())
            if w not in SEARCH_STOP]


def search_terms(keywords):
    """keywords -> (phrases, tokens). A multi-word keyword contributes BOTH the whole
    phrase (strong evidence) and each of its words (recall)."""
    phrases, tokens = [], []
    for k in keywords:
        k = " ".join((k or "").split())
        if not k:
            continue
        if " " in k and len(k) >= MIN_TERM:
            p = k.lower()
            if p not in phrases:
                phrases.append(p)
        for t in _tokenize(k):
            if t not in tokens:
                tokens.append(t)
    return phrases, tokens[:MAX_TERMS]


def score_title(title, phrases, tokens):
    t = (title or "").lower()
    ph = [p for p in phrases if p in t]
    tk = [w for w in tokens if w in t]
    return len(tk) + 2 * len(ph), ph, tk


def wiql_title_search(keywords, org, proj, state="all", min_terms=None, top=None):
    """Ranked title search. Returns rows sorted by score desc, capped at `top`,
    keeping only rows at or above the score floor."""
    phrases, tokens = search_terms(keywords)
    # A scorable term must always exist. If every keyword was too short or wholly
    # stopped ("ai", "the"), fall back to the raw keywords as phrases - otherwise the
    # score floor filters out every row and we are back to failing open.
    if not phrases and not tokens:
        phrases = [k.strip().lower() for k in keywords if k.strip()]
    query_terms = list(tokens) or list(phrases)
    for p in phrases:                       # a phrase whose words were all stopped
        if not any(w in p for w in tokens):
            query_terms.append(p)
    if not query_terms:
        raise RuntimeError("no usable search terms - refusing to report a clean board")
    clauses = " OR ".join(f"[System.Title] CONTAINS '{_wiql_lit(k)}'" for k in query_terms)
    where = f"[System.TeamProject] = '{_wiql_lit(proj)}' AND ({clauses})"
    if state == "open":
        where += " AND [System.State] NOT IN ('Closed','Resolved','Removed')"
    elif state == "closed":
        where += " AND [System.State] IN ('Closed','Resolved','Removed')"
    wiql = ("SELECT [System.Id],[System.Title],[System.State],[System.WorkItemType],"
            f"[System.ChangedDate] FROM WorkItems WHERE {where} "
            "ORDER BY [System.ChangedDate] DESC")
    rows = az_json(["boards", "query", "--org", org, "--wiql", wiql, "-o", "json"])
    # FLOOR. One shared word ("core", "hero") is a coincidence, not a duplicate; two
    # distinct words, or one whole phrase, is worth a human look. A floor of 1 would
    # fire on almost every create, and a gate that always fires gets --force'd.
    floor = min_terms if min_terms is not None else (1 if len(tokens) <= 1 else 2)
    out = []
    for r in rows:
        f = r.get("fields", r)
        title = f.get("System.Title")
        score, ph, tk = score_title(title, phrases, tokens)
        if not ph and len(tk) < floor:
            continue
        out.append({
            "id": f.get("System.Id"),
            "type": f.get("System.WorkItemType"),
            "state": f.get("System.State"),
            "changed": (f.get("System.ChangedDate") or "")[:10],
            "title": title,
            "score": score,
            "hits": ph + tk,
        })
    out.sort(key=lambda h: h["changed"], reverse=True)   # tie-break: newest first
    out.sort(key=lambda h: h["score"], reverse=True)     # stable, so score wins
    kept = len(out)
    cap = TOP_DEFAULT if top is None else top
    if cap:
        out = out[:cap]
    return out, {"returned": len(rows), "kept": kept, "shown": len(out), "floor": floor,
                 "phrases": phrases, "tokens": tokens}


def _print_hits(hits, meta):
    for h in hits:
        print(f"  #{h['id']:<6} [{h['score']:>2}] {h['state']:<9} {h['type']:<11} "
              f"{h['changed']}  {h['title']}")
        print(f"          matched: {', '.join(h['hits'])}")
    if meta["kept"] > meta["shown"]:
        print(f"\n  ... {meta['kept'] - meta['shown']} more at or above the floor "
              f"(raise --top to see them)")


def cmd_search(a):
    hits, meta = wiql_title_search(a.keywords, a.org, a.project, a.state,
                                   a.min_terms, a.top)
    print(f"terms: phrases={meta['phrases']} words={meta['tokens']} "
          f"(floor={meta['floor']} shared term(s))")
    if not hits:
        print(f"(no title scores at or above the floor; {meta['returned']} row(s) shared "
              f"at least one word, state={a.state})")
        return 0
    print(f"{meta['kept']} match(es) at or above the floor, ranked by shared terms "
          f"(of {meta['returned']} rows sharing any word) - review before creating:\n")
    _print_hits(hits, meta)
    return 0


# ---------------------------------------------------------------- formalization gate
def _title_keywords(title):
    """Default dup-search keywords from a house-style title: EVERY segment after the
    title prefix (ado.title_prefix), kept whole (search_terms splits them into phrase + words).

    Reading only the LAST segment, capped at a few words, throws away the page and section - the
    two strongest duplicate signals - and lets a duplicate whose description is worded differently
    through."""
    segs = [s.strip() for s in (title or "").split("//")]
    if segs and TITLE_PREFIX and segs[0].lower().startswith(TITLE_PREFIX.lower()):
        segs = segs[1:]
    return [s for s in segs if s] or [title]


# ---------------------------------------------------------------- Figma cross-check
# An open Figma thread on the same change is as much a duplicate as a ticket is, and the title
# search above reads ADO titles only. Matching by node alone misses a thread pinned at an offset on
# a page frame, outside every band the body cites.
#
# So for each Figma node the body cites, `new` lists the open threads pinned on that node, on a
# layer inside it, or on any frame between it and its section, at any offset. That list is reading.
# It REFUSES on an open thread that shares wording with the body, in the same section or pinned to a
# node the file no longer holds: its root or a reply holds a phrase the body quotes (two words or
# more) or cites one of the body's node ids. Link the thread in the body (its id anywhere in the
# text), or pass --figma-ok <id> when it is a separate ask. A Figma that cannot be read refuses too,
# never clears. --skip-figma "<why>" skips the whole Figma cross-check, both refusals included, and
# prints the reason; use it only when Figma cannot be read (or the Figma tools are not installed).
#
# A quoted phrase that more than FIG_NAME_THREADS open threads in the file hold is a name, not
# wording, and does not count: a product name sits in dozens of threads and would refuse every body
# that quotes it, while the phrases that identify a real duplicate sit in one or two.
#
# It also REFUSES on an open thread on the same layer as a cited node in another copy of that
# node's component, on any page and in any section, whatever its wording (figma_family.py): the
# same change asked on another page's copy of a shared footer shares no phrase with the body.
# Citing a whole copy counts every thread in any copy, so cite the layer. A family index that
# cannot be built or read refuses, as a Figma that cannot be read does.
#
# BLIND TO: a thread that says the same thing in other words, or shares only a name with the body,
# unless it sits on the same layer of a copy; a thread in another section that is not on a copy; an
# open thread in a file the body does not cite. An instance-layer id (I1:2;3:4) counts as the ids
# after its first, the layers in the mains.
# A body that cites no Figma node is not checked.
FIGMA_FILE_DEFAULT = kit_config.get("figma", "default_file")  # a bare node id means this file; unset: none
FIG_NAME_THREADS = 5
_FIG_URL = re.compile(r"figma\.com/(?:design|file|board|proto)/([A-Za-z0-9]{15,})[^\s\"'<>]*?"
                      r"node-id=(\d+)(?:-|:|%3A)(\d+)")
_FIG_BARE = re.compile(r"(?<![\w:-])(\d{3,6}):(\d{3,7})(?![\w:])")
_FIG_QUOTE = re.compile(r'"([^"]{3,160})"')
_FIG_PLAIN = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "\xa0": " "})


def _fig_norm(s):
    import html as _html
    return re.sub(r"\s+", " ", _html.unescape(s or "").translate(_FIG_PLAIN)).strip().casefold()


def _fig_refs(raw):
    """({file key: {node ids}}, [quoted phrases of 2+ words]) from a body's HTML."""
    import html as _html
    flat = _html.unescape(raw).translate(_FIG_PLAIN)
    refs = {}
    for key, a, b in _FIG_URL.findall(flat):
        refs.setdefault(key, set()).add(f"{a}:{b}")
    text = _TAG_RE.sub(" ", flat)
    for a, b in _FIG_BARE.findall(text) if FIGMA_FILE_DEFAULT else ():
        refs.setdefault(FIGMA_FILE_DEFAULT, set()).add(f"{a}:{b}")
    phrases = []
    for q in _FIG_QUOTE.findall(text):
        q = _fig_norm(q).strip(" .,;:!?")
        if len(q.split()) >= 2 and q not in phrases:
            phrases.append(q)
    return refs, phrases


def _fig_walk(key, ids):
    """({id: [(node id, type), ...] from the node's page}, {id: {every layer id under it}}) for
    each id the file holds, as figma_comments reads them, keeping each node's type so a SECTION can
    be told from a FRAME."""
    import figma_comments as fc
    lin = fc.lineage(key, ids)
    paths = {i: [(n["id"], n.get("type")) for n in p] for i, p in lin.items() if p}
    return paths, fc.subtrees(key, sorted(paths))


def _fig_section(path):
    """(section id, [ids strictly below it]). The section is the outermost SECTION on the path, or,
    on a canvas with no sections, the top frame, which then counts as below it too."""
    ids = [i for i, _ in path]
    for k, (_, t) in enumerate(path):
        if t == "SECTION":
            return ids[k], ids[k + 1:]
    canvas = next((k for k, (_, t) in enumerate(path) if t == "CANVAS"), 0)
    return (ids[canvas + 1] if len(ids) > canvas + 1 else None), ids[canvas + 1:]


def _figma_check(raw, a):
    """0 to go on, 3 for an unlinked thread that shares wording with the body, 2 did not run."""
    refs, phrases = _fig_refs(raw)
    ok = {s.strip() for s in (getattr(a, "figma_ok", None) or "").split(",") if s.strip()}
    if not refs:
        print("\nFigma cross-check: the body cites no Figma node, so there is nothing to check.")
        return 0
    if getattr(a, "skip_figma", None):
        print(f"\nFigma cross-check SKIPPED (--skip-figma): {a.skip_figma}")
        return 0
    refused = []
    try:
        import figma_comments as fc
        for key, cited in sorted(refs.items()):
            cs = fc.fetch(key)
            roots = {c["id"]: c for c in cs if not c.get("parent_id")}
            thread = {}
            for c in cs:
                thread.setdefault(c.get("parent_id") or c["id"], []).append(c)
            open_ids = [t for t, r in roots.items() if not r.get("resolved_at")]
            pin = {t: fc.node_of(roots[t]).replace("-", ":") for t in open_ids}
            dashed = {n: n.replace(":", "-") for n in cited}
            words = {t: _fig_norm(" ".join(c.get("message", "") for c in thread[t])) for t in open_ids}

            def holds(p, s):
                return re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", s)
            freq = {p: sum(1 for t in open_ids if holds(p, words[t])) for p in phrases}
            names = [p for p in phrases if freq[p] > FIG_NAME_THREADS]
            why = {}
            for t in open_ids:
                hits = [f'"{p}"' for p in phrases if p not in names and holds(p, words[t])]
                hits += [n for n in sorted(cited) if n in words[t] or dashed[n] in words[t]]
                if hits:
                    why[t] = hits
            paths, subs = _fig_walk(key, cited | {pin[t] for t in why if pin[t]})
            sections, scope = {}, {}
            for n in sorted(cited):
                if n not in paths:
                    continue
                sec, below = _fig_section(paths[n])
                sections[n] = sec
                for i in below[:-1]:
                    scope.setdefault(i, f"above {n}")
                for i in sorted(subs.get(n, ())):
                    scope.setdefault(i, f"inside {n}")
            for n in cited:
                if n in paths:
                    scope[n] = f"on {n}"
            gone = sorted(n for n in cited if n not in paths)
            secs = sorted({s for s in sections.values() if s})
            print(f"\n--- Figma cross-check: file {key}, {len(cited)} node(s) cited, "
                  f"{len(open_ids)} open threads read, section(s) {', '.join(secs) or 'none'}")
            if gone:
                print(f"  not in the file, so not checked: {', '.join(gone)}")
            if names:
                print("  quoted, but a name, not wording (open threads holding it): "
                      + ", ".join(f'"{p}" ({freq[p]})' for p in names))
            related = [t for t in open_ids if pin[t] in scope]
            print(f"  {len(related)} open thread(s) on, inside or above the cited nodes, below the "
                  f"section. Read them; they refuse nothing unless listed again below:")
            for t in sorted(related, key=lambda t: roots[t].get("created_at", "")):
                r = roots[t]
                print(f"    {t}  {scope[pin[t]]}, pinned to {pin[t]}  {fc.handle(r)}, "
                      f"{r.get('created_at', '')[:10]}: {fc.flat(r)[:90]}")
                print(f"      {fc.permalink(key, r)}")
            # A cited node inside a copy of a component: the open threads on the same layer of every
            # other copy, on any page. figma_family has the why; wording plays no part here.
            import figma_family
            fam = figma_family.check(key, sorted(cited), cs)
            if not fam.ran:
                raise RuntimeError(figma_family.coverage(fam))
            kin = {}
            for n in sorted(cited):
                for t in fam.same_layer(n):
                    kin.setdefault(t, []).append(n)
            if kin:
                print(f"  {len(kin)} open thread(s) sit on the same layer as a cited node, in a copy of "
                      f"its component:")
                for n in sorted(cited):
                    if fam.same_layer(n):
                        print("\n".join("  " + s for s in figma_family.lines(
                            fam, n, lambda c: fc.permalink(key, c), show_resolved=False)))
                for t in sorted(kin, key=lambda t: roots[t].get("created_at", "")):
                    linked = t in raw or any(c["id"] in raw for c in thread[t])
                    state = "linked" if linked else "--figma-ok" if t in ok else "NOT LINKED"
                    if state == "NOT LINKED":
                        refused.append(t)
                    print(f"  same layer: {t}  {state}  for {', '.join(kin[t])}")
            else:
                print("  0 open threads sit on the same layer as a cited node in a copy of its component.")
            print("  " + figma_family.coverage(fam))
            elsewhere = 0
            for t in sorted(why, key=lambda t: roots[t].get("created_at", "")):
                p = pin[t]
                if not p or p not in paths:
                    place = "pinned to a node the file no longer holds" if p else "pinned to no node"
                else:
                    sec = next((s for s in secs if s in [i for i, _ in paths[p]]), None)
                    if not sec:
                        elsewhere += 1
                        continue
                    place = f"on {p}, in section {sec}"
                linked = t in raw or any(c["id"] in raw for c in thread[t])
                state = "linked" if linked else "--figma-ok" if t in ok else "NOT LINKED"
                if state == "NOT LINKED" and t not in refused:
                    refused.append(t)
                r = roots[t]
                print(f"  shares wording: {t}  {state}  {place}  {fc.handle(r)}, "
                      f"{r.get('created_at', '')[:10]}")
                print(f"      matched {', '.join(why[t])[:200]}")
                print(f"      {fc.flat(r)[:160]}")
                print(f"      {fc.permalink(key, r)}")
            if elsewhere:
                print(f"  {elsewhere} more open thread(s) share wording but sit in other sections; "
                      f"not checked.")
    except (Exception, SystemExit) as e:
        print(f"\nFIGMA CROSS-CHECK DID NOT RUN: {str(e)[:200] or type(e).__name__}")
        print("Nothing is cleared by a check that did not run. Re-run when Figma answers, or pass "
              "--skip-figma \"<why>\".")
        return 2
    if refused:
        print(f"\nPOSSIBLE DUPLICATE IN FIGMA: {len(refused)} open thread(s) share wording with this "
              f"body or sit on the same layer as a cited node in a copy of its component, and the body "
              f"does not link them: {', '.join(refused)}.")
        print("If a thread already asks for this change, link it in the body as the source. If it is "
              "a separate ask, re-run with --figma-ok " + ",".join(refused) + ".")
        return 3
    return 0


def cmd_new(a):
    wtype = TYPE_ALIASES.get(a.type.lower())
    if not wtype:
        print(f"ERROR: --type must be one of Bug/Story/Task (got {a.type!r})")
        return 2

    # ---- formalization pre-checks (fail fast, before we ever create) ----
    errs = []
    if not TITLE_RE_LOOSE.match(a.title):
        errs.append(f"title must be at least '{TITLE_FORM_LOOSE}'; the house style is '{TITLE_FORM}'")
    if not a.parent:
        errs.append("--parent <feature-id> is required (never orphan a ticket)")
    if not a.iteration or "\\" not in a.iteration:
        errs.append("--iteration must be a real sprint path (e.g. "
                    f"'{a.project}\\\\<sprint name>'), not the project root")
    body = None
    if a.body:
        body = _to_html(open(a.body, encoding="utf-8").read())
        if not _strip(body):
            errs.append("--body file is empty")
    else:
        errs.append("--body <file> is required (the spec)")
    ac = None
    if a.ac:
        ac = _to_html(open(a.ac, encoding="utf-8").read())
        if not _strip(ac):
            errs.append("--ac file is empty")
    elif wtype == "User Story":
        errs.append("a User Story requires --ac <file> (real Acceptance Criteria, "
                    "NOT buried in the body)")
    # AC SHAPE. Populated is not the same as checkable: criteria written as numbered lists or prose
    # pass a presence check and cannot be falsified. `lint` flags the shape after the fact; refusing
    # it HERE is the part that makes it not recur, because a post-hoc warning on an item that
    # already exists gets read and then not acted on.
    if ac and _strip(ac):
        _up = _strip(_TAG_RE.sub(" ", ac)).upper()
        if not ("GIVEN" in _up and "THEN" in _up):
            errs.append("AC must be formal Gherkin - GIVEN the state, WHEN the action, THEN the "
                        "checkable outcome, grouped under Scenario labels. A numbered list or "
                        "prose is refused: it reads as criteria without being checkable.")
    if a.parent and not errs:
        try:
            ptype = _item(a.parent, a.org).get("fields", {}).get("System.WorkItemType", "")
            from urllib.parse import quote
            cats = (categories_from(_rest("GET", f"{a.org.rstrip('/')}/{quote(a.project, safe='')}/_apis/work/"
                                                 "backlogconfiguration?api-version=7.0"))
                    if USE_REST else dict(CATEGORY))
        except RuntimeError as e:
            print(f"ERROR: could not read parent #{a.parent} to check its type ({e})")
            return 2
        cat = same_category(wtype, ptype, cats)
        if cat:
            errs.append(same_category_reason("the new item", wtype, a.parent, ptype, cat))
    if errs:
        print("FORMALIZATION GATE FAILED - fix before creating:")
        for e in errs:
            print("  - " + e)
        return 2
    if not TITLE_RE.match(a.title):
        print(f"WARN: the title has no '- Section'; the house style is '{TITLE_FORM}'. lint will warn on it too.")
    raw = body + (ac or "")
    folded = bool(ac) and wtype in AC_OFF_FORM
    body, ac, ac_home = _fold_ac(wtype, body, ac)

    # ---- dup search (the precondition that stops duplicates) ----
    kws = a.keywords or _title_keywords(a.title)
    hits, meta = wiql_title_search(kws, a.org, a.project, "all",
                                   getattr(a, "min_terms", None), getattr(a, "top", None))
    if hits and not a.force:
        print(f"POSSIBLE DUPLICATE(S) for keywords {kws} - do NOT create blindly.")
        print(f"Ranked by shared terms; floor={meta['floor']}, "
              f"{meta['kept']} of {meta['returned']} rows scored:\n")
        _print_hits(hits, meta)
        print("\nIf one of these already covers it, attach to that item instead.")
        print("If this is genuinely new, re-run with --force.")
        return 3

    # ---- Figma cross-check: an open thread on the same change is a duplicate too ----
    fig = _figma_check(raw, a)
    if fig:
        return fig

    if a.dry_run:
        print(f"[dry-run] would create {wtype}: {a.title}")
        print(f"  parent={a.parent} iteration={a.iteration} keywords={kws}")
        print(f"  body-> {BODY_FIELD[wtype]} ({len(_strip(body))} chars)"
              + (f"; AC ({len(_strip(ac))} chars)" if ac else "")
              + (f"; AC -> {AC_OFF_FORM[wtype]} (the form has no AC field)" if folded else ""))
        return 0

    # ---- create (minimal), THEN set body fields via update ----
    # az create defaults --description to empty and that CLOBBERS a System.Description passed
    # through --fields (AcceptanceCriteria has no dedicated param, so it would stick - an item can
    # ship with an empty body and populated AC). Set every body field via a follow-up update,
    # which is the reliable path for HTML field values.
    create = ["boards", "work-item", "create", "--org", a.org, "--project", a.project,
              "--type", wtype, "--title", a.title, "--iteration", a.iteration]
    if a.area:
        create += ["--area", a.area]
    if a.assigned:
        create += ["--assigned-to", a.assigned]
    created = az_json(create + ["-o", "json"])
    new_id = created.get("id")
    fields = [f"{BODY_FIELD[wtype]}={body}"]
    if ac:
        fields.append(f"{AC_FIELD}={ac}")
    fields += [f"{k}={v}" for k, v in ac_home.items()]
    # az `--fields` takes MULTIPLE "k=v" after ONE flag; repeating the flag makes az keep only
    # the last, silently dropping earlier fields. Pass them all under a single --fields.
    az(["boards", "work-item", "update", "--id", str(new_id), "--org", a.org,
        "--fields", *fields, "-o", "json"])
    print(f"created #{new_id}: {wtype} // {a.title}")

    # ---- link parent ----
    az(["boards", "work-item", "relation", "add", "--org", a.org, "--id", str(new_id),
        "--relation-type", "Parent", "--target-id", str(a.parent)])
    print(f"linked parent -> #{a.parent}")

    # ---- verify what we just made ----
    print("\n--- lint (self-check) ---")
    a.ids = [new_id]
    return cmd_lint(a)


# ---------------------------------------------------------------- lint
def _item(iid, org):
    return az_json(["boards", "work-item", "show", "--id", str(iid), "--org", org,
                    "--expand", "relations", "-o", "json"])


def cmd_lint(a):
    any_fail = False
    for iid in a.ids:
        try:
            it = _item(iid, a.org)
        except RuntimeError as e:
            print(f"#{iid}: ERROR reading item ({e})")
            any_fail = True
            continue
        f = it.get("fields", {})
        rels = it.get("relations", []) or []
        wtype = f.get("System.WorkItemType", "")
        title = f.get("System.Title", "")
        checks = []  # (level, ok, msg)

        # body in the right field for this type
        bf = BODY_FIELD.get(wtype, "System.Description")
        checks.append(("FAIL", bool(_strip(f.get(bf, ""))),
                       f"body in {bf}" + ("" if wtype != "Bug" else " (Bug=ReproSteps, not Description)")))
        # A NON-EMPTY BODY IS NOT A COMPLETE ONE. `az boards work-item create --fields` truncates a
        # value at its FIRST NEWLINE, so a multi-paragraph body arrives as its opening line and
        # nothing else, and a "populated" check passes it. Pass `--rest` BEFORE the subcommand to
        # create over urllib and avoid the truncation at source. Signal used: block tags left open
        # with no closing '>' at the end, which is the shape a mid-tag cut leaves and which a
        # short-but-whole body does not.
        raw_body = f.get(bf, "") or ""
        if _strip(raw_body):
            opens = len(re.findall(r"<(?:p|ul|ol|li|div|table|tr|blockquote)\b[^>]*>", raw_body, re.I))
            closes = len(re.findall(r"</(?:p|ul|ol|li|div|table|tr|blockquote)\s*>", raw_body, re.I))
            cut = opens > closes and not raw_body.rstrip().endswith(">")
            # Always print this line, pass or fail. A check that only speaks up when it fails is one
            # nobody knows is running.
            checks.append(("FAIL", not cut,
                           (f"body TRUNCATED: " if cut else "body not truncated: ")
                           + f"{len(raw_body)} chars, {opens} block tag(s) open / {closes} closed"
                           + (", and it does not end on a tag. Re-send with --rest; "
                              "`az --fields` cuts at the first newline" if cut else "")))
        # Bug should NOT stash its content only in Description
        if wtype == "Bug" and _strip(f.get("System.Description", "")) and not _strip(f.get("Microsoft.VSTS.TCM.ReproSteps", "")):
            checks.append(("FAIL", False, "Bug content is in Description but ReproSteps is empty"))
        if wtype in AC_OFF_FORM:
            checks.append(_hidden_ac_check(wtype, f))
        # Story needs real AC
        if wtype == "User Story":
            ac_raw = _strip(f.get(AC_FIELD, ""))
            checks.append(("FAIL", bool(ac_raw),
                           "AcceptanceCriteria field populated (not buried in body)"))
            # AC SHAPE, not just presence. "Populated" passes numbered lists, prose and
            # half-converted criteria alike. A check that passes proves the CHECK passed, not the
            # CLAIM, so the shape is asserted rather than assumed.
            #
            # Requires GIVEN and THEN, because those two carry the falsifiability: a precondition
            # and an observable outcome. WHEN and a Scenario label are warnings, not failures - a
            # state-based criterion can legitimately omit the action.
            if ac_raw:
                up = _strip(_TAG_RE.sub(" ", ac_raw)).upper()
                has_given, has_then, has_when = "GIVEN" in up, "THEN" in up, "WHEN" in up
                checks.append(("FAIL", has_given and has_then,
                               "AC is Gherkin (GIVEN ... THEN ...), not a numbered list or prose"))
                checks.append(("WARN", has_when,
                               "AC names the action with WHEN"))
                checks.append(("WARN", "SCENARIO" in up,
                               "AC groups criteria under a Scenario label"))
        # Parent link
        has_parent = any(r.get("rel") == "System.LinkTypes.Hierarchy-Reverse" for r in rels)
        checks.append(("FAIL", has_parent, "has a Parent link"))
        # non-root iteration
        itp = f.get("System.IterationPath", "")
        checks.append(("FAIL", "\\" in itp, f"non-root iteration ({itp or 'unset'})"))
        # title convention
        checks.append(("FAIL", bool(TITLE_RE_LOOSE.match(title)), f"title = '{TITLE_FORM_LOOSE}'"))
        checks.append(("WARN", bool(TITLE_RE.match(title)), "title has an explicit '{Page - Section}'"))
        # comment pile
        cc = f.get("System.CommentCount", 0) or 0
        checks.append(("WARN", cc <= 3, f"comment count sane ({cc}); body should be the living spec, not a pile"))

        item_fail = any(lvl == "FAIL" and not ok for lvl, ok, _ in checks)
        any_fail = any_fail or item_fail
        print(f"\n#{iid} [{wtype}] {'OK' if not item_fail else 'FAILED'} - {title}")
        for lvl, ok, msg in checks:
            mark = "ok " if ok else ("XX " if lvl == "FAIL" else "!! ")
            print(f"  {mark}{msg}")
    return 1 if any_fail else 0


# ---------------------------------------------------------------- cli
def main():
    global USE_REST, ORG_DEFAULT, PROJ_DEFAULT
    p = argparse.ArgumentParser(prog="ado.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--org", default=ORG_DEFAULT)
    p.add_argument("--project", default=PROJ_DEFAULT)
    p.add_argument("--rest", action="store_true",
                   help="reach ADO over the PAT + urllib instead of the `az` CLI. Same gate, same "
                        "checks; only the transport changes. Needed on any machine where `az` sits "
                        "in an enterprise ask list and blocks on a prompt.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="title search open+closed (search FIRST)")
    s.add_argument("keywords", nargs="+")
    s.add_argument("--state", choices=["all", "open", "closed"], default="all")
    s.add_argument("--min-terms", dest="min_terms", type=int,
                   help="score floor: distinct shared terms required (default 2, or 1 "
                        "when the keywords yield a single term)")
    s.add_argument("--top", type=int, default=None,
                   help=f"cap the ranked list (default {TOP_DEFAULT}; 0 = uncapped)")
    s.set_defaults(func=cmd_search)

    n = sub.add_parser("new", help="create - gated on dup-search + formalization")
    n.add_argument("--type", required=True, help="Bug | Story | Task")
    n.add_argument("--title", required=True, help=TITLE_FORM)
    n.add_argument("--keywords", type=lambda s: [k.strip() for k in s.split(",") if k.strip()],
                   help="comma-separated dup-search terms (defaults to the title's "
                        "segments; each term is matched as a phrase AND word-by-word)")
    n.add_argument("--min-terms", dest="min_terms", type=int,
                   help="dup-gate score floor (default 2 distinct shared terms)")
    n.add_argument("--top", type=int, default=None, help="cap the ranked dup list")
    n.add_argument("--parent", type=int, help="parent Feature id (required)")
    n.add_argument("--iteration", help="sprint path (required, non-root)")
    n.add_argument("--body", help="file: ReproSteps (Bug) or Description (Story/Task)")
    n.add_argument("--ac", help="file: Acceptance Criteria (required for Story)")
    n.add_argument("--area")
    n.add_argument("--assigned")
    n.add_argument("--force", action="store_true", help="create despite ADO title dup matches "
                   "(the Figma cross-check still runs)")
    n.add_argument("--figma-ok", dest="figma_ok", metavar="ID[,ID]",
                   help="Figma thread ids the body does not link because each is a separate ask")
    n.add_argument("--skip-figma", dest="skip_figma", metavar="WHY",
                   help="create without the Figma cross-check; the reason is printed")
    n.add_argument("--dry-run", action="store_true")
    n.set_defaults(func=cmd_new)

    l = sub.add_parser("lint", help="formalization audit of existing item(s)")
    l.add_argument("ids", nargs="+", type=int)
    l.set_defaults(func=cmd_lint)

    a = p.parse_args()
    try:  # every command reaches ADO: say what to set rather than guess a project
        a.org = (a.org or kit_config.need("ado", "org")).rstrip("/")
        a.project = a.project or kit_config.need("ado", "project")
    except kit_config.NotConfigured as e:
        print(f"ado.py: {e}", file=sys.stderr)
        sys.exit(2)
    ORG_DEFAULT, PROJ_DEFAULT = a.org, a.project  # what _rest_dispatch falls back to
    USE_REST = bool(getattr(a, "rest", False))
    if USE_REST:
        # Say which wire it went out on, so nobody has to reconstruct it later.
        print("transport: PAT + urllib (--rest). Same gate, same checks; `az` not invoked.")
    try:
        sys.exit(a.func(a))
    except RuntimeError as e:
        print(("rest error: " if USE_REST else "az error: ") + str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
