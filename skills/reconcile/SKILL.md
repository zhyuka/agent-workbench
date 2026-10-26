---
name: reconcile
description: "Bring a workspace back to a clean, current baseline. Default is a catch-up on what changed since the last pass (new meeting transcripts, the trackers you use, session logs, workspace drift); a full pass over every surface when asked or after 30+ days. Use for '/reconcile', 'get organized', 'catch us up', 'reconcile the workspace', 'what needs cleaning/updating/fixing/consolidating'. Reports at most 12 verified findings and executes only what the user approves."
---

# Reconcile: workspace catch-up

A pass is done when transcripts, the trackers, the session logs and the working folder are back on
one current baseline, every live file sits in its home, and the user has approved, in one question,
everything the pass will change. This file only sequences the tools. Each tool owns its own
definition of clean (table at the end); when this file and a tool disagree, the tool wins, and you
quote what a tool prints rather than paraphrase a count. The tools below are in the plugin's
`tools/` folder; run each as `python "${CLAUDE_PLUGIN_ROOT}/tools/<name>.py"`.

## 0. Preflight, before any agent

```
python "${CLAUDE_PLUGIN_ROOT}/tools/recall.py" --last /reconcile   # last pass, days since, logs written since
```

- Print the last-pass date, the gap and the mode in one line. The date comes from that command:
  `_archive/*-reconcile/` misses any pass that archived nothing, and the workspace map holds no
  history.
- **Mode.** Catch-up (§1) by default. Full (§1 + §2) when the args ask for every surface or the gap
  is 30 days or more.
- Check for other sessions running in the same workspace. A peer session's finished work is not a
  finding.
- A surface behind a sign-in (a chat app, say): start one signed-in browser for the pass with
  `signed_in_browser.py serve <url>` in the background. It prints `SERVING <address>` once single
  sign-on finishes on its own; read it with `chromium.connect_over_cdp("<address>")`, never
  `browser.close()`, and run `signed_in_browser.py stop` when the pass is done. If it waits for a
  sign-in, ask once: finish it, or skip. A skipped surface is reported as "did not run", never
  skipped silently.
- Args often add a second job (a review thread, a list to send, a feedback batch). It runs after
  the report, as its own item in the menu, not folded into the pass.

## 1. Catch-up, the default

In this order. Each step answers "what changed since the last pass", not "audit everything".

1. **Transcripts**, if you keep them. `transcript_check.py audit`. Clean each raw capture that has
   no cleaned `.md` yet, with the tool you clean them with; cleaning is local and additive, so it
   runs during the pass. Then read the summary, decisions and action items in every `.md` newer than
   the last pass; each decision or ask is a candidate. The decisions list is the cleaning step's
   summary and can turn a proposal into a decision: before calling anything decided, quote the
   cleaned transcript or the raw captions and say who proposed it.
2. **Trackers, in the main thread.** For Azure DevOps, `ado_rest.py pull`, then the `ado` skill's
   reconcile sweep, which owns the checks. `ado_rest.py show <id>` for each item a transcript or log
   names, and for each comment awaiting the user's reply. Read the tip of the branch you ship to with
   `git ls-remote`; a teammate's push never shows in the local tree. Tracker work never goes to an
   agent: those agents stall.
3. **Session logs since the last pass.** The list `--last` printed. Per log: what shipped (commits,
   posts, closes) and what it left open. `recall.py <term> --since <date>`; never read a log whole.
4. **Workspace.** If `scheduled_audits.py` runs on a schedule, read the `today.md` it writes beside
   its log first: each red audit with its failing lines, and what `upkeep.py cleanup` removed and
   kept. If it is from today, re-run only the checks it shows red; if older, or there is none, run
   `workspace_audit.py` with each check `scheduled_audits.py` lists in `WORKSPACE_CHECKS`: every
   check but `secrets`, which runs by hand only. Quote the N of M each prints. Open the files it
   flags, and spot-read a few it does not.

## 2. Full pass, added on top of catch-up

- **Local dirs.** Every top-level folder and loose root file. Hash-verify duplicates. Split
  regenerable bulk from source by subdir size. For a git clone: remote, branch, last commit.
- **Older sessions.** Grep past the last pass for promises never finished. This sweep cannot see
  "already handled", so every hit goes through §4.
