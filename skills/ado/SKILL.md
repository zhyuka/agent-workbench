---
name: ado
description: Own the Azure DevOps work-item lifecycle end to end - search before creating, formalize on create, resolve when the code ships, and periodically reconcile the board against reality. Use for "file a ticket", "open a bug", "close these out", "why is the board out of date", "audit the backlog", or whenever work lands that an ADO item should record. Other skills that touch ADO should invoke this one instead of restating its rules.
---

# ADO - work-item lifecycle

The board is the team's only shared record, so each change has one well-formed item and each item's
state matches the code. Code that ships without its item moving is invisible work, and a board
nobody trusts stops being read. Resolving is a step you perform, never a thing that happens: the
implicit trigger is a merge to the default branch, build work often lands on another branch first,
commits cite `#NNNN` unevenly, and other people's work moves no ticket.

**Tools** (call them as `python "${CLAUDE_PLUGIN_ROOT}/tools/<name>.py"`):

    ado.py --rest search|new|lint ...        the gate: search first, formalization, self-lint
    ado_rest.py pull|audit|show|decisions    the reads and the board audit, from a cache
    ado_write.py state|field|comment|...     field, parent and comment writes, each read back

Each tool's `--help` holds its flags and gotchas. When a tool and this file disagree, the tool wins,
and a rule with one mechanical answer goes into the tool, not here. Pass `--rest` before the
subcommand, or `ado.py` shells through `az`, and where `az` is permission-gated every call prompts.

**Setup.** The org, project and title prefix come from `.claude/agent-workbench.json` in the project
(`kit_config.py --help` lists every key). A tool with no org or project set exits 2 and names the
key; it never guesses a project. When you work across more than one org or project, confirm the
target before any write.

**The token.** The tools read `ADO_PAT` from the environment, or the OS keychain (service
`agent-workbench`, account `ADO_PAT`), and never print it. Never ask for a token in chat, and never
try one you can't identify against other services to find its owner; ask which service it is for. A
hand-made call with an `az` access token can get a sign-in page back, which reads as an empty result,
not as no data.

Work items only: repo, branch and push work follow the project's own rules. Read an item's current
state before drafting any reply; items get closed and answered overnight.

## 1. SEARCH: the item very likely already exists

Run `ado.py --rest search <keywords> --state all` before creating anything; `new` refuses an
unsearched create. Search matches titles only, so the `recall` skill also finds an item named only in
a comment, a commit, a design thread or a chat.

- Design and product often file tickets for copy, naming and page work before a build ticket exists.
  Fill in and close the ticket they filed; don't create one.
- Read the hits before dismissing them, and use `--force` only once you can say why each fails to
  cover your case: a pre-launch audit does not cover a post-launch re-audit.
- A zero-hit search, or a WIQL query returning 0, means nothing until the same query has found an
  item you know exists.
- An open Figma thread is a duplicate too. When the body cites a Figma node, `new` refuses (exit 3)
  an unlinked open thread that shares wording or a node id with the item; link it, or name it with
  `--figma-ok <id>`. When Figma can't be read it exits 2, and `--skip-figma "<why>"` is the explicit
  way past.
- A `new` that errors may still have made the item: it creates first, then writes the body and links
  the parent. Search the exact title before running it again, and finish what it made with
  `ado_write.py field` and `parent` rather than re-running with `--force`.

## 2. CREATE: formalized, or not at all

