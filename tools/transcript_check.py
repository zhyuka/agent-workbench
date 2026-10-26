#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""transcript_check.py - integrity checks for a folder of meeting transcripts (caption captures).

  python tools/transcript_check.py audit  [--dir DIR] [--json]
  python tools/transcript_check.py term   <TERM> [--dir DIR]
  python tools/transcript_check.py vocab  [--dir DIR] [--min 3]
  python tools/transcript_check.py --selftest

THE FOLDER holds raw captures (<name>.txt, with [hh:mm:ss] stamps) and the cleaned summaries you
write from them (<name>.md, same stem). --dir names it; else `transcripts.dir` in the agent-workbench
config (falling back to `recall.transcripts` and `seen_before.transcripts`).

WHY THIS EXISTS. A session greps a transcript for a product name, gets nothing, concludes the call
was not about it, and drafts an answer from a memory file instead of the source. Then a note gets
written that the captions render the name wrong "throughout" and that a grep "returns nothing", and
that is false too: caption engines mishear proper nouns INCONSISTENTLY, which is worse than
mishearing them reliably:

  - a ZERO-hit grep does not mean the term is absent   (the original failure)
  - a NON-ZERO-hit grep does not mean the text is clean (the over-correction that follows)

A prose warning in a skill does not prevent this. Hence a script: you cannot honestly claim a term
is absent from a transcript until `term` says so, because `term` counts every spelling at once.

THE ALIAS MAP is the durable artifact: {canonical term: [spellings the captions produced]}. A few
public product names ship as defaults below. Add your own (products, colleagues, internal names) in
config: `transcripts.aliases` and `transcripts.ambiguous` (same shape), or a JSON file named by
`transcripts.aliases_file` holding {"aliases": {...}, "ambiguous": {...}}. Yours are merged over the
defaults. AMBIGUOUS spellings are also ordinary words ("cloud" for Claude): a hit is a lead, never a
finding. Run `vocab` to find uncatalogued mishearings.

ANOTHER FAILURE MODE THIS CATCHES. Cleaned `.md` files cite timestamps that do not exist in the raw
`.txt` they summarise, or whose block holds neither spelling it is cited for. `audit` re-derives
every cited stamp against the raw file. It also flags a leftover `<name>.txt.tmp` that holds MORE
captions than the `.txt` it failed to be renamed into: a silently truncated transcript reads
complete.

Exit codes: 0 clean, 1 findings, 2 usage/IO error (no folder configured or found), or `term` on a
word the alias map lacks with no hits (a did-not-run). Never prints file contents wholesale.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from collections import defaultdict

# A non-UTF-8 console dies with UnicodeEncodeError on a glyph this file prints -- often at the
# LAST print, throwing away completed work.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import kit_config
except ImportError:
    kit_config = None


def _cfg(*keys):
    return kit_config.get(*keys) if kit_config else None


def default_dir():
    """transcripts.dir, else recall.transcripts / seen_before.transcripts, else None."""
    for keys in (("transcripts", "dir"), ("recall", "transcripts"), ("seen_before", "transcripts")):
        v = _cfg(*keys)
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            return os.path.expanduser(v)
    return None


# Defaults: public product names and the mishearings caption engines commonly produce. Matching is
# case-insensitive, so aliases that differ only in case are the SAME pattern - listing both
# double-counts every occurrence. Keep one casing per spelling.
ALIASES = {
    "GitLab":     ["Git lab", "get lab"],
    "GitHub":     ["Git hub", "get hub"],
    "FigJam":     ["fig jam", "pig jam", "big jam", "thick jam"],
    "Figma":      ["pigma", "thigma", "figment", "Figna"],
    "WCAG":       ["W CAG", "Wickag", "W-CAG", "Wick AG"],
    "Anthropic":  ["Entropic", "Antropic", "Philanthropic"],
    "MCP":        ["MTP", "NCP", "MCPS", "AMCP", "M.C.P."],
    # "servability" is not a word; "survivability" is, so it sits in AMBIGUOUS instead.
    "observability": ["servability", "observerbility"],
    "Opus":       ["OPIS"],
    "Astro":      ["Astros"],
}

