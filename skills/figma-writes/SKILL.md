---
name: figma-writes
description: "Load right after figma-use, before the first use_figma call that creates, edits, clones, moves or deletes a node, and before any Figma comment post, reply, edit, resolve or delete. Covers what a Figma write can destroy, what the REST API can't do, and how to prove a write landed; figma-use covers the Plugin API itself."
---

# Figma writes

A write is done when it changed only what you meant to, where you meant to, and you have read it
back. `figma-use` (and `figma-use-figjam` on a `/board/` file) covers the Plugin API; this skill
covers what a write can destroy, what REST can't do, and how to prove a write landed. Read
`references/specific-jobs.md` first when the job is one of these: placing frames on a dark board or
nesting sections, uploading or replacing an image, building a hover, styling a board diagram,
setting Ready for dev, checking a text edit in the export, starting a new thread or cleaning up
comments, checking whether a draft was already posted, or reading an old version.

The tools named below live in this plugin's `tools/` folder; run them as
`python "${CLAUDE_PLUGIN_ROOT}/tools/<name>.py" ...`. They read the token from `FIGMA_TOKEN` or the
OS keychain (`figma_auth.py --source` says which), and file keys from their arguments or from
`figma.files` / `figma.default_file` in the agent-workbench config (`kit_config.py`).

## Other people's work

- Re-read a node just before you change, replace or delete it; if a person could have edited it
  since your last read, edit in place or ask, because a write from stale text erases their change.
- Never edit, copy or comment on a node with `visible: false` on itself or any ancestor; it is off
  on purpose. If one looks important, say it is hidden and ask. A fill, stroke or effect whose own
  `visible` is false is absent too; it still reports colour and opacity, so read colour, opacity and
  `visible` together before copying a paint.
- Write only where the work is yours. Design's personal pages (sandboxes, graveyard pages,
  in-progress copies) and the other sections of a shared board are teammates' work: never edit or
  delete there, even on a keyword match. A main edit reaches personal pages anyway; report what
  lands there, and don't undo it.
- On a board, build content from native text, sticky, table, code-block and shape objects the team
  can edit; upload an image only for a picture of real UI, never a rendered PNG of text. State each
  fact the team needs in full, with no pointers to repos, local paths or other docs; a reader on the
  board cannot follow them.

## Reruns and deletes

- Edit existing nodes in place and create only what is missing; creation is not idempotent, so
  before re-running a create that failed or timed out, count the parent's `children` first.
- Write text so a rerun gives the same result: replace the whole string or a found range, never
  append.
- Before `remove()` on a FigJam node, scan `findAllWithCriteria({ types: ['STAMP'] })` for a
  `stuckTo.id` in the delete set, and edit text in place instead of rebuilding a stamped node;
  `remove()` deletes stuck stamps without warning, and a recreated stamp may lose its author.
- Before removing any node, list the comment threads pinned to it (`client_meta.node_id` in
  `GET /v1/files/{key}/comments`). Removing the node leaves them attached to nothing, and only a
  person in the UI can clean them up. Move or answer them first.

## Prove each write

- After `appendChild` or `clone()`, read back `parent.id` and `absoluteBoundingBox`, re-append to
  the intended parent if needed, and re-assert `layoutSizingHorizontal/Vertical`; a clone can land
  on the page or auto-nest into a child section, and x/y alone does not show it.
- Read back every resize. Resizing a layer inside an instance (`I<instance>;<id>`) can return no
  error and not hold; resize the top-level instance instead.
- Run `get_screenshot` on every node you uploaded to or rebuilt, not a sample; a success response
  is not proof. Screenshot a section's child frames one at a time; a section screenshot renders from
  the page origin and pulls in overlapping page content, even with `contentsOnly: true`.

## Main components

- Fix a broken instance at its main when the main has one instance and no override, else as an
  instance override. Drop an unwanted part by its variant property (`CTA=no`), never by hiding a layer.
- Before calling a main edit contained, count its reach: `getInstancesAsync` misses instances nested
  in other components, so climb to each containing component and repeat down to page instances.
- After a main edit, read back what it reached: an instance that overrides the property keeps its old
  value, so one grid can end up mixed, and a fixed-size parent can clip the new size.

## Comments

- Post, reply, edit and delete only through `figma_post.py` and `figma_attach.py`, never by hand
  over REST or with a scratch browser script; the Figma MCP has no comments tool. `figma_post.py`
  refuses a post until `figma_comments.py frame` has read the node and `figma_comments.py search` the
  whole file, and refuses a reply stacked on the user's own comment, a second question, a pin it
  can't place clear of other layers, a repost soon after a delete and deleting a thread root with
  replies. `figma_post.py --help` lists each refusal and its override.
- Read the whole thread first; if the newest comment is the user's own, draft replacement text
  instead of stacking a reply, and if someone answered them, reply after that person. If their
  newest comment only points to another thread, put the update in their newest comment there, and
  leave the pointer or add the link to it.
- Once the user approves the new text, edit their comment yourself with `figma_attach.py edit`;
  never hand them the text to paste.
- Draft comments in a drafts file and post them only when the user says so, in one batch. Pass the
  file to `figma_post.py --drafts <path>` (or set `figma.drafts` in the config) so a posted draft
  leaves it and a node in its "Cut: do not re-raise" table is refused. A question only the user can
  decide goes to the user, not into the file.
- Keep each comment to one short ask, two or three sentences, and name the thing (page, section or
  component) with the id behind a link.
- Read `resolved_at`, not the reply count: design resolves threads without replying. Design resolves
  FIX notes, never you; resolve only a dead or moot thread, or a note folded into a merge, and only
  on the user's yes.
- Hold a finding on a frame design is still editing. Of two frames with the same name, post on the
  one design works in, usually the newer id. Figma re-anchors comments: navigate by the current id,
  identify by the posted one.
- A note saying you fixed something in the file states the before in words and carries its
  before/after picture in the comment itself, after a closing "Before and after:"; `figma_post.py`
  attaches it after the post. If attaching fails, run the step it prints yourself; never hand the
  step to the user, and never place an "Evidence" frame in the file or link one.
- To ping someone, write `@Last, First` exactly as Figma shows them on their comments;
  `figma_post.py` turns it into a mention chip.
