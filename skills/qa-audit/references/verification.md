# Verification: how to look, and where looking fails

`SKILL.md` owns the procedure. `bug-taxonomy.md` is *what* to look for. This file is *how* to look,
and where measurement gives a confident wrong answer.

## 1. The axioms

**Measure, don't look.** Every visual claim is backed by a number: a bounding box, a `scrollWidth`,
an image edge scan, a computed style. Glancing at a scaled screenshot cannot see a 1px stray divider,
a 12-vs-14px badge, or a dark bar baked into an image file.

**The generator is not the critic.** The builder never grades its own work. The context that built
it wants to believe it is done, so it under-runs validation. The critic is a separate agent with a
fresh context and an adversarial brief.

**A state change has two endpoints and a direction.** Checking the rest colour against the design,
then the hover colour against the design, can pass twice while the pair is wrong: a rest-to-hover
change of 1.06:1 is invisible (about 1.1:1 is the floor of what people notice), and a design that
specifies a brighten is broken by a dim of the same size. Compare each hover with its own base
selector, check the direction as well as the size, and calibrate on four fixtures first: a brighten,
a dim, an imperceptible change, and a background set through a `var()` token.

**Don't trust a state check you just wrote.** Typical first-version bugs, each giving a confident
wrong answer: comparing against the max rest value picks up the mobile rest rule; searching a rule
body for `rgba(...)` matches `color:` instead of `background:`; a per-file max compares unrelated
components in one file; a non-UTF-8 console dies on a non-ASCII comment.

**In a browser, force the state.** A synthetic mouse move can deliver `:hover` in one headless mode
and nothing in another, and a probe that measures nothing reports "no change on hover", which reads
as a defect. Use `CSS.forcePseudoState` over CDP on the element and every ancestor, park the real
mouse off the page, assert afterwards that `document.querySelectorAll(':hover')` is empty, and
calibrate on an element whose hover is known to work.

**Every failure is a cheap check that matched something next to the claim.** A regex matched the
wrong property; a pattern matched two different text slots; a grep for a component name matched the
name inside a comment in another file. Before trusting a check, state what it matches and confirm it
is the same set as what you are claiming.

## 2. Where measurement is blind

Run these four every time. Rounds of measurement that verified geometry, contrast, overflow and
focus to four decimals have returned SHIP on a page where two minutes of actually using it found
five bugs.

**Motion quality: compare rest against the hover END state.** Capture after a wait longer than the
site's transitions, then diff rest against hover. Known causes worth recognising: an element that
collapses to 0 before growing is transitioning `flex-grow` and `flex-basis` together (animate one
property); text re-wrapping on every frame is a fluid-width text block (pin it to its final width and
cross-fade). Check that the CSS rule actually lands: a `:has()` sibling rule can out-specify a plain
`:hover` rule, so measure the hovered element mid-hover. A hover that inverts polarity (dark on light
to light on dark) must not cross-fade text and fill over equal durations, or midway both are the same
grey; sample contrast every 10-20 ms both ways.

**A browser launched with reduced motion forced reads every transition as instant**, so every hover
looks dead. Measure motion in a browser launched without it.

**`transform` does not move a non-replaced inline box, yet `getComputedStyle` still reports the
matrix.** Measure motion as `getBoundingClientRect` deltas.

**Illustration fidelity: diff at native resolution.** A scaled full-page shot hides a CSS-approximated
avatar, a `color: transparent` that hid an initial, and a 0.5px hairline that snapped to a bright 1px
line. Gate on the mean OR the worst row band: a hairline barely moves the mean but spikes one row.
Keep the thresholds in the code, calibrated against measured noise. Two preconditions a diff can't
check for you: crop both sides to the same section, and confirm the page renders deterministically
(capture it twice and diff it against itself first if it animates).

**Undesigned affordances: find the states nobody designed.** A global `img:hover { opacity: .7 }` can
fade logos on pages nobody designed it for. List every `:hover`, `:focus-visible`, `:active`,
`transition` and `animation` rule and give each a "which design variant is this?" verdict; a rule
with none is an open finding. Check for designed states the build dropped, too.

**A container-level change that silently disables a child.** Invisible to a screenshot and to a
mean-pixel diff.
- `opacity` below 1 on an ancestor creates a compositing group, so a descendant's `backdrop-filter`
  stops sampling its backdrop and a frosted pill goes see-through. A design tool drawing a hover row
  at 0.9 opacity, ported literally, is a breakage, not a dim. Seen as a region mean moving 35.6 to
  34.6 while the pill visibly vanished. If the rule also exists as a phone rest state, the pill is
  broken permanently there. Grep the BUILT CSS after the fix.
