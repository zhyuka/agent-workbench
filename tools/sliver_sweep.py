"""sliver_sweep.py - find rounded boxes whose own fill leaks as a thin ring at the corners. Read-only.

USAGE
  python tools/sliver_sweep.py <base> <path> <widths> <dpr> <out.json> [--flat] [--red] [--in 1,3]
                         [--only SUBSTR] [--inject CSS] [--cap N] [--all]
  <base>    the server's origin, e.g. http://127.0.0.1:<port>
  <path>    the page's path on it, e.g. /<page>/
  <widths>  a list, 390,768,1440, or a range, w0,w1,step (step smaller than w0)
  example   python tools/sliver_sweep.py http://127.0.0.1:<port> /<page>/ 390,768,1440 2 out.json --flat --in 1,3
  Serve a fresh production build of the page, not a dev server that may be stale or mid-rebuild.

WHY THIS EXISTS
  Chromium smooths a rounded box's own fill along the arc, then smooths a child clipped to the same
  arc again, and the parent's fill shows between the two as a 1px coloured ring. It is common in
  card and panel components that round a frame and crop a photo or panel inside it.
  The fix that holds: paint the frame square and round it once with
  clip-path: inset(0 round R), plus will-change: transform where a child crop overlaps the corner.

WHAT IT MEASURES
  At every width, every visible element at least 24x24 with a corner radius of 4 or more is a target
  when it clips children (overflow not visible, or a clip-path) or paints an image itself
  (img/video/canvas/iframe, or a background-image). Each pixel on the arc is scored by its sRGB
  distance from the segment between the colour 2.5px outside the arc and the colour --in px inside
  it. A clean edge is a blend of the two and scores about 0. A corner over 12 is flagged, and the
  crops of a flagged target are saved in <out>_dump/ next to out.json.
  --flat    every url() background and every raster becomes flat grey, so photo texture can't score
  --red     paints the own fill of every clipping target red, so a leak of that fill reads as red
  --in 1,3  scores with the reference 1px and 3px inside the arc, from the same screenshot. Content
            that runs into the corner (a table header, a dot grid) tends to score at 1px only; a
            real leak of the frame's fill tends to score at both depths
  --only S  measures only targets whose signature (tag.classes|radii) contains S
  --inject  adds a CSS string to the page first, for example the pre-fix rule, to calibrate
  --cap N   repeats of one component (same tag, classes, radii) measured per page, first in DOM order (3)
  --all     prints every target, not just the flagged ones
  Each row of out.json names its target's signature and, as fig, the data-fig attribute of its
  nearest ancestor that has one (a page can mark each section with its design node that way).

EXIT
  0 no target flagged, 1 a target flagged, 2 could not run: bad arguments, the page did not load, or
  no target was measured, which includes an --only that matched nothing. A flag is a question, not a
  leak: open the dump. Calibrate one target with --only and --inject in both directions; a page total
  is not a calibration, because content at a corner flags on a clean build. The calibration to
  look for: with the pre-fix rule injected, --only <target> flags that target (exit 1); without it,
  it flags 0 (exit 0).

BLIND TO
  Elliptical radii, position: fixed, a target not wholly inside every clipping ancestor (a carousel
  slide half out of its track), clip-path shapes other than inset(... round R), leaks under 12 in sRGB
  distance, and every page and width you don't pass."""
import sys, io, os, math, json
# A missing third-party module is a could-not-run: main() reports it and returns 2.
try:
    import numpy as np
    from PIL import Image
    from pw_temp import sync_playwright
    MISSING = None
except ImportError as e:
    MISSING = e

