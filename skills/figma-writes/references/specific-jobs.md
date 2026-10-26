# Figma writes: detail for single jobs

Detail for one job each. Read the section when the job comes up; it is not loaded every session.

## Placement

- If a section nested inside another section comes back flattened, rebuild it as flat sibling
  sections or background shapes at page-level coordinates; a fixed-size section used as a table
  holds.
- Clear the default white fill (`fills = []`) on new frames before placing them on a dark board.
- A crop-mode image whose layer changed shape is squashed until its crop is refit.

## Board diagrams

- Draw a flow as a pipeline in bands, not swimlanes, and show ownership as a label on the node.
- Keep a diagram to three line weights, about five shapes and two colours; label only the branches,
  draw a loop as an arc back, cluster fan-in, and add a title and a legend.
- Use ISO-5807 shapes (`ENG_DATABASE` for a store, `DOCUMENT_SINGLE` for a document, a diamond for a
  decision), keep node text terse, and put no mark mid-edge.
- To screenshot a diagram built from page-level siblings, put a temporary transparent shape behind
  it, screenshot that shape with `contentsOnly: false`, then delete the shape.

## Ready for dev

- `use_figma` can't read or set `devStatus`, and REST can't write it. Set Ready for dev with the
  panel checkbox in the signed-in web editor, then confirm over REST.

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
- `get_screenshot` renders on the server, so it cannot show a heavy fill the client dropped; the
  size cap does.
- `filters` (such as `exposure`) is valid only on an IMAGE or VIDEO paint; spread onto a SOLID paint
  it throws an "Unrecognized key" error.

## Prototype reactions

- Build a hover with `setReactionsAsync` (`ON_HOVER`, `SMART_ANIMATE`), not the keyframe animation
  API; `ON_HOVER` reverts on mouse-out by itself.
- Give `duration` in seconds; CSS `cubic-bezier(a, b, c, d)` maps directly to `CUSTOM_CUBIC_BEZIER`
  with `easingFunctionCubicBezier: { x1: a, y1: b, x2: c, y2: d }`.
- Make the end state a `clone()` of the default frame and change only what animates; Smart Animate
  pairs layers by name.
- Point `NAVIGATE` only at top-level frames; a frame nested in a frame is rejected but a direct
  child of a section qualifies, so wrap the default and hover frames in a section.
- Fake a CSS `transform: scale()` hover by resizing the image rectangle by the same factor, recentred,
  inside a frame with `clipsContent: true`; Smart Animate tweens the geometry and the clip holds.

## Verifying a write

- After a text edit, check the REST `/v1/images` export of the whole card, not only `get_screenshot`:
  a line at its box's edge can wrap differently there and clip a word.
- Send `User-Agent: Mozilla/5.0` when a script downloads a `get_screenshot` URL; without it the
  download is 0 bytes.

## Comments

- Save the comments JSON to disk and digest it rather than reading it inline.
- To tell whether a draft was already posted, match its text against every comment in the file, not
  only threads on its node; people often pin on an ancestor.
- Start a new thread when the target node is a page-level frame with three or more thread roots; a
  shared node is not a shared subject.
- Scope a comment cleanup by the frames the pins sit on, never by `created_at`; a date filter misses
  recent pins.
- Find annotations by walking `.children` yourself; `findAll` and a `get_design_context` grep miss
  them on instances. Calibrate on a known id so a zero is measured.
- Post the approved drafts as one `figma_post.py batch <items.json>` (run `--check` first): every
  gate on every item, then the posts, then one browser for all their pictures, mentions and edits.
  Never run `figma_attach.py` once per comment in a loop.
- REST has no comment edit and no mention: `PUT` on a comment returns 404, and `@name` posts as plain
  text, which is why edits and mention chips go through the browser. Don't try the undocumented
  `message_meta` form. A mention chip notifies. A 403 on POST means a read-only token.
- Pins: `client_meta: { node_id, node_offset: { x, y } }`, offset from the node's top-left; an empty
  `client_meta` pins at canvas (0, 0). The smallest element under `node_offset` must be the subject;
  `figma_post.py` checks it.
- Pass `figma_attach.py` the comment id the post printed, never one copied from another thread.
- Only the user can move a misplaced pin on their own comment, by dragging it in the Figma UI; give
  them the link to the drag target. Fix its text with `figma_attach.py edit`, never by delete and
  repost.
- A live comment that differs from what was posted may be the user's own edit in the UI (REST keeps
  no history); check before calling it a tool fault or restoring the text.

## Versions

- Read `?version=X` as the file at the end of X's editing session, not at its `created_at`; for the
  file as of time T, use the version before the newest one created at or before T.
- Sort versions by `created_at`; version ids are not ordered in time.
- Expect your `use_figma` writes to show up as versions under the user's name; such a version is not
  a named save.
