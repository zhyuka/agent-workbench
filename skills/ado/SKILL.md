---
name: ado
description: Own the Azure DevOps work-item lifecycle end to end - search before creating, formalize on create, resolve when the code ships, and periodically reconcile the board against reality. Use for "file a ticket", "open a bug", "close these out", "why is the board out of date", "audit the backlog", or whenever work lands that an ADO item should record. Other skills that touch ADO should invoke this one instead of restating its rules.
---

# ADO - work-item lifecycle

The board is the team's only shared record. Code that ships without its item moving is **invisible
work**, and a board nobody trusts stops being read. `tools/ado.py` is the deterministic gate; this file
is the judgment around it.

**The phases:** §1 search -> §2 create -> §3 resolve -> §5 handle a review that lands on a resolved
item -> §6 reconcile the board. **§4 (writing bodies and comments) applies throughout**: read it
before your first write. §7 is the write hazards; §8 is scope.

**Tools** (paths relative to the plugin root):

    python "${CLAUDE_PLUGIN_ROOT}/tools/ado.py" --rest search|new|lint ...   search-first create gate, lint
    python "${CLAUDE_PLUGIN_ROOT}/tools/ado_rest.py" pull|audit|show|decisions  read and audit, from a cache
    python "${CLAUDE_PLUGIN_ROOT}/tools/ado_write.py" state|field|comment|...   writes, each read back

**Setup.** The org, project and title prefix come from `.claude/agent-workbench.json` in the project
(see `tools/kit_config.py` for every key). A tool with no org or project set exits 2 and names the
key; it never guesses a project. Confirm the target org and project before any write when you work
across more than one.

**The token.** The tools read `ADO_PAT` from the environment, or the OS keychain (service
`agent-workbench`, account `ADO_PAT`). They never print it. Never ask for a token in chat, and never
try a token you cannot identify against other services to find its owner; ask which service it is
for. A hand-made call with an `az` access token can get a sign-in page back, which reads as an empty
result, not as no data.

**This skill = work items only.** Repo, branch and push work follow the project's own rules.

## 0. The failure this exists to prevent

**Items sit New while their code has shipped.** That is structural, not forgetfulness: the implicit
resolve trigger is a merge to the default branch, build work often lands on another branch first,
commits cite `#NNNN` unevenly, and work done by other people moves no ticket at all. Resolving is a
**step you perform**, never a thing that happens.

## 1. SEARCH - the item very likely already exists

`ado.py --rest search <keywords> --state all` before creating anything. `new` refuses un-searched
creates. `--rest` goes **before** the subcommand; it uses the PAT, so no `az` prompt fires.
`ado.py search` matches titles only. If you have a cross-source search (a `recall` skill or
similar), use it too: an item is often named only in a comment, a commit, a design thread or a chat.

**Other people file tickets before a build ticket exists.** Design and product often file the item
for copy, naming or page work; fill in and close theirs rather than creating a parallel one.

Read the hits before dismissing them. Only `--force` once you can say why each fails to cover your
case; a *pre-launch* audit does not cover a *post-launch* re-audit, so that one is genuinely new.

A zero-hit search is only "no duplicates" once the same search has found a title you know exists.

**A `new` that errors may still have made the item:** it creates first, then writes the body and
links the parent. Before running it again, search the exact title; finish the item it made with
`ado_write.py field` and `parent` rather than re-running with `--force`.

**An open Figma thread is a duplicate too.** When the body cites a Figma node, `new` refuses (exit 3)
an unlinked open thread in that node's section that shares quoted wording or a node id; link it, or
pass `--figma-ok <id>`. That check needs the Figma tools (`figma_comments.py`, `figma_family.py`) and
a Figma token; when Figma cannot be read it exits 2, and `--skip-figma "<why>"` is the explicit way
past it.

## 2. CREATE - formalized, or not at all

Route every create through `ado.py --rest new` (title convention, parent, non-root iteration, right
body field per type, Gherkin AC, then self-lint).

