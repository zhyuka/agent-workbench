---
name: open-question
description: "File a question that needs the user's decision into the project's open-question ledger instead of guessing. Use when a search of the record finds no answer and the call is the user's: a fact only they hold, an outward or irreversible step, or two sources that disagree. Also for 'park this', 'flag this for me', 'add an open question'. The triage skill works the same ledger."
---

# open-question: park a decision, don't guess it

Files one question into the open-question ledger, then **returns you to the task you were in**.
One question, one entry, hand back. A guess or a private note is a decision the user never sees;
an entry in the ledger is one they work through with `triage`.

## The ledger

`.claude/open-questions.md` at the project root, unless the project's `CLAUDE.md` names another
path. Create it with a `# Open questions` heading if it is missing. One entry per question:

```
## Q-<n>: <the question, 5-12 words, ending in ?>
- Status: open
- Filed: <YYYY-MM-DD>
- Surfaced: <the task or file it came from>
- Why it matters: <one line>
- Depends on: <the role who can answer>
- Affects: <what changes with the answer>
- Evidence: <both sides of a conflict, quoted with file:line>
- Picture: <relative path, only for a visual question>
```

`<n>` is one more than the highest id in the file, open or closed. Ids are never reused.

## How to invoke

```
/open-question
/open-question <short description>
```

## Steps

**1 — Gather.** From context where possible; ask only for what is genuinely missing: the question,
the conflict, who can resolve it, what it affects, how it surfaced. Run the `recall` skill on the
question's terms first: if the user already answered it, cite that and apply it instead of filing.
For a question about one page or component, keep that first search as it is, then run one more,
separate search on the thing's name and `redesign|new design`: adding the name to the first search
narrows it and drops the items that answer the question without naming it. If the design tool has
an open comment thread on the node in question where the user already asked design to change it,
the call is made; don't file it.

**2 — Search the ledger.** If an open entry already asks this, add the new evidence to that entry
instead of filing a second one. If a closed entry answered it, apply that answer.

**3 — Write the entry.** The ledger is local and reversible, so write it without asking, then print
it in full. Write it plainly (the `humanize` skill) and attribute to roles, not people.

**Visual questions get a picture.** Save a PNG next to the ledger in `.claude/open-questions/`
named `Q-<n>-<what>-<width>.png`, with the build commit and capture date in the entry. **Look at the
PNG before linking it**: scroll-reveal pages capture blank. A question that is not visual gets no
picture.

**4 — Confirm** "Filed as Q-<n>." and resume the prior task.

## Rules

- **Never close a question here.** That is `triage`.
- **Quote both sides of a conflict with file:line.** "Two docs disagree" is not a question.
- **If the answer is knowable by running a command, run the command instead.** Do not park a
  measurable fact as a question.
- **One question per entry.** Several questions are several entries, each with its own id.
- **Filing is not asking.** When the user is in the session and the answer blocks the work, ask
  them with AskUserQuestion as well; the entry is the record.
