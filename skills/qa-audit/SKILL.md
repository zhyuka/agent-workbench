---
name: qa-audit
description: "Adversarial, measurement-driven QA pass on a built web page before it ships: an independent gate, not the builder's self-check. Use after a page is built or changed and 'looks done', for 'test everything', or before a push. Report-only: it finds and reports defects and never edits the build. Tool-agnostic; the recipes use Playwright against a fresh production build."
---

# QA audit: prove the page is right, don't eyeball it

QA is a different job from building: adversarial, exhaustive, driven by measurement, and run by
someone other than the author, because glancing at scaled screenshots cannot see a 1px stray
divider, a 12-vs-14px badge or a bar baked into an image file. The pass is done when every section
in scope has a measured verdict and the findings list is reported.

- **Measure, don't look.** Every visual claim is backed by a number and the command that produced it.
- **Report-only.** This pass never edits the build. It emits defects; the fix goes back to whoever
  builds, then you re-run QA. A gate that also patches is not a gate.

This file owns the procedure. How to look, and where looking fails, is `references/verification.md`;
what to look for is `references/bug-taxonomy.md` (groups A-L); what each check proves, with a recipe
for it, is `references/harness.md`. The expected values come from the design source. When you turn
a recipe into a script, the script owns what a defect is: when a check's threshold or definition
changes, edit the script, and when this file and the script disagree, the script wins.

## The harness

This skill ships no harness. It is a method; the recipes in `references/harness.md` use Playwright
(Python) because it drives a real browser with true device emulation rather than a window size
(`references/verification.md` §7), but any tool that measures the live DOM and the rendered pixels
will do.

**Point it at a fresh production build, not the dev server.** Build, serve the output on a port of
its own, and audit that URL. A long-running dev server can serve stale CSS with fresh HTML. Never
kill a dev server someone else is using. **Always pass the URL** to every probe: a default URL in a
script audits whatever page that is, not the one you changed.

Every probe follows one exit convention: 0 clean, 1 findings, 2 could not run. It asserts its own
preconditions (the page loaded, the viewport is the one asked for, the selector matched at least one
element) and aborts with 2 rather than pass. Read the output as well as the code: a probe whose
abort exits 1 reads as a defect, and one that did not run is not clean.

### The ratchet: what is new on this tree

On a site with known, accepted defects, keep a baseline file of the findings each proven check
already has, keyed by the check and route. Run the checks against it first, for the changed page's
routes, and for every route when the change touches a global stylesheet, the layout template or a
shared component.

- A finding not in the baseline is new on this tree: a BLOCKER.
- A baselined finding was already there when the baseline was taken. It is still a defect.
- A route with no baseline (a new page) is NOT COMPARED, so every finding there is new.
- Keep each line's shape. The key is the check and its arguments without host, port or output path,
  so another width or flag reads NOT COMPARED.
- A baselined finding this tree no longer has is gone. QA never writes the baseline. When the last
  round passes, the builder re-runs the same lines to refresh it and commits it. Its diff may only
  remove findings; one added there is a new defect being hidden, so fix it or ask.

## Procedure

1. **Ground on the design per section.** Start from the page's build plan when there is one: its
   required sections, states and rulings. With no plan, pull the expected values (sizes, spacing,
   type, colours, copy, every state and annotation) from the design source, taking both frames,
   desktop and phone, from the section: the Figma MCP cannot reach a phone frame from its desktop
   frame's node.
2. **Run the measurements** (`references/harness.md`) on the built page: overflow at every width the
   design specifies, images, assets, in-page links, geometry, corners, and the design's numbers as
   assertions. Record the numbers. A corner flag is a question: open its crop before calling it a
   leak. Run assets and in-page links on every page, not only the one you changed. Then measure
   contrast under the glyphs of the text over photos or gradients; a builder's proof covers only the
   selectors it named.
3. **Diff every section** at each design width against its design node: copy verbatim, type sizes,
   colours, chip and badge sizing, active versus dimmed states, arrow presence and type, dividers,
   equal heights, full-bleed images. No section passes without an explicit verdict. Each section the
   plan requires needs its own recorded verdict taken on the build you audit; one with none is a
   finding. Where the builder recorded a numeric verdict (a saved diff with its exit codes and worst
   delta), read it rather than re-deriving a weaker one, and check the section was not edited since.
