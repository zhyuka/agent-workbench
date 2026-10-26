"""Read and audit the whole ADO board over REST, using the PAT. No `az`, so nothing prompts.

  python ado_rest.py pull                 refresh the local cache (every item + comments)
  python ado_rest.py audit                every check, N of M each
  python ado_rest.py show <id>            one item with its comments and attached files
  python ado_rest.py decisions            open items tagged `decision` (ado.decision_tag), live

CONFIG. ado.org and ado.project from kit_config.py; unset, every command exits 2 naming the key.
Optional: ado.title_prefix (title-convention), ado.frozen_branch (frozen-branch-mentioned),
ado.backlog_iteration (done-in-backlog), ado.phase_suffixes (dup-title), ado.decision_tag.

WHY REST AND NOT az. Where `az` sits behind a permission prompt, every call blocks on a human click,
and a sweep of a few hundred items becomes hundreds of prompts and therefore a sweep nobody runs.
The PAT in `ADO_PAT` (or the OS keychain, see kit_config.token) authenticates over python urllib.

THE TOKEN IS NEVER PRINTED. It is read from the environment or keychain, used in an Authorization
header, and never echoed, logged, or written to the cache.

THE CACHE sits in ~/.cache/agent-workbench/ado/<org>--<project>.json, with downloaded images and
attachments beside it in <same name>-media/. Not beside this file: a plugin's folder is replaced on
every update and shared by every project that uses it.

WHAT THE CHECKS ENCODE. Each one is a rule that was prose and was still violated:

  closed-with-open-children  Closed means shipped AND QA'd AND no follow-ups. An item closed over an
                             open child is none of those. Open means New or Active, as in the ado
                             skill's close check; a Resolved child does not count.
  same-category-parent       A parent in the child's own backlog category (a Story under a Story, a
                             Bug under a Task): ADO then hides the parent from that backlog.
                             Closed children count. `pull` prints these too, with the fix.
  frozen-branch-mentioned    Text that couples a merge or PR to ado.frozen_branch, a branch nobody
                             may propose merging to. Runs only when that key is set.
  figma-not-a-url            The item mentions Figma but carries no figma.com link. (A bare node id
                             with no mention of Figma is not detected.)
  figma-colon-nodeid         A figma.com URL whose node-id has a colon where the hyphen goes.
  ac-not-gherkin             Acceptance criteria are Given/When/Then.
  orphan                     A build change hangs off a parent (a Story off a Feature); a standalone
                             item loses its thread.
  dup-title                  Near-identical titles are the duplicate-item failure ado.py guards on
                             create; this catches the ones already there.
  parent-missing             A parent id that does not resolve inside this project.
  iteration-mismatch         An open child planned in a different sprint from its Story parent.
  root-iteration             A Story, Bug or Task parked at the project root; only Epics and
                             Features belong there.
  done-in-backlog            The dateless backlog iteration (ado.backlog_iteration) is for UNSTARTED
                             work. Renaming an iteration can leave completed items in it. Runs only
                             when that key is set.
  ac-empty                   An open Story with no acceptance criteria. ado.py's lint asks for
                             them on a Story only; a Bug's, when it has any, go to System Info.
  bug-body-wrong-field       A Bug whose content sits in Description; ADO renders ReproSteps.
  body-empty                 An open item with an empty body. A pasted image counts as a body.
  title-convention           ado.py's strict TITLE_RE (<prefix> // Page - Section // what). ado.py
                             new enforces only the looser TITLE_RE_LOOSE, and its lint warns on
                             this one.
  dangling-ref               A body citing a work item that does not exist.
  comment-too-long           Over ~700 characters; the detail belongs in the body.
  comment-pile               Consecutive comments by one author with no reply between: patch, don't stack.
  comment-stale-branch       "not yet pushed" or "N commits ahead" goes false on the next push, and
                             then the comment says something untrue for as long as it stands.
  comment-dangling-ref       A comment citing a work item that does not exist.

It is a REPORT by default. `--expect clean` turns it into a gate so it can fail a run.
"""
import argparse
import base64
import difflib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ado import (AC_FIELD, BODY_FIELD, TITLE_RE,  # one copy of each rule: ado.py's
                 CATEGORY, categories_from, same_category)
import kit_config

ORG, PROJECT = kit_config.ado()  # the project's .claude/agent-workbench.json (kit_config.py); None when unset


