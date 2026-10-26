"""Extract 100% of the user-authored content from the Claude Code logs, with a real denominator.

  python tools/log_extract.py --out DIR [--grep REGEX] [--session ID] [--since YYYY-MM-DD] [--chunk N]
                              [--include-sealed]
  python tools/log_extract.py --selftest   planted logs: turns, copies, filters, seals and each exit 2

SEALED. A log a `.recall-sealed` marker's `logs` patterns name, and a log copy on disk inside the sealed
folder, is not read, every copy of it, in zips too. The markers are the ones recall.py honours, found in
its notes folders, and seals.py holds the rule for every tool that reads them. The output prints one
line per seal with how many log copies of how many logs it held back, and the copies line says how many
sealed copies were not read. A sealed log's turns are in neither the output nor the denominator, and the
exit codes do not change. --include-sealed reads sealed logs anyway, for the experiment's owner, and
says so.

WHY THIS EXISTS. "Did you read all the logs end to end" gets answered "yes" on the strength of a grep.
The logs run to gigabytes; nothing reads that literally. What CAN be read end to end is the
user-authored subset, a fraction of a percent of the bytes and the only part that carries an
instruction. This extracts that subset and prints the denominator so "I read all of it" is a checkable
claim rather than a feeling.

THE THREE THINGS A NAIVE GREP MISSES, all of them load-bearing:

  1. FILE EDITS ARE ANSWERS. A user often answers by editing a file in the editor, not by typing a
     reply. Each one is its own record, `type=="attachment"` with an attachment of type
     "edited_text_file", not a user turn. A filter on `type=="user"` plus non-empty text drops them,
     and gives a confident and false "the user never said that".
  2. TOOL RESULTS WEAR THE USER ROLE. Every tool result is also `type=="user"`. They are the bulk
     of the bytes. Counting them inflates "turns read" many times over and buries the real turns.
  3. MID-TURN INTERJECTIONS. A message sent while a turn is running is not a user turn. It is
     stored as an `attachment` record of type `queued_command`, the same type as task notifications
     and messages from other sessions, and only `humanTurn` marks the ones a person typed. Those
     are frequently the corrections.

OUTPUT. `user-turns.md`, one block per turn, newest last, with session id and timestamp so a claim
can be traced back. Plus `INDEX.md` with the per-session counts - that is the denominator.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Which logs exist is cc_logs.py's job: every Claude Code log on this machine, on disk or inside a backup
# zip. Reading two fixed folders would miss every archived and backed-up log.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cc_logs  # noqa: E402
import seals as sealing  # noqa: E402  the .recall-sealed rule, shared with recall.py


def seal_dirs():
    """(folders, names to prune) where markers are found: recall.py's notes folders and its PRUNE set, so a
    seal recall honours holds here too."""
    import recall
    return recall.notes_dirs(), recall.PRUNE


# A user turn whose text is exactly one of these is a harness artefact, not something the user typed.
SYNTHETIC = re.compile(
    r"^\s*(\[Request interrupted[^\]]*\]|<command-name>|<local-command-stdout>|"
    r"Caveat: The messages below|<system-reminder>|API Error|"
    r"This session is being continued from a previous conversation)",
    re.I)


def blocks(msg):
    """Content is a bare string on older turns and a list of parts on newer ones."""
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return c if isinstance(c, list) else []


def turn_text(rec):
    """-> (kind, text) for a user-authored turn, or None.

    kind is 'typed', 'edit' or 'mid-turn'. Returning the kind matters: an 'edit' is an answer to a
    question that was asked in chat, so it only makes sense next to the turn before it."""

    # A message sent while a turn is running is its own record too: type=="attachment" with an
    # attachment of type "queued_command" and the words under `prompt`, a string or a list of parts.
    # Only humanTurn marks one a person typed. The rest are task notifications, messages from other
    # sessions, and a harness reminder that has origin.kind "human" but no humanTurn. One with humanTurn
    # is not also stored as a user turn, so skipping these loses the message.
    if rec.get("type") == "attachment" and (rec.get("attachment") or {}).get("type") == "queued_command":
        att = rec["attachment"]
        if att.get("humanTurn") is not True:
            return None
        p = att.get("prompt")
        parts = ([b.get("text") or "" for b in p if isinstance(b, dict) and b.get("type") == "text"]
                 if isinstance(p, list) else [p or ""])
        text = "\n".join(s for s in parts if s.strip()).strip()
        return None if not text or SYNTHETIC.match(text) else ("mid-turn", text)

    # An edited file is its OWN top-level record with type=="attachment" and the payload under
    # `attachment`. It is NOT an `attachments` array on the user turn - assuming it returns none of
    # them, a clean, confident, total miss of exactly the channel a user answers in. Calibrate a log
    # filter against a known hit before trusting it.
    if rec.get("type") == "attachment":
        att = rec.get("attachment") or {}
        if att.get("type") != "edited_text_file":
            return None
        path = att.get("filename") or att.get("path") or "?"
        snip = att.get("snippet") or att.get("content") or ""
        return ("edit", f"[EDITED {path}]\n{snip}")

    if rec.get("type") != "user":
        return None

    parts = []
    for b in blocks(rec.get("message")):
        if not isinstance(b, dict):
            continue
        # tool results carry the user role but are not authored by anyone
        if b.get("type") == "tool_result":
            return None
        if b.get("type") == "text":
            parts.append(b.get("text") or "")

    text = "\n".join(p for p in parts if p.strip()).strip()
    if not text or SYNTHETIC.match(text):
        return None
    return ("typed", text)


def selftest():
    """Plant two sessions, one in two copies and one in a zip, run main on them, and check what it
    writes and how it exits, including each gap that makes it exit 2."""
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("pass " if cond else "FAIL ") + name)

    u = ["%08x-0000-4000-8000-%012x" % (i, i) for i in (1, 2)]
    day, early = "2026-10-01T00:00:00Z", "2026-09-01T00:00:00Z"

    def rec(uid, ts=day, **k):
        return json.dumps({"uuid": uid, "timestamp": ts, **k}) + "\n"

    def said(uid, text, ts=day):
        return rec(uid, ts, type="user", message={"content": text})

    def queued(uid, prompt, **att):
        return rec(uid, type="attachment", attachment={"type": "queued_command", "commandMode": "prompt",
                                                       "prompt": prompt, **att})

    live = (said("t1", "alpha typed")
            + rec("t2", type="user", message={"content": [{"type": "text", "text": "beta typed"}]})
            + rec("x1", type="user", message={"content": [{"type": "tool_result", "content": "tool bytes"}]})
            + rec("x5", type="user", message={"content": [{"type": "tool_result", "content": "more tool bytes"},
                                                          {"type": "text", "text": "text beside a tool result"}]})
            + said("x2", "[Request interrupted by user]")
            + rec("x3", type="assistant", message={"content": "assistant words"})
            + rec("e1", type="attachment",
                  attachment={"type": "edited_text_file", "filename": "/n/answers.md", "snippet": "gamma edited"})
            + rec("x4", type="attachment", attachment={"type": "todo", "content": "todo words"})
            + queued("q1", "zeta sent mid-turn", humanTurn=True, origin={"kind": "human"})
            + queued("q2", [{"type": "image", "source": {}}, {"type": "text", "text": "eta with an image"}],
                     humanTurn=True, origin={"kind": "human"})
            + queued("x6", "task words", origin={"kind": "task-notification"})
            + queued("x7", "peer words", origin={"kind": "peer"})
            + queued("x8", "reminder words", origin={"kind": "human"})              # no humanTurn
            + queued("x9", "[Request interrupted by user]", humanTurn=True, origin={"kind": "human"})
            + "not json\n")
    resumed = said("t1", "alpha typed") + said("t3", "delta typed")     # overlaps the live copy by one turn

    with tempfile.TemporaryDirectory() as root:
        def w(rel, data):
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(data)

        w(f".claude/projects/S/{u[0]}.jsonl", live)
        w(f"Desktop/_archive/s/{u[0]}.jsonl", resumed)
        w(f".claude/projects/S/{u[0]}/subagents/agent-a.jsonl", said("s1", "subagent words"))
        os.makedirs(os.path.join(root, "backup"))
        with zipfile.ZipFile(os.path.join(root, "backup", ".claude.zip"), "w") as zf:
            zf.writestr(f".claude/projects/S/{u[1]}.jsonl", said("z1", "epsilon zipped", early))
        downloads = os.path.join(root, "Downloads")
        os.makedirs(downloads)
        cache = os.path.join(root, "cache.json")
        notes = os.path.join(root, "notes")          # where every run looks for .recall-sealed markers
        os.makedirs(notes)

        def inventory(refresh=False):
            """refresh: walk every folder, as a first run does; with the last run's folder index, a find is
            started only in a folder new since then (cc_logs._find)."""
            return cc_logs.Inventory(root, refresh=refresh, cache=cache)

        def run(inv, *args):
            """-> (exit code, printed lines, turns written, INDEX.md, files written)"""
            out = tempfile.mkdtemp(dir=root)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                try:
                    code = main(["--out", out, *args], inv=inv, seal_roots=([notes], ()))
                except SystemExit as e:
                    code = e.code
            names = sorted(os.listdir(out))

            def read(n):
                with open(os.path.join(out, n), encoding="utf-8") as fh:
                    return fh.read()
            text = "".join(read(n) for n in names if n.startswith("user-turns"))
            return code, buf.getvalue(), text, read("INDEX.md") if "INDEX.md" in names else "", names

        inv = inventory()
        code, log, text, index, _ = run(inv)
        check("each authored turn counts once across a session's two copies and a zipped session: 7, exit 0",
              "  7 user-authored turns total" in log and "  2 main session(s) in 3 distinct copies, 3 read" in log
              and code == 0)
        check("a turn held in two copies of a session is written once",
              text.count("alpha typed") == 1 and "delta typed" in text and "epsilon zipped" in text)
        check("a file edit is an authored turn, written as an edit",
              "[edit]\n[EDITED /n/answers.md]\ngamma edited" in text and "  1 of those are FILE EDITS" in log)
        check("a message a person sent mid-turn is authored, from a string or the text of a list, written as mid-turn",
              "[mid-turn]\nzeta sent mid-turn" in text and "[mid-turn]\neta with an image" in text
              and "  2 of those were sent MID-TURN" in log)
        check("tool results, harness lines, assistant text, other attachments, queued messages without humanTurn"
              " and subagent logs are not authored",
              not any(s in text for s in ("tool bytes", "text beside a tool result", "Request interrupted",
                                          "assistant words", "todo words", "task words", "peer words",
                                          "reminder words", "subagent words")))
        check("an unparseable line is counted as skipped", "  1 unparseable line(s) skipped" in log)
        check("INDEX gives each session's kept and total turns",
              f"| {u[0][:8]} | 6 | 6 |" in index and f"| {u[1][:8]} | 1 | 1 |" in index)
        code, log, text, _, _ = run(inv, "--since", "2026-09-15")
        check("--since drops an earlier turn from the output, not from the denominator",
              "  7 user-authored turns total" in log and "  6 kept after filters" in log and "epsilon" not in text)
        code, log, text, _, _ = run(inv, "--grep", "ALPHA")
        check("--grep keeps only matching turns, ignoring case", "  1 kept after filters" in log and "beta" not in text)
        code, log, text, _, _ = run(inv, "--session", u[1][:8])
        check("--session reads that session only",
              "  1 main session(s) in 1 distinct copies, 1 read" in log and "alpha" not in text)
        code, log, text, _, names = run(inv, "--chunk", "2")
        check("--chunk 2 writes the 7 turns as 4 files",
              [n for n in names if n.startswith("user-turns")] == [f"user-turns-0{i}.md" for i in (1, 2, 3, 4)]
              and text.count("\n### ") == 7)
        code, log, _, _, _ = run(inv, "--session", "ffffffff")
        check("a --session that matches no log exits 2", code == 2 and "no main session log found" in log)

        floor = "  ⚠ 1 zip(s) or folder(s) not read in full, named above, so the denominator is a floor"
        # A real zip cut short (no central directory), walked by its local headers. "PK not really" would
        # not do: it holds no zip signature, so cc_logs skips it as NOT A ZIP.
        cut = cc_logs.plant_cut_zip(os.path.join(downloads, "cut.zip"),
                                    [(".claude/projects/S/%08x-0000-4000-8000-%012x.jsonl" % (99, 99),
                                      said("c1", "only in the cut zip").encode()), ("media/v.mp4", b"m" * 500)])
        code, log, _, _, _ = run(inventory())
        os.remove(cut)
        check("a zip cut short, holding a log with no other copy, is named DID NOT READ and exits 2",
              code == 2 and "DID NOT READ " in log and "cut.zip" in log and floor in log)
        notzip = os.path.join(downloads, "broken.zip")
        w(notzip, "PK not really")
        code, log, _, _, _ = run(inventory())
        os.remove(notzip)
        check("a file named .zip that holds no zip signature is NOT A ZIP, SKIPPED, and leaves the exit at 0",
              code == 0 and "NOT A ZIP, SKIPPED" in log and "broken.zip" in log and floor not in log)
        with cc_logs.find_quits(downloads):
            code, log, _, _, _ = run(inventory(refresh=True))
        check("a folder find quits on, and again when rerun, is named NOT SEARCHED and exits 2",
              code == 2 and "/Downloads: NOT SEARCHED in full" in log and floor in log)
        locked = os.path.join(downloads, "locked")
        os.makedirs(locked)
        try:
            os.chmod(locked, 0)
            code, log, _, _, _ = run(inventory())
        finally:
            os.chmod(locked, 0o755)
        os.rmdir(locked)
        check("a folder that cannot be opened is named and exits 2",
              code == 2 and "/Downloads/locked: Permission denied" in log and floor in log)
        real = cc_logs.iter_lines

        def fails_in_zip(c):
            for n, line in real(c):
                if c.via != "disk":
                    raise zipfile.BadZipFile("planted")
                yield n, line
        cc_logs.iter_lines = fails_in_zip
        try:
            code, log, text, _, _ = run(inv)
        finally:
            cc_logs.iter_lines = real
        check("a copy that fails partway is named DID NOT READ and exits 2",
              code == 2 and "  ⚠ DID NOT READ " in log and "  6 user-authored turns total" in log
              and floor not in log)

        # This reads the same logs recall does. A session a .recall-sealed marker names is not read
        # (seals.py, the rule recall.py uses).
        def marker(name, until, pats):
            w(f"notes/{name}/{sealing.SEAL}", json.dumps({"reason": f"planted {name}", "owner": "selftest",
                                                          "until": until, "logs": pats}))
        k, old = ["%08x-0000-4000-8000-%012x" % (i, i) for i in (3, 4)]
        marker("exp-sealed", "2999-12-31", [k])
        marker("exp-old", "2020-01-01", [old])
        w(f".claude/projects/S/{k}.jsonl", said("k1", "kappa sealed turn"))
        w(f".claude/projects/S/{old}.jsonl", said("l1", "lambda expired turn"))
        with zipfile.ZipFile(os.path.join(root, "backup", "sealed.zip"), "w") as zf:  # a turn more: read too
            zf.writestr(f".claude/projects/S/{k}.jsonl", said("k1", "kappa sealed turn")
                        + said("k2", "kappa only in the backup"))
        at = {n: os.path.join(notes, n).replace(sealing.HOME, "~") for n in ("exp-sealed", "exp-old")}
        inv = inventory()
        code, log, text, _, _ = run(inv)
        check("a session a seal's logs pattern names is not read, on disk or in a backup zip, and the output "
              "names the seal with its counts; exit 0",
              "kappa" not in text and code == 0
              and f'  seal: {at["exp-sealed"]} until 2999-12-31 ("planted exp-sealed", owner "selftest"): not '
                  f'searched: 2 log copies of 1 log; --include-sealed searches it' in log
              and "  3 main session(s) in 4 distinct copies, 4 read; 2 sealed copies not read" in log)
        check("an unsealed session is still read: the 7 turns as before, and the expired seal's one",
              "alpha typed" in text and "epsilon zipped" in text and "  8 user-authored turns total" in log)
        check("an expired seal is ignored: its session is read, and the output says the seal expired",
              "lambda expired turn" in text and "  seal EXPIRED on 2020-01-01, ignored, so its folder and logs "
                                                f"were searched as usual: {at['exp-old']}" in log)
        code, log, text, _, _ = run(inv, "--include-sealed")
        check("--include-sealed reads both copies of the sealed session, and says the seal was lifted",
              "kappa sealed turn" in text and "kappa only in the backup" in text and code == 0
              and f"  seal LIFTED by --include-sealed: {at['exp-sealed']} until 2999-12-31" in log
              and "searched anyway: 2 log copies of 1 log" in log and "  10 user-authored turns total" in log)
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


def main(argv=None, inv=None, seal_roots=None):
    """seal_roots: (folders, names to prune) to find markers in; None is recall.py's notes folders."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out")
    ap.add_argument("--grep", default=None, help="only keep turns matching this regex")
    ap.add_argument("--session", default=None)
    ap.add_argument("--since", default=None, help="YYYY-MM-DD")
    ap.add_argument("--chunk", type=int, default=0,
                    help="split the output into N-turn files for parallel reading")
    ap.add_argument("--include-sealed", action="store_true",
                    help="also read the logs a .recall-sealed marker holds back (for the experiment's owner); "
                         "the output says it was used")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.out:
        ap.error("--out is required")

    os.makedirs(a.out, exist_ok=True)
    pat = re.compile(a.grep, re.I) if a.grep else None
    since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc) if a.since else None

    # Every main session log cc_logs finds, on disk or in a zip, one copy per distinct content. A
    # resumed session's copies overlap, so a turn counts once by its uuid.
    inv = inv if inv is not None else cc_logs.Inventory(cc_logs.default_root())
    copies = sorted((c for c in inv.to_read() if c.kind == "main"), key=lambda c: (cc_logs.PREFER[c.via], c.path))
    if a.session:
        copies = [c for c in copies if c.key.startswith(a.session)]
    if not copies:
        ap.error("no main session log found" + (f" for session {a.session}" if a.session else ""))
    # Before any log is read: a log a .recall-sealed marker names is held back, every copy of it (seals.py).
    try:
        dirs, prune = seal_roots if seal_roots is not None else seal_dirs()
        seals = sealing.find(dirs, prune, lift=a.include_sealed)
    except Exception as e:
        print("  the seal check crashed, so no log was read: "
              + cc_logs.redact(f"{type(e).__name__}: {e}").splitlines()[0][:200])
        return 2
    copies, held, nomatch = sealing.hold_logs(inv, copies, seals)
    lifted = {s["dir"] for s in seals if s["state"] == "lifted"}
    kept_out = sum(h.get("log copies", 0) for d, h in held.items() if d not in lifted)

    rows, per_session, seen, failed, malformed = [], {}, set(), [], 0
    for c in copies:
        sid = c.key[:36]
        kept, total = per_session.get(sid, (0, 0))
        try:
            for _, line in cc_logs.iter_lines(c):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    malformed += 1
                    continue
                got = turn_text(rec)
                if not got:
                    continue
                uid = rec.get("uuid") or hashlib.sha1(line).hexdigest()
                if uid in seen:
                    continue
                seen.add(uid)
                total += 1
                kind, text = got
                ts = rec.get("timestamp") or ""
                if since and ts:
                    try:
                        if datetime.fromisoformat(ts.replace("Z", "+00:00")) < since:
                            continue
                    except Exception:
                        pass
                if pat and not pat.search(text):
                    continue
                kept += 1
                rows.append((ts, sid[:8], kind, text))
        except Exception as e:
            failed.append(f"{c.label()}: {type(e).__name__}")
        per_session[sid] = (kept, total)

    rows.sort(key=lambda r: r[0])

    def write(path, chunk):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for ts, sid, kind, text in chunk:
                fh.write(f"\n### {ts}  session {sid}  [{kind}]\n{text}\n")

    if a.chunk:
        n = 0
        for i in range(0, len(rows), a.chunk):
            n += 1
            write(os.path.join(a.out, f"user-turns-{n:02d}.md"), rows[i:i + a.chunk])
        print(f"  wrote {n} chunk file(s) of up to {a.chunk} turns")
    else:
        write(os.path.join(a.out, "user-turns.md"), rows)

    with open(os.path.join(a.out, "INDEX.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("| session | kept | authored turns in file |\n|---|---:|---:|\n")
        for fn, (k, t) in sorted(per_session.items(), key=lambda kv: -kv[1][1]):
            fh.write(f"| {fn[:8]} | {k} | {t} |\n")

    tot = sum(t for _, t in per_session.values())
    for ln in inv.coverage():
        print("  " + ln)
    for s in seals:                       # a seal never reads as a clean zero
        print("  " + sealing.line(s, [sealing.logs_part(held.get(s["dir"], {}))], nomatch.get(s["dir"])))
    print(f"  {len(per_session)} main session(s) in {len(copies)} distinct copies, "
          f"{len(copies) - len(failed)} read"
          + (f"; {kept_out} sealed cop{'y' if kept_out == 1 else 'ies'} not read (the seal line above)"
             if kept_out else ""))
    for f in failed:
        print(f"  ⚠ DID NOT READ {f}")
    print(f"  {tot} user-authored turns total, each counted once  <- this is the denominator")
    print(f"  {len(rows)} kept after filters")
    print(f"  {sum(1 for r in rows if r[2] == 'edit')} of those are FILE EDITS, not typed messages")
    print(f"  {sum(1 for r in rows if r[2] == 'mid-turn')} of those were sent MID-TURN, while a turn was running")
    if malformed:
        print(f"  {malformed} unparseable line(s) skipped")
    # A zip it could not read, or a folder find did not search or could not open, may hold a log,
    # so the denominator above is a floor and the exit is 2, as in cc_logs.py and recall.py.
    gaps = len(inv.did_not_read) + len(inv.not_searched) + len(inv.not_opened)
    if gaps:
        print(f"  ⚠ {gaps} zip(s) or folder(s) not read in full, named above, so the denominator is a floor")
    return 2 if failed or gaps else 0


if __name__ == "__main__":
    sys.exit(main())