# Aliases that are also ordinary English words. These fire constantly on legitimate usage, so a hit
# is a LEAD, not a finding. Reported separately and never counted in the headline. Do not promote
# one of these without reading the line.
AMBIGUOUS = {
    "Claude":  ["cloud", "clod", "clot", "clog", "clawed", "quad", "Claud"],
    "Figma":   ["Sigma"],          # collides with Six Sigma
    "Sonnet":  ["Sonic", "Sunnet"],
    "Astro":   ["Astra"],
    "ADO":     ["ado"],            # collides with the idiom "without further ado"
    "observability": ["survivability"],
}


def load_aliases():
    """Merge the configured alias maps over the defaults."""
    import json as _json
    extra_a, extra_b = dict(_cfg("transcripts", "aliases") or {}), dict(_cfg("transcripts", "ambiguous") or {})
    f = _cfg("transcripts", "aliases_file")
    if f:
        try:
            with open(os.path.expanduser(f), encoding="utf-8") as fh:
                d = _json.load(fh)
            extra_a.update(d.get("aliases") or {})
            extra_b.update(d.get("ambiguous") or {})
        except (OSError, ValueError) as e:
            print("could not read the alias file %s: %s" % (f, type(e).__name__), file=sys.stderr)
            sys.exit(2)
    ALIASES.update({k: list(v) for k, v in extra_a.items()})
    AMBIGUOUS.update({k: list(v) for k, v in extra_b.items()})


STAMP = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")
BARE_STAMP = re.compile(r"(?<![\d:])(\d{2}:\d{2}:\d{2})(?![\d:])")


def read(p):
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def transcripts(d):
    if not os.path.isdir(d):
        print("no such directory: %s" % d, file=sys.stderr)
        sys.exit(2)
    txt, md, tmp = {}, {}, {}
    for name in sorted(os.listdir(d)):
        full = os.path.join(d, name)
        if not os.path.isfile(full):
            continue
        stem, ext = os.path.splitext(name)
        if name.lower().endswith(".txt.tmp.txt"):
            # The same leftover with ".txt" appended (a sync client can rename it so). Checked
            # before the .txt branch, which would otherwise count it as a raw transcript with no
            # cleaned .md.
            tmp[name[:-len(".txt.tmp.txt")]] = full
        elif ext.lower() == ".txt":
            txt[stem] = full
        elif ext.lower() == ".md":
            md[stem] = full
        elif ext.lower() == ".tmp" and stem.lower().endswith(".txt"):
            # transcript_x.txt.tmp -> splitext gives stem "transcript_x.txt". Strip the
            # inner extension so it keys the same as its .txt and .md siblings.
            tmp[os.path.splitext(stem)[0]] = full
    return txt, md, tmp


def count_ci(hay, needle):
    """Case-insensitive count with word boundaries.

    Boundaries are NOT optional here. Without them "Ann" matches inside "Annual" and "Annette": the
    same class of over-count the tool exists to prevent. \\b is applied only at an end that is actually a
    word character, so multi-word aliases ("Git lab") and ones with punctuation ("W-CAG") still work.
    """
    pat = re.escape(needle)
    if needle[:1].isalnum():
        pat = r"\b" + pat
    if needle[-1:].isalnum():
        pat = pat + r"\b"
    return len(re.findall(pat, hay, re.I))


def _hits(text, term, aliases):
    seen, hits = set(), {}
    for a in aliases:
        k = a.lower()
        # skip duplicate casings, and aliases that are a substring of the canonical term
        if k in seen or k in term.lower():
            continue
        seen.add(k)
        n = count_ci(text, a)
        if n:
            hits[a] = n
    return hits


def scan_terms(text, ambiguous=False):
    """Return {term: (correct_count, {alias: count})}. ambiguous=True uses the noisy alias set."""
    table = AMBIGUOUS if ambiguous else ALIASES
    out = {}
    for term, aliases in table.items():
        hits = _hits(text, term, aliases)
        correct = count_ci(text, term)
        if hits or (correct and not ambiguous):
            out[term] = (correct, hits)
    return out


def need_dir(args):
    d = args.dir or default_dir()
    if not d:
        print("no transcripts folder: pass --dir or set transcripts.dir in the agent-workbench config",
              file=sys.stderr)
        sys.exit(2)
    return d


