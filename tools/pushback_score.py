#!/usr/bin/env python3
"""pushback_score.py - how often the user overrides or pushes back on a recommendation, by day, and
whether a challenger agent ran first.

    python tools/pushback_score.py --ledger FILE [--days 7]   print the daily lines
    python tools/pushback_score.py --ledger FILE --json OUT   also write the counts as JSON
    python tools/pushback_score.py --ledger FILE --sample N   print N random push matches, to check the pattern by eye
    python tools/pushback_score.py --selftest                 a planted ledger and log: each count, each split

WHY THIS EXISTS. When the user keeps having to type "think bigger" or "did you research everything",
the push itself is the cost. Scoring a few cases by hand once says little; counting it every day from
a record of requests says whether it is going down, and whether running a challenger agent before a
recommendation changes it. This only counts; it changes nothing about how the user is asked.

THE LEDGER (--ledger, required). A JSON-lines file of what the user answered and typed, one object
per line. This tool does not write it; any logger that emits these fields works:
  ts           ISO time of the answer or message
  session      the Claude Code session id it happened in
  source       "card" (an answered question with a recommended option), "typed" or "mid-turn"
               (a message the user typed); any other source is ignored
  answer_kind  for a card: "recommended", "other option" (picked another listed option) or "typed"
               (typed an answer instead)
  asked_at     for a card: when it was asked (else ts is used)
  text         what the user typed or picked

WHAT IT COUNTS, per local day:
  cards     answered cards. `other` = another listed option; `typed` = a typed answer instead.
  pushes    typed messages and typed card answers that match PUSH ("think bigger", "research
            everything", "from root", "r u sure", ...), against all typed messages that day.
Each is split by `challenged`: a `challenger` agent was spawned in the same session in the
CHALLENGE_WINDOW before the card was asked or the push was typed (read from that session's main log
under --projects, default ~/.claude/projects).

BLIND. Observational: a challenger runs where an agent chose to run one, so the split compares
different kinds of work, not like with like. PUSH is a pattern; --sample shows what it catches. A
session that leaves no log is never counted as challenged. A push about something other than the
last recommendation still counts. Today is as partial as the ledger is.

EXIT. 0 printed; 2 did not run (no ledger given, or it is missing or will not parse).
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import random
import re
import sys
import tempfile

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
CHALLENGE_WINDOW = dt.timedelta(minutes=90)

PUSH = re.compile(
    r"think (much )?bigger|research(ed)? (literally )?everything|did (u|you) (even )?(research|check|look)"
    r"|from (the )?root|root fix|r u sure|are you sure|best (approach|solution|way)"
    r"|literally everything|why (do i|did (u|you)|didn'?t (u|you))|should (have|of) (done|checked|researched)",
    re.I)


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def read_ledger(path):
    """(rows, None) or (None, why it did not run). A missing or unreadable ledger never reads as no
    push-backs."""
    if not path:
        return None, "no --ledger given"
    if not os.path.isfile(path):
        return None, f"no ledger at {path}"
    try:
        return load(path), None
    except (OSError, ValueError) as e:
        return None, f"the ledger could not be read ({type(e).__name__})"


def challenger_spawns(sessions, projects=PROJECTS):
    """{session id: [spawn times]} from each session's main log. Only lines naming the challenger are parsed."""
    out = {}
    for sid in sessions:
        times = []
        for path in glob.glob(os.path.join(projects, "*", sid + ".jsonl")):
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"challenger"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    for item in (d.get("message") or {}).get("content") or []:
                        if (isinstance(item, dict) and item.get("type") == "tool_use"
                                and (item.get("input") or {}).get("subagent_type") == "challenger"):
                            t = parse_ts(d.get("timestamp"))
                            if t:
                                times.append(t)
        out[sid] = sorted(times)
    return out


def challenged(when, spawns):
    return when is not None and any(when - CHALLENGE_WINDOW <= t <= when for t in spawns)


def score(rows, days, now, projects=PROJECTS):
    """{local date: counts}, newest last, for the last `days` local days including today."""
    first = (now.astimezone() - dt.timedelta(days=days - 1)).date()
    keep = []
    for r in rows:
        t = parse_ts(r.get("ts"))
        if t and t.astimezone().date() >= first:
            keep.append((t, r))
    spawns = challenger_spawns({r.get("session") for _, r in keep if r.get("session")}, projects)
    days_out = collections.OrderedDict(
        (first + dt.timedelta(days=i), collections.Counter()) for i in range(days))
    for t, r in keep:
        c = days_out.get(t.astimezone().date())
        if c is None:
            continue
        src = r.get("source")
        if src == "card":
            ch = challenged(parse_ts(r.get("asked_at")) or t, spawns.get(r.get("session"), []))
            pre = "ch_" if ch else ""
            c[pre + "cards"] += 1
            if r.get("answer_kind") == "other option":
                c[pre + "other"] += 1
            elif r.get("answer_kind") == "typed":
                c[pre + "typed_answer"] += 1
            typed_text = r.get("answer_kind") == "typed"
        else:
            if src not in ("typed", "mid-turn"):
                continue
            ch = challenged(t, spawns.get(r.get("session"), []))
            c["typed"] += 1
            typed_text = True
        if typed_text and PUSH.search(r.get("text") or ""):
            c[("ch_" if ch else "") + "pushes"] += 1
    return days_out


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "n/a"