- A stretched card link (`a::after { position: absolute; inset: 0 }`) paints over inner elements, so
  an inner `:hover` never resolves while the cursor still reads `pointer`. Drive the child from the
  card's hover.
- Whenever a container gains `opacity`, `filter`, `transform` or a stretched `::after`, check what it
  contains: walk the live tree from every `backdrop-filter` element up its ancestors for a computed
  opacity below 1, and run an `elementFromPoint` occlusion test at each one's centre.

**Asset currency.** The designer keeps working. Re-pull the design before auditing fidelity.

**Half-pixel images.** An `img` laid out on a half CSS pixel (flex-centring an odd-sized mark) paints
one device pixel off its box. Move it to a whole pixel with `position: relative` and `left`/`top`; a
`translate` moves box and paint together.

## 3. A verifier must be trustworthy

A checker that can emit a false pass is worse than none: it launders the defect.

- **Assert your own preconditions and abort rather than pass.** A raced viewport once measured 980
  wide and printed OK; an error page once printed OVERFLOW. Re-navigate until the page is healthy, or
  abort.
- **A check that cannot catch its own defect is theatre.** A diff first reported MATCH on the very
  hairline it was written to catch. A near-duplicate check built on Python's difflib was useless
  until `autojunk=False`.
- **A selector matching zero elements is a FAIL, not a pass.**
- **A filter on the wrong field silently drops most of its input.** An extractor that read only text
  nodes lost every sticky, shape and table cell of a board while reporting a clean run. Check what
  your filter excludes before trusting a count.
- **A gate lies three ways: a silent zero, a false positive, an undercount.** A list that dedupes by
  kind and text says at least one of each kind, never how many. Calibrate on a known positive and a
  known negative before trusting a zero; when widening a pattern the negative matters more.
- **Silent zeros worth knowing.** Pillow's `getbbox()` on an RGBA difference reads alpha only, so two
  opaque images that differ print `None`: convert both to RGB first. Stripped HTML still holds
  entities: `html.unescape()` after stripping. zsh does not word-split an unquoted `$var`: loop over
  an array.
- **A surprising number is a hypothesis.** Re-run a measurement taken while the machine was busy, and
  when a count jumps between two reads, suspect the method before the delta.

## 4. Coverage rules

**An exhaustive matrix, not sampling.** Every section, every breakpoint the design specifies, every
repeated component: one explicit verdict each. A fixed-geometry composition (absolute positions, a
mockup) can be right at both design widths and wrong between them, so also measure a few widths in
between and check the invariant; a break found only there is out of scope for the loop below and goes
to the tracker.

**Verify the whole affected class in the BUILT output.** When a change touches a shared path (a
markdown renderer, a global CSS rule, a layout template, a shared helper) it affects every page built
from it. Grep all of the build output for the changed marker AND count the denominator: "0 of 87
pages still show it". `tools/class_check.py` from this plugin prints that pair and refuses one
number without the other. For future inputs the guarantee is structural (the shared code can no
longer emit the old form); say so, and never imply you tested pages that don't exist yet.

**An exemption from a class fix is a measurement, not a label.** Write the member's design node and
its measured box at the design width beside the rule it is exempt from. An exemption labelled "mixed"
with no numbers is how a wrong width survives for weeks.

**A shared visual treatment is checked on the class's extremes.** A scrim that is invisible on busy
photos reads as an obvious rectangle on a smooth gradient. List the class, pick its extremes (darkest
and brightest, smoothest and busiest, shortest and longest copy), check those, then apply to the
whole class or state why a member is exempt.

**Bound the loop.** Stop on a zero-blocker pass or the 3rd round, whichever comes first. An
adversarial auditor pointed at undesigned breakpoints always finds something. Round 1 is the full
matrix, round 2 fixes and regressions, round 3 the remainder; anything still open becomes a ticket. A
finding is in scope only if it is on the page you were asked to build and at a breakpoint the design
specifies. From round 2 on only a blocker reopens the page; polish goes to the tracker in one batch.

**Re-measure after a fix.** Never close a defect off the code diff alone.

## 5. Keyboard and focus

Screenshots don't catch these.

- Tab through every interactive element; every one shows a visible focus ring.
- `overflow: hidden` silently clips outset focus rings. After touching such a container, Tab through
  everything inside it.