def _ready():
    """Stop with what to set, before the token is even read, when the org or project is unset."""
    if not ORG:
        kit_config.need("ado", "org")
    if not PROJECT:
        kit_config.need("ado", "project")
API = "api-version=7.0"


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "unset"


CACHE = os.path.join(os.path.expanduser("~"), ".cache", kit_config.NAME, "ado",
                     f"{_slug((ORG or '').rsplit('/', 1)[-1])}--{_slug(PROJECT)}.json")


def _hdr():
    tok = kit_config.token("ADO_PAT", "AZURE_DEVOPS_EXT_PAT")
    if not tok:
        sys.exit("No ADO_PAT / AZURE_DEVOPS_EXT_PAT in the environment or the OS keychain.")
    return {"Authorization": "Basic " + base64.b64encode((":" + tok).encode()).decode(),
            "Content-Type": "application/json"}


def _req(url, body=None, method=None):
    _ready()
    r = urllib.request.Request(url, headers=_hdr(),
                               data=json.dumps(body).encode() if body else None,
                               method=method or ("POST" if body else "GET"))
    try:
        return json.load(urllib.request.urlopen(r, timeout=120))
    except urllib.error.HTTPError as e:
        sys.exit(f"ADO {e.code}: {e.reason}")


def _p(path):
    return f"{ORG}/{urllib.parse.quote(PROJECT)}{path}"


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s).strip()


# ---------------------------------------------------------------- embedded media
# TEAMS LOG REQUIREMENTS AS SCREENSHOTS PASTED INTO FIELDS AND COMMENTS.
# strip_html() deletes <img> along with every other tag, so an image-only field comes back as the
# empty string and reads as "this ticket has no content", when the whole requirement is one pasted
# screenshot. A field that holds only an image MUST NOT render as absent.
IMG_RE = re.compile(r"<img[^>]*\bsrc=\"([^\"]+)\"", re.I)
MEDIA = CACHE[:-len(".json")] + "-media"  # beside the cache, for the same reason


def media_in(s):
    """Every image URL referenced by a field or comment body."""
    return IMG_RE.findall(s or "")


def fetch_media(wid, urls):
    """Download to <media>/<wid>/ and return local paths. A download that FAILS is reported as a
    failure, never skipped: a missing screenshot is a missing requirement, not an empty field."""
    out = []
    if not urls:
        return out
    d = os.path.join(MEDIA, str(wid))
    os.makedirs(d, exist_ok=True)
    for i, u in enumerate(dict.fromkeys(urls), 1):
        ext = ".png"
        m = re.search(r"fileName=[^&]*?(\.[A-Za-z0-9]{3,4})\b", u)
        if m:
            ext = m.group(1)
        p = os.path.join(d, f"{i:02d}{ext}")
        if os.path.exists(p) and os.path.getsize(p) > 0:
            out.append(p)
            continue
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(u, headers=_hdr()), timeout=120).read()
            with open(p, "wb") as fh:
                fh.write(raw)
            out.append(p)
        except Exception as e:
            out.append(f"FAILED {type(e).__name__}: {u[:90]}")
    return out


