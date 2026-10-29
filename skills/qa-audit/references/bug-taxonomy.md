# Defect taxonomy: classes that have really shipped and been caught

The checklist for a post-build QA pass, mined from the correction history and bug tickets of a real
site build. Run every group against the built page; each line is a defect class that has happened.
The classes builders and QA most often under-cover are groups D, H, I and K and, within A to C, the
bar baked into an image file, uneven card heights and hard-coded tokens, and most of all an
approximated icon, fill or illustration geometry.

## A. Images and media
- A bar or letterbox baked into the image file. Element geometry reports gap 0 because the `<img>`
  fills the card; the bar is inside the photo. Only a scan of the image file finds it.
- Hero or mockup image rescaled from the original (a silent size regression). Pin per-page container
  widths; assert the rendered width against the source, not the page max-width.
- Double-darkened image (a CSS overlay stacked on a baked-in dim). One source of dimming only.
- Image not filling its frame, or a gap under it. `object-fit: cover`, no baked bars; scan the file,
  not just geometry.
- Wrong crop of the right photo: the build used a photo whole where the design zoom-crops it. Rebuild
  the crop from the high-res source at the card's aspect.
- An asset compressed, cropped or swapped with no note. Any asset change gets a note with its
  reasoning where design will see it.
- Text or marks baked into screenshots used as content. Native text and native images only.

## B. Layout, overflow and responsive
- Horizontal overflow or sideways scroll, especially on a phone. Assert `scrollWidth <= viewport` at
  every design width, and tell real overflow from a screenshot artifact by measuring `scrollWidth`.
- Content clipped by an `overflow: hidden` ancestor (dropdowns, tooltips, menus cut off).
- An empty or no-results state changes the container width (layout jump). Measure populated against
  empty; assert equal.
- An interaction causes layout shift (heights change, a count moves). Reserve space for dynamic
  counts and badges.
- Inconsistent card heights from variable text with no min-height. Line-clamp plus min-height so
  rows match; carry the fix when cloning a card.
- Different card widths in a grid. Assert uniform track widths.
- Breakpoints dropped or diverged from the original, or conflicting breakpoint sets across files. One
  token set is the source of truth.
- Section spacing not per the design and not matching the same section on other pages. Measure
  against both.
- A stray or missing border or divider. Measure `border*` computed styles against the node; don't
  eyeball a 1px line.
- The nav flashes on load. Set the initial body class server-side, and check the nav tone per scroll
  band.

## C. Icons, avatars and tokens
- A wrong or never-before-used icon (a wrong glyph from the icon set; an arrow the site never uses).
  Icons come from the established set; flag any new one.
- A hand-drawn or approximated icon instead of the exact exported design vector, the most repeated
  icon miss. Never freehand a `<path>` "close to" a library glyph. Extract the literal exported
  vector for every icon and paste it verbatim (swap the colour for `currentColor`). Check three
  things per icon, because a plausible guess gets all three wrong: (1) glyph identity
  (`shield-check` has the tick, plain `shield` doesn't); (2) style (a stroked outline and a solid fill
  are different assets); (3) count and composition (a three-star sparkle is not two stars). Keep the
  exported viewBox and the default `preserveAspectRatio`; don't force it into a 24x24 box.
- A raster or gradient approximated instead of the real fill: a solid colour or a two-stop gradient
  standing in for an image fill or a gradient mesh. Download the real asset or reproduce the exact
  stops, angles and positions from the node; never eyeball a fill.
- Decorative illustration geometry guessed instead of measured. Inside a mockup, a fixed-width
  element is fixed, not `fit-content`. Positions and internal spacing (loading dots, row pitch, the
  clip point where a card runs past the frame) come from the node's coordinates or a pixel-measured
  pass over the design render. A uniform flex `gap` is not a substitute for per-element spacing.
- Avatar initials that include symbols, or a bad generated avatar. Sanitize to alphanumerics, in one
  shared helper.
- Colours hard-coded instead of tokens; radius or type off the scale; the wrong font family for the
  content (code in mono); a weight too heavy (700 for 500). Grep for raw hex and off-scale values as
  a gate.

