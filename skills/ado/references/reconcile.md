# ado: the reconcile sweep

The judgement calls after `ado_rest.py pull` and `ado_rest.py audit`. The audit checks structure;
these need a person's reading of the item. An audit check whose config key is unset prints "did not
run", never "ok".

| Check | What wrong looks like | Action |
|---|---|---|
| Shipped but open | New or Active, code already in the tree | Resolve with evidence (SKILL.md §3) |
| Resolved with nothing open | `Resolved`, no unanswered comment, no open linked item, AC met | Close it (the §3 follow-up test). Usually the most common stale state |
| Stale-open sprint | An open item in an ended sprint | Move it to the current sprint |
| Completed items | Done in an earlier sprint | Leave them in the sprint they were completed in; moving them destroys velocity |
| Feature state | A Feature still New with its work done | Close it only if it is a milestone (below) |
| Overlapping dated sprints | Two iterations spanning the same days | "Current" is ambiguous; it needs a project admin |

- Milestone Features close; standing-theme Features do not. A dated launch milestone ships and
  closes. "Accessibility", "Enhancements" or "Design system polish" are standing themes that are
  never done, and closing one leaves the next bug nowhere to land. Ask whether it is a date or a
  category.
- Reparent children before closing or deleting a parent, or they orphan.
- Merge items that share one blocker: if two items can't be closed independently because one
  decision or one owner gates both, they are one item. Two tickets on one blocker split the question
  across two owners and let each look like the other's problem. Fold the unique detail into the
  survivor before deleting the originals; if the survivor's title now looks like a duplicate of
  them, delete the originals rather than leave them Resolved.
- Before preserving a doomed ticket's content, confirm its work is still in scope.
- An iteration is when work is planned or done, not a status. Open work belongs in the sprint you
  intend to do it in, and completed work stays put. Unscheduled work goes in a dateless backlog
  iteration; a dated one overlapping the live sprint makes "current" ambiguous and splits filing.
  Set its name as `ado.backlog_iteration` so the audit's `done-in-backlog` check runs.
- Renaming an iteration re-homes every item in it, completed ones included. After a rename,
  re-check the bucket and place items back by when they actually finished (ClosedDate, or
  ChangedDate as a proxy), not in bulk.
- A title that breaks convention as a whole family stays as it is; renaming one member fragments the
  set. `lint` keeps reporting it: say so and move on.