Title: `<prefix> // <page> - <component> // <what>` when the project sets `ado.title_prefix`.
Right field: a Bug's spec goes in ReproSteps, a Story's in Description. Only a Story's form shows
the AC field; a Bug's AC goes in System Info and a Task's in its Description, and `ado.py` does that
and lints for it (`AC_OFF_FORM`). The body is the living spec.

`--iteration` takes the sprint path an item carries, `<project>\<sprint>`. Copy it from a recent
item: `ado.py --rest lint <id>` prints it on its `non-root iteration` line. Never keep a sprint name
in a file, and never pass the tree path with an `\Iteration\` node in it; ADO refuses that.

- **Parent:** Bug -> its page's or feature's **Build Story**; Story -> a **Feature**. **A Story cannot
  usefully parent a Story**: both sit in one backlog category, so ADO hides the parent from the
  backlog behind a "same category hierarchy" banner. Only Bug and Task may sit under a Story (when
  the team tracks bugs with requirements; when bugs are tracked with tasks, a Bug under a Task trips
  the same banner). `new` and `ado_write.py parent|reparent` refuse a same-category parent. So when a
  Story belongs to a page: **parent it to the Feature and add the page Story as `related`**.
- **Epics and Features legitimately sit at the project root**; the non-root rule is for
  Stories, Bugs and Tasks. Don't "fix" a Feature's root iteration.
- **AC must be checkable and derived from the body.** Never invent AC to satisfy the linter. A body
  too thin to derive AC from is the actual defect.
- **Scope to intent, not a snapshot.** "These 13 commits" is stale tomorrow; "everything shipped in
  <sprint>, rolling" stays correct.
- **No standalone design tickets** when design lives in a design tool. A Build Story links the design
  and carries the scope; a "content map" or "visual design" ticket mirroring the spec beside it
  drifts. Create a design item only for genuine design *work to assign*, never as a record of a
  finished design.
- **Design change or code change:** a frame change is a thread on the frame, a code change an item
  under the Build Story. A frame change gets an item too only when it needs an owner and a sprint,
  and then each links the other.
- **What does NOT deserve an item:** pure build detail with no team-facing meaning.
- **Count and type are part of formalization. "One item per change" is not a licence to file ten.**
  Two checks before every create: **(a) does this share an owner or a decision with something filed
  in this same session?** If yes it is a paragraph in that item, not a new id (the §6 merge test).
  **(b) Pick the type from what it IS**, not by habit: Bug = it violates an agreed spec. A rule that
  never existed, a refactor, or a system being standardised is a **Story**. An asset re-export or a
  tooling addition is a **Task**. Nine Bugs for one afternoon reads as a broken product rather than a
  day's work, and it buries the real defects. A type change after the fact is a second write that
  can fail, so get it right at create time.

## 3. RESOLVE - evidence, honesty, correct terminal state

**Never resolve on memory or a commit message alone.** Prove it against the working tree: grep the
symbol, read the file, or measure the rendered page.

- **Grep proves presence, not completion.** An icon name can grep positive because the icon already
  existed while the *restyle* was the ask. Read the body before trusting a hit.
- **Measure UI claims** on the rendered page, not by looking.
- **A data-gated feature can complete itself.** Something shipped dormant behind a data condition
  activates with no deploy once the data arrives. Check the gate's live condition.

**Terminal state by type.** In the Agile process a **Task has no `Resolved`** (the write fails with
"not in the list of supported values"); use **`Closed`**. Bug and User Story use `Resolved`, then
`Closed`.

**What the two states mean.** Agree this with the team once and write it into the project's rules.
A workable default: **`Closed` = the change is on the branch the team ships from, it has been QA'd,
and there are no follow-ups. `Resolved` = the build is done but something is still open.** Closed is
the normal end state for finished work, not a rare one.

- **`Closed` is not a production gate** unless the team says so. The sprint or release carries the
  deployment story; the work item carries the work.
- **Release governance is not a work-item state.** If the team holds a branch back on purpose, never
  cite that branch's position as evidence that an item is unfinished. Grep your own draft for the
  branch name before sending. If the project sets `ado.frozen_branch`, `ado_rest.py audit` flags
  text that couples a merge or PR to it.
- **Before Closing, check for follow-ups in three places, not one:** (1) **comments**: an unanswered
  question, a reviewer still QAing, a "part (b) is not done"; (2) **an open CHILD** (New/Active);
  (3) **the body/AC**: a scenario explicitly not met. Any one of those means it stays `Resolved`.
  None of them means Close it.
- **A `related` link never holds an item.** Related is a cross-reference, not a dependency, and
  spinning a follow-up out into its own ticket is exactly what CLEARS the parent. Treating
  related-Resolved as a follow-up **deadlocks the board**: A is held because B is Resolved, B is held
  because A is Resolved, and neither can ever close.
- **A reviewer saying "looks good" or "feel free to close" is a follow-up-free signal**: take it and
  Close, same pass. Leaving it Resolved after that is the stale-board failure, not caution.

**Never hardcode a branch ahead-count in a comment.** "the branch is 16 commits ahead" is false the
next time anyone pushes. Write "not merged to <branch>", which stays true until it stops being true.
`ado_rest.py audit` flags these (`comment-stale-branch`) after a `pull`.

**Say plainly where the work actually sits**, in words that stay true. This is a rule about **what
the comment claims**, not about which state to pick: state comes from the follow-up test above; the
comment carries the deploy caveat.

## 4. WRITING WORK-ITEM CONTENT - applies at every phase

**Read `references/writing-work-items.md` before you write a title, a description, acceptance
criteria or a comment.** It is the content standard: clickable references, what belongs in the body
versus a comment, comment length, the one-comment rule, and @mentions.

## 5. REVIEW LANDS ON A RESOLVED ITEM - triage before you schedule

**Read `references/review-triage.md` when feedback arrives on something already Resolved.** It holds
the triage order, when to reopen versus file new, and how to turn a reviewer's list into a
recommendation.

## 6. RECONCILE - the periodic sweep

Run whenever the board feels wrong, or on a schedule. `ado_rest.py pull`, then `ado_rest.py audit`
(add `--expect clean` to make it a gate), then:

| Check | What "wrong" looks like | Action |
|---|---|---|
| **Shipped but open** | New/Active, code already in the tree | Resolve with evidence (§3) |
| **Resolved with nothing open** | `Resolved`, no unanswered comment, no open child, AC met | **Close it** (§3 follow-up test). Usually the most common stale state |
| **Stale-open sprint** | Open item in an **ended** sprint | Move to the current sprint |
| **Completed items** | - | **Leave them** in the sprint they were completed in; moving destroys velocity |
| **Feature state** | Feature New but work done | Only close if it's a **milestone** (below) |
| **Overlapping dated sprints** | Two iterations spanning the same days | "Current" is ambiguous; needs a project admin |

`ado_rest.py audit` flags the rest: Figma cited with no link, a Closed parent over open children, a
child in another sprint from its parent, a same-category parent, near-duplicate titles, dangling
`#id` references, comment piles, and formalization gaps. Its `--help` lists every check. A check
whose config key is unset prints "did not run", never "ok".