# --flat: every url() background layer becomes flat grey and every raster turns flat grey, so a
# photo's texture cannot read as a sliver. Layers, clips, radii and gradient scrims are untouched.
FLATTEN = """() => { for (const el of document.querySelectorAll('body *')) {
  if (el.dataset.flat) continue; const cs = getComputedStyle(el);
  if (cs.backgroundImage.includes('url(')) { el.style.setProperty('background-image',
    cs.backgroundImage.replace(/url\\([^)]*\\)/g, 'linear-gradient(#7a7a7a,#7a7a7a)'), 'important'); el.dataset.flat = '1'; }
  if (['IMG', 'VIDEO', 'CANVAS', 'IFRAME'].includes(el.tagName)) { el.style.setProperty('filter', 'brightness(0) invert(0.48)', 'important'); el.dataset.flat = '1'; }
} }"""
# Every page this opens reads doNotTrack '1', so an analytics script that honours it never counts
# this run. A site that does not otherwise skip local or webdriver visits would count each one.
DNT_JS = ("(()=>{const d={get:()=>'1',configurable:true};"
          "try{Object.defineProperty(Navigator.prototype,'doNotTrack',d)}catch(e){}"
          "try{Object.defineProperty(window,'doNotTrack',d)}catch(e){}})();")
VH, TOPY, M = 900, 180, 4
# --in 1,3: how far inside the arc the reference colour is sampled, one score per depth from the same
# screenshot. The first depth fills cTL..cBL; each later depth fills cTL_<d>..cBL_<d>.
# main() sets both from --in.
INS, SUF = [1.0], [""]

def seg(p, o, i):
    p, o, i = (np.asarray(v, float) for v in (p, o, i))
    d = i - o; dd = (d*d).sum(axis=-1)
    t = np.where(dd > 0, ((p - o)*d).sum(axis=-1) / np.where(dd > 0, dd, 1), 0).clip(0, 1)
    q = o + t[..., None]*d
    dist = np.sqrt(((p - q)**2).sum(axis=-1))
    lp = p @ [0.2126, 0.7152, 0.0722]; lq = q @ [0.2126, 0.7152, 0.0722]
    return dist * np.where(lp >= lq, 1, -1)

def tri(p, o, b, i):
    """Signed distance from p to the triangle o-b-i in RGB: a pixel on a bordered arc is a blend of
    outside, border and inside, so anything in that hull is clean."""
    p, o, b, i = (np.asarray(v, float) for v in (p, o, b, i))
    e1, e2, v = b - o, i - o, p - o
    g = np.array([[e1 @ e1, e1 @ e2], [e1 @ e2, e2 @ e2]]); det = np.linalg.det(g)
    cands = []
    if abs(det) > 1e-6:
        st = np.linalg.solve(g, [v @ e1, v @ e2])
        if st[0] >= 0 and st[1] >= 0 and st.sum() <= 1: cands.append(o + st[0]*e1 + st[1]*e2)
    for u, w in ((o, b), (b, i), (o, i)):
        d = w - u; dd = d @ d; t = 0 if dd == 0 else min(1, max(0, ((p - u) @ d) / dd)); cands.append(u + t*d)
    q = min(cands, key=lambda c: ((p - c)**2).sum())
    L = np.array([0.2126, 0.7152, 0.0722])
    return float(np.sqrt(((p - q)**2).sum())) * (1 if p @ L >= q @ L else -1)

def corner(a, cx, cy, r, sx, sy, s, bw=0, bcol=None):
    """One score per depth in INS. A depth past the corner's centre samples the centre itself."""
    H, W = a.shape[:2]; best = [0.0]*len(INS)
    bord = bw >= 0.25 and bcol is not None and r - bw > 1
    for dy in range(0, int((r+3)*s)):
        for dx in range(0, int((r+3)*s)):
            d = math.hypot(dx+0.5, dy+0.5) / s
            near = abs(d - r) <= 0.75 or (bord and abs(d - (r - bw)) <= 0.75)
            if not near or dx < 2*s or dy < 2*s: continue
            ux, uy = (dx+0.5)/s/d, (dy+0.5)/s/d
            x, y = int(cx + sx*dx), int(cy + sy*dy)
            ox, oy = int(cx + sx*(r+2.5)*ux*s), int(cy + sy*(r+2.5)*uy*s)
            if not all(0 <= v < W for v in (x, ox)) or not all(0 <= v < H for v in (y, oy)): continue
            for k, IN in enumerate(INS):
                ri = max(0.0, r - (bw if bord else 0) - IN)
                ix, iy = int(cx + sx*ri*ux*s), int(cy + sy*ri*uy*s)
                if not (0 <= ix < W and 0 <= iy < H): continue
                v = tri(a[y, x], a[oy, ox], bcol, a[iy, ix]) if bord else float(seg(a[y, x], a[oy, ox], a[iy, ix]))
                if abs(v) > abs(best[k]): best[k] = v
    return [round(v, 1) for v in best]

