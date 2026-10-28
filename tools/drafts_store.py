#!/usr/bin/env python3
"""drafts_store.py - keep a drafts file of Figma comments true without anyone tending it.

    list [<drafts.md>]      the open drafts: kind, target, length, heading
    selftest                planted cases on a temporary file, no network

THE FILE. A markdown file of comments drafted for Figma and not yet posted: each draft is a `### `
heading naming its target ("NEW on ... <node id in backticks>" or `EDIT your own comment [<comment id>](...)`)
with the text to post as `> ` quoted lines; a `## Cut: do not re-raise` table lists items decided
against. Its path is $FIGMA_DRAFTS, else `figma.drafts` in the agent-workbench config. With neither,
every function here does nothing and refuses nothing.

WHY. A drafts file whose rule is "whoever posts a draft deletes it" is never tended: posted drafts
and notes about them pile up, and a "do not re-raise" table is enforced only by whoever reads it.

WHAT IT DOES. figma_post.py new and reply, and figma_attach.py edit, call remove_posted() once the
API has the comment: the one draft block whose quoted text is the posted text (or, for an EDIT
draft, whose heading names the edited comment and whose text matches) leaves the file. It removes
nothing when no block matches, or when two do. figma_post.py new calls cut_refusal() before
posting: a node named in a row of the Cut table is refused until that row is deleted, which is the
deliberate way to raise a cut item again. Writes are atomic (temp file, fsync, replace) and read the
file just before writing, because other sessions edit it too.

A draft reworded at post time beyond the match (0.95 for a NEW draft on the same node, 0.8 for an
EDIT draft of the same comment) stays, and the post names the drafts it kept on that target so the
session deletes the right one by hand.

A quote's lines are wrapped for the file, and Figma keeps every line break, so a quote copied as
a comment's text posts with a break every line. figma_post.py new and reply refuse such text
(wrapped_refusal()); text_of() joins a quote back into one line per paragraph, keeping list items,
blank lines and lines that end in a link or a colon.

BLIND TO a draft posted on a node other than the one its heading names, and to Cut rows that name
no node id (a topic only).
"""
import difflib, os, re, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import kit_config
except ImportError:
    kit_config = None


def drafts_path():
    """$FIGMA_DRAFTS, else figma.drafts from the config, else None (not configured)."""
    p = os.environ.get("FIGMA_DRAFTS") or (kit_config.get("figma", "drafts") if kit_config else None)
    return os.path.expanduser(p) if p else None


LABEL = "before and after:"
MATCH = 0.95        # a NEW draft against the posted text, same node
EDIT_MATCH = 0.8    # an EDIT draft against the posted text, same comment id
CUT = "## Cut: do not re-raise"


def norm(s):
    """Text as the API and the file both write it: straight quotes, x for the times sign, one space,
    lower case, and no closing picture label (the post adds it)."""
    s = (s or "").replace("×", "x").replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s[: -len(LABEL)].rstrip() if s.endswith(LABEL) else s


def blocks(text):
    """Each draft: a '### ' heading up to the next heading of level 2 or 3. Its quoted lines ('> '),
    the comment id an EDIT heading links to, and the node id a NEW heading names."""
    lines = text.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith("### ")]
    out = []
    for i in starts:
        j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith(("### ", "## "))), len(lines))
        body = lines[i:j]
        head = body[0]
        # A bare ">" is a blank line inside the quote, the break between two paragraphs. Skipped, it
        # would run every paragraph of a quote into one line in text_of().
        quote = "\n".join(ln[2:] if ln.startswith("> ") else "" for ln in body
                          if ln.startswith("> ") or ln.rstrip() == ">")
        cid = re.search(r"EDIT your own comment \[(\d{9,})\]", head)
        node = re.search(r"NEW on [^`]*`([\d:;I]+)`", head)
        out.append({"first": i, "end": j, "head": head[4:], "quote": quote,
                    "comment": cid.group(1) if cid else None, "node": node.group(1) if node else None})
    return out


def _matches(b, posted, comment_id, node_id):
    if not b["quote"]:
        return False
    a, p = norm(b["quote"]), norm(posted)
    if a == p:
        return True
    ratio = difflib.SequenceMatcher(None, a, p).ratio()
    # An EDIT draft names the one comment it edits, so a reworded post still consumes it. Several NEW
    # drafts can sit on one node, so those need a near-exact match.
    if comment_id and b["comment"] == str(comment_id):
        return ratio >= EDIT_MATCH
    return bool(node_id and b["node"] == node_id) and ratio >= MATCH


