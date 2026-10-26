"""memory_index.py - MEMORY.md is generated from the cards' own `index:` lines.

MEMORY.md is the index every session loads. Edited by hand, it becomes the most-edited shared file
on the machine, and parallel sessions overwrite each other's lines. So each card carries its own line:

    index: "<section> | <title> | <hook>"     listed under ## <section> as - [<title>](<card>) — <hook>
    index: none                              deliberately not listed (e.g. linked from a hub card)

The line is read at the top of the frontmatter or indented under `metadata:`, where the memory tool
puts it when it saves a card, and a harvest rewrites it where it sits.

A run reads MEMORY.md and the cards, then writes MEMORY.md from the cards. A line a session added or
edited by hand since the last run (the harness tells every session to add one) is copied into its
card first, so nothing typed there is lost. Order is kept: sections and lines stay where they are in
the current MEMORY.md, and a new card goes at the end of its section.

    python memory_index.py            dry run: what would change, exit 1 if anything would
    python memory_index.py --write    apply it (run it from a file watcher on the memory folder, or
                                      by hand after editing cards)
    python memory_index.py --seed     once: copy every MEMORY.md line into its card, and mark each
                                      card with no line `index: none`
    python memory_index.py --selftest plant the cases below in a temp folder and check each

THE MEMORY FOLDER is any ~/.claude/projects/<project>/memory: --memory DIR names one, else the
current project's (the folder Claude Code keeps for the working directory, or $CLAUDE_PROJECT_DIR).
The last generated index is kept per folder under ~/.cache/memory_index/, to tell hand edits apart.

What it cannot know: a card with no `index:` and no MEMORY.md line has no title or hook to list, so
it is reported as unindexed, never invented. A line removed by hand is put back if its card still
says where it goes; to drop one, set the card's `index: none`.

Claude Code loads only the first 200 lines or 25 KB of MEMORY.md and drops the rest without an error
(https://code.claude.com/docs/en/memory). So an index that would grow past LINE_CAP
lines or BYTE_CAP bytes, a margin under that, is not written: the run prints OVER CAP and exits 2, and
the index on disk stays whole. An index already over that shrinks is still written. The fix is fewer
lines (`index: none` on a card that is history, or cards folded), never a longer cap.

Exit: 0 nothing to change (or written), 1 dry run found changes or unindexed cards, 2 could not run
(a card's frontmatter does not parse; nothing is written) or the index would grow past the cap.
"""
import argparse
import json
import os
import re
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
SNAPS = os.path.expanduser("~/.cache/memory_index")


def snap_for(mem):
    """The last generated index for one memory folder: one file per folder, so two projects never
    read each other's snapshot as their own."""
    import hashlib
    key = hashlib.sha1(os.path.realpath(mem).encode("utf-8")).hexdigest()[:12]
    return os.path.join(SNAPS, f"{os.path.basename(os.path.dirname(mem.rstrip(os.sep)))[-60:]}-{key}.md")
LINE = re.compile(r"^- \[(?P<title>[^\]]+)\]\((?P<file>[^)/]+\.md)\) — (?P<hook>.+)$")
SEP = " | "
LINE_CAP, BYTE_CAP = 195, 24_800     # under Claude Code's 200 lines / 25 KB load limit (see above)


def over_cap(text):
    """What in `text` is past the cap, as words; [] when it fits."""
    n, b = text.count("\n"), len(text.encode("utf-8"))
    return [w for w in (f"{n} lines (cap {LINE_CAP})" if n > LINE_CAP else "",
                        f"{b} bytes (cap {BYTE_CAP})" if b > BYTE_CAP else "") if w]


class Refuse(Exception):
    pass


def atomic_write(path, text):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o644
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".mi-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def parse_index(md):
    """MEMORY.md -> (header lines, [section], {file: (section, title, hook)}, [file in order])."""
    header, sections, lines, order = [], [], {}, []
    sec = None
    for raw in md.split("\n"):
        if raw.startswith("## "):
            sec = raw[3:].strip()
            sections.append(sec)
        elif sec is None:
            header.append(raw)
        elif raw.startswith("- "):
            m = LINE.match(raw)
            if m and m["file"] not in lines:
                lines[m["file"]] = (sec, m["title"], m["hook"])
                order.append(m["file"])
    while header and not header[-1].strip():
        header.pop()
    return header or ["# Memory index"], sections, lines, order


