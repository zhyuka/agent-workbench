---
name: qa-audit
description: "Adversarial, measurement-driven QA pass on a built web page before it ships: an independent gate, not the builder's self-check. Use after a page is built or changed and 'looks done', for 'test everything', or before a push. Report-only: it finds and reports defects and never edits the build. Tool-agnostic; the recipes use Playwright against a fresh production build."
---

# QA audit: prove the page is right, don't eyeball it

The builder keeps declaring "matches the design" after glancing at scaled screenshots, and glancing
cannot see a 1px stray divider, a 12-vs-14px badge, a dark bar baked into an image *file*, or
whether a "mobile overflow" is real or just a cropped screenshot. **QA is a different job from
building: adversarial, exhaustive, and driven by measurement, run by someone other than the
author.** Its output is a findings list, never a code edit.

Two hard rules up front:
- **Measure, don't look.** Every visual claim is backed by a number.
- **Report-only.** This pass never edits the build. It emits defects; the fix goes back to whoever
  builds, then QA runs again.

This skill ships no harness. It is a method; the recipes below use Playwright (Python) because it
drives a real browser with true device emulation, but any tool that measures the live DOM and the
rendered pixels will do. WHAT to look for is `references/bug-taxonomy.md` (groups A-L). HOW to look,
and where looking fails, is `references/verification.md`.

## Set up

- **Audit a fresh production build, not the dev server.** Build, serve the output on a port of its
  own, and audit that URL. A long-running dev server can serve stale CSS with fresh HTML. Never kill
  a dev server someone else is using.
- **Always pass the URL** to every probe. A default URL in a script audits whatever page that is,
  not the one you changed.
- **True device emulation, not a window size.** In Playwright, a context per width:
  `browser.new_context(viewport={"width": w, "height": 900}, device_scale_factor=2,
  is_mobile=w < 768, has_touch=w < 768)`. A bare window size can lay the page out at a different,
  wider viewport, which makes a clean page look clipped on a phone.
- **Every probe you write follows one exit convention: 0 clean, 1 findings, 2 could not run.** It
  asserts its own preconditions (the page loaded, the viewport is the one asked for, the selector
  matched at least one element) and aborts with 2 rather than pass. Make the abort distinct from a
  finding; a probe whose abort exits 1 reads as a defect.
- **Calibrate each probe** on one planted bad case and one known good case before trusting it.

## The measurements

| check | how (Playwright recipe) | what it proves |
|---|---|---|
| overflow | per width: `document.documentElement.scrollWidth` vs `clientWidth`, plus every element whose `getBoundingClientRect().right` passes the viewport | real sideways scroll has offenders AND `scrollWidth > viewport`; with no offenders it was a screenshot artifact. A heading past the viewport fails even inside an `overflow: hidden` ancestor |
| geometry | `getBoundingClientRect()` and `getComputedStyle()` (font-size, padding, border, radius) per selector | equal heights, stray borders, badge sizing, spacing against the design's numbers |
| screenshots | `page.screenshot(full_page=True)` or `locator.screenshot()` per section, per width | a section-by-section diff against the design node's own export |
| images in the file | scan every committed image with PIL: rows or columns at an edge that are uniform and dark or light | a bar or letterbox baked into the FILE; element geometry reads gap 0 because the `<img>` fills its box |
| assets | `page.on("response")` for status >= 400, then every `img` with `naturalWidth == 0` and every CSS `background-image` URL fetched, after scrolling the page to trigger lazy loads | a missing file passes the build; it only 404s in the browser. It proves an image loads, not that it is the right image |
| in-page links | click each in-page scroll link and assert its target's top lands just under the sticky header | anchors guessed at build time and never clicked |
| keyboard | press Tab through the page, read `document.activeElement`, `matches(":focus-visible")` and the ring's computed outline or box-shadow at each stop; open each collapsed menu and unselected tab and walk what opened | a missing ring, a ring clipped by an `overflow: hidden` ancestor, a ring invisible against its own band, a hidden menu that is still tabbable |
| states | force the state over CDP (`CSS.forcePseudoState` with `["hover"]`, Chromium only), screenshot rest and forced, diff | every hover, focus, active and selected state against the design's variant |
| diff | mean absolute difference AND the worst row band, at native resolution, on two crops of the same section | a 1px hairline barely moves the mean but spikes one row; a mean-only diff calls it a match |
| copy | the rendered text per element against the design's text, with zero-width characters stripped and whitespace folded | wrong, missing or placeholder copy |
| contrast | WCAG ratio of each text leaf's colour against the pixels under its glyph boxes, both themes | text under its AA floor, especially over photos and gradients |

A forced state needs calibrating: a synthetic mouse move can deliver `:hover` in one headless mode
and nothing in another, and a probe that measures nothing reports "no change on hover", a false
defect. Force the element and every ancestor, park the mouse off the page, and check the probe on an
element whose hover is known to work first.

## Procedure

1. **Ground on the design per section.** Pull the expected values (sizes, spacing, type, colours,
   copy, every state and annotation) from the design source for both the desktop and the phone
   frame. Without them "looks right" creeps back in.