def put(res, key, vals):
    for s, v in zip(SUF, vals): res[key + s] = v

DISCOVER = """([cap]) => {
  const st = document.getElementById('__slred'); if (st) st.disabled = true;
  window.__slN = window.__slN || 0; window.__slSig = window.__slSig || {};
  const MEDIA = new Set(['IMG','VIDEO','CANVAS','IFRAME']);
  const out = [];
  const px = (v, w, h) => v.endsWith('%') ? parseFloat(v) / 100 * Math.min(w, h) : parseFloat(v) || 0;
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 24) continue;
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed') continue;
    if (!el.checkVisibility({opacityProperty: true, visibilityProperty: true})) continue;
    const raw = [cs.borderTopLeftRadius, cs.borderTopRightRadius, cs.borderBottomRightRadius, cs.borderBottomLeftRadius];
    if (raw.some(v => v.trim().includes(' '))) continue;               // elliptical
    if (raw.some(v => v.endsWith('%')) && Math.abs(r.width - r.height) > 0.5) continue;
    let rad = raw.map(v => px(v, r.width, r.height)), ins = [0, 0, 0, 0];
    const bwMax = Math.max(...[cs.borderTopWidth, cs.borderRightWidth, cs.borderBottomWidth, cs.borderLeftWidth].map(parseFloat));
    let bw = bwMax;
    const m = /^inset\\(([^)]*?)\\s*round\\s+([\\d.]+)px\\)$/.exec(cs.clipPath || '');
    if (m) {                                                            // clip-path rounding wins
      const v = m[1].trim() ? m[1].trim().split(/\\s+/).map(parseFloat) : [0];
      ins = v.length === 1 ? [v[0], v[0], v[0], v[0]] : v.length === 2 ? [v[0], v[1], v[0], v[1]]
          : v.length === 3 ? [v[0], v[1], v[2], v[1]] : v;
      rad = [1, 1, 1, 1].map(() => parseFloat(m[2]));
      bw = Math.max(0, bwMax - Math.min(...ins));
    } else if (cs.clipPath !== 'none' && rad.every(v => !v)) continue;  // some other clip shape
    const [tl, tr, br, bl] = rad;
    const f = Math.min(1, r.width/(tl+tr||1e9), r.width/(bl+br||1e9), r.height/(tl+bl||1e9), r.height/(tr+br||1e9));
    rad = rad.map(v => v * f);
    if (Math.max(...rad) < 4) continue;
    const clips = cs.overflowX !== 'visible' || cs.overflowY !== 'visible' || cs.clipPath !== 'none';
    const pseudo = ['::before','::after'].some(p => getComputedStyle(el, p).content !== 'none');
    const kids = el.children.length > 0 || pseudo;
    const bgimg = cs.backgroundImage !== 'none';
    const bgcol = !/rgba\\([^)]*,\\s*0\\)|transparent/.test(cs.backgroundColor);
    const media = MEDIA.has(el.tagName);
    if (!((clips && kids) || media || bgimg)) continue;
    let inside = true;
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const ac = getComputedStyle(a);
      if (ac.overflowX === 'visible' && ac.overflowY === 'visible' && ac.clipPath === 'none') continue;
      const ar = a.getBoundingClientRect();
      if (r.left < ar.left - 0.5 || r.right > ar.right + 0.5 || r.top < ar.top - 0.5 || r.bottom > ar.bottom + 0.5) { inside = false; break; }
    }
    if (!inside) continue;
    if (!el.dataset.sl) {
      const sig = el.tagName + '.' + [...el.classList].filter(c => !c.startsWith('astro-')).sort().join('.') + '|' + raw.join(',');
      const n = (window.__slSig[sig] = (window.__slSig[sig] || 0) + 1);
      el.dataset.sl = n <= cap ? String(++window.__slN) : 'x';
      el.dataset.slSig = sig;
    }
    if (el.dataset.sl === 'x') continue;
    if (clips && kids && (bgcol || bgimg)) el.dataset.slRed = '1';
    const fig = el.closest('[data-fig]');
    out.push({id: el.dataset.sl, sig: el.dataset.slSig, fig: fig ? fig.dataset.fig : null,
      x: r.left + ins[3], y: r.top + scrollY + ins[0], w: r.width - ins[1] - ins[3], h: r.height - ins[0] - ins[2],
      rad, ins, bw, red: el.dataset.slRed === '1', clips});
  }
  if (st) st.disabled = false;
  return out;
}"""
RECT = """([id, ins]) => { const e = document.querySelector(`[data-sl='${id}']`), r = e.getBoundingClientRect(), cs = getComputedStyle(e);
  const rgba = s => { const m = s.match(/[\\d.]+/g).map(Number); return [m[0], m[1], m[2], m.length > 3 ? m[3] : 1]; };
  const b = rgba(cs.borderTopColor), g = rgba(cs.backgroundColor);
  const bc = g[3] > 0 ? [[0, 1, 2].map(k => b[k]*b[3] + g[k]*(1 - b[3])), 1] : [b.slice(0, 3), b[3]];
  return [r.left + ins[3], r.top + ins[0], r.width - ins[1] - ins[3], r.height - ins[0] - ins[2], bc]; }"""