**Milestone Features close; standing-theme Features do not.** A dated launch milestone ships and
closes. "Accessibility", "Enhancements" or "Design system polish" are standing themes that are never
done; closing one leaves the next bug nowhere to land. Ask: *is this a date or a category?*

**Reparent children BEFORE closing or deleting a parent**, or they orphan.

**Merge items that share one blocker.** "One item per change" has an inverse. **The test: if two
items cannot be closed independently because one decision or one owner gates both, they are one
item.** Two tickets on one blocker split the question across two owners and let each look like the
other's problem. **Fold the unique detail into the survivor before deleting the originals**, and if
the survivor's title now looks like a duplicate of them on the board, that is the signal to
*delete* the originals, not leave them Resolved.

**Before preserving a doomed ticket's content, confirm the work is still in scope.** Check scope,
then fold.

**Iteration semantics.** An iteration is *when work is planned or done*, not a status. Open work
belongs in the sprint you intend to do it in; completed work stays put. Unscheduled "someday" work
wants a **dateless** backlog iteration: a *dated* one overlapping the live sprint makes "current"
ambiguous and splits filing. Set its name as `ado.backlog_iteration` so the audit's
`done-in-backlog` check runs.

**Renaming an iteration re-homes every item in it, completed ones included.** After any rename,
re-check the backlog bucket. Place completed items back by *when they actually finished*
(ClosedDate, or ChangedDate as a proxy), not in bulk.

