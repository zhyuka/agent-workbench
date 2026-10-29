"""Every thread on the same layer of the same component, on any page of the file.

  python figma_family.py nodes <file_key> <node_id> [<node_id> ...]
  python figma_family.py build <file_key> [--json FILE]
  python figma_family.py selftest

WHY THIS EXISTS. A shared component (a footer, a card, a banner) appears as separate copies on
separate pages. A finding measured on one copy gets drafted as a NEW comment while a thread on
another page already asks design the same thing about the same layer of the same component. Every
check that matches by where a comment sits (on the node, on an ancestor with an offset inside it,
on a child, in its section) says "clear", and a search by words misses it too when the thread
names the layer differently ("frame holding the banner" vs "heading"). None of them match by what
the node IS. This one does.

HOW. One GET of the whole file (for a large file: hundreds of MB, over a minute, one Tier-1 call,
gigabytes of memory to parse) becomes an index of every node: type, name, parent, page, box,
componentId and whether it or an ancestor is hidden. The index is cached at
~/.cache/agent-workbench/figma_family/<key>.pkl and is rebuilt when it is older than MAX_AGE_H hours. A node or pinned frame the index lacks is
fetched live: its path from figma_client.lineage, its subtree in one nodes?ids= read.

A COPY of component K is one of three things. An INSTANCE whose componentId is K. K's main
component itself. A FRAME whose tree has the same shape as K's main, meaning the same type and
name for every layer below it, with at least MIN_LAYERS layers. That last kind is a detached copy,
and REST gives no other link from it back to K. A widely used component can have dozens of each.

A thread's pin point is its pinned node's box plus node_offset. From the pinned node it descends
through visible layers to the deepest one under the point, taking the topmost where layers
overlap. Every copy on that path records (K, copy, the point measured from the copy's top-left).
For a cited node, every copy on ITS path gives K and the node's box measured the same way. A
thread is on the SAME LAYER when its point for the same K falls inside that box, with an 8px
margin, on any copy, this one included. A thread in a copy of K but outside the box is counted,
not listed.

The point is compared rather than the layer id because a detached copy's layers have ids of
their own. Calibrate on a known case before trusting a zero: a cited layer with a known thread on
another copy must list it, and an uncommented layer must list none. On a real file most layers
list zero or one other thread; a layer that covers most of its component lists many.
MIN_LAYERS is 10 because a small shape (a 4-layer button) is shared by thousands of instances of
other components, so a small frame's shape says nothing about where it came from, while a large
component's shape matches only its own main, instances and detached copies.

BLIND TO:
  - A detached copy of a library component. Its main is not in this file, so there is no shape to
    match. Instances of it are still found by componentId.
  - A detached copy whose layer names or structure were edited, and any copy smaller than
    MIN_LAYERS.
  - Another variant of the same component set. That is a different component, with its own layout.
  - Layers added after the index was built, inside a node it already holds. Every report prints
    the index age; `build` refreshes it.
  - Boxes are as of the build, not as of each comment. A moved layer takes its threads with it.
    Clipping and rotation are ignored, and so is a copy resized enough to move the layer.
  - Subject. A same-layer thread can be about something else. Read it, as with SAME NODE, OTHER
    SUBJECT in figma_comments.py.
"""
import hashlib, json, os, pickle, re, sys, tempfile, time, urllib.error, urllib.request
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API = "/v1"   # a Figma REST path; _get sends it through figma_client.py
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "agent-workbench", "figma_family")
MAX_AGE_H = 24
MIN_LAYERS = 10
MARGIN = 8
BOXY = {"FRAME": "BOX", "COMPONENT": "BOX", "INSTANCE": "BOX"}
T, NAME, PAR, PAGE, BOX, CID, HID = range(7)


def norm(nid):
    """A node id in any form a draft or a URL writes it: 1-2, 1:2, I1:2;3:4, I1-2%3B3-4."""
    nid = (nid or "").strip().replace("%3A", ":").replace("%3B", ";").replace("%3a", ":").replace("%3b", ";")
    return re.sub(r"(\d)-(\d)", r"\1:\2", nid)


def _token():
    import figma_auth
    try:
        return figma_auth.token()
    except SystemExit:  # figma_auth exits when no token is set; check() must not
        raise RuntimeError("no Figma token in this process (python figma_auth.py --source says where it looked)") from None