def measure(pg, info, vw):
    res = {}; free = VH - TOPY - 8
    parts = [("all", info["y"] - TOPY)] if info["h"] + 2*M <= free else [("top", info["y"] - TOPY), ("bot", info["y"] + info["h"] - (VH - 8))]
    for part, sy in parts:
        pg.evaluate(f"window.scrollTo(0, {max(0, sy)})"); pg.wait_for_timeout(120)
        x, y, w, h, bc = pg.evaluate(RECT, [info["id"], info["ins"]])
        bw = info["bw"]
        top = max(y, 0) if part != "bot" else max(y, TOPY)
        bot = min(y + h, VH) if part != "top" else min(y + h, VH - 4)
        cx0, cy0 = math.floor(x) - M, math.floor(top) - M
        cw, ch = math.ceil(x + w) + M - cx0, math.ceil(bot) + M - cy0
        cx0c, cy0c = max(cx0, 0), max(cy0, 0)
        clip = {"x": cx0c, "y": cy0c, "width": min(cw - (cx0c - cx0), vw - cx0c), "height": min(ch - (cy0c - cy0), VH - cy0c)}
        if clip["width"] < 8 or clip["height"] < 8: continue
        a = np.asarray(Image.open(io.BytesIO(pg.screenshot(clip=clip))).convert("RGB")).astype(float)
        s = a.shape[1] / clip["width"]
        X0, X1 = (x - cx0c)*s, (x + w - cx0c)*s
        Y0, Y1 = (y - cy0c)*s, (y + h - cy0c)*s
        rTL, rTR, rBR, rBL = info["rad"]
        seeTop = part in ("all", "top") and y >= cy0c + M - 0.01
        seeBot = part in ("all", "bot") and y + h <= cy0c + clip["height"] - M + 0.01
        # a translucent border over no fill of its own: composite it over the colour just outside
        bcol = np.array(bc[0], float) if bc[1] >= 1 else None
        if bc[1] < 1 and bw >= 0.25:
            o = a[int(Y0 - 2*s) if Y0 - 2*s >= 0 else int(Y1 + 2*s), int((X0 + X1)/2)] if 0 <= int((X0 + X1)/2) < a.shape[1] else a[0, 0]
            bcol = bc[1]*np.array(bc[0], float) + (1 - bc[1])*o
        kw = dict(bw=bw, bcol=bcol)
        res.setdefault("_shots", []).append((part, a, clip))
        if seeTop:
            if rTL >= 4: put(res, "cTL", corner(a, X0 + rTL*s, Y0 + rTL*s, rTL, -1, -1, s, **kw))
            if rTR >= 4: put(res, "cTR", corner(a, X1 - rTR*s, Y0 + rTR*s, rTR, +1, -1, s, **kw))
        if seeBot:
            if rBR >= 4: put(res, "cBR", corner(a, X1 - rBR*s, Y1 - rBR*s, rBR, +1, +1, s, **kw))
            if rBL >= 4: put(res, "cBL", corner(a, X0 + rBL*s, Y1 - rBL*s, rBL, -1, +1, s, **kw))
    return res