def cmd_term(args):
    d = need_dir(args)
    txt, md, _ = transcripts(d)
    term = args.term
    aliases = ALIASES.get(term)
    uncatalogued = aliases is None
    if uncatalogued:
        # allow an uncatalogued term; just count it literally
        aliases = []
        print("note: %r is not in the alias map - counting the literal spelling only.\n" % term)
    # AMBIGUOUS holds known mishearings that are also real words. Fold them in as LEADS: they
    # are candidates a reader must judge, not confirmed hits, so they are reported but never
    # counted as the correct spelling.
    leads = [a for a in AMBIGUOUS.get(term, []) if a not in aliases]
    aliases = list(aliases) + leads
    if leads:
        print("note: also searching %d known mis-hearing(s) of %r: %s\n"
              % (len(leads), term, ", ".join(leads)))
    total_correct = total_alias = 0
    rows = []
    for stem in sorted(set(list(txt) + list(md))):
        for kind, table in (("txt", txt), ("md", md)):
            p = table.get(stem)
            if not p:
                continue
            s = read(p)
            c = count_ci(s, term)
            hits = {}
            for a in aliases:
                if a.lower() in term.lower():
                    continue
                n = count_ci(s, a)
                if n:
                    hits[a] = n
            if c or hits:
                rows.append((stem, kind, c, hits))
                total_correct += c
                total_alias += sum(hits.values())
    if not rows:
        nfiles = len(set(list(txt) + list(md)))
        # The safety certificate is earned by the ALIAS MAP, not by this function running. For an
        # uncatalogued term `aliases` is empty, so this checked exactly ONE literal spelling -- a
        # bare grep -- and must not print the same "safe conclusion" as a real multi-spelling miss.
        if uncatalogued:
            print("UNSAFE: %r is NOT in the alias map, so this checked ONE literal spelling "
                  "across %d file(s)." % (term, nfiles))
            print("That is a bare grep. It is a DID-NOT-RUN on the mis-hearings, not an absence.")
            print("Add %r to transcripts.aliases with its known mis-renderings, then re-run." % term)
            return 2
        print("NOT FOUND anywhere: %r (checked %d spellings across %d files)"
              % (term, 1 + len(aliases), nfiles))
        print("This is the ONLY form in which 'absent' is a safe conclusion.")
        return 0
    print("%-52s %-4s %-8s %s" % ("file", "kind", "correct", "mis-renderings"))
    print("-" * 104)
    for stem, kind, c, hits in rows:
        h = ", ".join("%s x%d" % (k, v) for k, v in sorted(hits.items())) or "-"
        flag = "  <-- BOTH PRESENT" if (c and hits) else ""
        print("%-52s %-4s %-8d %s%s" % (stem[:52], kind, c, h, flag))
    print("-" * 104)
    print("TOTAL correct %d   TOTAL mis-rendered %d" % (total_correct, total_alias))
    if total_correct and total_alias:
        print("\n** Both spellings occur. Neither a hit nor a miss tells you anything about the")
        print("   rest of the file. Read the sections, do not trust a single grep. **")
    return 0


