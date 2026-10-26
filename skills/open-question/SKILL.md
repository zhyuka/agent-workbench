---
name: open-question
description: "File a question that needs the user's decision into the project's open-question ledger instead of guessing. Use when a search of the record finds no answer and the call is the user's: a fact only they hold, an outward or irreversible step, or two sources that disagree on one. Also for 'park this', 'flag this for me', 'add an open question'. The triage skill works the same ledger."
---

# open-question: park a decision, don't guess it

Files one question into the open-question ledger, then returns you to the task you were in. One
question, one entry, hand back. A guess or a private note is a decision the user never sees; an
entry in the ledger is one they work through with `triage`.

The ledger is `.claude/open-questions.md` at the project root, unless the project's `CLAUDE.md`
names another path. Create it with a `# Open questions` heading if it is missing. One entry per
question:

```
## Q-<n>: <the question, 5-8 words, ending in ?>
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

## Steps

**1 — Gather.** From context where possible; ask only for what is genuinely missing: the question,
the conflict, who can resolve it, what it affects, how it surfaced. Run the `recall` skill on the
question's terms first: if the user already answered it, cite that and apply it instead of filing.
For a question about a page, keep that first search as it is, then run one more, separate search
with two concepts: the page's name, and `redesign|new design`. Adding the page's name to the first
search narrows it and drops the items that answer the question without naming the page. Read the
page's open design items in your tracker: while one is active this sprint, the current build gets no
call, so put the issue on that item or its design frame. For a Figma design, run
`python "${CLAUDE_PLUGIN_ROOT}/tools/figma_comments.py" nodes <file key> <node id>` on every node the
question names: an open thread in which the user asks design to change the frame means the call is
made.

**2 — Search the ledger.** If an open entry already asks this, add the new evidence to that entry
instead of filing a second one. If a closed entry answered it, apply that answer.

**3 — Write the entry.** The ledger is local and reversible, so write it without asking, then print
it in full. A teammate can read the ledger: write it plainly (the `humanize` skill), attribute to
roles, not people, and say nothing about how it was found.

**Visual questions get a picture.** Save a PNG next to the ledger in `.claude/open-questions/`
named `Q-<n>-<what>-<width>.png`, with the build commit and capture date in the entry. **Look at the
PNG before linking it**: a scroll-reveal page captures blank. A question that is not visual gets no
picture.

**4 — Confirm** "Filed as Q-<n>." and resume the prior task.

## Rules

- Never close a question here; that is `triage`.
- Quote both sides of a conflict with file:line. "Two docs disagree" is not a question.
- If the answer is knowable by running a command, run the command instead. Don't park a measurable
  fact as a question; check what the tools in `${CLAUDE_PLUGIN_ROOT}/tools/` measure first (each
  answers `--help`).
- One question per entry. Several questions are several entries, each with its own id.
- Filing is not asking. When the user is in the session and the answer blocks the work, ask them on
  a question card as well, several questions on one card, one block each; the entry is the record.