# Options that take a value. The value is the token after the flag and leaves the positionals by that
# index, never by its value, so `--cap 1` with a dpr of 1 removes the flag's value, not the dpr.
VALUED = ("--only", "--inject", "--cap", "--in")

def main(argv=None):
    """argv is everything after the program name. Returns the exit code."""
    global INS, SUF
    av = sys.argv[1:] if argv is None else list(argv)
    if "-h" in av or "--help" in av:
        print(__doc__); return 0
    if MISSING is not None:
        print(f"COULD NOT RUN: {MISSING}. sliver_sweep.py needs numpy, Pillow and playwright."); return 2
    USAGE = "usage: python tools/sliver_sweep.py <base> <path> <widths> <dpr> <out.json> [options]; --help for more"
    # A non-number, or a value-taking flag given last, is a bad argument: usage and 2, never a traceback
    # and the 1 that means a target flagged.
    try:
        ONLY = av[av.index("--only") + 1] if "--only" in av else ""
        RED = "--red" in av
        INJ = av[av.index("--inject") + 1] if "--inject" in av else ""
        CAP = int(av[av.index("--cap") + 1]) if "--cap" in av else 3
        skip = {i + 1 for i, a in enumerate(av) if a in VALUED}
        args = [a for i, a in enumerate(av) if i not in skip and not a.startswith("--")]
        if len(args) != 5 or not args[4].endswith(".json"):
            print(USAGE); return 2
        BASE, PATH, WSPEC, DPR, OUTP = args[0], args[1], args[2], float(args[3]), args[4]
        FLAT = "--flat" in av
        DUMP = OUTP[:-5] + "_dump"
        ws = [int(v) for v in WSPEC.split(",")]
        WIDTHS = list(range(ws[0], ws[1] + 1, ws[2])) if len(ws) == 3 and ws[2] < ws[0] else ws
        INS = [float(v) for v in av[av.index("--in") + 1].split(",")] if "--in" in av else [1.0]
        SUF = [""] + [f"_{d:g}" for d in INS[1:]]
        CKEYS = [c + s for s in SUF for c in ("cTL", "cTR", "cBR", "cBL")]
    except (ValueError, IndexError):
        print(USAGE); return 2
    if not WIDTHS:
        print(f"COULD NOT RUN: widths {WSPEC} read as the range w0,w1,step and gave no width."); return 2

    out = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": WIDTHS[0], "height": VH}, device_scale_factor=DPR)
        ctx.add_init_script(DNT_JS)
        pg = ctx.new_page()
        try:
            resp = pg.goto(f"{BASE}{PATH}", wait_until="networkidle", timeout=120000)
        except Exception as e:
            print(f"COULD NOT RUN: {BASE}{PATH} did not load: {e}"); b.close(); return 2
        if resp is None or resp.status >= 400:
            print(f"COULD NOT RUN: {BASE}{PATH} returned {resp.status if resp else 'no response'}"); b.close(); return 2
        pg.add_style_tag(content="*,*::before,*::after{transition:none!important;animation-play-state:paused!important;caret-color:transparent!important}")
        if INJ: pg.add_style_tag(content=INJ)
        if RED: pg.evaluate("() => { const s = document.createElement('style'); s.id = '__slred'; s.textContent = \"[data-sl-red]{background:#ff0000 !important}\"; document.head.appendChild(s); }")
        H = pg.evaluate("document.documentElement.scrollHeight")
        for yy in range(0, H, 400): pg.evaluate(f"window.scrollTo(0,{yy})"); pg.wait_for_timeout(60)
        pg.wait_for_timeout(1500)
        for w in WIDTHS:
            pg.set_viewport_size({"width": w, "height": VH}); pg.wait_for_timeout(250)
            if FLAT: pg.evaluate(FLATTEN); pg.wait_for_timeout(100)
            for info in pg.evaluate(DISCOVER, [CAP]):
                if RED and not info["red"]: continue
                if ONLY and ONLY not in info["sig"]: continue
                if info["x"] < -0.5 or info["x"] + info["w"] > w + 0.5:
                    continue
                r = measure(pg, info, w)
                shots = r.pop("_shots", [])
                if any(abs(r.get(c, 0)) > 12 for c in CKEYS):
                    os.makedirs(DUMP, exist_ok=True)
                    for part, a, clip in shots:
                        Image.fromarray(a.astype("uint8")).save(f"{DUMP}/{w}_{info['id']}_{part}.png")
                out.append({"w": w, **{k: info[k] for k in ("id", "sig", "fig", "rad", "red", "clips")}, **r,
                            "box": f"x={info['x']:.2f} y={info['y']:.2f} w={info['w']:.2f} h={info['h']:.2f}"})
        b.close()
    json.dump(out, open(OUTP, "w"))
    CT = 12
    ids = {}
    for r in out: ids.setdefault(r["id"], []).append(r)
    print(f"{PATH} widths {WIDTHS[0]}..{WIDTHS[-1]} ({len(WIDTHS)}) dpr {DPR}{' RED' if RED else ''}{' FLAT' if FLAT else ''}{' ONLY '+ONLY if ONLY else ''}{' INJECT '+INJ if INJ else ''} targets {len(ids)}")
    if not ids:
        print(f"COULD NOT RUN: no target measured{' (--only matched nothing)' if ONLY else ''}. This is not a clean result.")
        return 2
    anyflag = False
    for d, s in zip(INS, SUF):
        CS = [c + s for c in ("cTL", "cTR", "cBR", "cBL")]
        print(f" reference {d:g}px inside the arc")
        for i, rs in sorted(ids.items(), key=lambda kv: int(kv[0])):
            ws_m = sorted({r["w"] for r in rs})
            fl = sorted({r["w"] for r in rs if any(abs(r.get(c, 0)) > CT for c in CS)})
            wc = max(((abs(r[c]), r[c], c, r["w"]) for r in rs for c in CS if c in r), default=None)
            if fl or "--all" in av:
                print(f"  [{i}] {rs[0]['sig'][:70]} fig={rs[0]['fig']} flagged {len(fl)} of {len(ws_m)}"
                      f" | worst {wc[1] if wc else '-'} {wc[2] if wc else ''}@{wc[3] if wc else ''} | widths {fl[:12]}{' ...' if len(fl) > 12 else ''}")
        nflag = sum(1 for rs in ids.values() if any(any(abs(r.get(c, 0)) > CT for c in CS) for r in rs))
        anyflag = anyflag or nflag > 0
        print(f"  {nflag} of {len(ids)} targets flagged at one or more widths")
    return 1 if anyflag else 0

if __name__ == "__main__":
    sys.exit(main())
