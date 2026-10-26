---
name: triage
description: "Works the open questions in the project's open-question ledger down one at a time, taking the user's decision on each and recording it. The only skill that closes one. Use for 'triage', 'resolve open questions', 'what's still open', 'go through the open questions'."
---

# triage: work the open questions down, one item at a time

Presents each open question in full, takes the decision, records it. **Never batches.**

**Scope:** the open entries in the open-question ledger that the `open-question` skill writes:
`.claude/open-questions.md` at the project root, unless the project's `CLAUDE.md` names another
path. Work items in an issue tracker are not in scope.

## How to invoke

```
/triage                 # every open question
/triage Q-12            # one item only
```

## Steps

**1 — Read the list fresh.** Never from memory. Read the ledger and list every entry whose
`Status` is `open`. If the file is missing or unreadable, say the list did not run, never "no open
questions". If nothing is open, say so and stop. List them terse first:

```
## Open questions: 4 pending
- Q-12: Who owns the plugin marketplaces? (filed 2026-01-05)
- Q-15: Does the footer keep the coming-soon line? (filed 2026-01-09)
...
Starting with Q-12.
```

**2 — Present ONE item in full**, with what the `recall` skill finds on its terms `--since` the
date it was filed (it may have been answered in a chat, a call, a commit or a thread since). Run
that search for every item, before you present it, even one that looks new or was searched in an
earlier session. The presentation quotes recall's `SUMMARY` line and names any source it skipped or
could not run. An item shown without them has not been looked up: run the search, then show it
again. Then offer:

> **Resolve**: give the answer; I record it in the ledger and in the file where the behaviour lives
> **Defer**: stays open, with a dated note on what is blocking
> **Skip**: no change, move on
> **Close, won't fix**: closed with a reason it will not be addressed
> **Moot**: nobody needs the answer; I remove the entry

**3 — Apply.** The answer goes in the file where the behaviour lives: the project rulings file for a
deliberate difference from the design, a skill or `CLAUDE.md` for workflow, a memory card for a
durable fact, a repo file for code. "Leave it" on a QA finding is not a ruling: find what raised it
(a gate, a checklist line, a skill, a QA prompt) and remove it there. Write a rulings entry for it
only when the build deliberately differs from the design, so a gate would flag it on every run.
**Confirm each such change separately.** Then update the ledger entry:

- **Resolve** or **Close, won't fix:** set `Status: resolved` or `Status: won't fix`, and add
  `- Decision: <the answer, in the user's words where they gave them>` and
  `- Closed: <YYYY-MM-DD>`, plus `- Applied in: <file>` when a file changed.
- **Defer:** add `- Deferred <YYYY-MM-DD>: <what is blocking>`; the status stays `open`.
- **Moot:** after a yes, remove the entry. If the ledger is tracked in git, the history keeps it.

Read the entry back after each write.

**4 — Next item.** Ask *"Ready for the next one?"* before each item. "Keep going" drops only that
question: present the next item in full (step 2) and wait for its decision. Never decide, apply or
skip an item the user has not seen.

**5 — Summary:** resolved / deferred / skipped / closed / removed as moot / still open.

## Rules

- **One item at a time. Never batch.** The value is that each decision is actually seen.
- **Every item gets its search first** (step 2), with its `SUMMARY` line shown. Skipping it is how
  an item already answered in a comment or a commit gets decided twice.
- **Never mark something resolved without the user providing the resolution.** Silence is not a
  decision and "seems done" is not either.
- **Never invent items.** Only what is open in the ledger.
- **Do not create new items mid-triage.** Note anything that surfaces and offer `open-question`
  *after* the current item closes.
- **Close, don't delete,** a question that was answered: the closed entry is the record. Remove only
  one the user calls moot (step 3).
- **Re-measure before accepting a "that's already done".** Items carried forward as open are
  routinely already fixed; check the live surface, not the item's text.
- If the list passes ~15 open items, say so. A large open list means triage is not happening; that
  is the signal, not the noise.