## D. Motion, transitions and interaction (the largest under-covered area)
- A transition or animation that is not in the design (a nav fade that should be a hard switch).
- Animations sibling pages have are missing on the new page (scroll-in reveals and the like).
- The same interaction implemented differently across pages (hover-zoom on one page's cards, absent
  on the equivalent tiles elsewhere).
- Motion built without inspecting the design prototype. Read the prototype's reactions and motion
  specs; never guess easing, duration or scale.
- A count-up or number animation not applied to every instance site-wide.
- `prefers-reduced-motion` not honoured on every animation.
- Graph or edge rendering that doesn't match the intended behaviour (routing, convergence, default
  framing). Study the reference implementation.

## E. Functional wiring (looks right, doesn't work)
- A dead control (filter, pagination, a link, an external-link icon with no data behind it). Click,
  keyboard and empty-data test every control; hide affordances that have no data.
- The wrong interaction model against the design (an invented pill where the design fills the
  search box). Build the exact designed interaction.
- Incomplete search, sort, relevance or empty state; a partial click target.
- A menu that opens in a clipped direction. Place it with the viewport in mind.
- Pagination that doesn't scroll to the top of the results.
- Modal close conditions or persistence toggles unspecified or unwired.
- Data from a baked snapshot instead of the live source. Wire it live; a fixture is an explicit
  fallback; surface the count delta.
- A copy-to-clipboard command that is invalid for the shell it names (`&&` in older PowerShell).
- Build or config errors shipped. The build and the check command run clean on a fresh build before
  "done".