- Every `<dialog>` traps Tab and Shift-Tab within its focusable children and returns focus to the
  trigger on close. An escaping Tab is a WCAG 2.1.2 failure.
- The ring is visible against the band it sits on. A dark ring on a dark photo reads "present" to a
  naive check. Score a ring against what is actually painted behind it, not against black when no
  ancestor sets a background.
- Fix every instance of the shared pattern, not just the flagged one.
- A `button` selector also matches `tabindex="-1"` elements: count stops with `el.tabIndex >= 0`, and
  treat a real Tab walk as ground truth.

## 6. Build output vs source

**The proxy rule.** Almost every miss a person had to catch HAD a check; it ran, passed, and measured
something that was not the claim:

| claimed | checked | why it passed while wrong |
|---|---|---|
| "the chip links to the page" | `href` set in the component's data | the template hard-coded a `<div>` and ignored `href` |
| "the route is wired" | grep of the source found the route | the hit was inside an HTML comment |
| "build is clean" | a build log | the log predated the edits; the output was stale |
| "contrast passes" | worst pixel in a region of the image file | no glyph sits there; the page crops elsewhere |
| "the field saved" | the CLI exited 0 | the shell ate everything after `&`; the value was truncated |

The claim is about the end state a person experiences; the evidence was an intermediate artifact
just edited. Before asserting, name the artifact the person touches and cite only that: the built
output or the live render, never the source; the value read back, never the exit code; the composited
pixels under the real glyph boxes, never a region of the source image; the design node's own render,
never a remembered impression; a comment's full thread, never a paraphrase of one reply.

**"Everywhere" is a tool, not an inspection.** "Across the site" or "every X" cannot be verified by
looking; write the enumerating check first, before the edits.

**A gate that cries wolf gets ignored,** so fix its own false positives. When a check legitimately
should not apply, declare the exemption in the report rather than quietly omitting a filter.

**Two readings of an instruction: do the reversible one.** "Go ahead" can mean "post it" or "draft
it"; you can do the other next turn, and you can't un-post.

**Source is not the build output.** Editing an image changes the source; the served file is stale
until a rebuild copies it. After any asset edit, rebuild, then grep the output for the change.

**A missing image file passes the build.** A static folder is served verbatim, so a renamed or
deleted asset only 404s in the browser. Loading proves an image loads, not that it is the right one;
a valid but wrong photo is caught only by the section diff against the design.

**An in-page scroll link is never tested by static QA.** Click each one and assert where it lands.

**A project's check command rarely catches layout overflow.** After any responsive CSS change, assert
`document.documentElement.scrollWidth <= window.innerWidth` at the phone width. The recurring cause is
a `nowrap` flex item with no width cap.

**Unreferenced files in the static folder still ship.** Grep the source for each asset; flag orphans.

**Minifiers rewrite media queries:** `(max-width: 767px)` can ship as `(width<=767px)`, so a string
count over built CSS reads 0. Parse the built CSS to count media-gated rules.

## 7. Harness gotchas

- **True device emulation, not a window size.** A window size can set the screenshot size while the
  page lays out at a different, wider viewport. Use Playwright's viewport and device options, or CDP
  `Emulation.setDeviceMetricsOverride`.
- **A real overflow has offenders AND `scrollWidth > viewport`.** If `scrollWidth == viewport` it was
  a screenshot artifact; say so rather than filing it.
- **Disabling the GPU drops paint** on translucent and `backdrop-filter` surfaces, so text over a
  dark translucent band can read as an empty box. Before "fixing" invisible text there, read
  `innerText` and the computed colour, and confirm the box auto-sized. Use a GPU-on capture for any
  visual judgement.
- **Other render artifacts not to mis-file:** `body { min-height: 100vh }` in an over-tall screenshot
  window pins the footer and shows a dark gap. An image source several times its rendered size can
  stall decoding and look like an element that doesn't render; compare the file's pixel size with its
  box first.
- **A strict Content-Security-Policy on the preview server blocks inline scripts**, so a JS finding
  from that build may be an artifact. Check which headers the preview serves.
- **Size the viewport to fit a section before an element screenshot.** A section taller than the
  viewport gets scrolled, and the sticky header covers its top.
- **Reuse one browser session** for all checks and kill the process tree on exit. A leaked headless
  browser can take down a dev server.
- **Clean up.** Repeated rounds leave orphaned Chromium profile folders in the temp dir and can fill
  the disk. Give each run its own temp folder (`TMPDIR`, `TEMP`, `TMP`) and delete it when the browser
  stops; delete leftovers by literal path, never by a glob.