# ATTACHED FILES ARE NOT IN ANY FIELD. A file attached to a work item is an `AttachedFile`
# relation, and a reader of fields and comments never sees one. A whole build spec can be an
# attached .html, and an item's screenshots can be attachments only, never pasted inline.
def fetch_attachments(wid, rels):
    """Download each AttachedFile relation to <media>/<wid>/attachments/ and return
    (name, size, created, local path or a FAILED line). The file name starts with the attachment
    id, so a re-run reuses a complete download. A download that fails or comes back short is
    reported, never skipped."""
    out = []
    d = os.path.join(MEDIA, str(wid), "attachments")
    for r in rels:
        at = r.get("attributes") or {}
        guid = r["url"].rsplit("/", 1)[-1]
        name = at.get("name") or guid
        size = at.get("resourceSize")
        made = (at.get("resourceCreatedDate") or "")[:10]
        p = os.path.join(d, guid[:8] + "-" + re.sub(r"[\\/:]", "_", name))
        if os.path.exists(p) and (size is None or os.path.getsize(p) == size):
            out.append((name, size, made, p))
            continue
        try:
            os.makedirs(d, exist_ok=True)
            # A temp file of its own. With one fixed name, two runs fetching the same item write into
            # one file, and one moves it into place while the other is still writing it.
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".part")
            try:
                with os.fdopen(fd, "wb") as fh, urllib.request.urlopen(
                        urllib.request.Request(r["url"], headers=_hdr()), timeout=300) as resp:
                    shutil.copyfileobj(resp, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                got = os.path.getsize(tmp)
                if size is not None and got != size:
                    os.remove(tmp)
                    out.append((name, size, made, f"FAILED short download, {got:,} of {size:,} bytes"))
                    continue
                try:
                    os.chmod(tmp, os.stat(p).st_mode & 0o777)  # mkstemp makes it 0600; keep the file's mode
                except FileNotFoundError:
                    pass
                os.replace(tmp, p)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            out.append((name, size, made, p))
        except Exception as e:
            out.append((name, size, made, f"FAILED {type(e).__name__}: {r['url'][:90]}"))
    return out


# NO LENGTH CAP. Cutting a field at a fixed length with no marker, then closing with "the text above
# is the whole item", hides part of a requirement while saying it is complete. A cut requirement
# reads as a complete one, so the whole field prints.
def render_field(label, raw, wid):
    """Print one field's text, in full, AND its images. Returns (images, characters of text).

    Prints nothing only when the field has neither, which is the one case where 'empty' is true."""
    txt = strip_html(raw)
    imgs = media_in(raw)
    if not txt and not imgs:
        return 0, 0
    print(f"\n  --- {label} ---")
    if txt:
        print("  " + txt.replace("\n", "\n  "))
    if imgs:
        local = fetch_media(wid, imgs)
        note = "IMAGE-ONLY, the text above is empty" if not txt else "plus text above"
        print(f"  [{len(imgs)} image(s), {note}] -- READ THESE, they usually carry the requirement:")
        for p in local:
            print(f"    {p}")
    return len(imgs), len(txt)


def pull():
    _ready()
    wiql = {"query": "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject]='"
                     + PROJECT.replace("'", "''") + "' ORDER BY [System.Id]"}
    ids = [w["id"] for w in _req(_p(f"/_apis/wit/wiql?{API}"), wiql)["workItems"]]
    print(f"  {len(ids)} work item(s)")

    items = {}
    for i in range(0, len(ids), 190):
        chunk = ids[i:i + 190]
        d = _req(f"{ORG}/_apis/wit/workitemsbatch?{API}",
                 {"ids": chunk, "$expand": "relations"})
        for w in d["value"]:
            items[w["id"]] = w
        print(f"    fetched {len(items)}/{len(ids)}")

    comments = {}
    for n, wid in enumerate(ids, 1):
        d = _req(_p(f"/_apis/wit/workItems/{wid}/comments?api-version=7.0-preview.3"))
        comments[str(wid)] = [{"id": c.get("id"), "text": c.get("text", ""),
                               "by": (c.get("createdBy") or {}).get("displayName", ""),
                               "on": c.get("createdDate", "")} for c in d.get("comments", [])]
        if n % 60 == 0:
            print(f"    comments {n}/{len(ids)}")

    # The board's own backlog categories, so same-category-parent checks what ADO checks. A failed
    # read is cached as None and the checks fall back to ado.py's CATEGORY.
    try:
        cats = categories_from(_req(_p(f"/_apis/work/backlogconfiguration?{API}")))
    except (SystemExit, Exception) as e:
        cats = None
        print(f"  backlog configuration not read ({e}); same-category-parent uses ado.py's CATEGORY")

    # Write a temp file beside the cache and move it into place, so a reader never sees a half-written
    # cache and a pull killed partway leaves the old one whole.
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CACHE), prefix=".ado-cache.", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pulled": time.time(),
                       "items": {str(k): v for k, v in items.items()}, "comments": comments,
                       "categories": cats},
                      fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, os.stat(CACHE).st_mode & 0o777)  # mkstemp makes it 0600; keep the file's mode
        except FileNotFoundError:
            pass
        os.replace(tmp, CACHE)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    print(f"  cached {len(items)} item(s), {sum(len(v) for v in comments.values())} comment(s)")

    # Printed on every pull, not only in the audit: the audit runs when someone asks for it, and
    # pull runs from many places, so a hidden parent is seen days sooner.
    warn_nested({str(k): v for k, v in items.items()}, cats)