def _get(url, timeout):
    """JSON from Figma, through figma_client.py beside this file: paced with every other tool, and a
    429 waited out. Raises on any other failure; a caller turns that into DID NOT RUN."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import figma_client
    return figma_client.get(url, token=_token(), tool="figma_family", timeout=timeout)


def _lineage(key, ids, timeout):
    """figma_client.lineage without kids, the way _get reads: {id: [page, ..., node] or None}."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import figma_client
    return figma_client.lineage(key, ids, kids=False, token=_token(), tool="figma_family", timeout=timeout)


def _box(n):
    b = n.get("absoluteBoundingBox")
    return (b["x"], b["y"], b["width"], b["height"]) if b and b.get("width") is not None else None


def _walk(root, parent, page, hidden, idx, shape, text=None):
    """Add root and every layer under it to idx; put the shape of every big box-like node in
    shape, and the words on every text-bearing layer (text, sticky, shape, table cell, code block)
    in text. Iterative, so a deep tree cannot hit the recursion limit."""
    full, size = {}, {}
    stack = [(root, parent, hidden, False)]
    while stack:
        n, par, hid, done = stack.pop()
        kids = n.get("children") or ()
        if not done:
            h = hid or n.get("visible", True) is False
            idx[n["id"]] = (n["type"], n.get("name", ""), par, page or n["id"], _box(n),
                            n.get("componentId"), h)
            words = n.get("characters") or n.get("code")
            if text is not None and isinstance(words, str) and words.strip():
                text[n["id"]] = words
            stack.append((n, par, hid, True))
            # Reversed, so siblings pop, and land in idx, in file order: bottom of the z-order first.
            stack.extend((k, n["id"], h, False) for k in reversed(kids))
            continue
        t = BOXY.get(n["type"], n["type"])
        ch = ",".join(full.pop(k["id"]) for k in kids)
        sz = 1 + sum(size.pop(k["id"]) for k in kids)
        full[n["id"]] = hashlib.sha1(f"{t}|{n.get('name', '')}|{ch}".encode()).hexdigest()[:16]
        size[n["id"]] = sz
        if t == "BOX" and sz >= MIN_LAYERS:
            # The root's own type and name are left out: an instance takes its set's name and a
            # detached copy is often renamed, but the layers below keep theirs.
            shape[n["id"]] = hashlib.sha1(f"{t}|{ch}".encode()).hexdigest()[:16]


def from_doc(doc):
    """The index for one GET /files/<key> response; text is the words on the canvas, for a search
    over what a file says rather than only its comments."""
    idx, shape, text = {}, {}, {}
    for page in doc["document"].get("children") or ():
        _walk(page, None, page["id"], False, idx, shape, text)
    comps, sets = doc.get("components") or {}, doc.get("componentSets") or {}
    names = {}
    for k, c in comps.items():
        s = (sets.get(c.get("componentSetId")) or {}).get("name")
        names[k] = f"{s} / {c.get('name', '?')}" if s else c.get("name", "?")
    by_shape = defaultdict(list)
    for k, h in shape.items():
        if idx[k][T] == "COMPONENT":
            by_shape[h].append(k)
    frame_of = {k: tuple(by_shape[h]) for k, h in shape.items()
                if idx[k][T] == "FRAME" and h in by_shape}
    return {"idx": idx, "shape": shape, "by_shape": dict(by_shape), "frame_of": frame_of,
            "names": names, "text": text, "version": doc.get("version"), "lastModified": doc.get("lastModified"),
            "built": time.time()}


def cache_path(key):
    return os.path.join(CACHE, f"{key}.pkl")


def build(key, src=None, log=sys.stderr):
    """Fetch the whole file (or read src, a saved GET /files/<key> response), index it, cache it."""
    t0 = time.time()
    if src:
        with open(src, "rb") as f:
            doc = json.load(f)
        built = os.path.getmtime(src)
    else:
        print(f"  building the family index for {key}: one GET of the whole file, this can take minutes "
              "and gigabytes of memory on a large file", file=log, flush=True)
        doc = _get(f"{API}/files/{key}", timeout=600)  # figma-tree-read: the copy index needs every node on every page
        built = time.time()
    data = from_doc(doc)
    del doc
    data["built"], data["key"] = built, key
    os.makedirs(CACHE, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, cache_path(key))
    print(f"  family index for {key}: {len(data['idx']):,} nodes, {len(data['frame_of']):,} detached "
          f"copies, {time.time() - t0:.0f} s", file=log, flush=True)
    return data


