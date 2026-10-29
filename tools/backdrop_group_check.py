#!/usr/bin/env python
"""backdrop_group_check.py - find backdrop-filter elements killed by an ancestor compositing group.

WHY THIS EXISTS
  `opacity` below 1 on an ANCESTOR creates a new compositing group. A descendant carrying
  `backdrop-filter` can then no longer sample what is behind the group, so a frosted pill loses its
  fill and whatever sits behind shows straight through. A design tool may draw a hover state's tag
  row at opacity 0.9, and porting that literally to CSS is not a dim, it is a breakage.

  The same rule often ships twice: as a :hover rule, and again as a mobile rest-state rule, where
  the pill is see-through permanently on a phone rather than only while hovered.

  A MEAN-PIXEL CHECK DOES NOT CATCH IT: the region's mean colour barely moves while the pill
  visibly vanishes. Nor does a CSS grep, which cannot tell whether the opacity rule and the
  backdrop-filter rule land on the same DOM branch. This walks the live tree.

  Same family, also checked here: a stretched card link (`a::after {position:absolute; inset:0}`)
  painting over a backdrop-filter element, which silently stops that element's :hover resolving
  while the cursor still reads pointer. Both are container-level changes that disable a child,
  invisible to static checks and to screenshots.

  And the reverse: in a card made tappable by a stretched link, a plain child painted above the
  ::after takes the tap, so tapping it opens nothing. Reported as deadspot.

USAGE
  python tools/backdrop_group_check.py <url> [<url> ...] [--widths 390,768,1024,1440] [--json out.json]

  Each url is loaded at each width (viewport height 900) in headless Edge, after scrolling the page
  once to fire every [data-animate] reveal. --json writes the findings list.

EXIT
  0 clean, 1 findings, 2 could not run: no Playwright, no Edge, or any url x width that failed to
  load or answered HTTP 400 or above. A finding outranks a failed load, so 1 can still carry
  unverified pairs; the summary names how many.
"""
import argparse, json, os, sys

