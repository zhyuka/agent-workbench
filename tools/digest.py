#!/usr/bin/env python3
"""digest.py - DIGEST.md: your open requests and the questions you put off, by age, and your standing answers.

    python tools/digest.py               write <ledger dir>/DIGEST.md from the ledger
    python tools/digest.py --print       print it instead of writing it
    python tools/digest.py --selftest    a planted ledger: header, open requests, answers, line budget

Reads requests.jsonl in the ledger folder (ledger.dir in the agent-workbench config), which ledger.py
writes; run ledger.py first. Nothing here decides a state: an open request is one ledger.py left
open, an open question is a card question you put off that no later card has answered (listed under
its id, which a later card or a triage pass names to close it), and a standing answer is one of your
decisions (or anything a ruling holds) that nothing later reversed or replaced. About 60 lines plus
the questions: when there are more, the oldest open requests and questions and the newest answers
win, and the rest are counted with the command that lists them.

RULINGS. A ruled entry is shown with its ruling from ledger.rulings. ledger.rulings_also lists globs
of other copies of the rulings file (e.g. in git worktrees): a ruling only a copy holds is "not in
the main file yet", and one no file holds reads as removed after it was written. With
rulings_also unset that second test is skipped.

Every entry is personal, so this file is for you. Nothing loads it into a session.

Exit 0 written; 2 the ledger could not be read.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger  # noqa: E402

OPEN_MAX = 22
QUESTION_MAX = 40
ANSWER_LINES = 28
PER_TOPIC = 3
TOOLS = os.path.dirname(os.path.abspath(__file__))


def one_line(s, n):
    """Your words on one line, verbatim except: pasted blocks, an HTML comment marker and agent text quoted
    with "> " are left out, so what shows is what you wrote."""
    s = ledger.own_words(s).replace("<", "&lt;").replace("|", "\\|")
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def when(e):
    return (e.get("ts") or "")[:10]


def age_days(e, now):
    t = ledger.parse_ts(e.get("ts") or "")
    return int((now - t).total_seconds() // 86400) if t else None


def where(e):
    """A link that opens the session in the desktop app, or its title and date when it has no app id."""
    title = one_line(e.get("title") or e["session"][:8], 60)
    if e.get("ccd"):
        return f"[{title}](claude://claude.ai/epitaxy/{e['ccd']})"
    return f"{title} ({when(e)}, session {e['session'][:8]})"


def words(e, n=170):
    """Your words. For a card: the question, then what you picked or typed."""
    if e.get("source") == "card":
        q = one_line(e.get("question") or "", 70)
        return f"card \"{q}\" → \"{one_line(e.get('text') or '', max(40, n - len(q) - 12))}\""
    return "\"" + one_line(e.get("text") or "", n) + "\""


def card_sections(memory=None):
    """{card file: (section, title)} from each memory card's `index: "<section> | <title> | ..."` line."""
    out = {}
    folders = ledger.default_memory() if memory is None else memory
    for p in [p for f in folders for p in glob.glob(os.path.join(f, "*.md"))]:
        try:
            head = open(p, encoding="utf-8").read(3000)
        except OSError:
            continue
        m = re.search(r"^index:\s*\"?([^|\"\n]+)\|([^|\"\n]+)", head, re.M)
        if m:
            out[os.path.basename(p)] = (m.group(1).strip(), m.group(2).strip())
    return out


def load_rulings(path=None):
    path = path or ledger.RULINGS
    if not path:
        return {}
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rs = d.get("rulings") if isinstance(d, dict) else d
    return {r.get("id"): r for r in rs or [] if isinstance(r, dict)}


def other_ruling_ids(patterns=None):
    """Ruling ids in the other copies of the rulings file (ledger.rulings_also), or None when unset."""
    pats = patterns if patterns is not None else ledger._cfg("rulings_also", [])
    if not pats:
        return None
    ids = set()
    for pat in pats:
        for p in glob.glob(os.path.expanduser(pat)):
            ids.update(load_rulings(p))
    return ids