def load(key, mode="auto", log=sys.stderr):
    """-> (Index or None, note). mode "auto" builds when the cache is missing or older than
    MAX_AGE_H, "cache" never builds and takes a stale cache as it is, "force" always builds.
    A failed build falls back to the stale cache and says so in note."""
    data, note = None, ""
    p = cache_path(key)
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                data = pickle.load(f)
        except Exception as e:
            note = f"the cached index could not be read ({e.__class__.__name__})"
    age = (time.time() - data["built"]) / 3600 if data else None
    # An index built before text was kept has no "text"; "auto" rebuilds it like a stale one.
    if mode == "force" or (mode == "auto" and (data is None or age > MAX_AGE_H or "text" not in data)):
        try:
            data = build(key, log=log)
        except Exception as e:
            why = f"the rebuild failed ({e.__class__.__name__}: {str(e)[:120]})"
            if data is None:
                return None, why
            note = f"{why}, so this uses the cached index"
    if data is None:
        return None, note or f"no family index for {key} yet (python figma_family.py build {key})"
    return Index(data), note


def inside(pt, b, m=0):
    return bool(b) and b[0] - m <= pt[0] <= b[0] + b[2] + m and b[1] - m <= pt[1] <= b[1] + b[3] + m


class Index:
    def __init__(self, data):
        self.d = data
        self.idx = data["idx"]
        self.text = data.get("text") or {}  # {node id: words}; empty in an index built before text was kept
        self.kids = defaultdict(list)
        self.inst = defaultdict(int)
        for k, v in self.idx.items():
            if v[PAR]:
                self.kids[v[PAR]].append(k)
            if v[T] == "INSTANCE" and v[CID]:
                self.inst[v[CID]] += 1
        self.frames = defaultdict(int)
        for ks in data["frame_of"].values():
            for k in ks:
                self.frames[k] += 1
        # GET /files lists children bottom to top. _walk adds them to idx in that order, so each
        # kids list is in it too, and descend() reads it backwards to take the topmost layer.

    def age_h(self):
        return (time.time() - self.d["built"]) / 3600

    def chain(self, n):
        out, seen = [], set()
        while n and n in self.idx and n not in seen:
            seen.add(n)
            out.append(n)
            n = self.idx[n][PAR]
        return out[::-1]

    def descend(self, n, pt):
        path = [n]
        while True:
            nxt = next((c for c in reversed(self.kids.get(n, ())) if not self.idx[c][HID]
                        and inside(pt, self.idx[c][BOX])), None)
            if not nxt:
                return path
            path.append(nxt)
            n = nxt

    def comps_of(self, n):
        v = self.idx[n]
        if v[T] == "INSTANCE":
            return (v[CID],) if v[CID] else ()
        if v[T] == "COMPONENT":
            return (n,)
        if v[T] == "FRAME":
            return self.d["frame_of"].get(n, ())
        return ()

    def name(self, k):
        return self.d["names"].get(k) or (self.idx.get(k) or ("", "?"))[NAME]

    def page_name(self, n):
        v = self.idx.get(n)
        return self.idx.get(v[PAGE], ("", "?"))[NAME].strip() if v else "?"

    def top_frame(self, n):
        c = self.chain(n)
        return c[1] if len(c) > 1 else n

    def add_live(self, key, ids):
        """Fetch nodes the index lacks, with the path to each, and add them. -> ids still missing.
        The path is the node's lineage (figma_client.lineage), the node's own subtree one nodes?ids=
        read."""
        want = sorted({i for i in ids if i and i not in self.idx})
        if not want:
            return []
        lin = _lineage(key, want, timeout=180)
        have = [i for i in want if lin.get(i)]
        got = (_get(f"{API}/files/{key}/nodes?ids={','.join(have)}", timeout=180).get("nodes") or {}) if have else {}
        found = []
        for i in have:
            path, n = lin[i], (got.get(i) or {}).get("document")
            if not n:
                continue
            page, par, hid = path[0]["id"], None, False
            for a in path[:-1]:
                h = hid or a.get("visible", True) is False
                if a["id"] not in self.idx and a["id"] not in have:     # one asked for is added whole below
                    self.idx[a["id"]] = (a["type"], a.get("name", ""), par, page, _box(a), a.get("componentId"), h)
                    if par:
                        self.kids[par].append(a["id"])
                par, hid = a["id"], h
            found.append((n, par, page, hid))
        for n, par, page, hid in found:
            new, shape = {}, {}
            _walk(n, par, page, hid, new, shape)
            for k, v in new.items():
                if k in self.idx:
                    continue
                self.idx[k] = v
                if v[PAR]:
                    self.kids[v[PAR]].append(k)
                if v[T] == "FRAME" and shape.get(k) in self.d["by_shape"]:
                    self.d["frame_of"][k] = tuple(self.d["by_shape"][shape[k]])
        return [i for i in want if i not in self.idx]

    def place(self, c):
        """-> ([(component, copy, point from the copy's top-left), ...], None), or (None, why) for a
        thread with no node, a deleted node, or a node with no box (a page)."""
        m = c.get("client_meta") or {}
        nid = norm(m.get("node_id") if isinstance(m, dict) else "")
        if not nid:
            return None, "canvas"
        if nid not in self.idx:
            return None, "gone"
        b = self.idx[nid][BOX]
        if not b:
            return None, "page"
        off = m.get("node_offset")
        if off:
            pt = (b[0] + off.get("x", 0), b[1] + off.get("y", 0))
            path = self.chain(nid)[:-1] + self.descend(nid, pt)
        else:
            pt = (b[0] + b[2] / 2, b[1] + b[3] / 2)
            path = self.chain(nid)
        out = []
        for p in path:
            pb = self.idx[p][BOX]
            if pb:
                out.extend((k, p, (pt[0] - pb[0], pt[1] - pb[1])) for k in self.comps_of(p))
        return out, None

    def levels(self, n):
        """Every copy on n's path, outermost first: (component, copy, n's box in the copy)."""
        nb = self.idx[n][BOX]
        out = []
        for p in self.chain(n):
            pb = self.idx[p][BOX]
            if nb and pb:
                out.extend((k, p, (nb[0] - pb[0], nb[1] - pb[1], nb[2], nb[3])) for k in self.comps_of(p))
        return out


