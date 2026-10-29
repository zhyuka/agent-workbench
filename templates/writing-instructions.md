# Writing instructions for Claude

How to write and edit anything Claude loads as instructions: a `CLAUDE.md`, a rules file, a skill,
an agent, a memory card. Read it before editing one.

## Each kind of text has one home

| Kind | Home | Loaded |
|---|---|---|
| A rule that holds in nearly every session | a `CLAUDE.md` or a rules file it imports | always |
| When to use a skill or agent | its `description` | always |
| How to do one job | the skill or agent body | when that job runs |
| Detail used in one step of a job | the skill's `references/` or the tool's `--help` | when that step runs |
| Anything with one mechanical right answer | code that refuses: a tool, a git hook, the project's check command, an ask rule | never read; it runs |
| A fact Claude can't derive (who owns what, a past decision that still binds) | the one file your setup names for that kind of fact (a memory card, or a file for people, the machine or standing decisions), the project rulings file, the tracker | on demand |
| Why a rule exists: the incident, the quote, the date, the measurement | git log, an archive folder, the gate records | never as an instruction |

Put each line where it belongs. Don't copy it into a second home; point to the first.

## What an instruction looks like

- **State the goal and the judgment, not the steps.** Say what done looks like and what to weigh.
  The model plans better steps than a hand-written list. Spell out steps only where order matters
  or a step is easy to skip.
- **One clause of why, not the story.** "Read only the named files, or the grade stops being
  blind" is enough. The incident that taught it goes in the record.
- **Say what to do, plainly.** No ★, MANDATORY, NEVER in capitals or "CRITICAL": current models
  follow plain instructions and overreact to shouting.
- **No dates, names or quotes in a rule.** "(per the lead, 2026-01-25)" or "user 1/24: '…'" ages
  and says nothing about what to do. If the ruling's date matters, the ruling lives in a record and
  the rule links it.
- **No counts that go stale** ("13 gates", "41 tools"). Say how to get the count.
- **One rule, one place.** Before adding a rule, search for it. If it is already in an
  always-loaded file, a skill doesn't repeat it.
- **Keep the measured lines word for word.** A line that a gate or backtest showed changes behaviour
  stays as written, examples included, however specific it looks; a paraphrase is a new line and
  has to pass the gate again. The gate record says which lines those are.

## Sizes

Enforce these with a lint that runs on every commit to the repo that holds them, so adding a line
past a limit means removing or merging one.

- A `description`: what it does and when to use it. At most 50 words for an agent and 80 for a skill,
  whose description also carries the phrases the user types. The caller's checklist goes in the
  calling skill's step, not here.
- An agent body: at most 600 words, and no HTML comments (provenance goes in the record).
- A skill's `SKILL.md` body: at most 5,000 tokens; the rest goes to `references/`.
- To fit a limit, move tool detail (flags, output legends, recipes) to `references/` or the tool's
  `--help` and merge repeated rules. Don't turn paragraphs into bullets or tables to save space.
- Everything that loads in every session (the rules files, `MEMORY.md`, every skill and agent
  description): one total, capped by a limit that only ever moves down. A new line there replaces
  an old one; moving a rule into an on-demand file makes room only when no step needs it every
  session.

## A correction from the user

When the user corrects you, fix the cause where it lives: the tool, the gate, or the one line of
the skill that led you wrong. Don't add a new paragraph quoting the correction. If no line led you
wrong, the fix may be the fact in its on-demand home from the table above, not a rule. Write a new
rule only after two independent occurrences; one incident is an anecdote. Edit a skill after the
task in flight, not during it.

## Changing an instruction file

1. Back up the old copy (`_archive/<what>-<date>/`) if git doesn't hold it.
2. List the old file's rules. Each one has to end up in the new file, at a home from the table above,
   or be dropped with a reason. Keep that list beside the backup.
3. Run the file's own check when one exists: the agent's or skill's eval suite, the project's check
   command for repo files, and `tools/doclint.py` from the agent-workbench plugin for prose.

A skill's `model:` line doesn't switch the model in headless `claude -p`, so don't add one expecting
it to.
