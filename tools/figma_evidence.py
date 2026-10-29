#!/usr/bin/env python
"""figma_evidence.py - the before/after picture a FIX note carries (figma_post.py, THE NINTH RULE).

    sheet    <file_key> <frame_id> (--before-at <ISO UTC> | --before-png <file>) --out <file.jpg>
             [--crop x,y,w,h] [--build <url> --viewport WxH] [--note "a line"]...
    selftest                               planted cases, no network

WHY. A note saying you fixed something in a design file is only as good as its evidence: design
needs to see the before and the after, and the picture belongs in the comment itself, not in an
"Evidence" frame placed in the file and linked. A REST comment cannot carry an image (POST takes
message, comment_id and client_meta only), so the sheet is attached to the comment in the Figma UI,
after its closing "Before and after:" (figma_attach.py does that step).

WHAT `sheet` DOES. Renders <frame_id> through /v1/images at 2x twice: as the file stood before the
edit and as it is now. --before-at picks the version before the newest version created at or before
that time, because a version holds its whole editing session. --before-png takes a render saved before the edit instead. --crop cuts both to design px
of the frame. --build adds the served page at --viewport, 2x, clipped to the same box, so give it
the frame's own width. The panels sit side by side under their labels, with each --note line below,
as a JPEG under 500 KB.

IN PLACE, AT ITS REAL SCALE. A frame with no fill renders transparent. Flattened to RGB, a 10% white
badge pill comes out solid white and hides its white text, so a sound badge looks broken. While the
frame's box is not opaque, `sheet` renders the next ancestor up and cuts the same box from it. That
shows the frame with whatever Figma draws under it, sibling layers included (a page's background
image frame), and the page's canvas colour when nothing does. Figma also shrinks a render over about
32 MP: a long page comes back at, say, 1.5x instead of 2x, and a crop that assumed 2x lands off
target. The scale is now read off each render's
size, and each panel is resized to 2x of its design px. Both are printed when they apply. A
--before-png with transparent pixels is laid over white, because the file it came from is unknown.

WHAT LIES ON TOP OF IT. A layer left on the section on top of an opaque screen is on the canvas and in
no render of the screen, so a label drawn there goes missing from the sheet. Each visible layer that a later sibling of the frame or of an ancestor puts over the
cut box is rendered on its own and pasted where the canvas draws it, and a NOTE names them.

REFUSES (exit 1, nothing written)
  - before and after are the same picture: the version is not from before the edit, or the edit did
    not reach this frame (or the crop misses it)
  - a render of one colour, which is what a failed or empty render looks like
  - --before-at earlier than the file's first listed session

THEN, printed when a sheet is written:
  1. figma_post.py new|reply ... --edited <ids> --picture <file.jpg>, which ends the text with
     "Before and after:" (or fixnote, for a comment you edit by hand)
  2. nothing by hand: right after the post, figma_post runs figma_attach.py, which attaches the
     file in the browser after "Before and after:" and checks it is there

EXIT  0 written, 1 refused, 2 did not run (no token, API error).
"""
import io, json, os, sys, urllib.error, urllib.parse, urllib.request

API = "/v1"   # a Figma REST path; _get sends it through figma_client.py
MAX_BYTES = 500 * 1024