def standing(rows, rulings, sections, elsewhere=None):
    """Your decisions and ruled entries, newest first, minus anything reversed or replaced later.

    elsewhere: ruling ids found in other copies of the rulings file. A ruled entry whose ruling is in
    none of them was removed after it was written, so it is not reported as waiting to land. None skips
    that test."""
    replaced = {e.get("supersedes") for e in rows if e.get("supersedes")}
    out = []
    for e in rows:
        if e.get("source") in ("chip", "spawned") or e.get("state") == "dropped":
            continue
        if not (e.get("kind") == "decision" or e.get("state") == "ruled"):
            continue
        if e["id"] in replaced or (e.get("scope") or {}).get("reversed"):
            continue
        r = rulings.get(e.get("ref")) if e.get("state") == "ruled" else None
        if r and r.get("reversed"):
            continue
        if r:
            topic, src = r.get("topic") or "Rulings", f"ruling `{r['id']}` ({ledger.RULINGS_NAME})"
        elif e.get("state") == "ruled" and elsewhere is not None and e.get("ref") not in elsewhere:
            topic, src = ("Held only in the session log",
                          f"ruling `{e.get('ref')}` was removed after it was written; the session log has the decision")
        elif e.get("state") == "ruled":
            topic, src = "Rulings", f"ruling `{e.get('ref')}` (written in another copy; not in the main file yet)"
        elif e.get("state") == "card" and e.get("ref") in sections:
            topic, src = sections[e["ref"]][0], f"card {e['ref']}"
        elif e.get("state") == "card":
            topic, src = "Memory cards", f"card {e.get('ref')}"
        elif e.get("state") == "done" and (e.get("ref") or "").startswith("edited "):
            topic, src = "Held in a commit or an edit", e["ref"]
        elif e.get("state") == "done" and e.get("ref") not in (None, "reported"):
            topic, src = "Held in a commit or an edit", f"commit {e['ref']}"
        else:
            topic, src = "Held only in the session log", "the session log"
        out.append((topic, src, e))
    out.sort(key=lambda x: x[2].get("ts") or "", reverse=True)
    return out