- **Parked surfaces, as tripwires.** One cheap call each for anything changed since the last pass;
  audit further only if it changed. Catch-up runs a tripwire only when the args or a new transcript
  name that surface.

## 3. Agents

Only for read-only sweeps that would flood the main context. Every brief uses absolute paths and full
tool paths with no `cd`, carries no findings from this session (the agent rediscovers from the
source), has a 20-minute budget after which it hands back what it has, and asks for 1.5 KB or less:
each claim with its evidence (command and output, or file:line), anything skipped marked "did not
run". An agent never touches a tracker, design-tool comments, a chat or a push, and its claim that
drives an edit or a delete is checked against the source first.

## 4. Verify, before anything is a finding

- **Prior art.** The `recall` skill, one concept per argument, for every item about to be called
  OPEN, NEW, STALE, OWED or UNRECONCILED. Exit 0: read the nearest hit and decide. Exit 2: a source
  did not run, so its silence clears nothing. An item that skipped this is a guess.
- **The live surface, never a local absence.** "Not pushed" needs `git ls-remote`; sessions push from
  scratch clones and delete them. Before drafting a reply, read the item's current state
  (`ado_rest.py show <id>`, `figma_comments.py inbox|nodes`).
- **Draft from the source that recorded the ask.** Open the transcript, comment or thread and quote
  it. A peer session's summary is secondhand.
- **Transcript terms.** Present or absent is `transcript_check.py term <TERM>`, never a grep.

## 5. Record

Per item: status (CURRENT / SUPERSEDED / DUPLICATE / STALE / REFERENCE) and disposition (KEEP /
COMBINE / RENAME / UPDATE / DELETE / ARCHIVE), each traced to the decision it came from.

## 6. Bucket

- **A. Archive reversibly.** `archive.py move <paths> --name reconcile --why "<group, origin, why,
  canonical now>"` makes `_archive/YYYY-MM-DD-reconcile/` and its `MANIFEST.md`, which names each
  path's origin; moving it back restores it.
- **B. Delete.** Regenerable cruft (`node_modules`, `.venv`, `__pycache__`, `dist`, `*.whl`, scratch
  images) and hash-verified exact duplicates. Nothing else is hard-deleted.
- **C. Team items.** Flagged to the owner, not executed.
- **D. Loose ends.** A decision only the user can make goes to the `open-question` skill. Never
  guessed, and never added here as a warning.

## 7. Structure and the map

The root holds folders plus the few files the workspace's `CLAUDE.md` names, and every keeper sits
in the home the workspace map gives it. Organize inside folders too: a `scripts/` subfolder, not a
flat dump. Check what is absent: `workspace_audit.py map` finds a live folder with no line in the
map, and `workspace_audit.py archive` finds an `_archive/*` entry with no `MANIFEST.md`. Propose
creations; a stub with a TODO beats invented content. When the disk and the map disagree, the disk
wins: fix the line. The map holds no history and no counts. Keep one canonical artifact per
concept, and delete intermediates the same turn.

## 8. Propagate

Fix the source of truth first, then the copies downstream (tracker, board). A move breaks path
references: grep the old path and fix each hit. Edit only your own scope: on a shared board, your
own section only; in a repo, your own files.

## 9. Report, then execute

- One report: at most 12 findings and about 600 words. Lead with what changed since the last pass.
  One line per finding (what, evidence, disposition), ranked by consequence; the rest as a count by
  category. Durable detail goes in the archive `MANIFEST.md`, not the reply.
- Then one AskUserQuestion, recommended option first, naming every outward action in it (a tracker
  comment, a design comment, a message). A multi-file edit shows its file list and sample lines.
- Report and park: nothing new starts mid-pass. What the pass cannot settle is filed (§6 D).
- Execute only the approved list. A pull, a rebase or a peer's push voids it; re-derive it. Before a
  delete or a move on macOS, read `references/macos-execution.md`.

## Tools own the definitions

| surface | owner |
|---|---|
| last pass, prior art | `recall.py` |
| transcripts | `transcript_check.py` |
| whole workspace | `workspace_audit.py` |
| `~/.claude` config: allow rules, scratch folders | `workspace_audit.py config` |
| Azure DevOps board | the `ado` skill's reconcile sweep |
| Figma threads | `figma_comments.py` |

When a check changes, edit the tool and sync this table.
