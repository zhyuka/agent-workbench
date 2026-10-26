---
name: figma-writes
description: "What can go wrong when writing to a Figma design file or FigJam board, beyond the Plugin API basics in figma-use. Use before any write through use_figma or the Figma REST API: editing, cloning or deleting nodes, replacing text, uploading images, placing frames, adding prototype reactions, and posting, replying to, editing or deleting comments. Load it alongside figma-use."
---

# Figma writes

Load `figma-use`, plus `figma-use-figjam` on a `/board/` file, for the Plugin API itself. This skill
covers what a write can destroy, what the REST API cannot do, and how to prove a write landed.

The tools named below live in this plugin's `tools/` folder; run them as
`python "${CLAUDE_PLUGIN_ROOT}/tools/<name>.py" ...`. They read the token from `FIGMA_TOKEN` or the
OS keychain (`figma_auth.py --source` says which), and file keys from their arguments or from
`figma.files` / `figma.default_file` in the agent-workbench config (`kit_config.py`).

## Before any write

- Re-read a node just before you change, replace or delete it; if a person could have edited it
  since your last read, edit in place or ask, because a write from stale text erases their change.
- Never edit, copy or comment on a node with `visible: false` on itself or any ancestor; it is off
  on purpose. If one looks important, say it is hidden and ask.
- Treat a fill, stroke or effect whose own `visible` is false as absent; it still reports colour and
  opacity, so read colour, opacity and `visible` together before copying a paint.

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

## Placement

- After `appendChild` or `clone()`, read back `parent.id` and `absoluteBoundingBox`, re-append to
  the intended parent if needed, and re-assert `layoutSizingHorizontal/Vertical`; a clone can land
  on the page or auto-nest into a child section, and x/y alone does not show it.
- Read back every resize. Resizing a layer inside an instance (`I<instance>;<id>`) can return no
  error and not hold; resize the top-level instance instead. A crop-mode image whose layer changed
  shape is squashed until its crop is refit.
- If a section nested inside another section comes back flattened, rebuild it as flat sibling
  sections or background shapes at page-level coordinates; a fixed-size section used as a table
  holds.
- Clear the default white fill (`fills = []`) on new frames before placing them on a dark board.

## Board content

- Build board content from native text, sticky, table, code-block and shape objects so the team can
  edit it; upload an image only for a picture of real UI, never a rendered PNG of text.
- State each fact the team needs on the board in full, with no pointers to repos, local paths or
  other docs; a reader on the board cannot follow them.
- Draw a flow as a pipeline in bands, not swimlanes, and show ownership as a label on the node.
- Keep a diagram to three line weights, about five shapes and two colours; label only the branches,
  draw a loop as an arc back, cluster fan-in, and add a title and a legend.
- To screenshot a diagram built from page-level siblings, put a temporary transparent shape behind
  it, screenshot that shape with `contentsOnly: false`, then delete the shape.

## Images

- Capture screenshots at 1x device pixel ratio and keep each under about 500 KB, re-encoding
  text-heavy PNGs as JPEG; the Figma client silently skips heavy image fills.
- Match the node's aspect ratio to the image's, on upload and on replace; `FILL` crops a mismatch.
- When capturing web pages, force scroll-reveal elements visible and wait for `document.fonts.ready`
  and every image load; otherwise the capture comes out blank or half-styled.
- POST the bytes to each `upload_assets` URL in your very next call; the URLs are single-use and
  expire within moments.
- Target existing nodes with `nodeIds`, then read back `fills`; if no IMAGE paint carries the
  returned `imageHash`, set `node.fills = [{ type: 'IMAGE', scaleMode: 'FILL', imageHash }]`.
- Run `get_screenshot` on every uploaded node, not a sample; a success response is not proof. It
  renders on the server, so it cannot show a heavy fill the client dropped; the size cap does.

## Prototype reactions

- Build a hover with `setReactionsAsync` (`ON_HOVER`, `SMART_ANIMATE`), not the keyframe animation
  API; `ON_HOVER` reverts on mouse-out by itself.
- Give `duration` in seconds; CSS `cubic-bezier(a, b, c, d)` maps directly to `CUSTOM_CUBIC_BEZIER`
  with `easingFunctionCubicBezier: { x1: a, y1: b, x2: c, y2: d }`.
- Make the end state a `clone()` of the default frame and change only what animates; Smart Animate
  pairs layers by name.
- Point `NAVIGATE` only at top-level frames; a frame nested in a frame is rejected but a direct
  child of a section qualifies, so wrap the default and hover frames in a section.

## Verifying a write

- Screenshot a section's child frames one at a time; a section screenshot renders from the page
  origin and pulls in overlapping page content, even with `contentsOnly: true`.
- Send `User-Agent: Mozilla/5.0` when a script downloads a `get_screenshot` URL; without it the
  download is 0 bytes.

