# 5. Review lands on a resolved item - triage before you schedule

The detail behind `SKILL.md` §5. A reviewer replying to work you already resolved is its own
moment: build workflows assume you just built something, and feedback workflows assume fresh
feedback. Neither covers it.

- **Reopen vs new item.** Reopen only if the item's **acceptance criteria were not met**. If the AC
  were met and the reviewer wants something *different*, that is a new item: the old AC is the
  record of what was agreed. (Removing a height cap whose AC were written around the cap existing is
  a new item, not a reopen.)
- **Check the points against EACH OTHER before scheduling any of them.** Review comments arrive as a
  flat numbered list, but they interact: one point (remove a cap) can make another worse (a
  bottom-anchored element now opens a gap). Shipping the first alone introduces a regression the
  reviewer may already have predicted. Link them `related`, and say in both items which one gates
  which.
- **A point that closes an open question is an answer, not a task.** Record it as decided and close
  the question; don't turn it into a ticket.
- **Separate "don't do it for that reason" from "don't do it."** A reviewer can reject a change's
  rationale while still wanting the change later for another reason. That is still a ticket, with
  the rejected rationale recorded so nobody re-proposes it for the wrong reason.
- **Verify the reviewer's numbers; it is cheap and it earns trust.** Reproduce their measurements
  and quote the confirmation back. Where you can *answer* a question with data rather than agree
  with a hypothesis, do.
- **Do not drip commits into a live review.** If the reviewer is still QAing, or any point still has
  an open question, hold and batch: tickets and one reply now, the build as one commit once
  answered.
- **Triage is a recommendation, not a transcription.** Turning a reviewer's numbered list into a
  numbered ticket is bookkeeping. The user wants a position: which of these do we do, which do we
  drop, and why. For each cluster say **do now / defer / recommend against**, and for anything
  deferred give the **falsifiable trigger** that would change the answer ("revisit if a fourth facet
  lands") rather than a date or a vague "later".
- **Argue the YAGNI case explicitly when scope is justified by future scale.** Check whether that
  future is a real workstream or a hypothesis, then whether its *specification* exists yet. A real
  but unspecified future is the strongest argument for waiting: building against a spec that does
  not exist performs a rework early and invites a second one when the real requirements land.
- **Split by cost, not by theme, and put the halves in different sprints.** A batch of review ideas
  is almost never one ticket. Pull out the cheap, self-contained items with no design dependency and
  schedule them **this sprint**; push the ones needing design or absent scope to the backlog,
  related-linked. Parking the whole batch in the backlog is the lazy read.