class Report:
    """What check() found. ran is False when there was no index; why then says why."""

    def __init__(self, key):
        self.key, self.ran, self.why, self.note = key, False, "", ""
        self.ix, self.hits, self.missing = None, {}, []
        self.placed = self.total = 0
        self.unplaced = defaultdict(int)

    def same_layer(self, n, open_only=True):
        """{thread id: root comment} on the same layer as n, on any copy."""
        out = {}
        for lv in self.hits.get(norm(n)) or ():
            for c, _ in lv["same"]:
                if not (open_only and c.get("resolved_at")):
                    out[c["id"]] = c
        return out

    def copies(self, n):
        """The copies of each component on n's path, as ids to search other sources with."""
        return [lv["copy"] for lv in self.hits.get(norm(n)) or ()]


def check(key, ids, comments=None, mode="auto", log=sys.stderr):
    """The same-layer threads for each id. Never raises: a failure is Report.ran False plus why."""
    rep = Report(key)
    ix, rep.note = load(key, mode, log)
    if ix is None:
        rep.why = rep.note
        return rep
    rep.ix = ix
    try:
        if comments is None:
            comments = _get(f"{API}/files/{key}/comments", timeout=90)["comments"]
        roots = [c for c in comments if not c.get("parent_id")]
        ids = [norm(i) for i in ids]
        built = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ix.d["built"]))
        later = {norm((c.get("client_meta") or {}).get("node_id")) for c in roots
                 if c.get("created_at", "") >= built and isinstance(c.get("client_meta"), dict)}
        try:
            rep.missing = ix.add_live(key, set(ids) | later)
        except Exception as e:
            rep.note = (rep.note + "; " if rep.note else "") + (
                f"the live fetch of nodes newer than the index failed ({e.__class__.__name__})")
            rep.missing = [i for i in set(ids) | later if i and i not in ix.idx]
    except Exception as e:
        rep.why = f"the comments could not be read ({e.__class__.__name__}: {str(e)[:120]})"
        return rep
    places = {}
    for c in roots:
        got, why = ix.place(c)
        if got is None:
            rep.unplaced[why] += 1
        else:
            places[c["id"]] = (c, got)
    rep.total, rep.placed = len(roots), len(places)
    for n in ids:
        if n not in ix.idx:
            rep.hits[n] = None
            continue
        out = []
        for k, copy, r in ix.levels(n):
            same, other = [], []
            for c, got in places.values():
                hit = next(((p, rp) for kk, p, rp in got if kk == k), None)
                if hit is None:
                    continue
                (same if inside(hit[1], r, MARGIN) else other).append((c, hit[0]))
            out.append({"comp": k, "copy": copy, "box": r, "whole": copy == n,
                        "same": sorted(same, key=lambda t: t[0].get("created_at", "")), "other": other})
        rep.hits[n] = out
    rep.ran = True
    return rep


