---
name: reconcile
description: "Bring a workspace back to a clean, current baseline. Default is a catch-up on what changed since the last pass (new meeting notes, the trackers you use, session logs, workspace drift); a full pass over every surface when asked or after 30+ days. Use for '/reconcile', 'get organized', 'catch us up', 'reconcile the workspace', 'what needs cleaning/updating/fixing/consolidating'. Reports at most 12 verified findings and executes only what the user approves."
---

# Reconcile: workspace catch-up

Brings notes, trackers, session logs, memory and the working folders back to one current baseline,
and leaves every live file in its home. This file only sequences the work. When a tool owns a
check, its definition of clean wins over this file; quote what the tool prints and never
paraphrase a count.

## 0. Preflight, before any agent

```
python "${CLAUDE_PLUGIN_ROOT}/tools/recall.py" --last /reconcile   # last pass, days since, logs since
```

- Print the last-pass date, the gap and the mode in one line. The date comes from that command,
  not from the archive folder names: a pass that archived nothing leaves no folder.
- **Mode.** Catch-up (§1) by default. Full (§1 + §2) when the args ask for every surface or the gap
  is 30 days or more.
- Check for other sessions running in the same workspace. A peer session's finished work is not a
  finding.
- A surface that needs a sign-in (a chat app, a tracker): if it can't be read, ask once to sign in
  or skip. Never skip it silently; a skipped surface is reported as "did not run".
- Args often add a second job (a review thread, a list to send, a feedback batch). It runs after
  the report, as its own item in the menu, not folded into the pass.

## 1. Catch-up, the default

In this order. Each step answers "what changed since the last pass", not "audit everything".

1. **Meeting notes and transcripts.** Clean any raw capture that has no cleaned copy yet, if you
   keep them; cleaning is local and additive, so it runs during the pass. Then read the summary,
   decisions and action items of every note newer than the last pass; each decision or ask is a
   candidate.
2. **Trackers, in the main thread.** Pull the live state of the trackers the workspace uses (issue
   tracker, review threads). Read each item a note or log names, and each comment awaiting the
   user's reply. Read remote branch tips with `git ls-remote`; a teammate's push never shows in the
   local tree. Tracker writes never go to an agent.
3. **Session logs since the last pass.** The list `--last` printed. Per log: what shipped (commits,
   posts, closes) and what it left open. `recall.py <term> --since <date>`; never read a log whole.
4. **Workspace.** Run the workspace's own audits, if it has any (a daily digest, a lint, a memory
   index check), and quote the N of M each prints. If a digest from today exists, re-run only the
   checks it shows red. Open the memory cards a check flags, and spot-read a few it does not.

## 2. Full pass, added on top of catch-up

- **Local dirs.** Every top-level folder and loose root file. Hash-verify duplicates. Split
  regenerable bulk from source by subdir size. For a git clone: remote, branch, last commit.
- **Older sessions.** Grep past the last pass for promises never finished. This sweep cannot see
  "already handled", so every hit goes through §4.
- **Parked surfaces, as tripwires.** One cheap call each for anything changed since the last pass;
  audit further only if it changed. Catch-up runs a tripwire only when the args or a new note name
  that surface.

## 3. Agents

Only for read-only sweeps that would flood the main context. Every brief:
- uses absolute paths and full tool paths, and no `cd`;
- carries no findings from this session; the agent rediscovers from the source;
- has a 20-minute budget, and at the limit hands back what it has;
- hands back 1.5 KB or less: each claim with its evidence (command and output, or file:line), and
  anything skipped marked "did not run";
- never writes to a tracker, a design tool, a chat, or a remote.

An agent's claim that drives an edit or a delete is checked against the source first.

## 4. Verify, before anything is a finding

- **Prior art.** The `recall` skill, one concept per argument, for every item about to be called
  OPEN, NEW, STALE, OWED or UNRECONCILED. Exit 0: read the nearest hit and decide. Exit 2: a source
  did not run, so its silence clears nothing. An item that skipped this is a guess.
- **The live surface, never a local absence.** "Not pushed" needs `git ls-remote`; sessions push
  from scratch clones and delete them. Before drafting a reply, read the item's current state in its
  tracker. A zero from a query is proven against a case whose answer is already known.
- **Draft from the source that recorded the ask.** Open the note, comment or thread and quote it.
  A peer session's summary is secondhand.

## 5. Record

Per item: status (CURRENT / SUPERSEDED / DUPLICATE / STALE / REFERENCE) and disposition (KEEP /
COMBINE / RENAME / UPDATE / DELETE / ARCHIVE), each traced to the decision it came from.

## 6. Bucket

- **A. Archive reversibly.** Move into `_archive/YYYY-MM-DD-reconcile/` with a `MANIFEST.md` that
  names each path's origin, its group and why it moved, and what is canonical now; moving it back
  restores it.
- **B. Delete.** Regenerable cruft (`node_modules`, `.venv`, `__pycache__`, `dist`, `*.whl`, scratch
  images) and hash-verified exact duplicates. Nothing else.
- **C. Team items.** Flagged to the owner, not executed.
- **D. Loose ends.** A decision only the user can make goes to the `open-question` skill. Never
  guessed, and never added here as a warning.

## 7. Structure and the map

The root holds folders plus the few files the workspace's `CLAUDE.md` names, and every keeper sits
in the home the workspace map gives it (if the workspace keeps one). Organize inside folders too: a
`scripts/` subfolder, not a flat dump. Check what is absent: a live folder with no line in the map,
an archive folder with no `MANIFEST.md`. Propose creations; a stub with a TODO beats invented
content. When the disk and the map disagree, the disk wins: fix the line. The map holds no history
and no counts.

## 8. Propagate

Fix the source of truth first, then the copies downstream (memory, tracker, board). A move breaks
path references: grep the old path and fix each hit.

## 9. Report, then execute

- One report: at most 12 findings and about 600 words. Lead with what changed since the last pass.
  One line per finding (what, evidence, disposition), ranked by consequence; the rest as a count by
  category. Durable detail goes in the archive `MANIFEST.md`, not the reply.
- Then one AskUserQuestion, recommended option first, naming every outward action in it (a tracker
  comment, a design comment, a message). A multi-file edit shows its file list and sample lines.
- Report and park: nothing new starts mid-pass. What the pass cannot settle is filed (§6 D).
- Execute only the approved list. A pull, a rebase or a peer's push voids it; re-derive it.

## Filesystem notes

- Nothing guards a delete, and `rm` has no undo. Print the resolved path, look at it, then delete.
- `mv` within one volume is a rename: atomic, no copy. Across volumes it copies then deletes, so
  check the destination is complete and the source is gone. In a cloud-synced folder a file may be
  a cloud-only placeholder, and reading it forces a download.
- On a case-insensitive filesystem (macOS default, Windows) a case-only rename is easy to get wrong,
  and `mkdir -p` of a case variant silently reuses the existing folder.
- A file that will not delete on macOS is usually flagged locked: `ls -lO` shows `uchg`; clear it
  with `chflags -R nouchg <path>`.
- You can delete a running server's files and the process keeps going. Stop it first:
  `pgrep -fl <folder>` or `lsof +D <folder>` finds it.
- Before restoring a folder that vanished, check it was not disposable scrap.

## Invariants

- Edit only your own scope: in a shared board, your own section; in a repo, your own files.
- Archive reversibly; hard-delete only bucket B.
- One canonical artifact per concept; intermediates deleted the same turn.
- Calibrate before believing a zero: a check is trusted only after it has caught a known bad case
  and passed a known good one.