## 7. Write hazards - `rc=0` is NOT evidence

`ado_write.py` `state`, `field`, `comment` and `editcmt` read the item back and print WRITE NOT
CONFIRMED (exit 1) when ADO stored something else, or DID NOT RUN (exit 2) when the read-back
failed. `attach` reads back too. `ado.py new` and `ado_write.py` `parent`, `reparent` and `delcmt`
do not, and `ado.py lint` catches only an *empty* or truncated field, so read those writes back
yourself (`ado_rest.py pull` then `show <id>`).

Known silent failures over `az`, each of which returned rc=0 and passed lint: `--fields` cuts a value
at its first newline, non-ASCII entities can be stripped from fields, and repeating `--fields` keeps
only the last one. Use `--rest`, and `ado_write.py field` for long bodies; it reads the value back.

`System.Parent` is a relation, not a field: use `ado_write.py parent` or `reparent`, never
`field System.Parent`.

The mirror image on the read side: a bad decode can **invent** corruption that is not there. Never
treat `errors="replace"` output as evidence of damage; check the raw bytes before you believe a
corruption finding, and never overwrite on the strength of one.

For a reviewed list of edits, write it as a `batch` file (`ado_write.py batch <file.json>`): one
approval covers the list, an item changed since the list was made is skipped, and an undo file is
written before each write.

## 8. Scope - whose board is it

Touch only what is yours. Someone else's items get a **comment**, not a state change, except pure
hygiene (a stale sprint on an open item), which is safe.

Do not restructure another person's in-flight sprint, even with dozens of items at the project
root: **flag, don't fix.**

A title that breaks convention **as a whole family** stays as it is: renaming one member fragments
the set. `lint` will keep reporting it. Say so and move on.

## Rules

- **Search before every create.** A duplicate work item is the failure this skill is named for.
- **Resolve when the code ships** to the branch the team ships from, by the team's definition (§3).
- **Closed means shipped + QA'd + no follow-ups.** It is not a production gate unless the team says so.
- **Read an item's CURRENT state before drafting any reply.** Items get closed and answered overnight.
- **A WIQL query returning 0 is not a clean board.** Prove the zero against a case whose answer you
  already know.
- **Use `ado_write.py field` for long fields.** `ado.py new --body` has failed silently on large
  bodies over `az` and exited 0; over `--rest` that is unverified. `field` reads the value back.
- **Park, do not guess.** A question only the user can answer goes to them (or, if the team tracks
  open questions as items tagged `decision`, list them with `ado_rest.py decisions`); never fill the
  gap with an assumption.

## Output cap

**Reconcile sweeps: one table per hygiene class, ~400 words total.**

- Columns: class · item ids · evidence · proposed action. No prose between tables.
- **A zero result is one line.** Say how the zero was proved, then move on.
- Over 20 items in a class, give the count and the top 5 ids, not the full list.

## How to invoke

```
/ado search <terms>            # ALWAYS first - never create before searching
/ado new                       # formalized create
/ado lint <ids>
/ado reconcile                 # §6 sweep: ado_rest.py pull, then ado_rest.py audit
```

## Maintenance - the scripts are the source of truth

**`ado.py` owns the search-first gate and formalization; `ado_rest.py` owns the reads and audit
checks; `ado_write.py` owns field, parent and comment writes.** This file describes them.

- When a check or a required field changes, edit the script and sync this prose. **Do not encode a
  rule only here.**
- If this file and a script disagree, **the script wins.**
- `ado.py` shells through `az` unless `--rest` comes before the subcommand; where `az` is
  permission-gated, every call prompts. Pass `--rest`.
