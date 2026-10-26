#!/usr/bin/env python
"""ado_write.py - the WRITE half of ado_rest.py.

WHY THIS EXISTS. ado.py is the canonical create/lint gate. Where `az` sits behind a permission
prompt, every az call blocks on a click nobody can make from a background run. ado_rest.py reads
over the PAT and python urllib, but it is read-only: pull / audit / show / decisions.

So this file is deliberately NARROW. It does the writes that a board reconcile needs and
nothing else, over the same PAT + urllib path:

    state    <id> <State>              System.State
    field    <id> <ref> <value>        any single field, e.g. Microsoft.VSTS.Common.AcceptanceCriteria;
                                       "" clears it
    comment  <id> <text> [--stack WHY] add a comment. REFUSES if the newest comment on the
                                       item is already yours - nobody has replied, so patch
                                       that one with `editcmt` instead of stacking a second.
                                       Allowed once a teammate has replied. Warns over 700 chars.
    parent   <id> <parentId>           attach an orphan to its parent (a RELATION, not a field -
                                       `field System.Parent` does NOT work). Refuses if already set.
    reparent <id> <newParentId> [--keep-related]
                                       MOVE an item to a different parent (remove + add in one
                                       rev-guarded patch). Refuses if there is no parent, or >1.
                                       --keep-related links the old parent as Related in the same
                                       patch. Both refuse a parent in the child's own backlog
                                       category (a Bug under a Task), before any write.
    editcmt  <id> <commentId> <text>   REPLACE a comment (this is how a prose pile gets fixed;
                                       ADO has no delete-and-repost that keeps the thread)
    delcmt   <id> <commentId>          soft-delete a comment, ONLY to collapse a same-author pile
                                       whose content has already been merged upward
    comments <id>                      list the item's comments with their ids (read-only), for
                                       editcmt/delcmt
    attach   <id> <file>... [--inline] upload each file and link it to the item (Attachments tab).
                                       --inline also appends each picture to the body field
                                       (ReproSteps on a Bug), in the same rev-guarded patch.
                                       Skips a file already attached with the same name and size,
                                       so a re-run adds nothing. Reads the item back after the
                                       patch and exits 1 unless every upload shows on it (and in
                                       the body, with --inline).
    batch    <file.json>               apply a reviewed list of state, field, parent and comment
                                       edits in one run, so one approval covers the list. Skips
                                       an item that changed since the list was made, writes an
                                       undo file in the same format first, stops at the first
                                       failed write. See batch() for the entry format
    --selftest                         run the read-back against a stand-in ADO on 127.0.0.1;
                                       nothing goes to ADO. Exit 0 all cases pass, 1 any fails

`state`, `field`, `comment` and `editcmt` read the item back after the write, with a fresh GET,
and compare what ADO stored with what was sent. A comment or an HTML field (Description, History,
AcceptanceCriteria, ReproSteps, SystemInfo) compares the visible text with entities decoded, block
tags read as spaces and whitespace collapsed, plus every href, src and @mention; any other text
field compares as plain text with whitespace collapsed, so an escaped "&lt;" in a title differs, and
so does a custom HTML field ADO re-encodes: a false alarm, never a false pass. AssignedTo
matches the stored identity's uniqueName, displayName or id; Tags match as a set; a date or time
matches the same moment, a date sent alone matching midnight UTC; State, Reason and tree paths
match in any case; a field cleared with "" must come back empty. How ADO stores a date, and
whether it shifts one by time zone, is UNVERIFIED: a shift would read as not confirmed.

A difference prints WRITE NOT CONFIRMED and exits 1. A read-back that fails prints DID NOT RUN
and exits 2: the write was sent, and whether it landed is unknown. `attach`
reads back too and exits 1 with READ BACK FAILED. `parent`, `reparent` and `delcmt` don't read
back yet.

Exit 1 also covers a write ADO refused, a stacking refusal and a missing token; exit 2 also
covers an unset org or project, where nothing was sent. A write that times out exits 1 even if it
landed. Not compared: markup that changes no text, link or mention, such as a lost <b>, <s> or
alt text, and text moved across list items. Only the first page of comments, up to 200, is read.

It does NOT create work items. Creation stays behind ado.py's search-first gate, which is the
whole reason that gate exists - a create is where duplicates happen, and this file has no
dup-search in it. For a new item run `ado.py --rest new`, which keeps the search-first gate and
skips `az`.

CONFIG. ado.org and ado.project from kit_config.py; unset, a write exits 2 naming the key.

The PAT is read from ADO_PAT / AZURE_DEVOPS_EXT_PAT, or the OS keychain (kit_config.token), and is
never printed, logged or echoed.
Text is sent as-is; ADO renders HTML, so pass HTML (<br>, <ul>) rather than markdown.
"""
import base64, datetime, json, os, re, sys, urllib.request, urllib.error, urllib.parse
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kit_config
from ado import categories_from, same_category, same_category_reason  # one copy of the rule: ado.py's

ORG, PROJECT = kit_config.ado()  # the project's .claude/agent-workbench.json (kit_config.py); None when unset
API = "api-version=7.0"


def _ready():
    """Stop with what to set, before the token is even read, when the org or project is unset."""
    try:
        if not ORG:
            kit_config.need("ado", "org")
        if not PROJECT:
            kit_config.need("ado", "project")
    except kit_config.NotConfigured as e:
        print(f"ado_write.py: {e}", file=sys.stderr)
        sys.exit(2)


def _hdr(ct="application/json"):
    tok = kit_config.token("ADO_PAT", "AZURE_DEVOPS_EXT_PAT")
    if not tok:
        sys.exit("No ADO_PAT / AZURE_DEVOPS_EXT_PAT in the environment or the OS keychain.")
    return {"Authorization": "Basic " + base64.b64encode((":" + tok).encode()).decode(),
            "Content-Type": ct}


def _req(url, body=None, method="GET", ct="application/json"):
    # bytes go up as they are (an attachment upload); anything else is JSON.
    data = body if isinstance(body, bytes) else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    _ready()
    r = urllib.request.Request(url, headers=_hdr(ct), data=data, method=method)
    try:
        raw = urllib.request.urlopen(r, timeout=120).read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"ADO {e.code} {e.reason}: {e.read().decode('utf-8', 'replace')[:600]}")


def _p(path):
    return f"{ORG}/{urllib.parse.quote(PROJECT)}{path}"


# ── read-back ────────────────────────────────────────────────────────────────────────────────
# A write's exit 0 is not evidence it landed. Items created over the az path have kept only their
# first line while the command exited 0, and a hand-written read-back after every write is a step
# people skip. So the tool reads back itself, with a fresh GET: the write's own response echoes
# what was sent, not what was stored.
#
# What counts as the same, from what ADO did to text sent this way: a comment can come back byte
# for byte, or with &#39; decoded to '; ReproSteps and SystemInfo can come back with " written as
# &quot; and a space added before each </p>. So a comment or an HTML field compares the visible
# text with entities decoded and whitespace collapsed, plus every href, src and @mention, never
# the raw string; titles came back exactly as sent, so a plain-text field compares as text. A raw
# comparison reports files missing that are attached.

# Block tags end a run of text, so "<p>a</p><p>b</p>" reads as "a b", the same as with a newline.
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th",
          "table", "blockquote", "pre", "hr"}
# A "<" is text unless it opens a complete tag (a name, attributes with no "<" or ">" in them, then
# ">"), a complete comment, or a complete "<!...>". Python's parser drops what follows an unclosed
# "<min", "a<b then c" or "<!--" on 3.13 and keeps it on 3.9, so escaping such a "<" first gives one
# answer on both. Any tag name counts, so an added <tbody> is markup, not text. A "<" inside an
# attribute value breaks its tag into text, which can only read as not confirmed.
_TAG = re.compile(r"<(?!/?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>|!--.*?-->|![^<>-][^<>]*>)", re.S)
# A date, or a date and time with an optional zone. A date alone is midnight UTC.
_WHEN = re.compile(r"(\d{4})-(\d\d)-(\d\d)(?:T(\d\d):(\d\d)(?::(\d\d)(?:\.(\d{1,7}))?)?(Z|[+-]\d\d:\d\d)?)?")
# Fields whose case ADO sets for itself: a tree path comes back in the node's casing.
_ANY_CASE = {"system.iterationpath", "system.areapath", "system.state", "system.reason"}
# Fields ADO keeps as HTML, and comments. Any other text field is plain text, compared as text, so
# an "&lt;" stored in a title is what a reader sees, not a "<". A custom HTML field not listed here
# is compared as plain text too: ADO's re-encoding would read as not confirmed, never as a match.
_HTML = {"", "system.description", "system.history", "microsoft.vsts.common.acceptancecriteria",
         "microsoft.vsts.tcm.reprosteps", "microsoft.vsts.tcm.systeminfo"}