4. **Run the four blind-spot checks** (`references/verification.md` §2); without them, measurement
   produces a confident false pass.
5. **Keyboard and accessibility**: `references/verification.md` §5.
6. **Build hygiene and the gate**: the served files match the source, no orphaned static files, and
   the project's build and check commands exit 0 (run them in the background and read the tail):
   `references/verification.md` §6.
7. **Report** (below). Run the `recall` skill on each finding's selector and design node id first:
   one already ruled, ticketed or withdrawn is not new. List the notable defects for the caller to
   file in the tracker, hand fixes back to the builder, and re-run QA after the fix. Stop at zero
   in-scope blockers or after round 3, whichever comes first, with the project's check command at
   exit 0; polish and what is left go to the tracker (`references/verification.md` §4). Findings are
   re-derived each round, never kept in a hand-maintained list.

## The checklist

Run the built page against every group of `references/bug-taxonomy.md`, top to bottom. These are the
groups this pass most often misses, so they get named here; the entries live in the taxonomy.

- **Motion and interaction parity (group D, the most-missed).** Stray motion not in the prototype,
  missing motion that sibling pages have, the same element behaving the same everywhere, and
  `prefers-reduced-motion` honoured everywhere.
- **Cross-page consistency (group H).** For each repeated class, compare its markup and computed
  styles across every page that uses it; the majority is the convention and the minority is the
  finding. Then diff spacing, heights, hover and tokens on what survives, and record intentional
  deltas.
- **Functional wiring (group E).** Click, keyboard-activate and empty-data every control; a filter,
  link or pager that looks right but does nothing is a defect. Gate affordances with no data, and
  confirm data comes from the live source, not a stale fixture.
- **Design tokens (group C) and accessibility depth (group G).** No raw hex, off-scale radius or
  off-scale type; grep for them. Code text uses the mono token. No colour-only status, AA contrast in
  both themes, and ARIA, alt text, `aria-live` and captions.
- **Component states and annotations.** Every state the design defines is implemented, and every
  annotation honoured. A hover, selected or disabled state in the design that is missing or
  approximated is a defect, and an annotation with no build behaviour or recorded decision is a miss.
- **A rebuild of an existing page (group I).** Diff against both the design and the previous live
  page, at every breakpoint; the old live page is a baseline too.

## Judging a finding

- Calibrate before trusting a check: run it on one planted bad case and one known good case first;
  an uncalibrated probe reports a false defect, not a blank. A check that passes proves the check
  passed, not the claim: a bounding box is not glyph pixels, and populated is not correct.
- Strip zero-width characters before comparing copy. Design copy can end in U+200B, so a MISSING
  whose two strings look the same may be only that.
- Matching the spec proves nothing about the words. List every readable string (text, title, meta,
  alt, aria, screen-reader-only) build versus spec, grep the build for other products' names and
  placeholders, and report each copy error in the spec as a decision for the user.
- A control inside an illustrative mockup is a depiction, not a defect; its hover is intended.

## Output cap

At most 15 defects per run, blockers first, ranked by what a user sees; over 15, the top 15 and a
count by category. One line per defect: page, element or `file:line`, the measured value against the
expected design node or value, and the command that measured it. Zero findings is one sentence.

- **POLISH**: a difference within the tolerances (a gap 2px or less off, a tint under tolerance), on
  a section whose recorded verdict, if any, passes and whose content, type, colour, contrast, images
  and behaviour are right.
- **BLOCKER**: everything else in scope. Wrong or missing copy, image or section; a state or
  interaction that is missing or broken; text under its contrast floor; overflow, clipping or a
  layout break at a width the design specifies; a size or type miss; anything that fails the
  section's recorded verdict. When unsure, it is a blocker. Polish never reopens a page after round 1.

Run this skill in a fresh agent that did not write the page and has no edit rights (the `page-qa`
agent). The builder's self-checks are a separate step; unless the project's check command runs these
probes, this pass is the only place they run.