def warn_nested(byid, cats):
    nested = nested_same_category(byid, cats or dict(CATEGORY))
    if nested:
        print(f"\n  WARN {len(nested)} parent link(s) inside one backlog category. ADO hides each "
              "parent from its backlog and turns off reordering until the child moves up a level:")
        for wid, pid, _ in nested:
            up = parent_id(byid[pid])
            print(f"    #{wid} {byid[wid][F].get(TYPE)} under {byid[pid][F].get(TYPE)} #{pid}. Fix: "
                  f"ado_write.py reparent {wid} {up or f'<the item above #{pid}>'} --keep-related")
    return len(nested)


# How old the cache may be before a verdict off it is worthless. The board is edited by several
# people and by other sessions, so this is deliberately short.
MAX_CACHE_AGE = 30 * 60


def load(stale_ok=False):
    """Load the cache, REFUSING a stale one.

    An audit off a cache with no freshness check prints a confident count against a board where
    those items were fixed minutes earlier. A checker that reports last week's board in today's
    format is worse than no checker: the number looks measured, so it gets quoted. Age is part of
    the read, and a stale cache stops the run rather than colouring it."""
    if not os.path.exists(CACHE):
        sys.exit("No cache. Run `ado_rest.py pull` first.")
    d = json.load(open(CACHE, encoding="utf-8"))
    age = time.time() - d.get("pulled", 0)
    if not stale_ok and age > MAX_CACHE_AGE:
        when = ("unknown - pulled before this cache recorded timestamps"
                if not d.get("pulled") else f"{age / 60:.0f} minutes old")
        sys.exit(f"REFUSED: cache is {when} (limit {MAX_CACHE_AGE // 60}m).\n"
                 f"  Run `ado_rest.py pull` first, or pass --stale-ok to audit it knowingly.\n"
                 f"  An audit off a stale cache reports items you have already fixed.")
    d["_age_min"] = age / 60
    return d


F = "fields"
TITLE = "System.Title"
STATE = "System.State"
TYPE = "System.WorkItemType"


def parent_id(w):
    for rel in w.get("relations") or []:
        if rel.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
            return rel["url"].rsplit("/", 1)[-1]
    return None


def nested_same_category(allitems, cats):
    """(child id, parent id, category) for each parent link inside one backlog category (ado.py
    CATEGORY says why). Every item, Closed too: Closed children hide their parent just the same."""
    out = []
    for wid, w in allitems.items():
        pid = parent_id(w)
        p = allitems.get(pid) if pid else None
        cat = p and same_category(w[F].get(TYPE), p[F].get(TYPE), cats)
        if cat:
            out.append((wid, pid, cat))
    return out


# Title endings that mark a planned pair on one stem, not a duplicate. ado.phase_suffixes overrides.
PHASES_DEFAULT = ("design", "build", "qa", "content map", "visual design", "copy", "review")