def coverage(rep):
    if not rep.ran:
        return f"family check DID NOT RUN for {rep.key}: {rep.why}. Its zero is not a clear."
    ix = rep.ix
    gone = ", ".join(f"{v} {k}" for k, v in sorted(rep.unplaced.items()))
    s = (f"family index for {rep.key}: built {ix.age_h():.1f} h ago, {len(ix.idx):,} nodes. "
         f"{rep.placed} of {rep.total} threads placed" + (f" (not placed: {gone})" if gone else "") + ".")
    if rep.note:
        s += f" Note: {rep.note}."
    return s


def lines(rep, n, link=None, show_resolved=True):
    """The printed block for one cited id. link(c) -> permalink, or None to leave links out."""
    n = norm(n)
    if not rep.ran:
        return [f"  family check DID NOT RUN: {rep.why}"]
    ix, levels = rep.ix, rep.hits.get(n)
    if levels is None:
        return [f"  family: {n} is not in this file, so no copy of it was looked for."]
    if not levels:
        return [f"  family: {n} is not inside a copy of any component (instance, main, or detached "
                f"frame of {MIN_LAYERS}+ layers), so there is no other copy to check."]
    out = []
    for lv in levels:
        k, copy, r = lv["comp"], lv["copy"], lv["box"]
        what = "is" if lv["whole"] else f"sits at ({r[0]:.0f}, {r[1]:.0f}) {r[2]:.0f}x{r[3]:.0f} in"
        out.append(f"  family: {n} {what} copy {copy} of {ix.name(k)!r} ({k}): "
                   f"{ix.inst.get(k, 0)} instance(s), {ix.frames.get(k, 0)} detached frame(s)"
                   + (", and the main" if k in ix.idx and ix.idx[k][T] == "COMPONENT" else "") + ".")
        same = [t for t in lv["same"] if show_resolved or not t[0].get("resolved_at")]
        nopen = sum(1 for c, _ in lv["same"] if not c.get("resolved_at"))
        out.append(f"    same layer, any copy: {nopen} open, {len(lv['same']) - nopen} resolved thread(s).")
        for c, p in same:
            where = "this copy" if p == copy else (f"copy {p} on page {ix.page_name(p)!r}, frame "
                                                   f"{ix.idx[ix.top_frame(p)][NAME]!r}")
            state = f"RESOLVED {c['resolved_at'][:10]}" if c.get("resolved_at") else "OPEN"
            msg = re.sub(r"\s+", " ", c.get("message", "")).strip()
            out.append(f"      thread {c['id']}  {state:19} {(c.get('user') or {}).get('handle', '?'):18} "
                       f"{c.get('created_at', '')[:10]}  on {where}")
            out.append(f"          {msg[:150]}")
            if link:
                out.append(f"          {link(c)}")
        no = sum(1 for c, _ in lv["other"] if not c.get("resolved_at"))
        if lv["other"]:
            out.append(f"    elsewhere in a copy of it: {no} open, {len(lv['other']) - no} resolved, "
                       "not listed (outside this layer's box).")
        if lv["whole"]:
            out.append("    The cited node is the whole copy, so every thread in any copy counts as the "
                       "same layer. Cite the layer the finding is about to narrow it.")
    return out