## REST calls

- Make every REST call through `figma_client.py` (`figma_client.get(...)` in Python, or
  `figma_client.py get <path>`): it shares one rate-limit budget across every process, waits out a
  429 instead of failing, and logs which tool spent the budget (`figma_client.py status`). A tool
  with its own HTTP client starves the others and gives up on the first 429.
- To learn where a node sits (its ancestors and their children), use `figma_client.py lineage`, not
  a whole-file or `files/<key>?ids=` read; those are the most expensive calls there are.

## Comments

- Read and write comments through REST (`/v1/files/{key}/comments`); the Figma MCP has no comments
  tool. Save the JSON to disk and digest it rather than reading it inline.
- Run `figma_comments.py` before drafting: `inbox` for threads waiting on you, `search` for an open
  thread on the same subject anywhere in the file, `nodes` or `check` for threads on the node, its
  layers, its ancestors and other copies of its component, and `frame` to read the node with hidden
  layers removed. `figma_post.py` refuses to post until `frame` and `search` have run.
- Read the whole thread first; if the newest comment is your own, draft replacement text instead of
  stacking a reply, and if someone answered you, reply after that person.
- Edit your own comment yourself once the new text is approved:
  `figma_attach.py edit <key> <comment id> --text-file <txt> [<picture>...]` replaces the text in the
  browser, adds the pictures, and checks the saved text over REST. Don't hand the user text to paste.
  It refuses a comment with replies; reply after them instead.
- Draft comments in a drafts file and post them only when the user says so, in one batch. Pass the
  file to `figma_post.py --drafts <path>` (or set `figma.drafts` in the config) so a posted draft
  leaves it and a node in its "Cut: do not re-raise" table is refused. A question only the user can
  decide goes to the user, not into the file.
- Keep each comment to one short ask: two or three sentences, one question.
- Name the thing, not the id: page, section or component, with the id behind a link.
- Read `resolved_at`, not the reply count: design resolves threads without replying, which looks the
  same as neglect.
- Hold a finding on a frame design is still editing; a correct measurement there is not yet worth a
  designer's time. Of two frames with the same name, post on the one design works in, usually the
  newer id.
- A comment records the node it was posted against, and Figma re-anchors it: navigate by the current
  id, identify by the posted one.
- Find annotations by walking `.children` yourself; `findAll` and a `get_design_context` grep miss
  them on instances. Calibrate on a known id so a zero is measured.
- A note saying you fixed something in the file carries its before/after picture in the comment
  itself, after a closing "Before and after:", and states the before in words. Make the picture with
  `figma_evidence.py sheet`. A REST comment cannot carry an image, so `figma_post.py new`, `reply`
  and `batch` attach it in the browser right after the post (`figma_attach.py`, which signs in by
  itself); never hand the step to the user, and never place an "Evidence" frame in the file or link
  one. Pass the comment id the post printed, never one copied from another thread.
- Post approved drafts as one `figma_post.py batch <items.json>` (run `--check` first): every gate on
  every item, then the posts, then ONE browser for all their pictures, mentions and edits. Never write
  a scratch browser script for a Figma comment step, and never run `figma_attach.py` once per comment
  in a loop.
- Start a new thread when the target node is a page-level frame with three or more thread roots; a
  shared node is not a shared subject.
- Expect no edits and no mentions over REST: `PUT` on a comment returns 404, so edits go through
  `figma_attach.py edit`, and `@name` posts as plain text. To ping someone, write `@Last, First`
  exactly as Figma shows them on their comments; `figma_post.py` turns it into a mention chip in the
  browser after the post and checks it over REST (the markdown form drops the `@` only for a chip).
  A mention added that way notifies. A 403 on POST means a read-only token.
- Pin with `client_meta: { node_id, node_offset: { x, y } }`, offset from the node's top-left; an
  empty `client_meta` pins at canvas (0, 0). Posting by hand, hit-test the pin: the smallest element
  under `node_offset` must be the subject.
- `figma_post.py` refuses a pin on a layer inside an instance, a pin it cannot place clear of smaller
  layers, more than one question, a repost soon after a delete, and deleting a thread root with
  replies; `figma_post.py --help` lists each refusal and any flag that overrides it.
- To tell whether a draft was already posted, match its text against every comment in the file, not
  only threads on its node; people often pin on an ancestor.
- Scope a comment cleanup by the frames the pins sit on, never by `created_at`; a date filter misses
  recent pins.

## Versions

- Read `?version=X` as the file at the end of X's editing session, not at its `created_at`; for the
  file as of time T, use the version before the newest one created at or before T.
- Sort versions by `created_at`; version ids are not ordered in time.
- Expect your `use_figma` writes to show up as versions under your name; such a version is not a
  named save.