def audit(expect_clean=False, scope_all=False, stale_ok=False):
    """Default scope is OPEN items. Closed items are the record of what happened, not a to-do list:
    retro-editing a Closed item to satisfy a rule written later would falsify the history and is
    pure churn. Fix the ones still in flight.

    `--all` restores the full sweep for a genuine archaeology pass."""
    d = load(stale_ok)
    allitems, comments = d["items"], d["comments"]
    OPEN_STATES = {"New", "Active", "Resolved"}
    items = (allitems if scope_all
             else {k: v for k, v in allitems.items() if v[F].get(STATE) in OPEN_STATES})
    total = len(items)
    findings = {}
    denom = {}  # a check that does not scan `items` states its own M
    frozen = kit_config.get("ado", "frozen_branch")
    backlog = kit_config.get("ado", "backlog_iteration")

    def add(k, msg):
        findings.setdefault(k, []).append(msg)

    # Hierarchy comes from ALL items, never the filtered set. Built from the open-only view, the
    # closed-with-open-children check could not fire at all - every Closed parent would be filtered
    # out before it ran. A check that cannot fail is worse than no check; it launders the defect.
    parent_of, children = {}, {}
    for wid, w in allitems.items():
        for rel in w.get("relations") or []:
            if rel.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
                pid = rel["url"].rsplit("/", 1)[-1]
                parent_of[wid] = pid
            if rel.get("rel") == "System.LinkTypes.Hierarchy-Forward":
                children.setdefault(wid, []).append(rel["url"].rsplit("/", 1)[-1])

    CONTAINER = ("Epic", "Feature")
    # A '#' and digits standing alone. A CSS colour reads the same way (#030712, #2563eb): the
    # trailing-word guard drops the ones with letters, and a number with a leading zero or above the
    # highest id on the board is not counted as a reference. BLIND TO: an all-digit colour below the
    # highest id.
    REF = re.compile(r"(?<![\w&])#([1-9]\d{0,6})(?!\w)")
    top_id = max((int(k) for k in allitems if str(k).isdigit()), default=0)

    def has(raw):
        # A field that holds only a pasted screenshot is not empty (see media_in above).
        return bool(strip_html(raw) or media_in(raw))

    def leaf(f):
        return (f.get("System.IterationPath") or "").split("\\")[-1]

    def dangling(text):
        return sorted({int(n) for n in REF.findall(text)
                       if int(n) <= top_id and str(int(n)) not in allitems})

    for k in ("parent-missing", "iteration-mismatch", "root-iteration", "ac-empty",
              "bug-body-wrong-field", "body-empty", "title-convention"):
        denom[k] = 0
    for wid, w in items.items():
        f = w[F]
        st, ty, ti = f.get(STATE, ""), f.get(TYPE, ""), f.get(TITLE, "")
        working = st in ("New", "Active")
        pid = parent_of.get(wid)
        if pid:
            denom["parent-missing"] += 1
            if pid not in allitems:
                add("parent-missing", f"#{wid} {st} {ty} '{ti[:50]}' parent #{pid} is not in this project")
            elif working and allitems[pid][F].get(TYPE) not in CONTAINER:
                denom["iteration-mismatch"] += 1
                if leaf(allitems[pid][F]) != leaf(f):
                    add("iteration-mismatch", f"#{wid} {st} '{ti[:45]}' is in {leaf(f)}, parent "
                                              f"#{pid} in {leaf(allitems[pid][F])}")
        if ty not in CONTAINER:
            denom["root-iteration"] += 1
            if "\\" not in (f.get("System.IterationPath") or ""):
                add("root-iteration", f"#{wid} {st} {ty} '{ti[:55]}' sits at the project root iteration")
            denom["title-convention"] += 1
            if not TITLE_RE.match(ti):
                add("title-convention", f"#{wid} {st} {ty} '{ti[:60]}'")
        if working and ty == "User Story":
            denom["ac-empty"] += 1
            if not has(f.get(AC_FIELD)):
                add("ac-empty", f"#{wid} {st} '{ti[:55]}' has no acceptance criteria")
        if ty == "Bug":
            denom["bug-body-wrong-field"] += 1
            if not has(f.get(BODY_FIELD["Bug"])) and has(f.get("System.Description")):
                add("bug-body-wrong-field", f"#{wid} {st} '{ti[:55]}' content is in Description, "
                                            "which a Bug does not render")
        if working and ty not in CONTAINER:
            denom["body-empty"] += 1
            if not has(f.get(BODY_FIELD.get(ty, "System.Description"))):
                add("body-empty", f"#{wid} {st} {ty} '{ti[:55]}' has an empty body")
        for n in dangling(" ".join(strip_html(f.get(x, "")) for x in (
                "System.Description", "Microsoft.VSTS.TCM.ReproSteps", AC_FIELD,
                "Microsoft.VSTS.TCM.SystemInfo"))):
            add("dangling-ref", f"#{wid} {st} '{ti[:50]}' cites #{n}, which does not exist")
        # ReproSteps is the BODY FIELD FOR BUGS. Leaving it out makes every Bug's body invisible to
        # every text check below, and Bugs are most of the build defects on a board.
        blob = " ".join([ti, strip_html(f.get("System.Description", "")),
                         strip_html(f.get("Microsoft.VSTS.TCM.ReproSteps", "")),
                         strip_html(f.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")),
                         " ".join(c["text"] for c in comments.get(wid, []))])

        if re.search(r"\bfigma\b", blob, re.I) and "figma.com" not in blob.lower():
            add("figma-not-a-url", f"#{wid} {st} '{ti[:55]}' mentions Figma with no figma.com URL")
        for m in re.finditer(r"figma\.com[^\s)\]]*node-id=(\d+):(\d+)", blob):
            add("figma-colon-nodeid", f"#{wid} URL uses a colon in node-id ({m.group(1)}:{m.group(2)}); needs a hyphen")

        if frozen and re.search(r"\b(merge|PR|pull request)\b[^.]{0,60}(?<![\w/-])" + re.escape(frozen)
                                + r"(?![\w/-])", blob, re.I):
            add("frozen-branch-mentioned", f"#{wid} {st} '{ti[:55]}' text couples a merge/PR to `{frozen}`")

        ac = strip_html(f.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""))
        if ty in ("User Story", "Bug", "Task") and ac and not re.search(r"\bgiven\b", ac, re.I):
            add("ac-not-gherkin", f"#{wid} {st} {ty} '{ti[:55]}' has AC that is not Given/When/Then")

        if ty in ("Task", "Bug", "User Story") and wid not in parent_of:
            add("orphan", f"#{wid} {st} {ty} '{ti[:55]}' has no parent")

    # Closed parents live outside the open-scoped loop, so this runs over the full set. So does
    # done-in-backlog: completed work parked in the unstarted bucket is the thing it looks for.
    # Each states its own M.
    denom["done-in-backlog"] = denom["closed-with-open-children"] = 0
    for wid, w in allitems.items():
        if w[F].get(STATE) in ("Resolved", "Closed") and backlog:
            denom["done-in-backlog"] += 1
            if leaf(w[F]) == backlog:
                add("done-in-backlog", f"#{wid} {w[F].get(STATE)} {w[F].get(TYPE, '')} "
                                       f"'{w[F].get(TITLE, '')[:50]}' is done but sits in {backlog}")
        if w[F].get(STATE) != "Closed":
            continue
        denom["closed-with-open-children"] += 1
        # New and Active only: the ado skill's close check reads an open child as New/Active.
        kids = [c for c in children.get(wid, [])
                if allitems.get(c, {}).get(F, {}).get(STATE) in ("New", "Active")]
        if kids:
            add("closed-with-open-children",
                f"#{wid} Closed '{w[F].get(TITLE,'')[:50]}' has open child/children: "
                + ", ".join("#" + k for k in kids))

    # A parent in the child's own backlog category hides the parent (ado.py CATEGORY says why), over
    # the full set. The categories are the board's own, cached by pull. Fix: move the child up a
    # level with `ado_write.py reparent <id> <id above> --keep-related`.
    cats = d.get("categories") or dict(CATEGORY)
    denom["same-category-parent"] = sum(1 for wid, pid in parent_of.items()
                                        if allitems[wid][F].get(TYPE) in cats and pid in allitems)
    for wid, pid, _ in nested_same_category(allitems, cats):
        f = allitems[wid][F]
        add("same-category-parent",
            f"#{wid} {f.get(STATE, '')} {f[TYPE]} '{f.get(TITLE, '')[:45]}' under "
            f"{allitems[pid][F].get(TYPE)} #{pid}, which the backlog then hides")

    # A repeated GENERIC task name is a template working as intended - every page epic gets its
    # own "Build", "QA". Comparing those is pure noise. Only compare descriptive titles, and only
    # when the two items share a parent (or both have none).
    #
    # A long shared prefix also drowns the one trailing word that carries ALL the meaning: "<long
    # stem> Design" and "<long stem> Build" score ~0.97 and must both exist. So the phase suffix is
    # split off and the halves compared separately - same stem + DIFFERENT phase is a pair and is
    # fine; same stem + same phase is the real duplicate this check is for.
    PHASES = tuple(str(p).lower() for p in (kit_config.get("ado", "phase_suffixes") or PHASES_DEFAULT))

    def _split_phase(t):
        low = t.lower().rstrip(" -–—")
        for p in sorted(PHASES, key=len, reverse=True):
            if low.endswith(" " + p):
                return low[: -(len(p) + 1)].strip(" -–—"), p
        return low, ""

    titles = [(wid, items[wid][F].get(TITLE, "")) for wid in items
              if len(items[wid][F].get(TITLE, "")) >= 25]
    seen = set()
    for i, (a, ta) in enumerate(titles):
        for b, tb in titles[i + 1:]:
            if (a, b) in seen or not ta or not tb:
                continue
            if parent_of.get(a) != parent_of.get(b):
                continue
            stem_a, phase_a = _split_phase(ta)
            stem_b, phase_b = _split_phase(tb)
            if phase_a != phase_b:
                continue  # Design vs Build on one stem: the pattern, not a duplicate.
            if difflib.SequenceMatcher(None, stem_a, stem_b).ratio() > 0.93:
                seen.add((a, b))
                add("dup-title", f"#{a} and #{b} are ~identical: '{ta[:60]}'")

    # Comment checks read the thread in date order: a raw count cannot tell a pile from a
    # conversation. The cache holds every comment the API returned, deleted ones excluded.
    COMMENT_MAX = 700  # the ado skill's target; ado_write.py warns at the same length
    STALE = re.compile(r"not yet pushed|local[- ]only|unpushed|not pushed|"
                       r"\d+\s+commits?\s+ahead|is\s+\d+\s+ahead", re.I)
    read = followed = 0
    for wid in items:
        thread = sorted(comments.get(wid, []), key=lambda c: (c.get("on", ""), c.get("id") or 0))
        prev = None
        for c in thread:
            read += 1
            body, who = strip_html(c.get("text", "")), c.get("by", "")
            where = f"#{wid} comment {c.get('id') or '?'} ({c.get('on', '')[:10]}, {who.split(',')[0]})"
            if len(body) > COMMENT_MAX:
                add("comment-too-long", f"{where} is {len(body)} chars")
            m = STALE.search(body)
            if m:
                add("comment-stale-branch", f"{where} says {m.group(0)[:38]!r}")
            for n in dangling(body):
                add("comment-dangling-ref", f"{where} cites #{n}, which does not exist")
            if prev is not None:
                followed += 1
                if who == prev:
                    add("comment-pile", f"{where} follows another by the same author, no reply between")
            prev = who
    for k in ("comment-too-long", "comment-stale-branch", "comment-dangling-ref"):
        denom[k] = read
    denom["comment-pile"] = followed

    order = ["closed-with-open-children", "same-category-parent", "frozen-branch-mentioned",
             "figma-not-a-url", "figma-colon-nodeid", "ac-not-gherkin", "orphan", "dup-title",
             "parent-missing", "iteration-mismatch", "root-iteration", "done-in-backlog",
             "ac-empty", "bug-body-wrong-field", "body-empty", "title-convention", "dangling-ref",
             "comment-too-long", "comment-pile", "comment-stale-branch", "comment-dangling-ref"]
    off = {"frozen-branch-mentioned": None if frozen else "ado.frozen_branch",
           "done-in-backlog": None if backlog else "ado.backlog_iteration"}
    bad = 0
    print(f"\n  {total} work item(s) audited  (cache {d.get('_age_min', 0):.0f}m old)\n")
    for k in order:
        if off.get(k):
            print(f"  --   {k:26} did not run: {off[k]} is not set")
            continue
        v = findings.get(k, [])
        bad += len(v)
        tag = "ok  " if not v else "FAIL"
        m = denom.get(k, total)
        print(f"  {tag} {k:26} {m - len(v):>3} of {m}")
        for line in v[:12]:
            print(f"         - {line}")
        if len(v) > 12:
            print(f"         ... and {len(v)-12} more")
    ran = len([k for k in order if not off.get(k)])
    print(f"\n  {bad} finding(s) across {ran} check(s)"
          + (f"; {len(order) - ran} did not run" if ran < len(order) else ""))
    if expect_clean and bad:
        return 1
    if not expect_clean:
        print("  Report mode. Re-run with --expect clean to make it a gate.")
    return 0