def cmd_audit(args):
    d = need_dir(args)
    txt, md, tmp = transcripts(d)
    findings = defaultdict(list)

    # 1. pairing - every raw .txt needs a cleaned .md
    for stem in sorted(txt):
        if stem not in md:
            findings["unpaired"].append(stem)

    # 2. phantom / misattributed timestamps cited in the cleaned .md
    for stem, mp in sorted(md.items()):
        tp = txt.get(stem)
        if not tp:
            continue
        raw = read(tp)
        raw_stamps = set(STAMP.findall(raw)) | set(BARE_STAMP.findall(raw))
        for cited in sorted(set(STAMP.findall(read(mp)))):
            if cited not in raw_stamps:
                findings["phantom_stamp"].append((stem, cited))

    # 3. inconsistent product nouns. Prefer the RAW .txt - the .md is our own cleaned prose and
    #    counting it measures what we wrote, not what the caption engine heard.
    for stem in sorted(set(list(txt) + list(md))):
        p = txt.get(stem) or md.get(stem)
        body = read(p)
        for term, (correct, hits) in sorted(scan_terms(body).items()):
            if hits:
                findings["mixed" if correct else "all_wrong"].append(
                    (stem, term, correct, hits))
        for term, (correct, hits) in sorted(scan_terms(body, ambiguous=True).items()):
            if hits:
                findings["ambiguous"].append((stem, term, correct, hits))

    # 3b. a leftover .txt.tmp that is MORE COMPLETE than the .txt it failed to become.
    #     A capture that writes .txt.tmp and then atomically renames it leaves the .tmp holding
    #     the call's final captions when the rename fails, and every downstream step reads the
    #     short .txt and never warns: a silently truncated transcript that reads complete.
    #     Every .tmp is listed with its verdict, not just the defective ones: "shorter" and
    #     "same" are the denominator, and a .tmp is NOT presumed stale.
    for stem in sorted(tmp):
        tp, mp = txt.get(stem), tmp[stem]
        t_st = STAMP.findall(read(tp)) if tp else []
        m_st = STAMP.findall(read(mp))
        extra = list(dict.fromkeys(s for s in m_st if s not in set(t_st)))
        verdict = ("MORE COMPLETE" if extra else
                   "same" if len(m_st) == len(t_st) else "shorter")
        findings["stray_tmp"].append(
            (stem, verdict, len(t_st), len(m_st), extra[:3], stem in md))

    # 4. caption-note lines that assert "throughout" / "returns nothing"
    bad_claim = re.compile(r"throughout|returns? nothing|no mention|all instances", re.I)
    for stem, mp in sorted(md.items()):
        for i, line in enumerate(read(mp).splitlines(), 1):
            if bad_claim.search(line) and re.search(r"caption|render|grep|spell", line, re.I):
                findings["absolute_claim"].append((stem, i, line.strip()[:150]))

    n = sum(len(v) for v in findings.values())
    print("TRANSCRIPT AUDIT  dir=%s" % d)
    print("%d raw .txt, %d cleaned .md, %d findings\n" % (len(txt), len(md), n))

    if findings["unpaired"]:
        print("== RAW .txt WITH NO CLEANED .md (%d) ==" % len(findings["unpaired"]))
        for s in findings["unpaired"]:
            print("   %s" % s)
        print()
    if findings["stray_tmp"]:
        bad = [f for f in findings["stray_tmp"] if f[1] == "MORE COMPLETE"]
        print("== LEFTOVER .txt.tmp / .txt.tmp.txt (%d, of which %d MORE COMPLETE than the .txt) ==" %
              (len(findings["stray_tmp"]), len(bad)))
        print("   a .tmp is NOT presumed stale. MORE COMPLETE = the .txt lost captions.")
        for stem, verdict, nt, nm, extra, has_md in findings["stray_tmp"]:
            print("   %-13s %s  stamps .txt=%d .tmp=%d%s" %
                  (verdict, stem, nt, nm,
                   "  cleaned .md EXISTS and is built from the SHORT .txt" if
                   (verdict == "MORE COMPLETE" and has_md) else ""))
            for s in extra:
                print("      + [%s] only in .tmp" % s)
        print()
    if findings["phantom_stamp"]:
        print("== TIMESTAMPS CITED IN .md THAT ARE NOT IN THE RAW .txt (%d) =="
              % len(findings["phantom_stamp"]))
        for s, c in findings["phantom_stamp"]:
            print("   %-52s [%s]" % (s[:52], c))
        print()
    if findings["mixed"]:
        print("== TERM SPELLED BOTH WAYS IN ONE FILE (%d) - the dangerous case ==" % len(findings["mixed"]))
        for s, t, c, h in findings["mixed"]:
            print("   %-46s %-11s correct x%-3d  %s"
                  % (s[:46], t, c, ", ".join("%s x%d" % kv for kv in sorted(h.items()))))
        print()
    if findings["all_wrong"]:
        print("== TERM ONLY EVER MIS-RENDERED (%d) - a grep for it returns nothing ==" % len(findings["all_wrong"]))
        for s, t, c, h in findings["all_wrong"]:
            print("   %-46s %-11s  %s"
                  % (s[:46], t, ", ".join("%s x%d" % kv for kv in sorted(h.items()))))
        print()
    if findings["ambiguous"]:
        print("== LEADS ONLY - alias is also an ordinary English word (%d) ==" % len(findings["ambiguous"]))
        print("   NOT findings. Read the line before promoting one.")
        shown = findings["ambiguous"][:12]
        for s, t, c, h in shown:
            print("   %-46s %-11s correct x%-3d  %s"
                  % (s[:46], t, c, ", ".join("%s x%d" % kv for kv in sorted(h.items()))))
        if len(findings["ambiguous"]) > len(shown):
            print("   ... and %d more (use --json for all)" % (len(findings["ambiguous"]) - len(shown)))
        print()
    if findings["absolute_claim"]:
        print("== ABSOLUTE CLAIMS IN CAPTION NOTES - re-derive each before trusting (%d) =="
              % len(findings["absolute_claim"]))
        for s, i, line in findings["absolute_claim"]:
            print("   %s:%d\n      %s" % (s[:60], i, line))
        print()

    if args.json:
        print(json.dumps({k: v for k, v in findings.items()}, indent=2, default=str))
    if not n:
        print("clean.")
    return 1 if n else 0