PROBE = r"""() => {
  const out = { grouped: [], occluded: [] };
  const all = Array.from(document.querySelectorAll('*'));
  const path = (el) => {
    const bits = [];
    for (let n = el; n && n.nodeType === 1 && bits.length < 5; n = n.parentElement) {
      bits.unshift(n.tagName.toLowerCase() + (n.className && typeof n.className === 'string'
        ? '.' + n.className.trim().split(/\s+/).slice(0, 2).join('.') : ''));
    }
    return bits.join(' > ');
  };
  // ★ SCROLL ONLY WHAT A VISITOR CAN. scrollIntoView also scrolls an overflow:hidden ancestor
  // whenever the element is cut by it and could fit inside it, and then probes a state the page
  // never shows. This centres it in each overflow:auto or scroll box, then in the window.
  const toView = (e) => {
    for (let a = e.parentElement; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
      const s = getComputedStyle(a);
      const y = /auto|scroll/.test(s.overflowY) && a.scrollHeight > a.clientHeight;
      const x = /auto|scroll/.test(s.overflowX) && a.scrollWidth > a.clientWidth;
      if (!x && !y) continue;
      const r = e.getBoundingClientRect(), q = a.getBoundingClientRect();
      if (y) a.scrollTop += r.top - q.top - a.clientTop - Math.max(0, (a.clientHeight - r.height) / 2);
      if (x) a.scrollLeft += r.left - q.left - a.clientLeft - Math.max(0, (a.clientWidth - r.width) / 2);
    }
    const r = e.getBoundingClientRect();
    const dy = r.height > innerHeight ? r.top : r.top - (innerHeight - r.height) / 2;
    window.scrollTo({ left: scrollX, top: scrollY + dy, behavior: 'instant' });
  };
  // The ancestor that hides the point (x, y) of n, or null. With the box left where the page has it,
  // the centre of an element a hidden box cuts off can lie outside that box, where elementFromPoint
  // returns whatever is there: a false "covered". Only ancestors on the containing-block chain clip,
  // so an absolute box escapes a static overflow:hidden parent.
  const hides = (n, x, y) => {
    let pos = getComputedStyle(n).position;
    for (let a = n.parentElement; a && a !== document.body; a = a.parentElement) {
      const s = getComputedStyle(a);
      const holds = s.transform !== 'none' || s.filter !== 'none' || s.perspective !== 'none'
        || /paint|strict|content/.test(s.contain);
      if (pos === 'fixed' && !holds) continue;
      if (pos === 'absolute' && s.position === 'static' && !holds) continue;
      const q = a.getBoundingClientRect();
      if ((s.overflowX !== 'visible' && (x < q.left || x >= q.right))
          || (s.overflowY !== 'visible' && (y < q.top || y >= q.bottom))) return a;
      pos = s.position;
    }
    return null;
  };
  for (const el of all) {
    const cs = getComputedStyle(el);
    const bf = cs.backdropFilter || cs.webkitBackdropFilter;
    if (!bf || bf === 'none') continue;
    // ancestor compositing group?
    for (let a = el.parentElement; a; a = a.parentElement) {
      const acs = getComputedStyle(a);
      const op = parseFloat(acs.opacity);
      // 0 < op < 1 only. opacity:0 means the subtree is not painted at all, which on many sites is
      // a scroll-reveal ([data-animate]) before it fires, not a compositing defect. Including it
      // makes every unrevealed section a false finding.
      if (op > 0 && op < 1) {
        out.grouped.push({ el: path(el), ancestor: path(a), opacity: op,
                           backdropFilter: bf, rect: el.getBoundingClientRect().toJSON() });
        break;
      }
    }
    // occluded by a stretched pseudo-element from a sibling/ancestor link?
    toView(el);
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (r.width && r.height && r.top >= 0 && r.bottom <= innerHeight && !hides(el, cx, cy)) {
      const top = document.elementFromPoint(cx, cy);
      if (top && top !== el && !el.contains(top)) {
        // Only a finding if the covered element HAS AN INTERACTION TO LOSE. A decorative
        // full-bleed wash is supposed to sit behind content, and reporting it is noise. Probe the
        // stylesheets for a :hover/:focus/:active rule whose selector targets this element - if
        // none exists, being covered costs nothing.
        let interactive = false;
        try {
          for (const sheet of document.styleSheets) {
            let rules; try { rules = sheet.cssRules; } catch (e) { continue; }
            if (!rules) continue;
            for (const rule of rules) {
              if (!rule.selectorText || !/:(hover|focus|active)/.test(rule.selectorText)) continue;
              const base = rule.selectorText.replace(/:(hover|focus-visible|focus|active)/g, '');
              for (const part of base.split(',')) {
                const p2 = part.trim();
                if (!p2) continue;
                try { if (el.matches(p2) || el.matches(p2.split(/\s+/).pop())) { interactive = true; } }
                catch (e) { /* unparseable fragment */ }
              }
              if (interactive) break;
            }
            if (interactive) break;
          }
        } catch (e) { interactive = true; }  // can't tell -> report rather than hide
        if (interactive) out.occluded.push({ el: path(el), coveredBy: path(top) });
      }
    }
  }
  // ★ DEAD SPOTS IN A STRETCHED CARD. The reverse of `occluded`: a control whose ::after is absolute
  // at inset 0 makes its whole containing card the tap target, and any plain child that paints above
  // that ::after takes the tap instead. A stack of avatar discs does, once `isolation: isolate` is
  // gone from its wrapper: a tap on the avatars opens nothing. For each such card, the centre of
  // every visible, non-interactive child must hit the control itself.
  out.deadspots = [];
  const CTRL = 'a[href],button,[role="button"],input,select,textarea,summary,[tabindex]:not([tabindex="-1"])';
  const seenCards = new Set();
  for (const c of document.querySelectorAll('a[href],button')) {
    const af = getComputedStyle(c, '::after');
    if (af.content === 'none' || af.position !== 'absolute') continue;
    if (!['top', 'right', 'bottom', 'left'].every((k) => parseFloat(af[k]) === 0)) continue;
    let card = c.parentElement;
    while (card && card !== document.body && getComputedStyle(card).position === 'static') card = card.parentElement;
    if (!card || card === document.body || seenCards.has(card)) continue;
    seenCards.add(card);
    toView(card);
    const cr = card.getBoundingClientRect();
    if (!cr.width || !cr.height || cr.top < 0 || cr.bottom > innerHeight) continue;
    for (const d of card.querySelectorAll('*')) {
      if (c.contains(d) || d.closest(CTRL) || d.matches(CTRL)) continue;
      const s = getComputedStyle(d);
      if (s.visibility === 'hidden' || s.pointerEvents === 'none' || parseFloat(s.opacity) === 0) continue;
      const r = d.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) continue;
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      if (x < cr.left || x >= cr.right || y < cr.top || y >= cr.bottom || hides(d, x, y)) continue;
      const hit = document.elementFromPoint(x, y);
      if (!hit || hit === c || c.contains(hit) || hit.closest(CTRL)) continue;
      out.deadspots.push({ el: path(d), card: path(card), control: path(c), hit: path(hit) });
    }
  }
  return out;
}"""