def _planted():
    """(doc, comments): a 10-layer main M, instances A and B on two pages, a detached copy D, a
    4-layer look-alike, and threads on each. figma_post.py's selftest uses it too."""
    def box(x, y, w, h):
        return {"x": x, "y": y, "width": w, "height": h}

    def layers(prefix, x, y):
        # a backdrop under everything, the heading at (10, 10) 100x20, eight bars along the bottom
        return [{"id": f"{prefix}bg", "type": "RECTANGLE", "name": "Backdrop", "absoluteBoundingBox": box(x, y, 300, 200)},
                {"id": f"{prefix}h", "type": "TEXT", "name": "Heading", "absoluteBoundingBox": box(x + 10, y + 10, 100, 20)}] + [
            {"id": f"{prefix}{i}", "type": "RECTANGLE", "name": f"Bar {i}",
             "absoluteBoundingBox": box(x + 10 + 20 * i, y + 150, 10, 10)} for i in range(8)]

    def copy(cid, typ, x, y, **kw):
        return dict(id=cid, type=typ, name=kw.pop("name", "footer"), absoluteBoundingBox=box(x, y, 300, 200),
                    children=layers(cid + "/", x, y), **kw)

    small = [{"id": "s1", "type": "TEXT", "name": "Label", "characters": "Planted build cost: $5.49 a build"},
             {"id": "s2", "type": "VECTOR", "name": "Icon"},
             {"id": "s3", "type": "VECTOR", "name": "Icon 2"}]
    doc = {"document": {"id": "0:0", "type": "DOCUMENT", "children": [
        {"id": "P1", "type": "CANVAS", "name": "Page A", "children": [
            copy("M", "COMPONENT", 0, 0, name="footer / main"),
            {"id": "F1", "type": "FRAME", "name": "Page A frame", "absoluteBoundingBox": box(1000, 0, 400, 900),
             "children": [copy("A", "INSTANCE", 1000, 500, componentId="M")]},
            {"id": "SB", "type": "COMPONENT", "name": "button", "absoluteBoundingBox": box(0, 400, 50, 20), "children": small}]},
        {"id": "P2", "type": "CANVAS", "name": "Page B", "children": [
            {"id": "F2", "type": "FRAME", "name": "Page B frame", "absoluteBoundingBox": box(5000, 0, 400, 900),
             "children": [copy("B", "INSTANCE", 5000, 600, componentId="M"),
                          copy("D", "FRAME", 5000, 0, name="footer (detached)"),
                          {"id": "SF", "type": "FRAME", "name": "not a button", "absoluteBoundingBox": box(5000, 300, 50, 20),
                           "children": [dict(s, id="x" + s["id"]) for s in small]}]}]}]},
        "components": {"M": {"name": "footer"}, "SB": {"name": "button"}}, "componentSets": {}}
    # Threads: on A's heading via the page frame and an offset (as the UI pins them), elsewhere in A,
    # on D's heading, on the look-alike frame, and resolved on B's heading.
    cm = lambda node, x, y: {"node_id": node, "node_offset": {"x": x, "y": y}}
    comments = [
        {"id": "t1", "message": "heading sits 9px high", "created_at": "2026-10-05T00:00:00Z", "client_meta": cm("F1", 20, 515)},
        {"id": "t2", "message": "bar colour", "created_at": "2026-10-05T00:00:00Z", "client_meta": cm("F1", 15, 655)},
        {"id": "t3", "message": "detached heading", "created_at": "2026-10-05T00:00:00Z", "client_meta": cm("D/h", 1, 1)},
        {"id": "t4", "message": "look-alike", "created_at": "2026-10-05T00:00:00Z", "client_meta": cm("SF", 1, 1)},
        {"id": "t5", "message": "old heading", "created_at": "2026-10-05T00:00:00Z", "resolved_at": "2026-10-05T01:00:00Z",
         "client_meta": cm("B/h", 2, 2)},
        {"id": "t6", "message": "on a page", "created_at": "2026-10-05T00:00:00Z", "client_meta": {"node_id": "P1"}},
        {"id": "t7", "message": "deleted", "created_at": "2026-10-05T00:00:00Z", "client_meta": cm("9:9", 0, 0)}]
    return doc, comments


def plant(key, doc):
    """Index doc and cache it under key, as build() would. For the selftests."""
    data = from_doc(doc)
    data["built"] = time.time()
    os.makedirs(CACHE, exist_ok=True)
    with open(cache_path(key), "wb") as f:
        pickle.dump(data, f)
    return data