def show(wid):
    d = load()
    w = d["items"].get(str(wid))
    if not w:
        sys.exit(f"#{wid} not in the cache.")
    f = w[F]
    print(f"  #{wid}  {f.get(TYPE)}  {f.get(STATE)}  {f.get(TITLE)}")
    print(f"  {ORG}/{urllib.parse.quote(PROJECT)}/_workitems/edit/{wid}")
    n = chars = parts = 0

    def _render(label, raw, wid):
        nonlocal n, chars, parts
        imgs, ln = render_field(label, raw, wid)
        n += imgs
        chars += ln
        parts += 1 if imgs or ln else 0

    # Walk EVERY text-bearing field, not a hardcoded few. A list of description + acceptance
    # criteria never prints ReproSteps, which is where a Bug's detail lives. A field this tool does
    # not know about is still a field somebody wrote the requirement into.
    known = [("description", "System.Description"),
             ("acceptance criteria", "Microsoft.VSTS.Common.AcceptanceCriteria"),
             ("repro steps / detail", "Microsoft.VSTS.TCM.ReproSteps")]
    seen = {k for _, k in known}
    for label, key in known:
        _render(label, f.get(key, ""), wid)
    for key, val in sorted(f.items()):
        if key in seen or not isinstance(val, str):
            continue
        if "<" not in val and len(val) < 200:
            continue          # a short plain scalar is a field value, not prose
        _render(key, val, wid)
    for c in d["comments"].get(str(wid), []):
        _render(f"comment {c['on'][:10]}", c["text"], wid)
    att = fetch_attachments(wid, [r for r in w.get("relations") or []
                                  if r.get("rel") == "AttachedFile"])
    if att:
        print("\n  --- attached files ---")
        for name, size, made, p in att:
            sz = f"{size:,} bytes" if size is not None else "size unknown"
            print(f"  {name}  ({sz}, attached {made})\n    {p}")
        print(f"  [{len(att)} attached file(s)] -- OPEN THESE: an attached .html or .md is often "
              "the whole spec, and a screenshot here is not in the media count")
    failed = sum(p.startswith("FAILED") for *_, p in att)
    # Say what was printed and stop there. Links to commits, PRs and other work items are not
    # printed here, so this must not call the item whole.
    print(f"\n  text: {parts} field(s)/comment(s), {chars} characters, each printed in full")
    print(f"  media: {n} image(s) pasted into fields and comments")
    print(f"  attachments: {len(att)} file(s)"
          + (f", {len(att) - failed} downloaded, {failed} FAILED" if att else ", none attached"))