# Every page this opens reads doNotTrack '1', so an analytics script that honours it never counts
# this run. A site that does not otherwise skip local or webdriver visits would count each one.
DNT_JS = ("(()=>{const d={get:()=>'1',configurable:true};"
          "try{Object.defineProperty(Navigator.prototype,'doNotTrack',d)}catch(e){}"
          "try{Object.defineProperty(window,'doNotTrack',d)}catch(e){}})();")


def run(urls, widths):
    try:
        from pw_temp import sync_playwright
    except ImportError:
        print("playwright not available", file=sys.stderr)
        return 2, [], [(u, w) for u in urls for w in widths]
    findings = []
    failed = []
    # Edge writes its temp files to the user temp dir, not the profile, and a managed install can
    # unpack policy extensions there for every fresh profile: hundreds of MB a run, left behind by
    # every killed run. pw_temp starts the driver, and so this Edge, on a temp folder of this run's
    # own, and reaps a killed run's at the next launch.
    with sync_playwright() as pw:
        # No Edge, or a browser that will not start, is a could-not-run (2), never a finding (1).
        try:
            b = pw.chromium.launch(channel="msedge", headless=True)
        except Exception as e:
            print(f"browser did not launch: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            return 2, [], [(u, w) for u in urls for w in widths]
        for url in urls:
            for w in widths:
                pg = b.new_page(viewport={"width": w, "height": 900})
                pg.add_init_script(DNT_JS)
                try:
                    resp = pg.goto(url, wait_until="networkidle", timeout=90000)
                    # A 404 page loads fine and holds no backdrop: count it as a failed load.
                    if resp is None or resp.status >= 400:
                        raise RuntimeError(f"HTTP {resp.status if resp else 'no response'}")
                    # Fire every scroll-reveal first. Without this the probe reads unrevealed
                    # sections at opacity 0 and elementFromPoint misses off-screen elements -
                    # both produce confident nonsense.
                    pg.evaluate("async () => { const h = document.body.scrollHeight;"
                                " for (let y = 0; y < h; y += 400) { window.scrollTo(0, y);"
                                " await new Promise(r => setTimeout(r, 40)); }"
                                " window.scrollTo(0, 0);"
                                " document.querySelectorAll('[data-animate]')"
                                "   .forEach(e => e.classList.add('is-visible')); }")
                    pg.wait_for_timeout(700)
                    res = pg.evaluate(PROBE)
                except Exception as e:
                    print(f"  {url} @{w}: FAILED {type(e).__name__}: {str(e)[:70]}")
                    failed.append((url, w))
                    pg.close()
                    continue
                for g in res["grouped"]:
                    findings.append(dict(url=url, width=w, kind="grouped", **g))
                for o in res["occluded"]:
                    findings.append(dict(url=url, width=w, kind="occluded", **o))
                for o in res.get("deadspots") or []:
                    findings.append(dict(url=url, width=w, kind="deadspot", **o))
                n = len(res["grouped"])
                m = len(res["occluded"])
                k = len(res.get("deadspots") or [])
                flag = "FINDING" if (n or m or k) else "clean"
                print(f"  {url:52s} @{w:<5} grouped={n} occluded={m} deadspots={k}  {flag}")
                pg.close()
        b.close()
    # A finding outranks an incomplete run; a run with any failed load is never CLEAN.
    return (1 if findings else 2 if failed else 0), findings, failed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--widths", default="390,768,1024,1440")
    ap.add_argument("--json")
    a = ap.parse_args()
    widths = [int(x) for x in a.widths.split(",") if x.strip()]
    code, findings, failed = run(a.urls, widths)
    print()
    total = len(a.urls) * len(widths)
    if failed:
        print(f"DID NOT RUN on {len(failed)} of {total} url x width combination(s). "
              f"Those are UNVERIFIED, not clean.")
    if not findings and not failed:
        print(f"CLEAN - 0 findings over {len(a.urls)} url(s) x {len(widths)} width(s)")
    elif findings:
        print(f"{len(findings)} FINDING(S):")
        for f in findings:
            if f["kind"] == "grouped":
                print(f"  [grouped ] {f['url']} @{f['width']}")
                print(f"      {f['el']}")
                print(f"      killed by ancestor opacity {f['opacity']} on {f['ancestor']}")
            elif f["kind"] == "deadspot":
                print(f"  [deadspot] {f['url']} @{f['width']}")
                print(f"      {f['el']}  takes the tap from  {f['control']}  (hit {f['hit']})")
            else:
                print(f"  [occluded] {f['url']} @{f['width']}")
                print(f"      {f['el']}  covered by  {f['coveredBy']}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(findings, fh, indent=2)
        print(f"\njson -> {a.json}")
    sys.exit(code)


if __name__ == "__main__":
    main()