## F. Copy and content
- Placeholder or lorem in a "finished" build.
- Designed styling applied to authored markdown (step circles on a README's numbered list).
- Wrong auto-hyperlinking.
- Empty-state copy, heading or label not verbatim to the design.
- AI-sounding style in copy and in code comments. Plain prose; comments only carry a correction, a
  decision or an action.

## G. Accessibility
- An invisible or clipped keyboard focus ring. A visible, unclipped ring on every focusable element,
  tested on light and dark backgrounds (a dark ring on a dark photo reads "present" to a naive check
  and is invisible).
- Broken tab order or focus management (a hidden menu still tabbable; scroll jank on focus).
- Colour-only status (WCAG 1.4.1). Add an icon or text.
- Contrast failures. Measure all text and badges in both themes; enforce AA.
- Missing ARIA, alt, table semantics, `aria-live` for dynamic counts, captions for video.

## H. Cross-page consistency
- The same section built differently on different pages. Compare its markup and computed styles
  across pages; the majority is the convention, the minority is the finding.
- A reusable component re-implemented per page. Same check. Identify shared shells before building.
- Nav inconsistency (light or dark, an unspecified divider, weight).
- Values that should match differ across pages (stat numbers, per-type colours). Reconcile them, and
  document the intentional deltas.

## I. Verification, measurement and self-review
- Claimed done, pixel-perfect or validated when it wasn't. "Done" is a measured audit, not a claim:
  an independent mechanical QA, bounded at 3 rounds.
- Verified against the design only, never against the previous live page. For a rebuild, diff both,
  at every breakpoint.
- Relied on screenshots instead of the code, the DOM and the design file. Pull values from the
  design; measure the DOM; screenshots confirm, they don't measure.
- Didn't inspect component variants and states. List every variant and state in the design and check
  each.
- Ran auth-gated checks before the human signed in. Confirm authentication first.
- Didn't open a ticket's attachments or screenshots before acting.
- A shallow audit (fix one, miss ten). Fan out one agent per surface; don't stop at the first
  defect.
- A fix broke another component. Re-run the functional checks after any change.
- **Checked a proxy, not the claim.** An `href` in a data array against the rendered `<a>`; a grep
  hit in `src/` that was inside a comment; exit 0 on a truncated write; a crop of the image file
  against the pixels under the glyphs. The check ran, passed, and measured the wrong thing. Cite only
  the artifact the person touches (`verification.md` §6, the proxy rule).
- **Verified an "everywhere" claim by inspection.** "Across the site" or "every X" cannot be sampled.
  Write the enumerating check first, then edit.
- **Acted on a paraphrase of a person's words.** A reply read without its parent; a designer's one
  stated exception dropped in the retelling. Quote verbatim; fetch the thread before acting.
- A silent trade-off between the design and site consistency. Record every deliberate deviation, from
  the design or from consistency, as a note with its reasoning.

## J. Process, tracking and handoff
- A ticket updated by comment instead of editing it; work items without links; the wrong item type
  (a defect not filed as a bug under its parent); duplicate or mis-ordered tickets.
- A drip of tracker or design comments instead of one clean close-out. Consolidate or delete the
  interim ones so the result reads as one shot.
- Corrections posted as thread replies instead of replacing the wrong comment cleanly.
- Design comments mis-anchored, unattached, or missing the link or screenshot needed to act;
  low-value comments that restate the reviewer's point.
- Built the data adapter before the designs were final; put work in progress on shared surfaces.

## K. Shortcuts and altitude
- **Dressing a shortcut up as "robust" or "pixel-perfect"**, such as a raster PNG of a UI
  illustration. A picture of UI is not UI: rebuild decorative illustrations in CSS or SVG (raster only
  for real photos). Name the trade-off honestly.
- **"Flagging, not fixing" to avoid effort.** If a fix path is known, even if it is more work, do it;
  flag only a genuine design or product decision or a true blocker.
- Over-applying a research pass; taking instructions too literally; simulating instead of running the
  tool; doing only what was literally asked instead of owning the outcome; not using agents for
  coverage.
- Model-quality regression. High-fidelity build and QA run on the stronger model; re-verify anything
  produced on a weaker one.

## L. Root-cause mechanisms
The symptoms above, turned into named causes, plus a few classes the groups under-cover.
- **Check against the entire live dataset, never a sample.** Treat any sample-derived "always" or
  "never" as a hypothesis; a mockup's example values may be real data or placeholder, so check which.
  Scan all N before building or removing a data-driven feature or filter.
- **A filter or label must actually discriminate the data.** A field with one value across every
  record makes its whole filter dead weight; check that it partitions the set before shipping. The
  label says what the data discriminates.
- **Stress the design against the real data's size and range.** A one-up carousel is unusable at two
  dozen rows; orthogonal edges tangle at high fan-in, where curves read better. Ship the scalable
  variant and flag the deviation.
- **A bug in a duplicated primitive means every call site is wrong.** Grep them all and consolidate to
  one helper or component; don't patch one site.
- **The same string, mark or interaction renders identically across tabs and breakpoints.** A mono
  identifier set sans-serif in another tab, or a filled arrow on desktop and an outline arrow on the
  phone for the same link, is a defect.
- **Layout root causes (name the cause, not the symptom):** a `nowrap` flex item in a flex column
  doesn't cap itself, so it needs `max-width: 100%`; an auto cross-axis margin overrides
  `align-items: stretch`, so guard with `width: 100%; min-width: 0`; `flex: 1` on an `<a>` balloons
  its focus and hit area.
- **Accessibility specifics:** a hidden-but-focusable subtree needs `inert` (not just `aria-hidden`
  and opacity), on desktop and phone; programmatic focus doesn't fire `:focus-visible`, so ring on
  `:focus` too; keyboard focus on a transform-panned canvas scrolls the document, so freeze page
  scroll on Tab and pan internally; close overlays when a breakpoint is crossed.
- **Hover and click differ on purpose, and two controls never do the same thing.** Hover is a
  transient peek, click is a pinned path; a Fit button wired to Reset reads as a dead control.
- **Study the reference implementation's actual client code** when mirroring or redesigning an
  existing app, and pick the option that is legible at real scale.
- **Value-preserving tokenization.** Swap a literal for a token only when the value already equals
  the token; never add a property that wasn't there (zero visual change, nothing a reviewer has to
  ask about). Token reconciliation runs both ways, and accessibility-corrected values go back to the
  design file.
- **Third-party markdown rendering:** rewrite relative links and images to absolute source URLs,
  keep and dedupe heading ids for the table of contents, and add a check that fails on any relative
  href.
- **Re-verify "already fine" claims.** A prior audit note can be the very artifact the next bug has
  to fix. A scoped-out item can be wrong.
- **Prove the fix in the built output with a before and after count.** "0 of 367 avatars non-letter,
  was 16". A structural claim needs a denominator.