Route every create through `ado.py --rest new`; it sets the title convention
(`<prefix> // <page> - <component> // <what>` when the project sets `ado.title_prefix`), the parent,
a non-root iteration, the right body field for the type and the AC, then lints the result. Copy the
`--iteration` sprint path, `<project>\<sprint>`, from a recent item (`ado.py --rest lint <id>` prints
it on its `non-root iteration` line), and never keep a sprint name in a file; ADO refuses a tree path
with an `\Iteration\` node in it. A Bug's spec goes in ReproSteps and a Story's or Task's in
Description. Put a long body in with `ado_write.py field` on that field, which reads the value back:
a long `--body` over `az` has failed silently and exited 0.

- Parent a Bug to its page's or feature's Build Story and a Story to a Feature. A Story cannot parent
  a Story: both sit in one backlog category, so ADO hides the child's parent from the backlog view.
  Only a Bug or a Task sits under a Story, so a Story that belongs to a page gets the Feature as
  parent and the page Story as `related`. Converting it to a Bug also works when it is genuinely a
  defect (`ado_write.py field <id> System.WorkItemType Bug`, then read it back). If ADO refuses the
  change, it is a manual edit in the browser.
- Epics and Features sit at the project root on purpose; the non-root rule is for Stories, Bugs and
  Tasks.
- Derive checkable AC from the body; a body too thin to derive AC from is the defect, so never invent
  AC to satisfy the linter.
- Scope to intent, not a snapshot: "everything shipped in <sprint>, rolling" stays correct, "these 13
  commits" is stale tomorrow.
- ADO tracks build; design lives in the design tool. A page's Build Story links the design and
  carries the scope, so don't mirror the spec into a design ticket beside it. Create a design item
  only for design work to assign. A frame change is a thread on the frame and a code change is an
  item under the Build Story; a frame change gets an item too only when it needs an owner and a
  sprint, and then each links the other.
- Pure build detail with no team-facing meaning gets no item.
- Count and type are part of formalizing. Before every create, ask whether it shares an owner or a
  decision with something filed in this session; if so it is a paragraph in that item, not a new id.
  Pick the type from what it is: a Bug violates an agreed spec; a rule that never existed, a refactor
  or a system being standardized is a Story; an asset re-export or a tooling addition is a Task. Nine
  Bugs for one afternoon reads as a broken product and buries the real defects.

## 3. RESOLVE: evidence, honesty, the right end state

Resolve on evidence against the working tree, never on memory or a commit message alone: grep the
symbol, read the file, or measure the rendered page.

- Grep proves presence, not completion: an icon that already existed greps positive when the ask was
  its restyle. Read the body before trusting a hit.
- Measure UI claims on the rendered page, not by looking.
- A data-gated feature can complete itself with no deploy; check the gate's live condition.
- An analytics item resolves when a row from the production host arrives in the data.

A Task has no `Resolved` state, so it ends `Closed`; a Bug or User Story goes to `Resolved`, then
`Closed`. Agree with the team once what the two states mean and write it into the project's rules. A
workable default gives each one meaning:

- `Closed`: the change is on the branch the team ships from, it has been QA'd, and nothing follows
  up. This is the normal end state for finished work, not a production gate; the sprint carries the
  release story.
- `Resolved`: the build is done but something is still open.

Before closing, check for follow-ups in three places: an unanswered question or a reviewer still
checking in the comments, an open (New or Active) child, and a scenario in the body or AC that is not
met. Any one keeps it `Resolved`; none means close it. A `related` link never holds an item: spinning
a follow-up into its own ticket is what clears the parent, and treating a related item as a follow-up
deadlocks the board. A reviewer's "looks good" or "feel free to close" means close it in the same
pass.

If the team holds a branch back on purpose (set it as `ado.frozen_branch`), that branch's position
has nothing to do with a work-item state: the gap is the designed state, and citing it as a reason
an item is unfinished, undeployed or a live defect is the error. Before sending a comment, grep your
own draft for the branch name, `prod` and `live`, then run
`grep -niE "not on <branch>|not live|still live|in production|should (merge|deploy)|push to <branch>"`;
delete any hit that sits next to a state, a severity or a recommendation. With the key set,
`ado_rest.py audit` flags text that couples a merge or PR to that branch. Say plainly where the work
sits ("not merged to <branch>"), in words that stay true; never a branch ahead-count, which is false
after the next push. After any batch of comments, run `ado_rest.py pull`, then `ado_rest.py audit`,
which flags an ahead-count as `comment-stale-branch`.

## 4. Writing work-item content

Before you write a description, acceptance criteria or comment, read
`references/writing-work-items.md`: clickable links, body versus comment, fixing an unread error in
place, short comments and one per thread, @mentions and attachments. The title convention and the
AC are set and linted by `ado.py new` (§2).

## 5. Review lands on a resolved item

When feedback arrives on something already resolved, read `references/review-triage.md`: when to
reopen versus file new, checking the points against each other, splitting a batch by cost, and
turning a reviewer's list into a recommendation. A reopened item restates its original evidence
rather than replacing it.

## 6. Reconcile the board

Run the sweep in the `reconcile` skill's tracker step, or whenever the board feels wrong:
`ado_rest.py pull`, then `ado_rest.py audit`, which checks the structure (`--help` lists each check).
Then make the judgement calls in `references/reconcile.md`: shipped but open, resolved with nothing
open, stale sprints, milestone versus standing Features, merges and renames. Report one table per
class (class, item ids, evidence, proposed action) with no prose between them, about 400 words in
all: a zero result is one line saying how the zero was proved, and over 20 items in a class gives
the count and the top 5 ids.

## 7. Write hazards: rc=0 is not evidence

`ado_write.py` `state`, `field`, `comment` and `editcmt` read the item back and print WRITE NOT
CONFIRMED when ADO stored something else. `ado.py new` and `ado_write.py` `parent`, `reparent` and
`delcmt` don't, and `ado.py lint` catches only an empty or truncated field, so read those writes
back yourself (`ado_rest.py pull`, then `show <id>`). Over `az`, a value cut at its first newline, stripped
entities and a dropped second field each returned rc=0 and passed lint. A bad decode can invent
corruption: check the raw bytes before believing a corruption finding from `errors="replace"`
output, and never overwrite on the strength of one.

## 8. Scope: whose board is it

Touch only what is yours: someone else's item gets a comment, not a state change, except pure
hygiene such as a stale sprint on an open item. Don't restructure another person's in-flight sprint,
even with dozens of their items at the project root; flag it.

## 9. Sign-offs

Part of a build sent back before sign-off makes no new item and never a Bug: it is the build's own
miss, fixed at once, and the fix commit names the Build Story as `#N`, which ADO links on push. File
a Bug only for a problem that got past sign-off or for work someone other than the builder must do.
Closing a Bug, or resolving or closing a Story, on a recorded sign-off is a state change under §3:
the sign-off supplies the evidence, and the follow-up test still applies. Show every such change in
full and get one yes before making any; `ado_write.py batch <file.json>` applies an approved list in
one run.