def lines(days_out):
    out = []
    for day, c in days_out.items():
        cards = c["cards"] + c["ch_cards"]
        if not cards and not c["typed"]:
            out.append(f"- {day:%m/%d}: nothing in the ledger")
            continue
        s = (f"- {day:%m/%d}: {cards} cards, another option on {c['other'] + c['ch_other']} "
             f"({pct(c['other'] + c['ch_other'], cards)}), typed instead on {c['typed_answer'] + c['ch_typed_answer']}; "
             f"{c['pushes'] + c['ch_pushes']} push-backs in {c['typed'] + c['typed_answer'] + c['ch_typed_answer']} "
             f"typed messages")
        if c["ch_cards"] or c["ch_pushes"]:
            s += (f". After a challenger: {c['ch_cards']} cards, another option on {c['ch_other']}, "
                  f"{c['ch_pushes']} push-backs")
        out.append(s)
    return out


def selftest():
    fails = []

    def check(name, ok):
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            fails.append(name)

    cases = [("u need to think bigger", True), ("/plan you need to think bigger. Like what", True),
             ("that broke again (pls fix from root so this stops happening", True),
             ("r u sure u researched literally everything possible?", True),
             ("did you even research how to make it faster?", True),
             ("push it", False), ("Option A (Recommended)", False), ("yes both look good", False),
             ("the root node is 12:345", False)]
    for text, want in cases:
        check(f"PUSH {'matches' if want else 'skips'} {text[:40]!r}", bool(PUSH.search(text)) == want)

    with tempfile.TemporaryDirectory() as t:
        now = dt.datetime(2030, 1, 15, 20, 0, tzinfo=dt.timezone.utc)
        spawn = now - dt.timedelta(hours=3)
        proj = os.path.join(t, "projects", "-home-user-project")
        os.makedirs(proj)
        with open(os.path.join(proj, "s1.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": spawn.isoformat().replace("+00:00", "Z"), "type": "assistant",
                                "message": {"content": [{"type": "tool_use", "name": "Agent",
                                                         "input": {"subagent_type": "challenger"}}]}}) + "\n")
            f.write(json.dumps({"timestamp": spawn.isoformat(), "type": "assistant",
                                "message": {"content": [{"type": "text", "text": "the challenger said"}]}}) + "\n")

        def iso(d):
            return (now - d).isoformat().replace("+00:00", "Z")

        rows = [
            {"source": "card", "session": "s1", "ts": iso(dt.timedelta(hours=2, minutes=50)),
             "asked_at": iso(dt.timedelta(hours=2, minutes=55)), "answer_kind": "other option", "text": "B"},
            {"source": "card", "session": "s1", "ts": iso(dt.timedelta(hours=1)),
             "asked_at": iso(dt.timedelta(hours=1)), "answer_kind": "recommended", "text": "A (Recommended)"},
            {"source": "card", "session": "s2", "ts": iso(dt.timedelta(hours=1)),
             "asked_at": iso(dt.timedelta(hours=1)), "answer_kind": "typed", "text": "u need to think bigger"},
            {"source": "typed", "session": "s2", "ts": iso(dt.timedelta(minutes=30)), "text": "r u sure? from root pls"},
            {"source": "typed", "session": "s1", "ts": iso(dt.timedelta(hours=2, minutes=30)), "text": "think bigger"},
            {"source": "typed", "session": "s2", "ts": iso(dt.timedelta(minutes=20)), "text": "push it"},
            {"source": "chip", "session": "s2", "ts": iso(dt.timedelta(minutes=10)), "text": "think bigger"},
            {"source": "typed", "session": "s2", "ts": iso(dt.timedelta(days=9)), "text": "think bigger"},
        ]
        led = os.path.join(t, "requests.jsonl")
        with open(led, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        projects = os.path.join(t, "projects")
        c = score(load(led), 7, now, projects)[now.astimezone().date()]
        check("the card 5 min after a challenger spawn counts as challenged", c["ch_cards"] == 1 and c["ch_other"] == 1)
        check("a card 2 h after the spawn is not challenged", c["cards"] == 2)
        check("a typed card answer is an override, not another option", c["typed_answer"] == 1 and c["other"] == 0)
        check("pushes: typed card answer and typed message, not 'push it'", c["pushes"] == 2)
        check("a push 30 min after the spawn counts as challenged", c["ch_pushes"] == 1)
        check("a chip is not a typed message", c["typed"] == 3)
        check("a row older than the window is left out",
              sum(v["typed"] for v in score(load(led), 7, now, projects).values()) == 3)
        check("a missing ledger reads as did not run", read_ledger(os.path.join(t, "nope.jsonl"))[1] is not None)
        bad = os.path.join(t, "bad.jsonl")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json\n")
        check("a broken ledger reads as did not run", read_ledger(bad)[1] is not None)
    total = len(cases) + 9
    print(f"{total - len(fails)} of {total} checks passed")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(prog="pushback_score.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json")
    ap.add_argument("--sample", type=int)
    ap.add_argument("--ledger", help="the request ledger, JSON lines (see THE LEDGER)")
    ap.add_argument("--projects", default=PROJECTS, help="Claude Code's projects folder (default ~/.claude/projects)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rows, why = read_ledger(a.ledger)
    if why:
        print(f"did not run: {why}", file=sys.stderr)
        return 2
    if a.sample:
        hits = [r for r in rows if (r.get("source") in ("typed", "mid-turn") or r.get("answer_kind") == "typed")
                and PUSH.search(r.get("text") or "")]
        random.seed(7)
        for r in random.sample(hits, min(a.sample, len(hits))):
            print(f"{r.get('ts', '')[:16]} {r.get('session', '')[:8]}  {(r.get('text') or '')[:160]!r}")
        print(f"{len(hits)} matches in {len(rows)} ledger entries")
        return 0
    days_out = score(rows, a.days, dt.datetime.now(dt.timezone.utc), a.projects)
    print("\n".join(lines(days_out)))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({str(k): dict(v) for k, v in days_out.items()}, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