def index_at(fm):
    """The position in the frontmatter lines `fm` of the card's index line, or None. A top-level
    `index:` wins; else one indented under `metadata:`, where the memory tool puts it when it saves
    a card (missing that place logged new cards as UNINDEXED)."""
    nested, in_meta = None, False
    for i, ln in enumerate(fm):
        if ln.startswith("index:"):
            return i
        if ln[:1] not in (" ", "\t"):
            in_meta = ln.rstrip() == "metadata:"
        elif in_meta and nested is None and ln.lstrip().startswith("index:"):
            nested = i
    return nested


def card_index(text, name):
    """The card's index value: None (no field), "none", or (section, title, hook)."""
    if not text.startswith("---\n"):
        raise Refuse(f"{name}: no frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise Refuse(f"{name}: frontmatter never closes")
    fm = text[4:end].split("\n")
    i = index_at(fm)
    if i is None:
        return None
    v = fm[i].lstrip()[6:].strip()
    if v == "none":
        return "none"
    try:
        v = json.loads(v) if v.startswith('"') else v
    except ValueError:
        raise Refuse(f"{name}: index value does not parse: {v[:60]}")
    parts = v.split(SEP, 2)
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise Refuse(f"{name}: index needs '<section> | <title> | <hook>', got {v[:60]!r}")
    return tuple(p.strip() for p in parts)


def set_card_index(text, value):
    """Return the card text with its index line set where it already is, keeping its indent
    (inserted after description:, else first, when the card has none)."""
    end = text.find("\n---", 4)
    fm = text[4:end].split("\n")
    line = "index: none" if value == "none" else "index: " + json.dumps(SEP.join(value), ensure_ascii=False)
    i = index_at(fm)
    if i is not None:
        fm[i] = fm[i][:len(fm[i]) - len(fm[i].lstrip())] + line
    else:
        at = next((i + 1 for i, ln in enumerate(fm) if ln.startswith("description:")), 0)
        while at < len(fm) and fm[at][:1] in (" ", "\t"):
            at += 1
        fm.insert(at, line)
    return "---\n" + "\n".join(fm) + text[end:]


def run(mem, snap, write, seed=False, out=print):
    idx_path = os.path.join(mem, "MEMORY.md")
    cur = open(idx_path, encoding="utf-8").read() if os.path.exists(idx_path) else "# Memory index\n"
    last = open(snap, encoding="utf-8").read() if os.path.exists(snap) else None
    header, sections, lines, order = parse_index(cur)
    last_lines = parse_index(last)[2] if last is not None else {}

    cards, texts = {}, {}
    for f in sorted(os.listdir(mem)):
        if f.endswith(".md") and f != "MEMORY.md" and os.path.isfile(os.path.join(mem, f)):
            texts[f] = open(os.path.join(mem, f), encoding="utf-8").read()
            cards[f] = card_index(texts[f], f)          # raises Refuse before anything is written

    harvest, dropped, notes = {}, [], []
    for f in order:
        ln = lines[f]
        if f not in cards:
            dropped.append(f)
            continue
        hand = last is None or last_lines.get(f) != ln     # added or edited since the last run
        have = cards[f]
        if have is None:
            harvest[f] = ln
        elif have == "none":
            if hand and last is not None:
                harvest[f] = ln
        elif have != ln and hand and last is not None:
            harvest[f] = ln
    if seed:
        for f, v in cards.items():
            if v is None and f not in lines:
                harvest[f] = "none"
    for f, v in harvest.items():
        cards[f] = v
    unindexed = sorted(f for f, v in cards.items() if v is None)

    # Build: current section order, then any new section; current line order, then new cards.
    sec_order = list(sections)
    for f, v in sorted(cards.items()):
        if isinstance(v, tuple) and v[0] not in sec_order:
            sec_order.append(v[0])
    body = []
    for s in sec_order:
        files = [f for f in order if isinstance(cards.get(f), tuple) and cards[f][0] == s]
        files += sorted((f for f, v in cards.items() if isinstance(v, tuple) and v[0] == s and f not in files),
                        key=lambda f: cards[f][1].lower())
        if not files:
            continue
        body += ["", f"## {s}"] + [f"- [{cards[f][1]}]({f}) — {cards[f][2]}" for f in files]
    new = "\n".join(header + body) + "\n"

    for f in sorted(harvest):
        out(f"  harvest  {f}: index <- {'none' if harvest[f] == 'none' else SEP.join(harvest[f])[:90]}")
    for f in dropped:
        out(f"  drop     {f}: listed in MEMORY.md, no such card")
    for f in unindexed:
        out(f"  UNINDEXED {f}: no index line and no MEMORY.md line; set `index:` in the card")
    changed = new != cur
    over = over_cap(new)
    grows = len(new.encode("utf-8")) > len(cur.encode("utf-8")) or new.count("\n") > cur.count("\n")
    held = bool(over and grows and changed)
    listed = sum(1 for v in cards.values() if isinstance(v, tuple))
    out(f"{listed} of {len(cards)} cards listed, {sum(1 for v in cards.values() if v == 'none')} marked none, "
        f"{len(unindexed)} unindexed; {len(harvest)} harvested, {len(dropped)} dropped; "
        f"MEMORY.md {'NOT written (over cap)' if held and write else 'would change' if changed and not write else 'changed' if changed else 'unchanged'}")
    if over:
        out(f"OVER CAP: the index would be {', '.join(over)}. Claude Code drops what is past 200 lines / 25 KB "
            "without an error. " + ("The index on disk is left as it was. " if held else "")
            + "Free lines: set `index: none` on cards that are history, or fold cards.")
    if write:
        for f in sorted(harvest):
            atomic_write(os.path.join(mem, f), set_card_index(texts[f], harvest[f]))
        if not held:
            if changed:
                atomic_write(idx_path, new)
            atomic_write(snap, new)
    if held:
        return 2
    return 1 if (not write and (changed or harvest or unindexed)) else 0


def selftest():
    """Each case is planted, then checked. Prints N of M; exit 1 on any miss."""
    import shutil
    ok, total, miss = 0, 0, []

    def check(name, cond):
        nonlocal ok, total
        total += 1
        ok += bool(cond)
        if not cond:
            miss.append(name)

    root = tempfile.mkdtemp(prefix="mi-selftest-")
    mem, snap = os.path.join(root, "mem"), os.path.join(root, "last.md")
    os.makedirs(mem)

    def card(f, idx=None):
        fm = f'---\nname: {f[:-3]}\ndescription: "d"\n' + (f"index: {idx}\n" if idx else "") + "metadata:\n  type: project\n---\n\nbody\n"
        open(os.path.join(mem, f), "w", encoding="utf-8").write(fm)

    card("a.md", json.dumps("S1 | A | hook a"))
    card("b.md")                                  # no field, has a line: harvested
    card("c.md")                                  # no field, no line: reported, never invented
    card("d.md", "none")                          # deliberately unlisted

    def nested(f, idx, under="metadata"):         # the memory tool saves a card's index line here
        open(os.path.join(mem, f), "w", encoding="utf-8").write(
            f'---\nname: {f[:-3]}\ndescription: "d"\n{under}:\n  node_type: memory\n  index: {idx}\n'
            "  type: project\n---\n\nbody\n")
    nested("f.md", json.dumps("S1 | F | hook f"))  # listed
    nested("g.md", "none")                         # unlisted on purpose, not unindexed
    nested("h.md", json.dumps("S1 | H | hook h"), under="other")  # not the card's index line
    open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8").write(
        "# Memory index\n\n## S1\n- [B](b.md) — hook b\n- [A](a.md) — hook a\n- [Gone](e.md) — no card\n")
    log = []
    rc = run(mem, snap, write=False, out=log.append)
    check("dry run reports changes (exit 1)", rc == 1)
    check("dry run writes nothing", "index:" not in open(os.path.join(mem, "b.md")).read())
    run(mem, snap, write=True, out=log.append)
    idx = open(os.path.join(mem, "MEMORY.md"), encoding="utf-8").read()
    check("a line with no card is dropped", "e.md" not in idx)
    check("a hand line is harvested into its card", card_index(open(os.path.join(mem, "b.md")).read(), "b") == ("S1", "B", "hook b"))
    check("order is kept (b before a)", "b.md" in idx and "a.md" in idx and idx.index("b.md") < idx.index("a.md"))
    check("an unindexed card is reported, not listed", "c.md" not in idx and any("UNINDEXED c.md" in l for l in log))
    check("index: none stays unlisted", "d.md" not in idx)
    check("an index line under metadata: is listed", "- [F](f.md) — hook f" in idx)
    check("an index: none under metadata: stays unlisted and is not reported",
          "g.md" not in idx and not any("UNINDEXED g.md" in l for l in log))
    check("an index: under another key is not the card's", "h.md" not in idx and any("UNINDEXED h.md" in l for l in log))
    t = set_card_index(open(os.path.join(mem, "f.md"), encoding="utf-8").read(), ("S1", "F", "hook f2"))
    check("a nested index line is set where it sits, not doubled",
          t.count("index:") == 1 and "\n  index: " in t and card_index(t, "f") == ("S1", "F", "hook f2"))
    m1 = os.stat(os.path.join(mem, "MEMORY.md")).st_mtime_ns
    log2 = []
    rc2 = run(mem, snap, write=True, out=log2.append)
    check("second run changes nothing", os.stat(os.path.join(mem, "MEMORY.md")).st_mtime_ns == m1 and "unchanged" in log2[-1])
    # A session edits a hook by hand: the edit wins and lands in the card.
    open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8").write(idx.replace("hook a", "hook a2"))
    run(mem, snap, write=True, out=log.append)
    check("a hand-edited hook is harvested", card_index(open(os.path.join(mem, "a.md")).read(), "a") == ("S1", "A", "hook a2"))
    # The card changes: the card wins over the generated line.
    t = open(os.path.join(mem, "a.md")).read()
    open(os.path.join(mem, "a.md"), "w").write(set_card_index(t, ("S2", "A", "moved")))
    run(mem, snap, write=True, out=log.append)
    idx = open(os.path.join(mem, "MEMORY.md"), encoding="utf-8").read()
    check("a card's new section is listed, at the end",
          "## S1" in idx and "## S2\n- [A](a.md) — moved" in idx and idx.index("## S1") < idx.index("## S2"))
    # A broken card refuses the whole run and writes nothing.
    open(os.path.join(mem, "z.md"), "w").write("---\nname: z\nno close\n")
    before = open(os.path.join(mem, "MEMORY.md")).read()
    try:
        run(mem, snap, write=True, out=log.append)
        refused = False
    except Refuse:
        refused = True
    check("a card whose frontmatter never closes refuses the run", refused and open(os.path.join(mem, "MEMORY.md")).read() == before)
    # The load cap: an index that would grow past it is not written; one over it that shrinks is.
    global LINE_CAP
    saved_cap = LINE_CAP
    os.remove(os.path.join(mem, "z.md"))
    run(mem, snap, write=True, out=log.append)
    at_cap = open(os.path.join(mem, "MEMORY.md"), encoding="utf-8").read()
    LINE_CAP = at_cap.count("\n")
    card("y.md", json.dumps("S1 | Y | hook y"))
    log3 = []
    rc3 = run(mem, snap, write=True, out=log3.append)
    check("an index that would grow past the load cap is not written (exit 2, OVER CAP)",
          rc3 == 2 and open(os.path.join(mem, "MEMORY.md"), encoding="utf-8").read() == at_cap
          and any("OVER CAP" in l for l in log3))
    os.remove(os.path.join(mem, "y.md"))
    t = open(os.path.join(mem, "f.md"), encoding="utf-8").read()
    open(os.path.join(mem, "f.md"), "w", encoding="utf-8").write(set_card_index(t, "none"))
    LINE_CAP = 1
    log4 = []
    rc4 = run(mem, snap, write=True, out=log4.append)
    check("an index over the cap that shrinks is still written, and says OVER CAP",
          rc4 == 0 and "f.md" not in open(os.path.join(mem, "MEMORY.md"), encoding="utf-8").read()
          and any("OVER CAP" in l for l in log4))
    LINE_CAP = saved_cap
    check("today's cap is under Claude Code's 200 lines / 25 KB", LINE_CAP < 200 and BYTE_CAP < 25_000)
    shutil.rmtree(root)
    print(f"selftest {ok} of {total}" + (f"; MISSED: {', '.join(miss)}" if miss else ""))
    return 0 if ok == total else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="apply: harvest hand lines, then write MEMORY.md")
    ap.add_argument("--seed", action="store_true", help="once: every line into its card; unlisted cards get index: none")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing when nothing changed; stamp each line with the time (for a scheduler's log)")
    ap.add_argument("--memory", "--dir", dest="dir", default=None,
                    help="the memory folder (default: the current project's)")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    buf = []
    out = buf.append if a.quiet else print
    if a.dir:
        mem = os.path.abspath(os.path.expanduser(a.dir))
    else:
        import cc_logs
        mem = cc_logs.memory_dir()
    if not os.path.isdir(mem):
        print(f"DID NOT RUN: no memory folder at {mem}. Pass --memory DIR.")
        return 2
    try:
        rc = run(mem, snap_for(mem), write=a.write or a.seed, seed=a.seed, out=out)
    except Refuse as e:
        rc, buf = 2, buf + [f"DID NOT RUN: {e}. Nothing written."]
        if not a.quiet:
            print(buf[-1])
    if a.quiet and (rc or len(buf) > 1 or "unchanged" not in (buf[-1] if buf else "")):
        import time
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        for line in buf:
            print(f"{stamp} {line}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
