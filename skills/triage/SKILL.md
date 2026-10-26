---
name: triage
description: "Works the open questions down one at a time, taking the user's decision on each and recording it: the entries in the project's open-question ledger, and the questions the user put off on a question card that the request ledger keeps open. The only skill that closes one. Use for 'triage', 'resolve open questions', 'what's still open', 'go through the open questions'."
---

# triage: work the open questions down, one item at a time

Triage is done when every open question has been seen in full, the user's answer is recorded in the
file where the behaviour lives, and the item is closed, noted or left open on purpose. Given an id
(`/triage Q-12`), work only that item.

Two lists are in scope. The open entries in the open-question ledger that the `open-question` skill
writes: `.claude/open-questions.md` at the project root, unless the project's `CLAUDE.md` names
another path. And, if you keep the request ledger, every question the user put off on a question
card: `python "${CLAUDE_PLUGIN_ROOT}/tools/ledger.py" --questions` lists them under their ids; name
the id on the card where you take the decision, so the ledger closes it. Work items in an issue
tracker belong to the `ado` skill.

- **One item at a time. Never batch.** The value is that each decision is actually seen.
- Record a resolution only when the user gives one; silence is not a decision, and "seems done" is
  not either.
- Work only what the lists hold, and create nothing mid-triage: note what surfaces and offer
  `open-question` after the current item closes.
- Close, don't delete, a question that was answered: the closed entry is the record. Remove only one
  the user calls moot (step 4).
- Re-measure before accepting "that's already done": items carried forward as open are routinely
  already fixed, so check the live surface, not the item's text.
- If the list passes about 15 open items, say so: a large open list means triage isn't happening.

## Steps

**1 — Drafts waiting to go out**, if you keep them: the drafts file of Figma comments
(`figma.drafts` in the config) and any file of messages waiting to be sent. An item marked held or
waiting is reported as held, never asked. Comment drafts are not walked one at a time; they wait in
the drafts file until the user says to post them. Never hand the user text to paste in the Figma UI:
the posting tools make every edit themselves. A message is walked in chat one at a time, as in steps
3 to 5. What a post, a Drop, a Rewrite or a Sent does to the files is in
`references/review-decisions.md`.

**2 — Read the list fresh.** Never from memory. Read the ledger and list every entry whose `Status`
is `open`, and run `ledger.py --questions` if you keep the request ledger. A file that is missing or
unreadable, or a non-zero exit, means the list did not run: say so, never "no open questions". If
nothing is open, say so and stop. List them terse first, one line each (id, question, date filed),
then name the one you start with.

**3 — Present ONE item in full**, with what the `recall` skill finds on its terms `--since` the date
it was filed (it may have been answered in a chat, a call or a thread since). Run that search for
every item, before you present it, even one that looks new or was searched in an earlier session.
The presentation quotes recall's `SUMMARY` line and names any source it skipped or could not run. An
item shown without them has not been looked up: run the search, then show it again. Show a ledger
entry with every field, read from the file. Then offer:

> **Resolve**: give the answer; I record it in the ledger and in the file where the behaviour lives
> **Defer**: stays open, with a dated note on what is blocking
> **Skip**: no change, move on
> **Close, won't fix**: closed with a reason it will not be addressed
> **Moot**: nobody needs the answer; I remove the entry

**4 — Apply.** The answer goes in the file where the behaviour lives: the project's rulings file for
a site rule, a skill or `CLAUDE.md` for workflow, the place your setup keeps durable facts for a
fact, a repo file for code. "Leave it" on a QA finding is not a ruling: find what raised it (a gate,
a checklist line, a skill, a QA prompt) and remove it there. Write a rulings entry for it only when
the build deliberately differs from the design, so a gate would flag it on every run.

Confirm each such change separately. Then update the ledger entry:

- **Resolve** or **Close, won't fix:** set `Status: resolved` or `Status: won't fix`, and add
  `- Decision: <the answer, in the user's words where they gave them>` and
  `- Closed: <YYYY-MM-DD>`, plus `- Applied in: <file>` when a file changed.
- **Defer:** add `- Deferred <YYYY-MM-DD>: <what is blocking>`; the status stays `open`.
- **Moot:** after a yes, remove the entry. If the ledger is tracked in git, the history keeps it.

Read the entry back after each write. A question from the request ledger has no entry to edit: the
card that names its id is the record.

**5 — Next item.** Ask *"Ready for the next one?"* before each item. "Keep going" drops only that
question: present the next item in full (step 3) and wait for its decision. Never decide, apply or
skip an item the user has not seen.

**6 — Summary:** resolved / deferred / skipped / closed / removed as moot / still open.