def _write(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def remove_posted(posted, comment_id=None, node_id=None, path=None):
    """Remove the one draft that was just posted. Returns a line to print, or None when the file is
    missing or holds no draft for this node or comment."""
    path = path or drafts_path()
    if not path:
        return None
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    hits = [b for b in blocks(text) if _matches(b, posted, comment_id, node_id)]
    if not hits:
        near = [b for b in blocks(text)
                if (comment_id and b["comment"] == str(comment_id)) or (node_id and b["node"] == node_id)]
        if near:   # a draft on this target whose text differs: reworded before posting, or another ask
            return (f"  drafts.md: the posted text matches none of the {len(near)} drafts on this target, "
                    "so none was removed; if one was reworded and posted, delete its block: "
                    + "; ".join(h["head"][:60] for h in near))
        return None
    if len(hits) > 1:
        return (f"  drafts.md: {len(hits)} drafts match the posted text, so none was removed: "
                + "; ".join(h["head"][:60] for h in hits))
    lines = text.split("\n")
    b = hits[0]
    _write(path, "\n".join(lines[: b["first"]] + lines[b["end"]:]))
    return f"  drafts.md: removed the posted draft \"{b['head'][:80]}\""


# A line that starts a list item: an intended break. Any other break inside a paragraph is the file's
# wrapping, unless the line before ends with a URL or a colon.
LIST_ITEM = re.compile(r"[ \t]*(?:\d+[.)]|[*•\-])[ \t]")


def wrapped_lines(text):
    """The line breaks inside a paragraph that look like a quote's wrapping, as (end of line, start of
    next) pairs. Text copied from a wrapped quote in the drafts file posts with a break every line."""
    out = []
    for para in re.split(r"\n[ \t]*\n", text or ""):
        lines = para.split("\n")
        for a, b in zip(lines, lines[1:]):
            if LIST_ITEM.match(b) or re.search(r"https?://\S+$", a.rstrip()) or a.rstrip().endswith(":"):
                continue
            out.append((a.rstrip()[-30:], b.strip()[:30]))
    return out


def wrapped_refusal(text):
    """Refusal text or None, for a post or an edit."""
    w = wrapped_lines(text)
    if not w:
        return None
    return (f"REFUSED: the text breaks {len(w)} line(s) mid-paragraph, as a wrapped quote in the drafts file does, "
            f"for one: \"...{w[0][0]}\" / \"{w[0][1]}...\". Figma keeps each break. Build the text with "
            "drafts_store.text_of(), or join the lines; an intended break is a list item or a blank line.")


def text_of(quote):
    """A draft's quote as the comment's text: the file's wrapping joined back into one line per paragraph,
    list items and blank lines kept."""
    paras = []
    for para in re.split(r"\n[ \t]*\n", (quote or "").strip()):
        lines, out = para.split("\n"), []
        for ln in lines:
            if out and not LIST_ITEM.match(ln) and not re.search(r"https?://\S+$", out[-1].rstrip()) \
                    and not out[-1].rstrip().endswith(":"):
                out[-1] = out[-1].rstrip() + " " + ln.strip()
            else:
                out.append(ln.rstrip())
        paras.append("\n".join(out))
    return "\n\n".join(paras)


def cut_rows(text):
    """The Cut table's rows as (item cell, ids in it): node ids like 7106:58553 and thread ids."""
    if CUT not in text:
        return []
    sec = text[text.index(CUT):]
    nxt = sec.find("\n## ", len(CUT))
    sec = sec if nxt < 0 else sec[:nxt]
    rows = []
    for ln in sec.split("\n"):
        if not ln.startswith("|") or ln.startswith(("| Item", "|---")):
            continue
        cell = ln.split("|")[1]
        ids = set(re.findall(r"(?<![\d-])(\d{3,6}:\d{3,7})(?![\d])", cell)) | set(re.findall(r"\b(\d{10})\b", cell))
        rows.append((cell.strip(), ids))
    return rows


def cut_refusal(node_id, path=None):
    """Refusal text when the Cut table names node_id, else None."""
    path = path or drafts_path()
    if not path:
        return None
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    for cell, ids in cut_rows(text):
        if node_id in ids:
            return (f"REFUSED: {node_id} is in drafts.md's \"Cut: do not re-raise\" table ({cell[:90]}). "
                    "To raise it again on purpose, delete that row first.")
    return None


SAMPLE = """# Figma comments owed

## Open

### Hero button — NEW on the hero row [`7127:62456`](https://x) · **Not sent.**

> FIX (done in Figma): The hero button reads "Install" again. Please confirm and resolve. Before and after:

### Footer — EDIT your own comment [1970745074](https://x#1970745074) on the footer · **Not sent.**

> FIX (done in Figma): The footer heading sits 64 below the photo. Please check and resolve. Before and after:

### Held question — `9669:36462` · Last reviewed: 2026-10-20 — held.

> Who owns the load credential?

## Cut: do not re-raise

| Item | Why |
|---|---|
| Comparison table header cell, `7106:58553` | Intentional |
| Mobile frames | Known work |
"""


def selftest():
    results = []

    def case(name, ok):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    td = tempfile.mkdtemp(prefix="drafts-store-selftest-")
    p = os.path.join(td, "drafts.md")
    reset = lambda: open(p, "w", encoding="utf-8").write(SAMPLE)
    reset()
    bs = blocks(SAMPLE)
    case("three drafts parsed, with an EDIT's comment id and a NEW's node id",
         len(bs) == 3 and bs[0]["node"] == "7127:62456" and bs[1]["comment"] == "1970745074")
    posted = 'FIX (done in Figma): The hero button reads "Install" again. Please confirm and resolve.\n\nBefore and after:'
    got = remove_posted(posted, node_id="7127:62456", path=p)
    after = open(p, encoding="utf-8").read()
    case("the posted NEW draft leaves the file, label and line breaks aside",
         got and "Hero button" not in after and "Footer" in after and "Held question" in after and CUT in after)
    reset()
    got = remove_posted("Q: Is the hero row meant to be 720 or 760 tall?", node_id="7127:62456", path=p)
    case("another post on the same node, about something else, removes nothing and names the draft it kept",
         got and "none was removed" in got and "Hero button" in got and open(p, encoding="utf-8").read() == SAMPLE)
    case("a post whose text matches no draft removes nothing",
         remove_posted("FIX (done in Figma): something else entirely.", node_id="1:2", path=p) is None
         and open(p, encoding="utf-8").read() == SAMPLE)
    edited = "FIX (done in Figma): The footer heading sits 64 below the photo, as on the site. Please check and resolve."
    case("an EDIT draft leaves by its comment id and a close match",
         remove_posted(edited, comment_id="1970745074", path=p) and "Footer" not in open(p, encoding="utf-8").read())
    reset()
    case("... but not by a close match on another comment's id",
         remove_posted(edited, comment_id="1111111111", path=p) is None)
    got = remove_posted("Q: Should the badge be 12 or 14 tall?", comment_id="1970745074", path=p)
    case("... nor by unrelated text on its own comment id, which it names",
         got and "none was removed" in got and open(p, encoding="utf-8").read() == SAMPLE)
    twice = SAMPLE.replace("### Held question", "### Hero again — NEW on [`7127:62456`](https://x)\n\n> FIX (done in Figma): "
                           "The hero button reads \"Install\" again. Please confirm and resolve.\n\n### Held question")
    open(p, "w", encoding="utf-8").write(twice)
    got = remove_posted(posted, node_id="7127:62456", path=p)
    case("two drafts with the posted text: neither is removed, and it says so",
         got and "none was removed" in got and open(p, encoding="utf-8").read() == twice)
    reset()
    case("a held question with no post is never touched by an unrelated post",
         remove_posted("Who owns the deploy key?", path=p) is None)
    case("a node in the Cut table is refused", "Cut: do not re-raise" in (cut_refusal("7106:58553", path=p) or ""))
    case("a node not in it passes", cut_refusal("7106:5855", path=p) is None and cut_refusal("9669:36462", path=p) is None)
    case("a missing file refuses nothing and removes nothing",
         cut_refusal("7106:58553", path=p + "x") is None and remove_posted(posted, path=p + "x") is None)
    wrapped = ("FIX (done in Figma): These cards are 450 tall now, on desktop and phone, so the row fits its\n"
               "seven pills with the last one back in place.\n\nBefore and after:")
    listed = ("FIX (done in Figma): Two fixes.\n\n1. The title reads \"A\". It read \"B\".\n"
              "2. The badge reads \"C\".\n\nFirst: https://www.figma.com/design/K/File?node-id=1-2\n"
              "Second: https://www.figma.com/design/K/File?node-id=3-4\n\nBefore and after:")
    case("a quote's wrapping, copied as a comment's text, is refused", wrapped_refusal(wrapped) is not None)
    case("list items and lines that end in a link or a colon are kept", wrapped_refusal(listed) is None)
    case("text_of joins the wrapping back into one line", wrapped_refusal(text_of(wrapped)) is None
         and "fits its seven pills" in text_of(wrapped))
    case("text_of keeps list items and link lines as they are", text_of(listed) == listed)
    md = "### Two fixes — EDIT your own comment [1111111111](https://x#1111111111)\n\n" + "\n".join(
        ("> " + ln) if ln else ">" for ln in listed.split("\n")) + "\n"
    case("a quote's blank '>' lines come through blocks() and text_of() as paragraph breaks",
         text_of(blocks(md)[0]["quote"]) == listed)
    os.remove(p)
    os.rmdir(td)
    print(f"{sum(results)} of {len(results)} planted cases behave as expected.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] in (["-h"], ["--help"]):
        print(__doc__)
        sys.exit(0)
    if args[:1] in (["selftest"], ["--selftest"]):
        sys.exit(selftest())
    if args[:1] == ["list"]:
        path = args[1] if len(args) > 1 else drafts_path()
        if not path:
            print("drafts_store.py: not configured: set $FIGMA_DRAFTS or figma.drafts, or name the file")
            sys.exit(2)
        for b in blocks(open(path, encoding="utf-8").read()):
            kind = "EDIT" if b["comment"] else "NEW" if b["node"] else "other"
            print(f"{kind:5} {b['comment'] or b['node'] or '-':14} {len(b['quote']):5} chars  {b['head'][:80]}")
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