def selftest():
    """The planted file from _planted(), each rule checked on it. No network."""
    global CACHE
    doc, comments = _planted()
    results = []

    def case(name, ok, got=""):
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n        got: {got!r}"))
    saved, CACHE = CACHE, tempfile.mkdtemp()
    try:
        rep0 = check("K", ["B/h"], comments, mode="cache")
        case("no index: did not run, and says so", not rep0.ran and "DID NOT RUN" in coverage(rep0), coverage(rep0))
        data = plant("K", doc)
        case("detached frame D matches main M by shape", data["frame_of"].get("D") == ("M",), data["frame_of"])
        case("4-layer look-alike SF does not match button SB", "SF" not in data["frame_of"], data["frame_of"])
        case("text: the words on label s1 and its copy xs1 are kept, and no layer without words is",
             sorted(data["text"]) == ["s1", "xs1"] and "$5.49" in data["text"]["s1"], data["text"])
        rep = check("K", ["B/h", "B/3", "F2", "A"], comments, mode="cache")
        same = sorted(rep.same_layer("B/h", open_only=False))
        case("heading on B finds t1 (A, other page) and t3 (detached), t5 resolved on B; not t2",
             same == ["t1", "t3", "t5"], same)
        case("the gate list leaves out the resolved t5", sorted(rep.same_layer("B/h")) == ["t1", "t3"],
             rep.same_layer("B/h"))
        case("an uncommented layer B/3 has no same-layer thread", not rep.same_layer("B/3", open_only=False),
             rep.same_layer("B/3", open_only=False))
        case("a page frame outside any copy has no family", rep.hits.get("F2") == [], rep.hits.get("F2"))
        case("the whole copy A takes every thread in any copy", sorted(rep.same_layer("A")) == ["t1", "t2", "t3"],
             rep.same_layer("A"))
        case("unplaced threads are counted by reason", dict(rep.unplaced) == {"page": 1, "gone": 1},
             dict(rep.unplaced))
        case("a pin over two layers lands on the top one", rep.ix.descend("A", (1020, 515)) == ["A", "A/h"],
             rep.ix.descend("A", (1020, 515)))
        text = "\n".join(lines(rep, "B/h"))
        case("the report names the other page", "Page A" in text and "detached" in text, text)
        import figma_auth
        real_token, figma_auth.token = figma_auth.token, lambda required=True: sys.exit("planted: no token")
        try:
            rep = check("K", ["77:77", "B/h"], comments, mode="cache")
            got = (rep.ran, "live fetch" in (rep.note or ""), sorted(rep.same_layer("B/h")))
        except BaseException as e:
            got = repr(e)
        finally:
            figma_auth.token = real_token
        case("no token for the live fetch: still runs on the index, and notes it", got == (True, True, ["t1", "t3"]), got)
        # hits holds None for an id the file lacks, so lines() can say so; the readers treat it as empty.
        try:
            got = (rep.hits.get("77:77", "absent"), rep.same_layer("77:77"), rep.copies("77:77"))
        except Exception as e:
            got = repr(e)
        case("an id not in the file has no family, and same_layer and copies return empty",
             got == (None, {}, []), got)
    finally:
        import shutil
        shutil.rmtree(CACHE, ignore_errors=True)
        CACHE = saved
    print(f"{sum(results)} of {len(results)} planted cases behave as expected.")
    return 0 if all(results) else 1


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if not argv:
        sys.exit(__doc__)
    cmd = argv[0]
    if cmd == "selftest":
        return selftest()
    if cmd == "build" and len(argv) >= 2:
        src = argv[argv.index("--json") + 1] if "--json" in argv else None
        build(argv[1], src)
        return 0
    if cmd == "nodes" and len(argv) >= 3:
        import figma_comments as fc
        key = argv[1]
        rep = check(key, argv[2:])
        for n in argv[2:]:
            v = (rep.ix.idx.get(norm(n)) if rep.ran else None) or ("?", "")
            print(f"{n} {v[NAME]!r} ({v[T]})")
            print("\n".join(lines(rep, n, link=lambda c: fc.permalink(key, c))))
            print()
        print(coverage(rep))
        if not rep.ran:
            return 2
        return 3 if any(rep.same_layer(n) for n in argv[2:]) else 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