def decisions():
    # Live, never the cache: whoever triages acts on this list, and a cache up to MAX_CACHE_AGE old
    # can miss a tag added since.
    _ready()
    tag = str(kit_config.get("ado", "decision_tag") or "decision")
    wiql = {"query": "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject]='"
                     + PROJECT.replace("'", "''")
                     + "' AND [System.Tags] CONTAINS '" + tag.replace("'", "''") + "'"
                       " AND [System.State] NOT IN ('Closed','Removed')"
                       " ORDER BY [System.ChangedDate] DESC"}
    ids = [w["id"] for w in _req(_p(f"/_apis/wit/wiql?{API}"), wiql)["workItems"]]
    rows = []
    for i in range(0, len(ids), 190):
        d = _req(f"{ORG}/_apis/wit/workitemsbatch?{API}",
                 {"ids": ids[i:i + 190],
                  "fields": [TYPE, STATE, TITLE, "System.AssignedTo", "System.Tags"]})
        rows += d["value"]
    order = {wid: n for n, wid in enumerate(ids)}
    rows.sort(key=lambda w: order[w["id"]])
    # CONTAINS is the server's match, not ours: keep only an exact tag, and say what was dropped
    # rather than dropping it silently.
    keep = [w for w in rows if tag.lower() in
            [t.strip().lower() for t in (w[F].get("System.Tags") or "").split(";")]]
    print(f"  {len(keep)} open decision(s): tag `{tag}`, state not Closed or Removed (live query)")
    for w in keep:
        f = w[F]
        who = (f.get("System.AssignedTo") or {}).get("displayName", "unassigned")
        print(f"  #{w['id']}  {f.get(TYPE)}  {f.get(STATE)}  {who}  {f.get(TITLE)}")
        print(f"    {ORG}/{urllib.parse.quote(PROJECT)}/_workitems/edit/{w['id']}/")
    for w in rows:
        if w not in keep:
            print(f"  dropped #{w['id']}: tags {w[F].get('System.Tags')!r} match CONTAINS, "
                  "not the exact tag")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["pull", "audit", "show", "decisions"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--expect", choices=["clean"], default=None)
    ap.add_argument("--stale-ok", action="store_true",
                    help="audit a cache older than the freshness limit anyway")
    ap.add_argument("--all", action="store_true", help="include Closed/Removed: archaeology, not a to-do list")
    a = ap.parse_args()
    if a.cmd == "pull":
        pull()
        return 0
    if a.cmd == "audit":
        return audit(a.expect == "clean", a.all, a.stale_ok)
    if a.cmd == "decisions":
        return decisions()
    return show(a.arg)


if __name__ == "__main__":
    sys.exit(kit_config.run(main))