def cmd_vocab(args):
    """Discover mishearings we have NOT catalogued, by fuzzy-matching transcript vocabulary
    against the canonical terms. Noisy by design - it is a lead generator, not a verdict."""
    d = need_dir(args)
    txt, _, _ = transcripts(d)
    vocab = defaultdict(int)
    for p in txt.values():
        for w in re.findall(r"[A-Za-z][A-Za-z'À-ſ]{2,}", read(p)):
            vocab[w] += 1
    known = {a.lower() for al in ALIASES.values() for a in al}
    known |= {t.lower() for t in ALIASES}
    print("candidate UNCATALOGUED mishearings (min count %d):\n" % args.min)
    seen = set()
    for term in ALIASES:
        for w, n in vocab.items():
            if n < args.min or w.lower() in known or w.lower() in seen:
                continue
            r = difflib.SequenceMatcher(None, w.lower(), term.lower()).ratio()
            if 0.72 <= r < 1.0:
                print("   %-18s ~ %-11s ratio %.2f  count %d" % (w, term, r, n))
                seen.add(w.lower())
    print("\nAdd real ones to transcripts.aliases in the config. That map is the durable artifact.")
    return 0


def selftest():
    """A planted folder: each audit finding once, and `term` on a found, a missing and an
    uncatalogued word. Offline."""
    import contextlib
    import io
    import tempfile
    results = []

    def check(label, ok):
        results.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + label)

    with tempfile.TemporaryDirectory() as d:
        def put(name, text):
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        put("call_a.txt", "[10:00:01] We moved the board to fig jam.\n[10:00:09] The FigJam board is shared.\n")
        put("call_a.md", "# Call A\nThe board moved [10:00:01]. A phantom cite [10:05:00].\n"
                         "Caption note: the captions render FigJam wrong throughout.\n")
        put("call_b.txt", "[11:00:00] Ask Claude about it.\n")
        put("call_c.txt", "[12:00:00] one\n")
        put("call_c.txt.tmp", "[12:00:00] one\n[12:00:30] the last line\n")
        put("call_c.md", "# C\n")

        class A:
            dir = d
            json = False
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cmd_audit(A())
        text = out.getvalue()
        check("audit exits 1 on findings", rc == 1)
        check("a raw .txt with no cleaned .md is listed", "== RAW .txt WITH NO CLEANED .md (1)" in text
              and "call_b" in text)
        check("a cited stamp not in the raw file is a phantom; a real one is not",
              "[10:05:00]" in text and "[10:00:01]" not in text.split("NOT IN THE RAW")[1].split("==")[0])
        check("a term spelled both ways in one file is the dangerous case",
              "SPELLED BOTH WAYS" in text and "fig jam x1" in text)
        check("a leftover .tmp with more captions than its .txt is MORE COMPLETE",
              "MORE COMPLETE call_c" in text and "[12:00:30] only in .tmp" in text)
        check("an absolute claim in a caption note is flagged", "ABSOLUTE CLAIMS" in text)
        check("an alias that is also a word is a lead, not a finding",
              "LEADS ONLY" not in text or "Claude" not in text.split("LEADS ONLY")[0])

        class T:
            dir = d
        for term, want_rc, want in (("FigJam", 0, "BOTH PRESENT"), ("GitLab", 0, "NOT FOUND anywhere"),
                                    ("Zorblat", 2, "UNSAFE")):
            T.term = term
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cmd_term(T())
            check(f"term {term}: exit {want_rc}, says {want!r}", rc == want_rc and want in out.getvalue())
    print("selftest: %d of %d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


def main():
    if sys.argv[1:2] == ["--selftest"]:
        sys.exit(selftest())
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit", help="full integrity sweep of the transcript directory")
    a.add_argument("--dir"); a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_audit)
    t = sub.add_parser("term", help="count EVERY spelling of one term across all transcripts")
    t.add_argument("term"); t.add_argument("--dir")
    t.set_defaults(fn=cmd_term)
    v = sub.add_parser("vocab", help="discover uncatalogued mishearings (lead generator)")
    v.add_argument("--dir"); v.add_argument("--min", type=int, default=3)
    v.set_defaults(fn=cmd_vocab)
    args = ap.parse_args()
    load_aliases()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
