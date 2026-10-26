# qa-audit: what each check proves

This skill ships no harness script. Each row below is a check, a recipe for it in Playwright
(Python), and what it can prove; any tool that measures the live DOM and the rendered pixels will
do. Once you turn the recipes into a script, its `--help` is the current list and wins over this
page. What each check can and cannot prove, including where `assets` stops and what a diff gates on,
is in `verification.md`. This table describes the checks, not the design rules they enforce.

**True device emulation, not a window size.** One context per width:
`browser.new_context(viewport={"width": w, "height": 900}, device_scale_factor=2,
is_mobile=w < 768, has_touch=w < 768)`. A bare window size can lay the page out at a different,
wider viewport, which makes a clean page look clipped on a phone. Pass the widths explicitly rather
than relying on a default list in the code.

| check | how (Playwright recipe) | what it proves |
|---|---|---|
| overflow | per width: `document.documentElement.scrollWidth` vs `clientWidth`, plus every element whose `getBoundingClientRect().right` passes the viewport | real sideways scroll has offenders AND `scrollWidth > viewport`; with no offenders it was a screenshot artifact. A heading past the viewport fails even inside an `overflow: hidden` ancestor |
| geometry | `getBoundingClientRect()` and `getComputedStyle()` (font-size, padding, border, radius) per selector | equal heights, stray borders, badge sizing, a panel that jumps |
| assert | a spec file of the design's numbers per selector (count, equal heights, geometry, computed styles), each checked as an assertion | the values pulled from the design hold on the built page |
| overlay | an absolutely placed overlay's offset in the build against the design's child position minus its parent's | an overlay placed from absolute design coordinates that were never made relative to its parent |
| section insets | each section's content insets on the built page against its design page frame, at that frame's width, with the tolerance kept in the code | a side over tolerance: spacing that drifted from the design |
| screenshots | `page.screenshot(full_page=True)` or `locator.screenshot()` per section, per width | a section-by-section diff against the design node's own export |
| images in the file | scan every committed image with PIL: rows or columns at an edge that are uniform and dark or light | a bar or letterbox baked into the FILE; element geometry reads gap 0 because the `<img>` fills its box. The only check that finds a bar inside the file |
| assets | `page.on("response")` for status >= 400, then every `img` with `naturalWidth == 0` and every CSS `background-image` URL fetched, after scrolling the page to trigger lazy loads | a missing file passes the build; it only 404s in the browser. It proves an image loads, not that it is the right image |
| in-page links | click each in-page scroll link and assert its target's top lands just under the sticky header; a target with no heading is measured by its own top | anchors guessed at build time and never clicked |
| keyboard | press Tab through the page, read `document.activeElement`, `matches(":focus-visible")` and the ring's computed outline or box-shadow at each stop; open each collapsed menu and unselected tab and walk what opened. Refuse to score an element that did not take focus, or a ring over a photo. Then a second pass with motion on | a missing ring, a ring clipped by an `overflow: hidden` ancestor, a ring invisible against its own band, a hidden menu that is still tabbable; the second pass finds a stop drawn under half opacity, or still fading in, when it takes focus |
| states | force the state over CDP (`CSS.forcePseudoState` with `["hover"]`, Chromium only), screenshot rest and forced, diff | every hover, focus, active and selected state against the design's variant |
| affordances | list every `:hover`, `:focus-visible`, `:active`, `transition` and `animation` rule in the built CSS as a unit | each needs a "which design variant is this?" verdict (`verification.md` §2); one with none is an open finding |
| diff | mean absolute difference AND the worst row band, at native resolution, on two crops of the same section | a 1px hairline barely moves the mean but spikes one row; a mean-only diff calls it a match |
| corners | per width, each rounded box that clips children or paints an image: score the pixels on its corner arc against the blend of the colour just outside and just inside it, and save a crop of each flagged box | a box whose own fill leaks as a thin ring at the corners. A flag is a question, not a leak: content that runs into a corner flags on a clean build, so calibrate that one box with a planted leak, in both directions, before reporting it |
| copy | the rendered text per element against the design's text for the node it cites, with zero-width characters stripped and whitespace folded | wrong, missing or placeholder copy; a MISSING is a finding |
| contrast | WCAG ratio of each text leaf's colour against the pixels under its glyph boxes, at both viewports and in both themes; exit 2 when a surface could not be measured | text under its AA floor, especially over photos and gradients |

**Score a focus ring against what is painted behind it.** A probe that falls back to black when no
ancestor sets a background reports a visible ring on a page with a transparent `html` and `body` as
invisible.

**A forced state needs calibrating.** A synthetic mouse move can deliver `:hover` in one headless
mode and nothing in another, and a probe that measures nothing reports "no change on hover", a false
defect. Force the element and every ancestor, park the mouse off the page, and check the probe on an
element whose hover is known to work first.