def render(rows, now=None, rulings=None, sections=None, ledger_path=None, elsewhere=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if rulings is None:
        rulings = load_rulings()
        elsewhere = other_ruling_ids() if elsewhere is None else elsewhere
    sections = card_sections() if sections is None else sections
    rows = list(rows)
    opens = sorted((e for e in rows if e.get("kind") == "request" and e.get("state") == "open"
                    and not e.get("repeated_as")), key=lambda e: e.get("ts") or "")
    local = now.astimezone()
    out = [
        "<!-- Generated file: do not edit. The next run of digest.py replaces it. -->",
        "# Requests and answers",
        "",
        f"Generated {local:%Y-%m-%d %H:%M %Z} by `python {TOOLS}/digest.py`, from "
        f"{len(rows)} ledger entries (`python {TOOLS}/ledger.py` first). Personal: not for a team digest.",
        "",
        f"## Open requests ({len(opens)}, oldest first)",
        "",
    ]
    if not opens:
        out.append("None. Every request has a commit, a work item, a ruling, a card or a reply that settles it.")
    for e in opens[:OPEN_MAX]:
        d = age_days(e, now)
        out.append(f"- {when(e)} ({d}d) · {where(e)} · {words(e)}")
    if len(opens) > OPEN_MAX:
        out.append(f"- and {len(opens) - OPEN_MAX} newer: `python {TOOLS}/ledger.py --open`")
    asked = sorted((e for e in rows if e.get("kind") == "question" and e.get("state") == "open"
                    and not e.get("repeated_as")), key=lambda e: e.get("ts") or "")
    out += ["", f"## Open questions to you ({len(asked)}, oldest first)", "",
            "Put off on a card. Each closes when a later card or a triage pass names its id and you answer it."]
    if not asked:
        out.append("None.")
    for e in asked[:QUESTION_MAX]:
        out.append(f"- `{e.get('qid')}` {when(e)} ({age_days(e, now)}d) · {where(e)} · {words(e)}")
    if len(asked) > QUESTION_MAX:
        out.append(f"- and {len(asked) - QUESTION_MAX} newer: `python {TOOLS}/ledger.py --questions`")
    out += ["", "## Standing answers (newest first, by topic)", ""]
    ans = standing(rows, rulings, sections, elsewhere)
    topics, order = {}, []
    for topic, src, e in ans:
        if topic not in topics:
            topics[topic] = []
            order.append(topic)
        topics[topic].append((src, e))
    used, shown = 0, 0
    for topic in order:            # topics in order of their newest answer; the newest 3 of each
        if used + 2 > ANSWER_LINES:
            break
        items = topics[topic]
        out.append(f"**{topic}** ({len(items)})")
        used += 1
        for src, e in items[:PER_TOPIC]:
            if used >= ANSWER_LINES:
                break
            out.append(f"- {when(e)} · {words(e, 140)} · {src}")
            used += 1
            shown += 1
    if shown < len(ans):
        out.append(f"- and {len(ans) - shown} older or in other topics: `python {TOOLS}/ledger.py` keeps them all "
                   f"in {ledger_path or os.path.join(ledger.LEDGER_DIR, 'requests.jsonl')}")
    out.append("")
    return "\n".join(out)


def write(text, path=None):
    path = path or os.path.join(ledger.LEDGER_DIR, "DIGEST.md")
    ledger.write_atomic(path, text)
    bad = open(path, encoding="utf-8").read().count(chr(0xFFFD))
    return bad


def selftest():
    results = []

    def check(label, ok):
        results.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + label)

    now = dt.datetime(2026, 10, 23, 18, 0, tzinfo=dt.timezone.utc)
    rows = [
        {"id": "a", "ts": "2026-10-21T21:22:00Z", "session": "0830382a-x", "ccd": "local_abc", "title": "Rate limits",
         "source": "typed", "kind": "request", "state": "open", "text": "research the planted durable fix"},
        {"id": "b", "ts": "2026-10-03T03:31:00Z", "session": "4b32273c-x", "title": "Mac setup", "source": "card",
         "kind": "request", "state": "open", "text": "Later", "question": "Port the planted backup job?"},
        {"id": "c", "ts": "2026-10-22T10:00:00Z", "session": "s", "source": "typed", "kind": "request", "state": "done",
         "ref": "1234abc", "text": "fix the planted crash"},
        {"id": "d", "ts": "2026-10-23T09:00:00Z", "session": "s", "source": "card", "kind": "decision", "state": "ruled",
         "ref": "planted-rule", "text": "ship as drawn", "question": "Planted band: ship as drawn?"},
        {"id": "e", "ts": "2026-10-20T09:00:00Z", "session": "s", "source": "typed", "kind": "decision", "state": "ruled",
         "ref": "planted-reversed", "text": "badge keeps 12px"},
        {"id": "f", "ts": "2026-10-19T09:00:00Z", "session": "s", "source": "chip", "kind": "request", "state": "ruled",
         "ref": "planted-rule", "text": "Do this task here: agent words"},
        {"id": "g", "ts": "2026-10-18T09:00:00Z", "session": "s", "source": "typed", "kind": "decision", "state": "card",
         "ref": "people_planted.md", "text": "the planted owner is the design lead"},
        {"id": "h", "ts": "2026-10-14T09:00:00Z", "session": "s", "source": "typed", "kind": "request", "state": "open",
         "text": "ok let me know when both are done", "repeated_as": "i"},
        {"id": "j", "ts": "2026-10-23T06:37:00Z", "session": "s", "source": "typed", "kind": "request", "state": "open",
         "text": "<!-- reply 1 -->\n> The planted agent quote.\n\nwhere? that planted page is a mess"},
        {"id": "k", "ts": "2026-10-16T09:00:00Z", "session": "s", "source": "card", "kind": "question", "qid": "q-0a1b2c",
         "state": "open", "text": "Later", "question": "Move the planted capture store?"},
        {"id": "l", "ts": "2026-10-15T09:00:00Z", "session": "s", "source": "card", "kind": "question", "qid": "q-3d4e5f",
         "state": "answered", "ref": "card:x:0", "text": "Hold", "question": "Rotate the planted logs?"},
        {"id": "m", "ts": "2026-10-14T09:00:00Z", "session": "s", "source": "card", "kind": "question", "qid": "q-6a7b8c",
         "state": "open", "repeated_as": "k", "text": "Defer", "question": "Prune the planted cache?"},
    ]
    rulings = {"planted-rule": {"id": "planted-rule", "topic": "Planted bands"},
               "planted-reversed": {"id": "planted-reversed", "topic": "Badges", "reversed": "2026-10-22, the user"}}
    text = render(rows, now, rulings, {"people_planted.md": ("People", "Planted")}, "/x/requests.jsonl")
    lines = text.splitlines()
    check("the generated-file header and the generator command come first",
          lines[0].startswith("<!-- Generated file") and "digest.py" in lines[3] and "ledger.py" in lines[3])
    i_b, i_a = text.find("Port the planted backup"), text.find("research the planted durable")
    check("open requests are listed oldest first, with their words", 0 < i_b < i_a)
    check("a session with an app id links into the desktop app; one without shows its title and date",
          "(claude://claude.ai/epitaxy/local_abc)" in text and "Mac setup (2026-10-03, session 4b32273c)" in text)
    check("a reply with a marker shows your words, not the marker or the agent text it quotes",
          "\"where? that planted page is a mess\"" in text and "planted agent quote" not in text)
    check("a done request is not listed, nor an earlier copy of a request typed again",
          "fix the planted crash" not in text and "let me know when both" not in text)
    check("a standing answer shows its topic, words, source and date; a reversed ruling and agent words do not",
          "**Planted bands**" in text and "ship as drawn" in text and "ruling `planted-rule`" in text
          and "2026-10-23" in text and "badge keeps" not in text and "agent words" not in text)
    check("a decision held in a memory card is listed under the card's index section",
          "**People**" in text and "card people_planted.md" in text)
    check("an open question put off is listed under its id with the question; an answered one and one a "
          "later card carries are not",
          "`q-0a1b2c` 2026-10-16" in text and "Move the planted capture store?" in text
          and "q-3d4e5f" not in text and "q-6a7b8c" not in text and "Open questions to you (1," in text)
    gone = rows + [
        {"id": "n", "ts": "2026-10-23T08:00:00Z", "session": "s", "source": "card", "kind": "decision", "state": "ruled",
         "ref": "planted-in-worktree", "text": "planted worktree answer"},
        {"id": "o", "ts": "2026-10-23T07:00:00Z", "session": "s", "source": "card", "kind": "decision", "state": "ruled",
         "ref": "planted-removed", "text": "planted removed answer"}]
    gt = render(gone, now, rulings, {}, "/x/requests.jsonl", elsewhere={"planted-in-worktree"})
    check("a ruling only another copy holds is 'not in the main file yet'; one no copy holds reads as removed",
          "ruling `planted-in-worktree` (written in another copy; not in the main file yet)" in gt
          and "ruling `planted-removed` was removed after it was written" in gt
          and "planted-removed` (written in another copy" not in gt)
    many = rows + [dict(rows[0], id=f"x{i}", ts=f"2026-10-22T{i % 24:02d}:00:00Z", text=f"planted ask {i}")
                   for i in range(40)]
    big = render(many, now, rulings, {}, "/x/requests.jsonl").splitlines()
    check("with 42 open requests it stays near 60 lines and points at the full list",
          len(big) <= 70 and any("ledger.py --open" in l for l in big))
    with tempfile.TemporaryDirectory() as t:
        p = os.path.join(t, "DIGEST.md")
        bad = write(text + "\nem dash — kept\n", p)
        check("the file is written whole and its non-ASCII survives (0 U+FFFD)",
              bad == 0 and open(p, encoding="utf-8").read().endswith("em dash — kept\n"))
    print(f"selftest: {sum(results)} of {len(results)} checks passed")
    return 0 if all(results) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="print instead of writing DIGEST.md")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dir", default=ledger.LEDGER_DIR, help=f"ledger folder (default {ledger.LEDGER_DIR})")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    path = os.path.join(a.dir, "requests.jsonl")
    if not os.path.isfile(path):
        print(f"digest: no ledger at {path}; run ledger.py first", file=sys.stderr)
        return 2
    rows = ledger.load_jsonl(path).values()
    text = render(rows, ledger_path=path)
    if a.print:
        print(text)
        return 0
    out = os.path.join(a.dir, "DIGEST.md")
    bad = write(text, out)
    print(f"digest: {len(text.splitlines())} lines, {bad} U+FFFD. Written {out}")
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