def _plain(value):
    return " ".join(str(value).split())


class _TextParts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.links = [], []

    def handle_data(self, data):
        self.text.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK:
            self.text.append(" ")
        self.links += [v for k, v in attrs if k in ("href", "src", "data-vss-mention") and v]

    def handle_endtag(self, tag):
        if tag in _BLOCK:
            self.text.append(" ")

    handle_startendtag = handle_starttag


def _visible(value):
    """(text, links) of an ADO value: tags dropped, entities decoded, whitespace collapsed."""
    p = _TextParts()
    p.feed(_TAG.sub("&lt;", "" if value is None else str(value)))
    p.close()
    return " ".join("".join(p.text).split()), p.links


def _tags(value):
    return {t.strip().lower() for t in str(value or "").split(";") if t.strip()}


def _when(value):
    """A date or date-time as a UTC datetime, or None if it isn't a whole, valid one."""
    m = _WHEN.fullmatch(str(value or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, zone = m.groups()
    try:
        t = datetime.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0),
                              int((frac or "0").ljust(6, "0")[:6]), tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    if zone and zone != "Z":
        shift = datetime.timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
        t = t - shift if zone[0] == "+" else t + shift
    return t


def _field(fields, ref):
    """A field by its reference name, which ADO matches without regard to case."""
    if ref in fields:
        return fields[ref]
    return next((v for k, v in fields.items() if k.lower() == ref.lower()), None)


def _same(ref, sent, stored):
    """True when ADO stored what was sent, by the rules in the module docstring."""
    ref = ref or ""
    if isinstance(stored, bool):
        return sent.strip().lower() == str(stored).lower()
    sent_when, stored_when = _when(sent), _when(stored)
    if sent_when and stored_when:  # the same moment: a date sent alone comes back as midnight UTC
        return sent_when == stored_when
    if ref.lower() in _ANY_CASE and isinstance(stored, str):
        return _plain(sent).lower() == _plain(stored).lower()
    if ref.lower() == "system.assignedto" or isinstance(stored, dict):
        if sent.strip() == "":
            return not stored
        if not stored:
            return False
        ids = ({str(stored.get(k) or "").strip().lower() for k in ("uniqueName", "displayName", "id")}
               if isinstance(stored, dict) else {str(stored).strip().lower()})
        s = sent.strip().lower()
        m = re.search(r"<([^<>]+)>\s*$", s)  # "Name <email>" names the identity by its email
        return bool(({s, m.group(1).strip()} if m else {s}) & (ids - {""}))
    if ref.lower() == "system.tags":
        return _tags(sent) == _tags(stored)
    if sent == "":
        return stored in (None, "")
    if stored is None:
        return False
    if isinstance(stored, (int, float)) and not isinstance(stored, bool):
        try:
            return float(sent) == float(stored)
        except ValueError:
            return False
    if ref.lower() in _HTML:
        return _visible(sent) == _visible(stored)
    return _plain(sent) == _plain(stored)


def _difference(sent, stored, ref=""):
    """The first place the two differ, short enough to print."""
    if stored is None:
        return "nothing is stored"
    if isinstance(stored, dict):
        return f"stored identity is {stored.get('uniqueName') or stored.get('displayName') or stored}"
    html = (ref or "").lower() in _HTML
    (st, sl), (gt, gl) = (_visible(sent), _visible(stored)) if html else ((_plain(sent), []), (_plain(stored), []))
    if st != gt:
        i = next((k for k in range(min(len(st), len(gt))) if st[k] != gt[k]), min(len(st), len(gt)))
        return (f"text differs at character {i} of {len(st)}: sent ...{st[max(0, i - 30):i + 40]!r}, "
                f"stored ...{gt[max(0, i - 30):i + 40]!r} ({len(gt)} characters)")
    lost = [u for u in sl if u not in gl]
    return f"link address missing: {lost[0]}" if lost else "links differ"


def _read_back(url):
    """GET for a read-back. A failure here exits 2: the write was sent, the result is unknown."""
    try:
        return _req(url)
    except SystemExit as e:
        why = e.code
    except Exception as e:  # a dropped connection, a timeout, a page that isn't JSON
        why = f"{type(e).__name__}: {e}"
    print(f"DID NOT RUN: the write was sent, but reading it back failed ({str(why)[:300]}).\n"
          f"  Whether it landed is unknown. Check the item before writing it again.")
    sys.exit(2)


def _not_confirmed(what):
    print(f"WRITE NOT CONFIRMED: {what}")
    sys.exit(1)


def patch_fields(wid, pairs):
    # An "add" with an empty value is a silent no-op in ADO (seen on System.Tags): exit 0, rev
    # unchanged. Clearing a field takes "remove".
    ops = [{"op": "remove", "path": f"/fields/{ref}"} if val == "" else
           {"op": "add", "path": f"/fields/{ref}", "value": val} for ref, val in pairs]
    _req(_p(f"/_apis/wit/workitems/{wid}?{API}"), ops, "PATCH", "application/json-patch+json")
    back = _read_back(_p(f"/_apis/wit/workitems/{wid}?{API}"))
    f = back.get("fields") or {}
    for ref, val in pairs:
        if not _same(ref, val, _field(f, ref)):
            _not_confirmed(f"#{wid} {ref} at rev {back.get('rev')}: {_difference(val, _field(f, ref), ref)}")
    print(f"  #{wid} OK  state={f.get('System.State')}  rev={back.get('rev')}  "
          f"read back: {len(pairs)} of {len(pairs)} field(s) as sent")


def _me():
    """The identity behind the PAT, as (id, display_name).

    Match on the GUID, never on a name. `authenticatedUser.providerDisplayName` can be an
    EMAIL ("pat.doe@example.com") while a comment's `createdBy.displayName` is a formatted
    name ("Doe, Pat"), so comparing names compares two strings that can never be equal and
    the refusal below becomes unreachable: a gate that always passes, the silent-zero
    failure. Both endpoints carry the same stable GUID, so that is the comparison.

    The name is returned for the message text only. The PAT itself is never echoed.
    """
    d = _req(f"{ORG}/_apis/connectionData?api-version=7.0-preview")
    u = d.get("authenticatedUser") or {}
    return ((u.get("id") or "").strip().lower(),
            (u.get("customDisplayName") or u.get("providerDisplayName") or "").strip())


def _live_comments(wid):
    cs = [c for c in _req(_p(f"/_apis/wit/workItems/{wid}/comments"
                             f"?api-version=7.0-preview.3")).get("comments", [])
          if not c.get("isDeleted")]
    cs.sort(key=lambda c: c.get("createdDate", ""))
    return cs


def add_comment(wid, text, stack_reason=None):
    """Add a comment - but REFUSE to stack a second unanswered one on your own.

    WHY THIS GATE EXISTS. "One comment per thread - edit it, do not stack a second one"
    and "the detail belongs in the BODY, not the comment" can be written down and still
    lose: under load, sessions stack a correction comment anyway. A ticket should be clean
    and current; its history tab already records every change, so a pile of comments
    tracking the changes is noise for the reviewer.

    Prose did not hold, so this is the mechanism. Same shape as ado.py making the
    dup-search a hard precondition of `new`: the miss was COMPLIANCE, not guidance,
    and more prose does nothing about compliance.

    THE TEST is the newest live comment on the item, which implements the carve-out
    for threads someone has already replied to:
      - newest comment is MINE   -> nobody has replied since; patching it rewrites
                                    nothing anyone has read.          REFUSE.
      - newest comment is THEIRS -> they replied; my earlier comments are the context
                                    they answered, and collapsing them would leave
                                    their reply answering something off-screen. ALLOW.
      - no comments at all       -> ALLOW.

    `--stack "<reason>"` overrides, because a gate with no escape hatch gets routed
    around instead of used. The reason is required and is printed, so the override is
    visible rather than silent.
    """
    if len(text) > 700:
        print(f"  ! {len(text)} chars. Target is ~700; a wall of text gets rewritten by hand. "
              f"Mechanism and measurements belong in Description/AC; the comment points at them.")

    if stack_reason is None:
        cs = _live_comments(wid)
        if cs:
            newest = cs[-1]
            by = newest.get("createdBy") or {}
            who = (by.get("id") or "").strip().lower()
            me, my_name = _me()
            if me and who == me:
                head = " ".join(newest.get("text", "").split())[:160]
                sys.exit(
                    f"REFUSED: #{wid}'s newest comment ({newest['id']}) is already yours "
                    f"({my_name or 'this PAT'}), so nobody has replied since.\n"
                    f"  Stacking a second one makes the reviewer read a running log instead of "
                    f"one current message.\n\n"
                    f"  yours now: {head}...\n\n"
                    f"  Patch it instead:\n"
                    f'    python ado_write.py editcmt {wid} {newest["id"]} "<merged text>"\n\n'
                    f"  If the new material is mechanism, alternatives or measurements, it is the "
                    f"living spec -\n"
                    f"  rewrite the BODY and let the comment point at it:\n"
                    f'    python ado_write.py field {wid} System.Description "<clean current truth>"\n\n'
                    f"  Genuinely need a second comment? "
                    f'python ado_write.py comment {wid} "<text>" --stack "<why>"')
    else:
        print(f"  --stack override: {stack_reason}")

    d = _req(_p(f"/_apis/wit/workItems/{wid}/comments?api-version=7.0-preview.3"),
             {"text": text}, "POST")
    _confirm_comment(wid, d.get("id"), text, "added")


def edit_comment(wid, cid, text):
    _req(_p(f"/_apis/wit/workItems/{wid}/comments/{cid}?api-version=7.0-preview.3"),
         {"text": text}, "PATCH")
    _confirm_comment(wid, cid, text, "replaced")


def _confirm_comment(wid, cid, text, did):
    """Read the comment list again and find this comment by id. The list is the one the stacking
    gate already reads; ADO returns up to 200 comments, so a comment past that would read as
    missing."""
    back = _read_back(_p(f"/_apis/wit/workItems/{wid}/comments?api-version=7.0-preview.3"))
    found = [c for c in back.get("comments", []) if str(c.get("id")) == str(cid) and not c.get("isDeleted")]
    if cid is None or not found:
        _not_confirmed(f"#{wid} comment {cid}: ADO answered the write, but no such comment is on the item")
    if not _same(None, text, found[0].get("text")):
        _not_confirmed(f"#{wid} comment {cid}: {_difference(text, found[0].get('text'))}")
    print(f"  #{wid} comment {cid} {did} ({len(text)} chars), read back as sent")


def del_comment(wid, cid):
    """Soft-delete a comment. Used ONLY to collapse a same-author pile after its content has
    been merged into the surviving comment - never to erase a record."""
    _req(_p(f"/_apis/wit/workItems/{wid}/comments/{cid}?api-version=7.0-preview.3"),
         None, "DELETE")
    print(f"  #{wid} comment {cid} deleted")


def list_comments(wid):
    d = _req(_p(f"/_apis/wit/workItems/{wid}/comments?api-version=7.0-preview.3"))
    for c in d.get("comments", []):
        print(f"  id={c['id']}  {c.get('createdDate','')[:16]}  "
              f"{(c.get('createdBy') or {}).get('displayName','')}  {len(c.get('text',''))} chars")


def _refuse_same_category(wid, child_type, parent):
    """Exit before any write when `parent` is in the child's own backlog category (ado.py
    CATEGORY says why). The categories come from the board's own backlog configuration."""
    p = _req(_p(f"/_apis/wit/workitems/{parent}?fields=System.WorkItemType&{API}"))
    ptype = p.get("fields", {}).get("System.WorkItemType", "")
    cats = categories_from(_req(_p(f"/_apis/work/backlogconfiguration?{API}")))
    cat = same_category(child_type, ptype, cats)
    if cat:
        sys.exit("REFUSED: " + same_category_reason(f"#{wid}", child_type, parent, ptype, cat)
                 + f" `reparent {wid} <id> --keep-related` does both.")


def set_parent(wid, parent):
    """Attach an ORPHAN to its parent.

    A parent is a RELATION, not a field, so `field System.Parent <id>` silently does the wrong
    thing. This is the only correct path, and the fix for the `orphan` check in ado_rest.py audit
    without `az`. REFUSES if a parent link already exists, because ADO accepts a second
    Hierarchy-Reverse relation and then the item has two parents and neither tree is true.
    """
    cur = _req(_p(f"/_apis/wit/workitems/{wid}?$expand=relations&{API}"))
    for rel in (cur.get("relations") or []):
        if rel.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
            have = rel.get("url", "").rsplit("/", 1)[-1]
            sys.exit(f"REFUSED: #{wid} already has parent #{have}. "
                     f"Remove it deliberately before re-parenting to #{parent}.")
    _refuse_same_category(wid, cur.get("fields", {}).get("System.WorkItemType", ""), parent)
    ops = [{"op": "add", "path": "/relations/-", "value": {
        "rel": "System.LinkTypes.Hierarchy-Reverse",
        "url": f"{ORG}/_apis/wit/workItems/{parent}"}}]
    d = _req(_p(f"/_apis/wit/workitems/{wid}?{API}"), ops, "PATCH",
             "application/json-patch+json")
    print(f"  #{wid} OK  parent -> #{parent}  rev={d.get('rev')}")


def reparent(wid, parent, keep_related=False):
    """MOVE an item to a different parent. Removes the existing Hierarchy-Reverse relation by INDEX
    and adds the new one in one patch, so the item is never briefly parentless and never briefly
    double-parented. The index is the position in the item's own relations array - ADO has no
    remove-by-url - so it is read immediately before the patch and the patch carries the rev it was
    read at, which makes a concurrent edit fail loudly instead of silently removing the wrong link.

    --keep-related links the old parent as Related in the same patch: the fix ADO's "same category
    hierarchy" banner asks for, which moves the item up a level without losing the link to the
    Task or Story it was filed under (a Bug filed under a QA Task, say).
    """
    cur = _req(_p(f"/_apis/wit/workitems/{wid}?$expand=relations&{API}"))
    rels = cur.get("relations") or []
    idx = [i for i, r in enumerate(rels)
           if r.get("rel") == "System.LinkTypes.Hierarchy-Reverse"]
    if not idx:
        sys.exit(f"REFUSED: #{wid} has no parent to move. Use `parent` to attach one.")
    if len(idx) > 1:
        sys.exit(f"REFUSED: #{wid} has {len(idx)} parent links. Fix that by hand first.")
    old = rels[idx[0]].get("url", "").rsplit("/", 1)[-1]
    if str(old) == str(parent):
        print(f"  #{wid} already parented to #{parent}, nothing to do")
        return
    _refuse_same_category(wid, cur.get("fields", {}).get("System.WorkItemType", ""), parent)
    ops = [
        {"op": "test", "path": "/rev", "value": cur.get("rev")},
        {"op": "remove", "path": f"/relations/{idx[0]}"},
        {"op": "add", "path": "/relations/-", "value": {
            "rel": "System.LinkTypes.Hierarchy-Reverse",
            "url": f"{ORG}/_apis/wit/workItems/{parent}"}},
    ]
    related = {r.get("url", "").rsplit("/", 1)[-1] for r in rels
               if r.get("rel") == "System.LinkTypes.Related"}
    if keep_related and str(old) not in related:
        ops.append({"op": "add", "path": "/relations/-", "value": {
            "rel": "System.LinkTypes.Related", "url": f"{ORG}/_apis/wit/workItems/{old}"}})
    d = _req(_p(f"/_apis/wit/workitems/{wid}?{API}"), ops, "PATCH",
             "application/json-patch+json")
    print(f"  #{wid} OK  parent #{old} -> #{parent}"
          + (f", #{old} kept as related" if keep_related else "") + f"  rev={d.get('rev')}")


def batch(path):
    """Apply a reviewed list of writes in one run, so one approval covers the list.

    The file is a JSON list. Each entry: {"id": N, "expect": {ref: value}, "set": {ref: value},
    "comment": {"id": commentId, "text": "..."}}, all but "id" optional. "set" may name
    System.Parent, which goes through reparent() and its refusals; "comment" replaces that
    comment's text, as editcmt does. An item whose "expect" no longer matches what ADO holds is
    skipped: someone changed it after the list was made. Before each write the item's current
    values go to <file>.undo.json, in this same format, so `batch <file>.undo.json` puts back what
    landed. The run stops at the first write that fails, since what follows may depend on it.
    Returns 0 when every item applied, 1 otherwise.
    """
    with open(path, encoding="utf-8") as fh:
        items = json.load(fh)
    undo_path = re.sub(r"\.json$", "", path) + ".undo.json"
    if os.path.exists(undo_path):
        print(f"REFUSED: {undo_path} is there from an earlier run, and this run would overwrite it. "
              f"Put the items still to do in a new batch file.")
        return 1
    undo, applied, skipped = [], [], []

    def save_undo():
        tmp = undo_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(undo, fh, indent=1, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, undo_path)

    for n, it in enumerate(items, 1):
        wid = str(it["id"])
        # System.Parent comes back only with relations expanded
        f = _req(_p(f"/_apis/wit/workitems/{wid}?$expand=relations&{API}")).get("fields") or {}
        drift = [(ref, val) for ref, val in (it.get("expect") or {}).items()
                 if not _same(ref, str(val), _field(f, ref))]
        if drift:
            ref, val = drift[0]
            print(f"  SKIPPED #{wid}: {ref} is now {_field(f, ref)!r}, the list expected {val!r}. "
                  f"Someone changed it after the list was made.")
            skipped.append(wid)
            continue
        sets = {ref: str(val) for ref, val in (it.get("set") or {}).items()}
        before = {}
        for ref in sets:
            v = _field(f, ref)
            before[ref] = "" if v is None else str(v.get("uniqueName") or "" if isinstance(v, dict) else v)
        entry = {"id": int(wid), "expect": dict(sets), "set": before, "status": "sending"}
        cmt = it.get("comment")
        if cmt:
            old = _req(_p(f"/_apis/wit/workItems/{wid}/comments/{cmt['id']}?api-version=7.0-preview.3"))
            entry["comment"] = {"id": cmt["id"], "text": old.get("text", "")}
        undo.append(entry)
        save_undo()
        try:
            parent = sets.pop("System.Parent", None)
            if parent:
                reparent(wid, parent)
                now = _field(_read_back(_p(f"/_apis/wit/workitems/{wid}?$expand=relations&{API}")).get("fields") or {},
                             "System.Parent")
                if not _same("System.Parent", parent, now):
                    _not_confirmed(f"#{wid} System.Parent: sent {parent}, stored {now}")
            if sets:
                patch_fields(wid, list(sets.items()))
            if cmt:
                edit_comment(wid, cmt["id"], cmt["text"])
        except SystemExit as e:
            entry["status"] = "failed"
            save_undo()
            if isinstance(e.code, str):
                print(e.code)
            print(f"  STOPPED at #{wid}, item {n} of {len(items)}: the write above failed, so nothing "
                  f"after it ran. What landed is in {undo_path}.")
            break
        entry["status"] = "applied"
        save_undo()
        applied.append(wid)
    print(f"batch: {len(applied)} of {len(items)} applied, {len(skipped)} skipped"
          + (f" ({', '.join('#' + s for s in skipped)})" if skipped else "")
          + f". Undo: python ado_write.py batch {undo_path}")
    return 0 if len(applied) == len(items) else 1


ATTACH_MAX = 60 * 1024 * 1024  # one-request upload; ADO wants a chunked upload past ~130 MB


def attach(wid, paths, inline=False):
    """Upload files and link each to the item as an AttachedFile relation.

    So a bug carries its pictures (the design frame and the build at the same width). Two steps per file: POST the bytes to /wit/attachments,
    which returns a URL, then one PATCH that adds every relation. The PATCH carries the rev it
    was read at, so a concurrent edit fails loudly instead of being overwritten by --inline.

    A file already linked under the same name and size is skipped, so running the same command
    twice adds nothing. A same-name file of a different size is a new picture and is attached.
    """
    for p in paths:
        if not os.path.isfile(p):
            sys.exit(f"REFUSED: no file at {p}. Nothing uploaded.")
        if os.path.getsize(p) > ATTACH_MAX:
            sys.exit(f"REFUSED: {p} is over {ATTACH_MAX // 2**20} MB. Nothing uploaded.")
    cur = _req(_p(f"/_apis/wit/workitems/{wid}?$expand=relations&{API}"))
    have = {((r.get("attributes") or {}).get("name"), (r.get("attributes") or {}).get("resourceSize"))
            for r in (cur.get("relations") or []) if r.get("rel") == "AttachedFile"}
    ops, imgs = [{"op": "test", "path": "/rev", "value": cur.get("rev")}], []
    for p in paths:
        name, size = os.path.basename(p), os.path.getsize(p)
        if (name, size) in have:
            print(f"  #{wid} already has {name} ({size} bytes), skipped")
            continue
        with open(p, "rb") as f:
            up = _req(_p(f"/_apis/wit/attachments?fileName={urllib.parse.quote(name)}&{API}"),
                      f.read(), "POST", "application/octet-stream")
        ops.append({"op": "add", "path": "/relations/-",
                    "value": {"rel": "AttachedFile", "url": up["url"]}})
        imgs.append((name, up["url"]))
        print(f"  uploaded {name}  {up['url']}")
    if not imgs:
        print(f"  #{wid} nothing new to attach")
        return
    if inline:
        f = cur.get("fields") or {}
        ref = BODY_FIELD.get(f.get("System.WorkItemType"), "System.Description")
        tags = "".join(f'<p><img src="{u}" alt="{n}"></p>' for n, u in imgs)
        ops.append({"op": "add", "path": f"/fields/{ref}", "value": (f.get(ref) or "") + tags})
    d = _req(_p(f"/_apis/wit/workitems/{wid}?{API}"), ops, "PATCH",
             "application/json-patch+json")
    # Read it back, as every write here should be: rc=0 from a PATCH is not evidence the relation
    # landed (the ado skill, section 7). Each upload's URL must be an AttachedFile on the item, and
    # with --inline in the body field too. The upload returns its URL with ?fileName=<name>; the
    # relation stores it without the query (comparing whole URLs reports attached files as
    # missing), so the comparison is on the URL before its query. The body keeps the URL as sent.
    back = _req(_p(f"/_apis/wit/workitems/{wid}?$expand=relations&{API}"))
    rels = {r.get("url", "").split("?")[0]: r.get("attributes") or {} for r in (back.get("relations") or [])
            if r.get("rel") == "AttachedFile"}
    body = (back.get("fields") or {}).get(ref, "") if inline else ""
    missing = [n for n, u in imgs if u.split("?")[0] not in rels or (inline and u not in body)]
    for n, u in imgs:
        if u.split("?")[0] in rels:
            print(f"  read back {n}: attached, {rels[u.split('?')[0]].get('resourceSize', '?')} bytes")
    if missing:
        sys.exit(f"READ BACK FAILED: #{wid} rev={back.get('rev')} does not show {', '.join(missing)}"
                 + (" in its attachments and body" if inline else " in its attachments"))
    print(f"  #{wid} OK  {len(imgs)} of {len(imgs)} attached{' and shown in the body' if inline else ''}"
          f", read back  rev={back.get('rev')}")


# The body field per work-item type, for attach --inline; any type not listed uses System.Description.
BODY_FIELD = {"Bug": "Microsoft.VSTS.TCM.ReproSteps"}


# ── self-test ────────────────────────────────────────────────────────────────────────────────
# A stand-in ADO on 127.0.0.1, one per case. A case says what the server keeps of a write
# (`store`: the value kept, or None to keep nothing), what it answers, and the exit a correct tool
# gives. The server always answers a write by echoing what was SENT, so a tool that trusts the
# write's response is fooled. Reads before the write always succeed; `read_status` applies after.
# `existing` puts comments on the item first; `stored_ref` stores a field under another name.
# A case passes on the exit code AND on its words: `says` must be printed (by default "read back",
# "WRITE NOT CONFIRMED" or "DID NOT RUN" for exit 0, 1 or 2), and `names`, when given, too. So a
# crash that happens to exit 1 is not a pass.
# `--against <file>` runs the same cases on another copy of this tool: run on a copy without the
# read-back, the planted bad cases must miss.

_ORIGINAL = "ORIGINAL COMMENT"
_SAYS = {0: "read back", 1: "WRITE NOT CONFIRMED", 2: "DID NOT RUN"}

# The maker's cases: known-good writes, from what ADO did to text this tool sent, and the calls on
# field types made from that reading. A read-back that fails these
# cries wolf.
_MAKER_CASES = [
    dict(name="stores the text as sent", op="comment",
         sent="<div>Answered by design: keep the shared footer line on every page.</div>",
         store=lambda s: s, expect=0),
    dict(name="decodes &#39; and changes whitespace", op="comment",
         sent="<div>the pricing page&#39;s question,\n  so the  row stays</div>",
         store=lambda s: s.replace("&#39;", "'").replace(",\n  so the  ", ", so the "), expect=0),
    dict(name="an edit stored as sent", op="editcmt", sent="<p>Fixed on <code>2fed223</code>.</p>",
         store=lambda s: s, expect=0),
    dict(name="state stored as sent", op="state", sent="Resolved", prior="Active",
         store=lambda s: s, expect=0),
    dict(name="AssignedTo comes back as an identity", op="field", ref="System.AssignedTo",
         sent="Pat Doe <pat.doe@example.com>", prior=None, expect=0,
         store=lambda s: {"displayName": "Doe, Pat", "uniqueName": "pat.doe@example.com", "id": "5e1f"}),
    dict(name="Tags come back reordered, in the project's casing", op="field", ref="System.Tags",
         sent="decision; Checkout", prior=None, store=lambda s: "checkout; Decision", expect=0),
    dict(name="a cleared field comes back absent", op="field",
         ref="Microsoft.VSTS.Common.AcceptanceCriteria", sent="", prior="<p>old</p>",
         store=lambda s: s, expect=0),
    dict(name="writes \" as &quot; and adds a space before </p>", op="field",
         ref="Microsoft.VSTS.TCM.ReproSteps",
         sent='<p>The home page\'s "Find your starting point" row.</p>\n\n<p><b>1. A focus ring</b> is cut.</p>',
         store=lambda s: s.replace('"Find', "&quot;Find").replace('point"', "point&quot;").replace(".</p>", ". </p>"),
         expect=0),
]

# The planter's cases: written by a separate agent from a table of observed failures, without
# this code. Don't change them alone; a case that fails is reported, not edited to pass.
# Cases 3-6 are the table's. x1-x6 are the planter's extras: each tries to make the read-back
# lie, either a false pass (stored value wrong, a sloppy compare calls it a match) or a false
# alarm (ADO legitimately changes the value, a strict compare cries wolf).

_COMMIT = "https://dev.azure.com/example-org/Example-Project/_git/example-repo/commit/4f2a91c"
_FIGMA = "https://www.figma.com/design/AbC123xyz/Example?node-id=1234-56789"

PLANTED = [
    # ---- table cases 3 to 6 -------------------------------------------------------------------
    dict(
        name="3 entity dropped from Description",
        op="field",
        ref="System.Description",
        sent=(
            "<p>Steps: open /checkout &rarr; choose <b>Express</b> &rarr; the score panel stays empty.</p>"
            "<p>Same on the Q&amp;A page.</p>"
        ),
        store=lambda s: s.replace("&rarr;", ""),
        prior="<p>Score panel empty.</p>",
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="System.Description",
        why="Table case 3, as observed on the az path: the non-ASCII entity &rarr; vanishes "
            "on storage, so the visible text loses both arrows; output should name System.Description.",
    ),
    dict(
        name="4 comment edit cut at first newline",
        op="editcmt",
        ref=None,
        sent=(
            "<p>Fixed on the work branch in 4f2a91c.</p>\n"
            "<ul><li>Hero band matches node 1234-56789 at 1440 and 375.</li>\n"
            "<li>Card hover restored; the Q&amp;A link points at /faq.</li></ul>\n"
            "<p>Ready for QA &rarr; please re-test on staging.</p>"
        ),
        store=lambda s: s.split("\n", 1)[0],
        expect=1,
        says="WRITE NOT CONFIRMED",
        why="Table case 4 on the comment-edit path: only the first line is kept, so three lines of text "
            "are gone; whitespace collapsing must not hide a loss of text.",
    ),
    dict(
        name="5 comment answered 200, nothing stored",
        op="comment",
        ref=None,
        sent=(
            "<p>QA pass done &#39;as built&#39; on staging.</p>"
            "<ul><li>Hero band: matches</li>"
            "<li>Footer: see <a href=\"" + _COMMIT + "\">commit 4f2a91c</a></li></ul>"
        ),
        store=lambda s: None,
        expect=1,
        says="WRITE NOT CONFIRMED",
        why="Table case 5: the write answers 200 with an id and echoes the text, but no comment exists; "
            "if the fresh GET for that id answers 404, that is a missing comment (1), not a failed read (2).",
    ),
    dict(
        name="6 state accepted, read-back fails",
        op="state",
        ref=None,
        sent="Resolved",
        store=lambda s: s,
        prior="Active",
        read_status=503,
        expect=2,
        says="DID NOT RUN",
        why="Table case 6: the write succeeds but every later read answers 503, so nothing can be "
            "confirmed; the tool must say DID NOT RUN, not pass on the write's echo and not exit 1.",
    ),
    # ---- planter's extras ---------------------------------------------------------------------
    dict(
        name="x1 link text kept, address dropped",
        op="comment",
        ref=None,
        sent=(
            "<p>Fixed in <a href=\"" + _COMMIT + "\">commit 4f2a91c</a>. "
            "The hero now matches the <a href=\"" + _FIGMA + "\">Figma node</a>.</p>"
        ),
        store=lambda s: s.replace(' href="' + _FIGMA + '"', "", 1),
        expect=1,
        says="WRITE NOT CONFIRMED",
        why="False pass: visible text is identical, only the second link's href is gone; the maker's "
            "call counts that as a change. ADO's rich-text sanitizer does strip hrefs it rejects; "
            "UNVERIFIED that it ever strips an https one.",
    ),
    dict(
        name="x2 AssignedTo comes back as an identity",
        op="field",
        ref="System.AssignedTo",
        sent="pat.tester@example.com",
        store=lambda s: dict(
            displayName="Pat Tester",
            uniqueName="Pat.Tester@example.com",
            id="a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        ),
        prior=None,
        expect=0,
        says="read back",
        why="False alarm: ADO returns AssignedTo as an object; uniqueName carries the "
            "directory's casing, not what was typed; matching uniqueName case aside is a pass.",
    ),
    dict(
        name="x3 Tags recased and reordered",
        op="field",
        ref="System.Tags",
        sent="readback; QA-Pass; trial-2026",
        store=lambda s: "qa-pass; readback; trial-2026",
        prior="readback",
        expect=0,
        says="read back",
        why="False alarm: ADO tags are case-insensitive and reuse a tag's existing project casing "
            "(here qa-pass), stored as 'a; b'; same set, case aside. Whether ADO reorders is UNVERIFIED.",
    ),
    dict(
        name="x4 cleared Description comes back absent",
        op="field",
        ref="System.Description",
        sent="",
        store=lambda s: s,  # nothing kept: ADO omits an emptied field from the GET's fields
        prior="<p>Old description, to be cleared.</p>",
        expect=0,
        says="read back",
        why="False alarm: a field cleared with \"\" is left out of ADO's work-item GET entirely; a tool "
            "that indexes fields[ref] crashes or calls it unreadable. Absent or empty is a pass.",
    ),
    dict(
        name="x5 clear ignored, old criteria kept",
        op="field",
        ref="Microsoft.VSTS.Common.AcceptanceCriteria",
        sent="",
        store=lambda s: None,
        prior="<ul><li>Hero matches node 1234-56789 at 1440 and 375.</li></ul>",
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="Microsoft.VSTS.Common.AcceptanceCriteria",
        why="False pass: \"\" is a substring of anything and normalizes to nothing, so a 'sent found in "
            "stored' check passes; the prior text is still there, so the clear did not happen.",
    ),
    dict(
        name="x6 state answered 200, prior kept",
        op="state",
        ref=None,
        sent="Closed",
        store=lambda s: None,
        prior="Resolved",
        expect=1,
        says="WRITE NOT CONFIRMED",
        why="Case 5's server on State, the most-written field; real ADO rejects a bad "
            "transition with 400, so this guards against trusting the write's echo on the state path.",
    ),
]

# ---- second batch: paths a separate reviewer found untested by PLANTED ------------------------
# The stand-in's extra keys: existing (comments already on the item, same author) and
# stored_ref (the key the server stores and returns a field under).

_MENTION = ' data-vss-mention="version:2.0,6c1f3a2b-8d4e-4f5a-9b0c-1d2e3f4a5b6c"'
_RETEST = "<p>Re-tested on staging: hero band matches node 1234-56789.</p>"

PLANTED_2 = [
    # 1. identity path, known-bad
    dict(
        name="b1 AssignedTo comes back as a different person",
        op="field",
        ref="System.AssignedTo",
        sent="Pat Tester",
        store=lambda s: dict(
            displayName="Pat Testerson",
            uniqueName="Pat.Testerson@example.com",
            id="9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4",
        ),
        prior=None,
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="System.AssignedTo",
        why="False pass: 'pat tester' is a substring of 'pat testerson'; the sent text must equal one "
            "of displayName, uniqueName or id, not sit inside one. Stand-in known-bad, not observed ADO.",
    ),
    dict(
        name="b2 AssignedTo comes back empty",
        op="field",
        ref="System.AssignedTo",
        sent="pat.tester@example.com",
        store=lambda s: None,
        prior=None,
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="System.AssignedTo",
        why="False pass: the field is absent after the write; absent is right only for a \"\" clear "
            "(x4), so a tool that skips a missing identity as 'nothing to compare' is fooled.",
    ),
    # 2. Tags, known-bad
    dict(
        name="b3 Tags come back with one dropped",
        op="field",
        ref="System.Tags",
        sent="readback; QA-Pass; trial-2026",
        store=lambda s: "qa-pass; readback",
        prior="readback",
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="System.Tags",
        why="False pass: stored is a subset of sent, so 'every stored tag was sent' passes; trial-2026 "
            "is missing. Sets must be equal, case aside.",
    ),
    dict(
        name="b4 Tags keep a tag the write replaced",
        op="field",
        ref="System.Tags",
        sent="readback; qa-pass",
        store=lambda s: "needs-design; qa-pass; readback",
        prior="needs-design; readback",
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="System.Tags",
        why="False pass the other way: 'every sent tag is stored' passes, but needs-design should be gone. "
            "Assumes the tool's tag write replaces the set, as 'match as a set' implies; if it adds, this case is wrong.",
    ),
    # 3. number field
    dict(
        name="b5 StoryPoints stored as a different number",
        op="field",
        ref="Microsoft.VSTS.Scheduling.StoryPoints",
        sent="3",
        store=lambda s: 3.5,
        prior=None,
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="Microsoft.VSTS.Scheduling.StoryPoints",
        why="False pass: '3' is a prefix and substring of '3.5', and int(3.5) == 3; compare as numbers. "
            "Stand-in known-bad, not observed ADO.",
    ),
    dict(
        name="b6 StoryPoints 3 comes back as 3.0",
        op="field",
        ref="Microsoft.VSTS.Scheduling.StoryPoints",
        sent="3",
        store=lambda s: 3.0,
        prior=None,
        expect=0,
        says="read back",
        why="False alarm: StoryPoints is a Double in ADO and the GET returns the JSON number 3.0; a "
            "string compare of '3' with '3.0' cries wolf.",
    ),
    # 4. comment lookup
    dict(
        name="b7 no new comment, an older one holds the same text",
        op="comment",
        ref=None,
        sent=_RETEST,
        store=lambda s: None,
        existing=["<p>QA pass started on staging.</p>", _RETEST],
        expect=1,
        says="WRITE NOT CONFIRMED",
        why="False pass: the write answers 200 with a new id, but only the older identical comment exists; "
            "a tool that searches the item's comments for its text finds that one. Look up by the returned id.",
    ),
    # 5. lost @mention
    dict(
        name="b8 @mention attribute stripped, text and href kept",
        op="comment",
        ref=None,
        sent="<div><a href=\"#\"" + _MENTION + ">@Pat Doe</a> please re-test the hero band on staging.</div>",
        store=lambda s: s.replace(_MENTION, "", 1),
        expect=1,
        says="WRITE NOT CONFIRMED",
        why="Without data-vss-mention nobody is notified, and the ado skill checks for that attribute; the text "
            "and href survive, so the maker's text-plus-href compare (its stated blind spot) will pass this.",
    ),
    # 6. casing ADO normalises
    dict(
        name="b9 IterationPath comes back in the node's casing",
        op="field",
        ref="System.IterationPath",
        sent="example project\\sprint 6",
        store=lambda s: "Example Project\\Sprint 6",
        prior="Example Project\\Sprint 5",
        expect=0,
        says="read back",
        why="False alarm: ADO resolves an iteration path to its node, case aside, and returns the node's "
            "own casing; I believe so, UNVERIFIED on this org.",
    ),
    dict(
        name="b10 ref sent lower case, stored under System.Title",
        op="field",
        ref="system.title",
        stored_ref="System.Title",
        sent="Gallery cards wrap at the wrong width",
        store=lambda s: s,
        prior="Gallery cards wrap",
        expect=0,
        says="read back",
        why="False alarm: ADO accepts a field reference name in any case and the GET returns it under the "
            "canonical name; a tool that looks up fields[ref] as sent finds nothing.",
    ),
    # 7. whitespace between blocks, and a bare less-than sign
    dict(
        name="b11 newline between paragraphs removed",
        op="field",
        ref="System.Description",
        sent="<p>Hero band matches node 1234-56789.</p>\n<p>Footer links fixed.</p>",
        store=lambda s: s.replace("</p>\n<p>", "</p><p>"),
        prior="<p>Hero band off.</p>",
        expect=0,
        says="read back",
        why="False alarm: same two paragraphs on screen; measured, Python's html.parser yields "
            "'matches.Footer' for the stored form, so a compare with no break at block ends cries wolf.",
    ),
    dict(
        name="b12 bare less-than sign stored as sent",
        op="field",
        ref="System.Title",
        sent="Gallery cards: wrap when width<min, then 24 px gap",
        store=lambda s: s,
        prior="Gallery cards: wrap",
        expect=0,
        says="read back",
        why="Known good: Title is plain text and ADO stores it as sent; a tool must not error or flag "
            "it because '<min,' looks like the start of a tag.",
    ),
    dict(
        name="b13 bare less-than sign, title cut after width",
        op="field",
        ref="System.Title",
        sent="Gallery cards: wrap when width<min, then 24 px gap",
        store=lambda s: s.split("<", 1)[0],
        prior="Gallery cards: wrap",
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="System.Title",
        why="False pass: measured, Python's html.parser drops everything from a bare '<' on, so sent and "
            "stored both extract to 'Gallery cards: wrap when width'.",
    ),
    # 8. dates and Booleans
    dict(
        name="b14 DueDate comes back as an ISO datetime",
        op="field",
        ref="Microsoft.VSTS.Scheduling.DueDate",
        sent="2026-10-30",
        store=lambda s: "2026-10-30T00:00:00Z",
        prior=None,
        expect=0,
        says="read back",
        why="False alarm: ADO returns DateTime fields as ISO 8601 UTC; same day. Whether ADO shifts a "
            "date-only value by the caller's time zone is UNVERIFIED.",
    ),
    dict(
        name="b15 Boolean true comes back as True",
        op="field",
        ref="Custom.ReadyForQA",
        sent="true",
        store=lambda s: True,
        prior=None,
        expect=0,
        says="read back",
        why="False alarm: ADO returns a Boolean field as JSON true, which Python reads as True and str() "
            "gives 'True'. The field name is made up; inherited processes do allow Boolean custom fields.",
    ),
    dict(
        name="b16 Boolean false comes back as True",
        op="field",
        ref="Custom.ReadyForQA",
        sent="false",
        store=lambda s: True,
        prior=None,
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="Custom.ReadyForQA",
        why="False pass: bool('false') is True in Python, so a truthiness compare calls it a match; "
            "the stored value is the opposite of the one sent.",
    ),
]

# ---- third batch: date and less-than paths a second reviewer found untested ------------------
# Measured on Python 3.13: datetime.fromisoformat rejects '2026-10-30T10:0' and
# '2026-10-30T99:99'; html.parser reads 'if a<b then c' as 'if a' and 'if a&lt;b then c' as
# 'if a<b then c'.

_DUE = "Microsoft.VSTS.Scheduling.DueDate"

PLANTED_3 = [
    # 1. date mismatches
    dict(
        name="c1 DueDate write dropped, old time on the same day kept",
        op="field",
        ref=_DUE,
        sent="2026-10-30",
        store=lambda s: None,
        prior="2026-10-30T15:00:00Z",
        expect=1,
        says="WRITE NOT CONFIRMED",
        names=_DUE,
        why="False pass: same calendar day, so a date-part compare matches; a date-only value is midnight "
            "(b14), and 15:00Z on the 30th is not midnight on the 30th in any time zone from UTC-12 to UTC+14.",
    ),
    dict(
        name="c2 DueDate sent with a digit missing, stored at 10:05",
        op="field",
        ref=_DUE,
        sent="2026-10-30T10:0",
        store=lambda s: "2026-10-30T10:05:00Z",
        prior=None,
        expect=1,
        says="WRITE NOT CONFIRMED",
        names=_DUE,
        why="False pass: the stored text starts with the sent text, so a prefix compare matches once the "
            "date parse fails; 10:0 can only mean 10:00. The read worked, so this is 1, not 2.",
    ),
    dict(
        name="c3 date-shaped Title stored with a bad time added",
        op="field",
        ref="System.Title",
        sent="2026-10-30",
        store=lambda s: "2026-10-30T99:99",
        prior="Release date",
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="System.Title",
        why="False pass: Title is plain text, so a tool that spots a date shape and compares date parts "
            "or prefixes is fooled; the stored text is not the text sent.",
    ),
    # 2. a stray less-than sign before a real tag name
    dict(
        name="c4 'if a<b then c' cut to 'if a'",
        op="field",
        ref="System.Description",
        sent="if a<b then c",
        store=lambda s: s.split("<", 1)[0],
        prior="<p>Condition to check.</p>",
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="System.Description",
        why="False pass: measured, html.parser reads '<b then c' as an unfinished <b> tag and drops it, "
            "so sent and stored both extract to 'if a'; ' then c' is gone from the item.",
    ),
    dict(
        name="c5 'if a<b then c' stored with the sign escaped",
        op="field",
        ref="System.Description",
        sent="if a<b then c",
        store=lambda s: s.replace("<", "&lt;"),
        prior="<p>Condition to check.</p>",
        expect=0,
        says="read back",
        why="False alarm: an HTML field shows 'if a&lt;b then c' as 'if a<b then c'; measured, html.parser "
            "decodes the stored form in full but cuts the sent form to 'if a'. Whether ADO escapes it is UNVERIFIED.",
    ),
    # 3. a tag ADO adds that changes no text
    dict(
        name="c6 table gains a tbody",
        op="field",
        ref="System.Description",
        sent="<table><tr><td>x</td></tr></table>",
        store=lambda s: "<table><tbody><tr><td>x</td></tr></tbody></table>",
        prior="<p>No table yet.</p>",
        expect=0,
        says="read back",
        why="False alarm: an HTML5 parser inserts tbody into a table, and the text 'x' is unchanged; a markup "
            "compare cries wolf. Whether ADO's sanitizer inserts it is UNVERIFIED.",
    ),
    # 4. bad partner for b9
    dict(
        name="c7 IterationPath comes back as a different sprint",
        op="field",
        ref="System.IterationPath",
        sent="example project\\sprint 6",
        store=lambda s: "Example Project\\Sprint 7",
        prior="Example Project\\Sprint 5",
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="System.IterationPath",
        why="False pass for a compare that is loose enough to pass b9: casing aside, Sprint 7 is not "
            "Sprint 6.",
    ),
    # 5. good partner for b14 with an explicit time
    dict(
        name="c8 DueDate comes back with milliseconds",
        op="field",
        ref=_DUE,
        sent="2026-10-30T10:05:00Z",
        store=lambda s: "2026-10-30T10:05:00.000Z",
        prior=None,
        expect=0,
        says="read back",
        why="False alarm: ADO's REST API often writes DateTimes with fractional seconds; same instant, "
            "so a string compare cries wolf.",
    ),
    # planter's own: make the date or less-than rules lie
    dict(
        name="cx1 DueDate sent with an offset, stored as the same instant in UTC",
        op="field",
        ref=_DUE,
        sent="2026-10-30T12:05:00+02:00",
        store=lambda s: "2026-10-30T10:05:00Z",
        prior=None,
        expect=0,
        says="read back",
        why="False alarm: ADO keeps DateTimes in UTC and returns them with Z; 12:05+02:00 is 10:05Z. A "
            "wall-clock or string compare cries wolf.",
    ),
    dict(
        name="cx2 DueDate offset dropped on storage",
        op="field",
        ref=_DUE,
        sent="2026-10-30T23:30:00-05:00",
        store=lambda s: "2026-10-30T23:30:00Z",
        prior=None,
        expect=1,
        says="WRITE NOT CONFIRMED",
        names=_DUE,
        why="False pass: same wall-clock time and same date part, but 23:30-05:00 is 2026-10-31T04:30Z, "
            "five hours and a day away; a compare that drops the offset is fooled.",
    ),
    dict(
        name="cx3 plain-text Title stored with the sign escaped",
        op="field",
        ref="System.Title",
        sent="Gallery cards: wrap when width<min, then 24 px gap",
        store=lambda s: s.replace("<", "&lt;"),
        prior="Gallery cards: wrap",
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="System.Title",
        why="False pass, the mirror of c5: Title is plain text, so the item now shows a literal '&lt;'; a "
            "tool that decodes entities in every field is fooled. ADO stores Title as sent (b12); this is a stand-in fault.",
    ),
]

# ---- fourth batch: HTML comment openers ------------------------------------------------------
# Measured: html.parser on 3.13 reads 'Done <!-- note then more text' as 'Done'; on 3.9 it keeps the whole string. Both read 'a <!-- x --> b' as 'a b'.

PLANTED_4 = [
    dict(
        name="d1 unclosed comment opener, stored cut to 'Done'",
        op="field",
        ref="System.Description",
        sent="Done <!-- note then more text",
        store=lambda s: "Done",
        prior="<p>In progress.</p>",
        expect=1,
        says="WRITE NOT CONFIRMED",
        names="System.Description",
        why="False pass on 3.13: its parser drops everything after an unclosed '<!--', so sent and stored "
            "both read 'Done'; 3.9 keeps the text. The words after the opener are gone from the item.",
    ),
    dict(
        name="d2 closed comment stripped on storage",
        op="field",
        ref="System.Description",
        sent="a <!-- x --> b",
        store=lambda s: "a b",
        prior="<p>In progress.</p>",
        expect=0,
        says="read back",
        why="False alarm: a closed comment is never shown, so removing it changes nothing a reader sees; "
            "a raw compare cries wolf. Whether ADO's sanitizer strips comments is UNVERIFIED.",
    ),
    dict(
        name="dx1 comment holding '>' across a line, stripped on storage",
        op="field",
        ref="System.Description",
        sent="<p>Ready for QA.</p><!-- checked: width > 1440\nand 375 --><p>Footer fixed.</p>",
        store=lambda s: "<p>Ready for QA.</p><p>Footer fixed.</p>",
        prior="<p>In progress.</p>",
        expect=0,
        says="read back",
        why="False alarm: a comment ends only at '-->', so its '>' and newline are hidden too; measured, a "
            "'<[^>]*>' strip leaves ' 1440 and 375 -->' as text, while both parsers read the two values the same.",
    ),
]

# ---- fifth batch: `batch`, one approval for a reviewed list ----------------------------------
PLANTED_5 = [
    dict(
        name="b1 batch applies a state change and keeps the prior value for undo",
        op="batch", ref="System.State", prior="Active", store=lambda s: s, expect=0,
        batch=[{"id": 4242, "expect": {"System.State": "Active"}, "set": {"System.State": "Resolved"}}],
        says="1 of 1 applied", names='"System.State": "Active"',
        why="Known good: the item still holds what the list expected, so the write goes and the undo "
            "file can put Active back.",
    ),
    dict(
        name="b2 batch skips an item someone changed after the list was made",
        op="batch", ref="System.State", prior="Active", store=lambda s: s, expect=1, write_status=500,
        batch=[{"id": 4242, "expect": {"System.State": "New"}, "set": {"System.State": "Resolved"}}],
        says="SKIPPED", names="0 of 1 applied",
        why="False pass: writing over a teammate's later change. The stand-in refuses any write, so a "
            "batch that wrote anyway prints STOPPED, not SKIPPED.",
    ),
    dict(
        name="b3 batch stops at a write ADO did not keep",
        op="batch", ref="System.State", prior="Active", store=lambda s: None, expect=1,
        batch=[{"id": 4242, "expect": {"System.State": "Active"}, "set": {"System.State": "Closed"}},
               {"id": 4242, "expect": {"System.State": "Active"}, "set": {"System.State": "New"}}],
        says="STOPPED", names='"status": "failed"', not_says=["item 2 of 2"],
        why="False pass: going on after a write that didn't land, so later items act on a state that "
            "isn't there. The undo file has to show the failed item as failed.",
    ),
    dict(
        name="b4 batch reads the parent it expects (ADO omits it from a plain GET)",
        op="batch", ref="System.State", prior="Active", store=lambda s: s, expect=0,
        extra_fields={"System.Parent": 1198},
        batch=[{"id": 4242, "expect": {"System.Parent": "1198"}, "set": {"System.State": "Resolved"}}],
        says="1 of 1 applied",
        why="False alarm, measured: a plain GET has no System.Parent, so a batch "
            "that reads it that way skips every item whose parent it checks.",
    ),
    dict(
        name="b5 batch refuses to overwrite an earlier run's undo file",
        op="batch", ref="System.State", prior="Active", store=lambda s: s, expect=1, undo_exists=True,
        batch=[{"id": 4242, "expect": {"System.State": "Active"}, "set": {"System.State": "Resolved"}}],
        says="REFUSED", names='"System.State": "New"',
        why="Lost record: a second run of the same file replaced the first run's undo file, and with it "
            "the only list of what to put back.",
    ),
]


def _standin(case):
    import http.server
    ref = "System.State" if case["op"] == "state" else case.get("ref")
    st = {"fields": {"System.State": "Active", **case.get("extra_fields", {})},
          "comments": [], "written": False, "next": 9000, "rev": 1}
    prior = case.get("prior", "PRIOR VALUE")
    if ref:
        ref = case.get("stored_ref") or ref
        st["fields"].pop(ref, None)
        if prior is not None:
            st["fields"][ref] = prior
    for k, text in enumerate(case.get("existing") or []):
        st["comments"].append({"id": 7001 + k, "text": text, "createdBy": {"id": "me"},
                               "isDeleted": False, "createdDate": "2026-10-0%dT00:00:00Z" % (k % 9 + 1)})
    if case["op"] == "editcmt":
        st["comments"].append({"id": 8001, "text": _ORIGINAL, "createdBy": {"id": "someone-else"},
                               "isDeleted": False, "createdDate": "2026-10-10T00:00:00Z"})

    class Standin(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if st["written"] and case.get("read_status", 200) != 200:
                return self._send(case["read_status"], {"message": "stand-in: read refused"})
            path = urllib.parse.urlparse(self.path).path
            if path.endswith("/connectionData"):
                return self._send(200, {"authenticatedUser": {"id": "me", "providerDisplayName": "selftest"}})
            if path.endswith("/comments"):
                return self._send(200, {"comments": st["comments"]})
            fields = dict(st["fields"])
            if "expand=relations" not in self.path:  # ADO leaves System.Parent out of a plain GET
                fields.pop("System.Parent", None)
            return self._send(200, {"id": 4242, "rev": st["rev"], "fields": fields})

        def _write(self):
            if case.get("write_status", 200) != 200:
                return self._send(case["write_status"], {"message": "stand-in: write refused"})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
            path = urllib.parse.urlparse(self.path).path
            st["written"] = True
            if path.endswith("/comments"):
                st["next"] += 1
                kept = case["store"](body["text"])
                if kept is not None:
                    st["comments"].append({"id": st["next"], "text": kept, "createdBy": {"id": "me"},
                                           "isDeleted": False, "createdDate": "2026-10-22T00:00:00Z"})
                return self._send(200, {"id": st["next"], "text": body["text"]})
            if "/comments/" in path:
                cid, kept = path.rsplit("/", 1)[1], case["store"](body["text"])
                for c in st["comments"]:
                    if str(c["id"]) == cid and kept is not None:
                        c["text"] = kept
                return self._send(200, {"id": int(cid), "text": body["text"]})
            echo = dict(st["fields"])
            for op in body:
                if op.get("op") not in ("add", "remove"):
                    continue
                name = case.get("stored_ref") or op["path"].split("/fields/", 1)[1]
                sent = op.get("value", "") if op["op"] == "add" else ""
                echo[name], kept = sent, case["store"](sent)
                if kept is None:
                    continue
                if kept == "":
                    st["fields"].pop(name, None)
                else:
                    st["fields"][name] = kept
            st["rev"] += 1
            return self._send(200, {"id": 4242, "rev": st["rev"], "fields": echo})

        do_POST = do_PATCH = _write

    return Standin


def _run_case(mod, case):
    """Run one write of `mod` against a fresh stand-in. Returns (exit code, printed output)."""
    import contextlib, http.server, io, threading
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _standin(case))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    keys = ("ADO_PAT", "NO_PROXY", "no_proxy")
    saved = (mod.ORG, mod.PROJECT, {k: os.environ.get(k) for k in keys})
    mod.ORG, mod.PROJECT = f"http://127.0.0.1:{srv.server_address[1]}/org", "proj"
    os.environ["ADO_PAT"] = "selftest-not-a-token"  # the stand-in ignores it; the real one is restored
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"  # never through a proxy
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                if case["op"] == "comment":
                    mod.add_comment("4242", case["sent"], "selftest")
                elif case["op"] == "editcmt":
                    mod.edit_comment("4242", "8001", case["sent"])
                elif case["op"] == "batch":
                    import tempfile
                    path = os.path.join(tempfile.mkdtemp(), "b.json")
                    with open(path, "w", encoding="utf-8") as fh:
                        json.dump(case["batch"], fh)
                    if case.get("undo_exists"):
                        with open(path[:-5] + ".undo.json", "w", encoding="utf-8") as fh:
                            fh.write('[{"id": 4242, "set": {"System.State": "New"}, "status": "applied"}]')
                    rc = mod.batch(path)
                    undo_file = path[:-5] + ".undo.json"
                    if os.path.exists(undo_file):
                        print("undo holds: " + open(undo_file, encoding="utf-8").read().replace("\n", " "))
                    raise SystemExit(rc)
                else:
                    ref = "System.State" if case["op"] == "state" else case["ref"]
                    mod.patch_fields("4242", [(ref, case["sent"])])
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                if isinstance(e.code, str):
                    print(e.code)
            except Exception as e:  # what python itself would exit 1 on
                print(f"{type(e).__name__}: {e}")
                code = 1
    finally:
        srv.shutdown()
        srv.server_close()
        mod.ORG, mod.PROJECT = saved[0], saved[1]
        for k, v in saved[2].items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return code, out.getvalue()


def _selftest(against=None):
    mod = sys.modules[__name__]
    if against:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ado_write_against", against)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print(f"selftest against {against}")
    planted_all = [c for n in ("PLANTED", "PLANTED_2", "PLANTED_3", "PLANTED_4", "PLANTED_5")
                   for c in globals().get(n, [])]
    cases = [("maker", c) for c in _MAKER_CASES] + [("planted", c) for c in planted_all]
    wrong, codes = [], {}
    for i, (by, c) in enumerate(cases, 1):
        code, out = _run_case(mod, c)
        codes[i] = code
        words = [c.get("says") or _SAYS[c["expect"]]] + ([c["names"]] if c.get("names") else [])
        unsaid = [w for w in words if w not in out] + [f"(said) {w}" for w in c.get("not_says", []) if w in out]
        ok = code == c["expect"] and not unsaid
        last = (out.strip().splitlines() or ["(nothing printed)"])[-1][:160]
        print(f"  {'ok  ' if ok else 'MISS'} {i:2} {by:7} {c['op']:7} expect {c['expect']} got {code}  {c['name']}")
        if not ok:
            wrong.append(i)
            print(f"          last line: {last}" + (f"\n          not printed: {unsaid}" if unsaid else ""))
    planted = sum(1 for by, _ in cases if by == "planted")
    print(f"selftest: {len(cases) - len(wrong)} of {len(cases)} cases gave the expected exit and words "
          f"({len(_MAKER_CASES)} maker, {planted} planted)" + (f"; missed {wrong}" if wrong else ""))
    if against:  # a copy with no read-back also misses every good case on its words, so count the bad
        bad = [i for i, (_, c) in enumerate(cases, 1) if c["expect"] != 0]
        print(f"  against: {sum(1 for i in bad if codes[i] == 0)} of {len(bad)} bad cases got exit 0 "
              f"from that copy, the success a missing read-back gives")
    if not planted:
        print("  no planted cases: the bad cases are missing, so this run proves only the good ones")
    return 0 if not wrong and planted else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        a = sys.argv.index("--against") + 1 if "--against" in sys.argv else 0
        sys.exit(_selftest(sys.argv[a] if a and a < len(sys.argv) else None))
    if {"-h", "--help"} & set(sys.argv[1:]):
        print(__doc__)
        sys.exit(0)
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, wid = sys.argv[1], sys.argv[2]
    if cmd == "batch":
        sys.exit(batch(wid))
    if cmd == "state":
        patch_fields(wid, [("System.State", sys.argv[3])])
    elif cmd == "field":
        patch_fields(wid, [(sys.argv[3], sys.argv[4])])
    elif cmd == "comment":
        reason = None
        if "--stack" in sys.argv:
            i = sys.argv.index("--stack")
            reason = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
            if not reason.strip():
                sys.exit("--stack requires a reason. It is printed, so the override is visible.")
        add_comment(wid, sys.argv[3], reason)
    elif cmd == "editcmt":
        edit_comment(wid, sys.argv[3], sys.argv[4])
    elif cmd == "delcmt":
        del_comment(wid, sys.argv[3])
    elif cmd == "comments":
        list_comments(wid)
    elif cmd == "parent":
        set_parent(wid, sys.argv[3])
    elif cmd == "reparent":
        reparent(wid, sys.argv[3], keep_related="--keep-related" in sys.argv[4:])
    elif cmd == "attach":
        files = [a for a in sys.argv[3:] if a != "--inline"]
        if not files:
            sys.exit("attach needs at least one file.")
        attach(wid, files, inline="--inline" in sys.argv[3:])
    else:
        sys.exit(f"unknown command {cmd}")