2. **Run the measurements** on the built page at every width the design specifies: overflow,
   images, assets, in-page links, geometry against the pulled numbers. Record the numbers. **Run
   assets and in-page links on every page, not just the one you changed.**
3. **Diff every section** at each design width against its design node: copy verbatim, type sizes,
   colours, chip and badge sizing, active-vs-dimmed states, arrow presence and type, dividers, equal
   heights, image full-bleed. No section is "passed" without an explicit verdict. If you want a
   designer's read of each section, spawn one `section-reviewer` agent per section and width with
   the two pictures, and mark each finding as caught by both, by the reviewer only, or by
   measurement only.
4. **Run the blind-spot checks** (`references/verification.md` §2): motion quality, illustration
   fidelity at native resolution, undesigned affordances, and container changes that silently
   disable a child. Measurement without them produces a confident false pass; use the page as a
   person would, too.
5. **Keyboard and accessibility**: `references/verification.md` §5.
6. **Build hygiene**: the served files match the source, no orphaned assets, the project's build and
   check commands exit 0 (`references/verification.md` §6).
7. **Report** (format below). Run the `recall` skill on each finding's selector and design node id
   first: one already ruled, ticketed or withdrawn is not new. Hand fixes back to the builder and
   **re-run QA after the fix**. Stop at zero in-scope blockers or after round 3, whichever comes
   first; what is left goes to the tracker (`references/verification.md` §4).

### A baseline of known findings

On a site with known, accepted defects, keep a baseline file of the findings each proven check
already has, keyed by the check and route (without host or port). Run the checks against it: a
finding not in the baseline is new on this tree and a blocker; a baselined finding is still a
defect; a route with no baseline is NOT COMPARED, so every finding there is new. QA never writes the
baseline. When the last round passes, the builder refreshes it, and its diff may only remove
findings; one added there is a new defect being hidden.

## The checklist

`references/bug-taxonomy.md` is the full list of defect classes. **Run the built page against all of
it, every group, top to bottom.** These groups are the ones most often missed:

- **Motion and interaction parity (group D).** Stray motion not in the prototype; missing motion
  that sibling pages have; the same element behaving differently across pages;
  `prefers-reduced-motion` honoured everywhere.
- **Cross-page consistency (group H).** For each repeated class, compare its markup and computed
  styles across every page that uses it; the majority is the convention and the minority is the
  finding. Record intentional deltas.
- **Functional wiring (group E).** Click, keyboard-activate and empty-data every control; a filter,
  link or pager that looks right but does nothing is a defect. Confirm data comes from the live
  source, not a stale fixture.
- **Design tokens (group C) and accessibility depth (group G).** No raw hex, off-scale radius or
  off-scale type; grep for them. Code text uses the mono token. No colour-only status, AA contrast in
  both themes, ARIA, alt text, `aria-live`, captions.
- **Component states and annotations.** Every state the design defines is implemented, and every
  annotation is honoured or has a recorded decision.
- **A rebuild of an existing page (group I).** Diff against both the design and the previous live
  page, at every breakpoint.

## How to invoke

```
/qa-audit <page-or-url>
/qa-audit                      # the page most recently built
```

Run it in a fresh agent that did not write the page and has no edit rights (the `page-qa` agent).

## Rules

- **Report only. Never edit the build.** A gate that also patches is not a gate.
- **Measure, never eyeball.** Every finding cites a number and the command that produced it.
- **Calibrate before trusting a check.** Run it against one case you know is positive first; an
  uncalibrated probe reports a false defect, not a blank.
- **Strip zero-width characters before comparing copy.** Design copy can end in U+200B, so a
  MISSING whose two strings look the same may be only that.
- **A check that passes proves the check passed, not the claim.** A bounding box is not glyph
  pixels; populated is not correct.
- **A control inside an illustrative mockup is a depiction, not a defect.** Its hover is intended.
- **Park, do not guess.** A question only the user can answer goes to the `open-question` skill.

## Output cap

**Hard ceiling: 15 defects per run.**

- Tag every defect BLOCKER or POLISH, blockers first.
  - **POLISH**: a difference within the tolerances (a gap 2px or less off, a tint under tolerance),
    on a section whose content, type, colour, contrast, images and behaviour are right.
  - **BLOCKER**: everything else in scope. Wrong or missing copy, image or section; a state or
    interaction that is missing or broken; text under its contrast floor; overflow, clipping or a
    layout break at a width the design specifies; a size or type miss.
  - When unsure, it is a blocker. Polish never reopens a page after round 1.
- Rank by user-visible severity. Over 15, report the top 15 plus a count by category.
- **One line per defect**: page, element (`file:line` when known), measured value vs expected (the
  design node and value), the command that measured it.
- Report zero findings as a single sentence. A clean page does not need a paragraph.

## Findings are re-derived, never kept

Do not hand-maintain a list of QA findings. Re-run the measurements each round. A finding that
outlasts the round goes to the tracker.

When you turn a recipe into a script, the script owns what a defect is: put the thresholds in the
code, keep it read-only, and bring this file into line when they change.