def _get(url, raw=False):
    """A Figma REST path goes through figma_client.py beside this file, on the interactive lane (a
    person waits on it): the client reads a 429's Retry-After and waits it out. A full URL on the API host (a
    versions next_page link) goes the same way. A render URL Figma hands back, on another host, is
    fetched as it is, with no token. Anything else that fails is DID NOT RUN, exit 2."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import figma_client
    try:
        if "://" in url and urllib.parse.urlsplit(url).netloc != figma_client.FIGMA_HOST:
            data = urllib.request.urlopen(url, timeout=300).read()
        else:
            data = figma_client.get_bytes(url, default_lane="interactive", tool="figma_evidence", timeout=300)
    except (urllib.error.URLError, OSError, RuntimeError) as e:   # RuntimeError: figma_client.NoToken
        print(f"DID NOT RUN: {url.split('?')[0]}: {e}")
        sys.exit(2)
    return data if raw else json.loads(data)


def before_version(versions, when):
    """The version id that holds the file before `when`, or None. A version holds its whole editing
    session, so the one created last before `when` can hold later edits, and the file before `when`
    is the version before that one."""
    ok = sorted((v for v in versions if v["created_at"] <= when), key=lambda v: v["created_at"])
    return ok[-2]["id"] if len(ok) > 1 else None


def versions(key, oldest_needed, get=None):
    """The file's versions from the newest back to the two that hold it at `oldest_needed` (the open
    session and the one before), page by page. A page holds 30; reading only the first makes an edit
    older than the 30 newest versions refuse with "no version holds the file before" it.
    figma_client takes a next_page URL as it comes."""
    get = get or _get
    out, url = [], f"{API}/files/{key}/versions"
    while url:
        page = get(url)
        out += page.get("versions") or []
        if sum(v["created_at"] <= oldest_needed for v in out) >= 2:
            break
        url = (page.get("pagination") or {}).get("next_page")
    return out


def render(key, frame, version=None):
    """The node as Figma draws it, asked for at 2x, as an RGBA PIL image of exactly its box
    (use_absolute_bounds). Figma returns less than 2x for a render over about 32 MP; panel() reads
    the real scale off the size."""
    from PIL import Image
    q = {"ids": frame, "format": "png", "scale": "2", "use_absolute_bounds": "true"}
    if version:
        q["version"] = version
    url = (_get(f"{API}/images/{key}?{urllib.parse.urlencode(q)}").get("images") or {}).get(frame)
    if not url:
        print(f"DID NOT RUN: Figma returned no render for {frame}" + (f" at version {version}" if version else ""))
        sys.exit(2)
    return Image.open(io.BytesIO(_get(url, raw=True))).convert("RGBA")


def _client():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import figma_client
    return figma_client


def _lineage(key, frame, version=None, kids=True):
    """figma_client.lineage of one node, or None when the file does not hold it. Anything that fails is DID NOT RUN, exit 2, as _get."""
    fc = _client()
    try:
        return fc.lineage(key, [frame], kids=kids, version=version, default_lane="interactive",
                          tool="figma_evidence", timeout=300).get(frame)
    except (urllib.error.URLError, OSError, RuntimeError) as e:   # RuntimeError: figma_client.NoToken
        print(f"DID NOT RUN: the lineage of {frame}: {e}")
        sys.exit(2)


def chain(key, frame, version=None):
    """([(id, (x, y, w, h))] for the frame and then each ancestor up to its page, nearest first, and
    the page's canvas colour as an RGB tuple), from the frame's lineage."""
    found = _lineage(key, frame, version, kids=False)
    if not found:
        print(f"DID NOT RUN: {frame} is not in the file" + (f" at version {version}" if version else ""))
        sys.exit(2)
    page = next((n for n in found if n.get("type") == "CANVAS"), {})
    bg = page.get("backgroundColor") or {"r": 1, "g": 1, "b": 1}
    canvas = tuple(round(255 * bg[c]) for c in "rgb")
    links = [(n["id"], tuple(n["absoluteBoundingBox"][k] for k in ("x", "y", "width", "height")))
             for n in reversed(found) if n.get("type") not in ("DOCUMENT", "CANVAS") and n.get("absoluteBoundingBox")]
    return links, canvas


def pick_over(lineage, box):
    """[(id, (x, y, w, h), parent id)] for each visible layer that Figma draws on top of `box` from outside the
    last node of `lineage` (figma_client.lineage's list for it, with kids), bottom first: the rule is
    figma_client.drawn_over's, the one copy every tool calls."""
    return [(o["id"], tuple(o["box"]), o["on"]) for o in _client().drawn_over(lineage, box) or []]


def drawn_over(key, frame, box, version=None):
    """The layers outside <frame> that Figma draws on top of `box` (design px, absolute). A render of
    <frame> leaves them out, so a label the designer left on the section above an opaque screen is on
    the canvas and missing from the sheet. /v1/images contents_only=false would draw them, but on a
    large file it answers 400 "Document images too large or too many images"."""
    return canvas_layers(key, frame, box, version)[1]


def canvas_layers(key, frame, box=None, version=None):
    """(<frame>'s own properties, without children, and drawn_over's list for `box`, or for the
    frame's own box when `box` is None), from the frame's lineage; ({}, []) when the frame is not in
    the file."""
    path = _lineage(key, frame, version)
    if not path:
        return {}, []
    node = path[-1]
    if box is None:
        b = node.get("absoluteBoundingBox") or {}
        box = (b.get("x", 0), b.get("y", 0), b.get("width", 0), b.get("height", 0))
    return node, pick_over(path, box)


def overlay(im, hits, origin, render_fn, scale=2):
    """`im` (a panel at `scale` whose top-left is design point `origin`) with each hit's own render
    pasted where the canvas draws it, bottom first."""
    from PIL import Image
    out = im.convert("RGBA")
    for hid, (x, y, w, h), *_ in hits:
        layer = render_fn(hid)
        size = (max(1, round(scale * w)), max(1, round(scale * h)))
        if layer.size != size:
            layer = layer.resize(size)
        sheet_ = Image.new("RGBA", out.size, (0, 0, 0, 0))
        sheet_.paste(layer, (round(scale * (x - origin[0])), round(scale * (y - origin[1]))))
        out.alpha_composite(sheet_)
    return out.convert("RGB")


def with_over(key, frame, links, crop, im, drawn_in, version=None):
    """The panel with the layers drawn over it from outside the frame, and a note naming them. A
    layer inside the node the panel was cut from (`drawn_in`, from panel()) is in it already."""
    fx, fy, fw, fh = links[0][1]
    ox, oy = (fx + crop[0], fy + crop[1]) if crop else (fx, fy)
    box = (ox, oy, crop[2], crop[3]) if crop else (fx, fy, fw, fh)
    inside = {i for i, _ in links[:[i for i, _ in links].index(drawn_in[-1]) + 1]} if drawn_in else {frame}
    hits = [h for h in drawn_over(key, frame, box, version) if h[2] not in inside]
    if not hits:
        return im, []
    return (overlay(im, hits, (ox, oy), lambda n: render(key, n, version)),
            [f"drew {len(hits)} layer(s) that lie on top of the frame from outside it, as the canvas does: "
             + ", ".join(h[0] for h in hits)])


def opaque(im):
    """True when no pixel of the image lets anything show through (alpha 250 or more)."""
    return im.mode != "RGBA" or im.getchannel("A").getextrema()[0] >= 250


def panel(render_fn, links, crop=None, canvas=(255, 255, 255), drawn_in=None):
    """(The frame's picture as an RGB image at 2x of its design px, drawn in place, and a list of
    notes on how it was made.) links is chain()'s list; render_fn(id) is that node's RGBA render of
    exactly its box, at whatever scale Figma returned. While the cut box is not opaque, the next
    ancestor is rendered and cut to the same box; past the last one, the cut is laid on the canvas
    colour. Coordinates are scaled by each render's own size, never by an assumed 2x. `drawn_in`, a
    list, gets the id of each node rendered, last one used."""
    from PIL import Image
    fid, (fx, fy, fw, fh) = links[0]
    cx, cy, cw, ch = crop or (0, 0, fw, fh)
    want = (max(1, round(2 * cw)), max(1, round(2 * ch)))
    notes, cut = [], None
    for nid, (ax, ay, aw, ah) in links:
        if drawn_in is not None:
            drawn_in.append(nid)
        im = render_fn(nid).convert("RGBA")
        sx, sy = im.width / aw, im.height / ah
        x0, y0 = fx - ax + cx, fy - ay + cy
        cut = im.crop((round(x0 * sx), round(y0 * sy), round((x0 + cw) * sx), round((y0 + ch) * sy)))
        if abs(sx - 2) > 0.02 or abs(sy - 2) > 0.02:
            notes.append(f"{nid} came back at {sx:.2f}x, not 2x (Figma shrinks a render over about 32 MP); "
                         "cut at that scale and resized to 2x")
        if cut.size != want:
            cut = cut.resize(want, Image.LANCZOS)
        if opaque(cut):
            if nid != fid:
                notes.append(f"{fid} is see-through here, so it is shown as drawn in {nid}, with what lies under it")
            return cut.convert("RGB"), notes
    base = Image.new("RGBA", want, canvas + (255,))
    base.alpha_composite(cut)
    notes.append(f"no ancestor of {fid} is opaque in its box, so it is laid on the page's canvas colour {canvas}")
    return base.convert("RGB"), notes


def flatten(im, under=(255, 255, 255)):
    """An image as RGB, laid over `under` where it is see-through, never by dropping alpha."""
    from PIL import Image
    if im.mode == "RGB":
        return im
    base = Image.new("RGBA", im.size, under + (255,))
    base.alpha_composite(im.convert("RGBA"))
    return base.convert("RGB")


def capture(url, viewport, clip):
    """The served page at viewport (w, h), 2x, clipped to (x, y, w, h) in CSS px."""
    from PIL import Image
    from pw_temp import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        try:
            pg = b.new_page(viewport={"width": viewport[0], "height": viewport[1]}, device_scale_factor=2)
            pg.goto(url, wait_until="load", timeout=60000)
            pg.evaluate("document.fonts.ready")
            pg.add_style_tag(content="*,*::before,*::after{animation:none!important;transition:none!important}"
                                     "[data-animate]{opacity:1!important;transform:none!important}")
            pg.wait_for_timeout(800)
            png = pg.screenshot(clip=dict(zip(("x", "y", "width", "height"), clip)))
        finally:
            b.close()
    return Image.open(io.BytesIO(png)).convert("RGB")


def one_colour(im):
    """True when 99.5% of the pixels share one colour at 4 bits a channel."""
    q = im.convert("RGB").resize((min(im.width, 400), min(im.height, 400))).point(lambda v: v >> 4)
    counts = q.getcolors(4096) or [(0, None)]
    return max(c for c, _ in counts) >= 0.995 * q.width * q.height


def same(a, b):
    """True when two renders are the same picture (no pixel differs by more than 2 of 255)."""
    from PIL import ImageChops
    if a.size != b.size:
        return False
    return max(hi for _, hi in ImageChops.difference(a, b).getextrema()) <= 2


def compose(panels, notes, scale=0.5):
    """[(label, image)] side by side at `scale` (the renders are 2x), labels above, notes below."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        big = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
        small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 15)
    except OSError:
        big = small = ImageFont.load_default()
    shown = [(lab, im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS))
             for lab, im in panels]
    pad, gap, top = 24, 24, 64
    w = pad * 2 + sum(im.width for _, im in shown) + gap * (len(shown) - 1)
    h = top + max(im.height for _, im in shown) + 20 + 22 * len(notes) + pad
    sheet = Image.new("RGB", (w, h), (255, 255, 255))
    d, x = ImageDraw.Draw(sheet), pad
    for lab, im in shown:
        sheet.paste(im, (x, top))
        d.text((x, 28), lab, fill=(0, 0, 0), font=big)
        x += im.width + gap
    y = top + max(im.height for _, im in shown) + 14
    for line in notes:
        d.text((pad, y), line, fill=(60, 60, 60), font=small)
        y += 22
    return sheet


def save_small(im, out):
    """Write a JPEG under MAX_BYTES: quality from 88 down to 46 in steps of 6, then the same at 80%
    of the size, until it fits or the sheet is under 400 px wide. The size written."""
    from PIL import Image
    while True:
        for q in range(88, 40, -6):
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q, optimize=True)
            if buf.tell() <= MAX_BYTES:
                break
        if buf.tell() <= MAX_BYTES or im.width < 400:
            open(out, "wb").write(buf.getvalue())
            return buf.tell()
        im = im.resize((round(im.width * 0.8), round(im.height * 0.8)), Image.LANCZOS)


def sheet(key, frame, out, before_at=None, before_png=None, crop=None, build=None, viewport=None, notes=()):
    from PIL import Image
    frame = frame.replace("-", ":")
    told = []
    if before_png:
        saved = Image.open(before_png)
        if not opaque(saved.convert("RGBA")):
            told.append(f"before: {before_png} has see-through pixels, laid over white")
        before, how = flatten(saved), f"a render saved before the edit ({before_png})"
        if crop:
            box = tuple(round(2 * c) for c in crop[:2]) + tuple(round(2 * (c + d)) for c, d in zip(crop[:2], crop[2:]))
            before = before.crop(box)
    else:
        v = before_version(versions(key, before_at), before_at)
        if not v:
            print(f"REFUSED: no version of {key} holds the file before {before_at}; pass --before-png.")
            return 1
        links, canvas = chain(key, frame, v)
        used = []
        before, notes_b = panel(lambda n: render(key, n, v), links, crop, canvas, used)
        before, over_b = with_over(key, frame, links, crop, before, used, v)
        told += [f"before: {t}" for t in notes_b + over_b]
        how = f"version {v}, the one before the session open at {before_at}"
    links, canvas = chain(key, frame)
    used = []
    after, notes_a = panel(lambda n: render(key, n), links, crop, canvas, used)
    after, over_a = with_over(key, frame, links, crop, after, used)
    told += [f"after: {t}" for t in notes_a + over_a]
    for name, im in (("before", before), ("after", after)):
        if one_colour(im):
            print(f"REFUSED: the {name} render of {frame} is one colour, which is what a failed render looks like.")
            return 1
    if same(before, after):
        print(f"REFUSED: before and after of {frame} are the same picture. The before is {how}; it is not "
              "from before the edit, or the edit did not reach this frame" + (" or the crop" if crop else "") + ".")
        return 1
    panels = [("Before: Figma", before), ("After: Figma", after)]
    if build:
        clip = crop or (0, 0, viewport[0], viewport[1])
        panels.append((f"Build at {viewport[0]}", capture(build, viewport, clip)))
    size = save_small(compose(panels, list(notes)), out)
    print(f"WROTE {out}  {size // 1024} KB, {len(panels)} panels; before = {how}")
    for t in told:
        print(f"  NOTE {t}")
    print("THEN:")
    print(f"  1. python figma_post.py new|reply ... --edited <ids> --picture {out}")
    print("  2. figma_post attaches it right after the post (figma_attach.py, in the browser; REST cannot attach it)")
    return 0


def selftest():
    from PIL import Image, ImageDraw
    import tempfile
    results = []

    def case(name, ok):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    a = Image.new("RGB", (780, 400), (40, 60, 70))
    ImageDraw.Draw(a).rectangle([0, 300, 30, 399], fill=(110, 110, 110))
    b = a.copy()
    ImageDraw.Draw(b).rectangle([0, 300, 30, 399], fill=(40, 60, 70))
    case("same picture twice: same", same(a, a.copy()))
    case("strips filled in after the edit: not the same", not same(a, b))
    case("a 2/255 wobble is the same picture", same(a, a.point(lambda v: min(255, v + 2))))
    case("a flat render is one colour", one_colour(Image.new("RGB", (780, 400), (255, 255, 255))))
    case("a photo-like render is not", not one_colour(Image.effect_noise((300, 300), 60).convert("RGB")))
    vs = [{"id": "v1", "created_at": "2026-10-21T03:10:35Z"}, {"id": "v2", "created_at": "2026-10-21T05:34:23Z"},
          {"id": "v3", "created_at": "2026-10-21T06:06:07Z"}]
    case("edit at 05:50, inside v2's session: the version before it, v1", before_version(vs, "2026-10-21T05:50:00Z") == "v1")
    case("edit at 06:10, inside v3's session: v2", before_version(vs, "2026-10-21T06:10:00Z") == "v2")
    case("edit before the first session: none", before_version(vs, "2026-10-21T01:00:00Z") is None)
    case("edit after the last session: the version before the last", before_version(vs, "2026-10-21T09:00:00Z") == "v2")
    pages = {f"{API}/files/K/versions": {"versions": vs[:0:-1], "pagination": {"next_page": "https://api.figma.com/v1/files/K/versions?page_size=30&before=v2"}},
             "https://api.figma.com/v1/files/K/versions?page_size=30&before=v2": {"versions": vs[:1], "pagination": {}}}
    asked = []
    got = versions("K", "2026-10-21T05:50:00Z", get=lambda u: asked.append(u) or pages[u])
    case("an edit older than the first page: the second page is read and v1 found",
         before_version(got, "2026-10-21T05:50:00Z") == "v1" and len(asked) == 2)
    asked.clear()
    versions("K", "2026-10-21T06:10:00Z", get=lambda u: asked.append(u) or pages[u])
    case("two versions at or before the edit on page 1: page 2 is not asked for", len(asked) == 1)
    s = compose([("Before: Figma", a), ("After: Figma", b), ("Build at 390", b)], ["one note", "two"])
    case("three 2x panels at half size, side by side, with labels and notes",
         s.width == 24 * 2 + 3 * 390 + 2 * 24 and s.height == 64 + 200 + 20 + 44 + 24)
    with tempfile.TemporaryDirectory() as td:
        big = Image.effect_noise((2400, 1600), 90).convert("RGB")
        n = save_small(big, os.path.join(td, "x.jpg"))
        case(f"a noisy sheet is written under 500 KB ({n // 1024} KB)", n <= MAX_BYTES)
    # In place, at its real scale. A no-fill hero row inside a dark page: its 10% white
    # pill and white text, rendered alone, come back see-through.
    dark = (30, 20, 40)
    page = Image.new("RGBA", (800, 400), dark + (255,))
    row = Image.new("RGBA", (400, 80), (0, 0, 0, 0))
    for im, off in ((page, (200, 100)), (row, (0, 0))):
        layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
        g = ImageDraw.Draw(layer)
        g.rectangle([off[0] + 20, off[1] + 10, off[0] + 380, off[1] + 70], fill=(255, 255, 255, 26))
        g.rectangle([off[0] + 60, off[1] + 30, off[0] + 140, off[1] + 50], fill=(255, 255, 255, 255))
        im.alpha_composite(layer)
    links = [("F", (100, 50, 200, 40)), ("A", (0, 0, 400, 200))]
    calls = []

    def fake(renders):
        return lambda n: calls.append(n) or renders[n]
    old = row.convert("RGB")
    case("planted: the old alpha drop turns the pill as white as its text",
         old.getpixel((30, 15)) == old.getpixel((100, 40)) == (255, 255, 255))
    got, notes = panel(fake({"F": row, "A": page}), links)
    pill, text = got.getpixel((30, 15)), got.getpixel((100, 40))
    case("a see-through frame is drawn in its ancestor: the pill stays dark and the text shows",
         max(pill) < 80 and text == (255, 255, 255) and got.size == (400, 80) and calls == ["F", "A"])
    calls.clear()
    got, notes = panel(fake({"F": page}), [("F", (0, 0, 400, 200))])
    case("an opaque frame is rendered once, with no note", calls == ["F"] and not notes)
    # Figma shrinks a render over ~32 MP: a 1000x2000 page comes back at 1.5x. A red mark at design
    # (400, 1500, 50, 50); the frame sits at (380, 1480, 90, 90) and renders see-through.
    tall = Image.new("RGBA", (1500, 3000), dark + (255,))
    ImageDraw.Draw(tall).rectangle([600, 2250, 674, 2324], fill=(220, 0, 0, 255))

    def red(p):
        return p[0] > 180 and p[1] < 60 and p[2] < 60
    got, notes = panel(fake({"F": Image.new("RGBA", (180, 180), (0, 0, 0, 0)), "A": tall}),
                       [("F", (380, 1480, 90, 90)), ("A", (0, 0, 1000, 2000))])
    case("a shrunk ancestor render is cut at its real scale: the mark sits where the frame has it",
         got.size == (180, 180) and red(got.getpixel((90, 90))) and max(got.getpixel((20, 20))) < 60
         and any("1.50x" in t for t in notes))
    got, notes = panel(fake({"F": tall}), [("F", (0, 0, 1000, 2000))], crop=(400, 1500, 50, 50))
    case("--crop on a shrunk render lands on its box: all mark, at 2x",
         got.size == (100, 100) and all(red(got.getpixel(p)) for p in ((10, 10), (50, 50), (90, 90))))
    got, notes = panel(fake({"F": row, "A": row.resize((800, 160))}),
                       [("F", (0, 0, 200, 40)), ("A", (0, 0, 400, 80))], canvas=(229, 229, 229))
    case("nothing opaque under the frame: laid on the canvas colour, and said so",
         got.getpixel((2, 2)) == (229, 229, 229) and any("canvas" in t for t in notes))
    half = Image.new("RGBA", (10, 10), (0, 0, 0, 128))
    case("a see-through saved PNG is laid over white, not alpha-dropped to black",
         abs(flatten(half).getpixel((5, 5))[0] - 127) <= 2 and half.convert("RGB").getpixel((5, 5)) == (0, 0, 0))
    # Drawn over from outside: labels on the section put back. A label on the
    # section after the screen and over the cut is picked; one under the screen, a hidden one and one
    # beside it are not. The picked one is pasted where the canvas draws it.
    def nd(i, x, y, w, h, **kw):
        return {"id": i, "absoluteBoundingBox": {"x": x, "y": y, "width": w, "height": h}, **kw}

    def lin(path, kids):
        """A lineage as figma_client.lineage gives it: page first, each entry but the last with kids."""
        return [dict(n, kids=kids.get(n["id"], [])) if j < len(path) - 1 else n for j, n in enumerate(path)]
    sec, scr = nd("9:1", 0, 0, 2000, 2000, type="SECTION"), nd("1:1", 100, 100, 390, 900, type="FRAME")
    kids = {"0:1": [sec], "9:1": [nd("7:9", 400, 500, 30, 8), scr, nd("7:10", 400, 520, 26, 6),
                                  nd("7:11", 400, 540, 26, 6, visible=False), nd("7:12", 1500, 1500, 80, 20)]}
    path = lin([{"id": "0:1", "type": "CANVAS"}, sec, scr], kids)
    got = pick_over(path, (100, 172, 390, 707))
    case("a section layer on top of the cut is picked, and only that one",
         [g[0] for g in got] == ["7:10"] and got[0][2] == "9:1")
    case("nothing is picked when the cut misses it", pick_over(path, (100, 172, 390, 300)) == [])
    hero = nd("2:2", 0, 0, 1440, 700, type="FRAME", clipsContent=True)
    mock = nd("2:3", 100, 600, 800, 400, type="FRAME")
    top = nd("2:1", 0, 0, 1440, 5000)
    clip_kids = {"0:1": [top], "2:1": [hero, nd("2:4", 0, 700, 1440, 900)], "2:2": [mock]}
    case("a frame over only the part a clipping ancestor cuts off is not picked",
         pick_over(lin([{"id": "0:1", "type": "CANVAS"}, top, hero, mock], clip_kids), (100, 600, 800, 400)) == [])
    hero["clipsContent"] = False
    case("...and is picked when that ancestor does not clip",
         [g[0] for g in pick_over(lin([{"id": "0:1", "type": "CANVAS"}, top, hero, mock], clip_kids),
                                  (100, 600, 800, 400))] == ["2:4"])
    base = Image.new("RGB", (780, 1414), (30, 20, 40))
    dot = Image.new("RGBA", (52, 12), (220, 0, 0, 255))
    pasted = overlay(base, got, (100, 172), lambda n: dot)
    case("the picked layer is pasted where the canvas draws it, at 2x",
         red(pasted.getpixel((2 * (400 - 100) + 5, 2 * (520 - 172) + 5))) and not red(pasted.getpixel((5, 5))))
    print(f"{sum(results)} of {len(results)} planted cases behave as expected.")
    return 0 if all(results) else 1


def _opt(args, flag, many=False):
    vals = []
    while flag in args:
        i = args.index(flag)
        vals.append(args[i + 1] if i + 1 < len(args) else "")
        del args[i:i + 2]
    return vals if many else (vals[-1] if vals else None)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if args[0] == "selftest":
        sys.exit(selftest())
    if args[0] == "sheet":
        out, at, png = _opt(args, "--out"), _opt(args, "--before-at"), _opt(args, "--before-png")
        crop, build, vp = _opt(args, "--crop"), _opt(args, "--build"), _opt(args, "--viewport")
        notes = _opt(args, "--note", many=True)
        if len(args) != 3 or not out or not (at or png) or (build and not vp):
            sys.exit(__doc__)
        crop = tuple(float(c) for c in crop.split(",")) if crop else None
        vp = tuple(int(c) for c in vp.lower().split("x")) if vp else None
        sys.exit(sheet(args[1], args[2], out, at, png, crop, build, vp, notes))
    sys.exit(__doc__)
