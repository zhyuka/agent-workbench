"""Enumerate every component set used in a Figma frame: variant matrix, component properties,
the variable bindings on each instance node itself (not its child layers) resolved per MODE, and
which variants an instance actually uses.

  python tools/figma_variants.py <node-id> [--file-key KEY] [--mode Dark] [--unused]

Without --file-key it reads the file from the config key figma.default_file (kit_config.py); with
neither, it stops with exit 2 and names the key.

WHY THIS EXISTS. Resolved fill values read off one instance in one mode miss four things at once:

  1. THE VARIANT MATRIX. A `badge` set with type(default|inactive) x state(default|hover) has 4
     variants. Reading one instance tells you about one of the four.
  2. COMPONENT PROPERTIES that are not variants. The same badge can carry a TEXT property and a
     BOOLEAN such as `external link`. Neither shows up in a fill dump, and the boolean changes the
     rendered content.
  3. VARIABLE BINDINGS. The fill is not rgba(255,255,255,0.15); it is a variable such as
     `badge/primary/background`, which RESOLVES to that in Dark mode and to something else in
     Light. Building the resolved number hardcodes one mode.
  4. ALIAS CHAINS. That variable can be an alias to another, such as `white/15%`, which is itself
     an alias. One hop is not enough; this walks to the leaf.

The concrete miss: a badge whose own hover is rgba(255,255,255,0.30) where the default is 0.15. A
dump of the default variant reports 0.15 for both and reads as "no hover change".

WHAT IT DOES NOT DO. It cannot tell you whether the BUILD implements a variant - it has no view of
the CSS. It lists what the design defines so the gap is visible; closing it is still a judgement.

EXIT. 0 listed; 1 no Figma token, the node does not resolve, or Figma answered an error; 2 bad
arguments or no file key (neither --file-key nor figma.default_file).

Auth: via figma_auth.py beside this file (its header gives the order it looks in). Never printed.
"""
import argparse, json, sys, urllib.error, urllib.parse
from collections import defaultdict

import figma_auth
import kit_config

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API = "/v1"   # a Figma REST path; get() sends it through figma_client.py beside this file


def token():
    return figma_auth.token()


def get(url, timeout=120):
    """Through figma_client.py: paced with every other tool, and a 429 waited out."""
    import figma_client
    try:
        return figma_client.get(url, token=token(), tool="figma_variants", timeout=timeout)
    except urllib.error.HTTPError as e:
        sys.exit(f"Figma API {e.code}: {e.reason}")


def rgba(c):
    if not isinstance(c, dict) or "r" not in c:
        return c
    return f"rgba({round(c['r']*255)},{round(c['g']*255)},{round(c['b']*255)},{round(c.get('a',1),2)})"


class Vars:
    """Variable table with alias resolution. /variables/local returns SUBSCRIBED variables too
    (often more of them than local ones), so filter collection.remote before counting. Here we only
    look ids up, so remote ones are welcome."""

    def __init__(self, key):
        self.v, self.c = {}, {}
        try:
            d = get(f"{API}/files/{key}/variables/local")["meta"]
            self.v = d.get("variables", {})
            self.c = d.get("variableCollections", {})
        except SystemExit:
            print("  (variables endpoint unavailable - bindings will show ids only)\n")

    def modes(self, var):
        coll = self.c.get(var.get("variableCollectionId"), {})
        return {m["modeId"]: m["name"] for m in coll.get("modes", [])}

    def resolve(self, vid, depth=0):
        """-> {modeName: value} with aliases followed to the leaf."""
        if depth > 8 or vid not in self.v:
            return {}
        var = self.v[vid]
        names = self.modes(var)
        out = {}
        for mid, val in (var.get("valuesByMode") or {}).items():
            mn = names.get(mid, mid)
            if isinstance(val, dict) and val.get("type") == "VARIABLE_ALIAS":
                inner = self.resolve(val["id"], depth + 1)
                # a leaf collection often has a single unnamed mode; collapse it
                out[mn] = list(inner.values())[0] if len(inner) == 1 else inner
            else:
                out[mn] = rgba(val)
        return out

    def name(self, vid):
        return self.v.get(vid, {}).get("name", vid)


_SET_CACHE = {}


def fetch_set_variants(key, setid):
    """Variant names defined by a COMPONENT_SET, read from the set node itself.

    The frame payload only names the components actually instantiated, so a variant the frame never
    uses is absent from it entirely. Fetching the set node returns every child, which is the real
    matrix the build must satisfy."""
    if setid in _SET_CACHE:
        return _SET_CACHE[setid]
    names = []
    try:
        d = get(f"{API}/files/{key}/nodes?ids={urllib.parse.quote(setid)}")
        node = (d.get("nodes") or {}).get(setid, {}).get("document") or {}
        if node.get("type") == "COMPONENT_SET":
            names = [c.get("name", "?") for c in node.get("children", []) or []]
    except SystemExit:
        pass
    _SET_CACHE[setid] = names
    return names


def walk(node, fn, depth=0):
    """Call fn on node and its children. fn returning False leaves that node's subtree out."""
    if fn(node, depth) is False:
        return
    for ch in node.get("children", []) or []:
        walk(ch, fn, depth + 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("node")
    ap.add_argument("--file-key", default=None, help="the Figma file; default: the config key figma.default_file")
    ap.add_argument("--mode", default=None, help="only print this mode's resolved value, e.g. Dark")
    ap.add_argument("--unused", action="store_true", help="list variants the frame never instantiates")
    a = ap.parse_args()
    a.file_key = a.file_key or kit_config.need("figma", "default_file")

    nid = a.node.replace("-", ":")
    doc = get(f"{API}/files/{a.file_key}/nodes?ids={urllib.parse.quote(nid)}")
    entry = (doc.get("nodes") or {}).get(nid)
    if not entry or not entry.get("document"):
        sys.exit(f"{nid} does not resolve. It may have been deleted.")
    root = entry["document"]
    comps = entry.get("components", {})
    csets = entry.get("componentSets", {})

    # every visible instance in the frame, grouped by the set it comes from. A node hidden on itself
    # is counted and its subtree is not walked, so an instance under a hidden ancestor is left out.
    used = defaultdict(list)
    hidden = 0

    def visit(n, d):
        nonlocal hidden
        if n.get("visible") is False:
            hidden += 1
            return False
        if n.get("type") == "INSTANCE" and n.get("componentId"):
            c = comps.get(n["componentId"], {})
            setid = c.get("componentSetId") or n["componentId"]
            used[setid].append(n)

    walk(root, visit)

    vars_ = Vars(a.file_key)
    print(f"  frame {nid}  \"{root.get('name','?')}\"  {len(used)} component set(s) in use"
          f"   ({hidden} hidden nodes and their subtrees left out)\n")

    for setid, insts in sorted(used.items(), key=lambda kv: -len(kv[1])):
        s = csets.get(setid) or {}
        sname = s.get("name") or comps.get(setid, {}).get("name", setid)
        print(f"  == {sname}   [{setid}]   {len(insts)} instance(s)")

        # Variant axes must come from the SET DEFINITION, not from the instances present. When
        # nothing in the frame instantiates `state=hover`, an instance-derived axis list reports
        # state(default) and the hover variant - a state the build has to implement - never appears.
        defined = set()
        axes = defaultdict(set)
        sd = fetch_set_variants(a.file_key, setid)
        for vname in sd:
            defined.add(vname)
            for part in vname.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    axes[k.strip()].add(v.strip())
        if not defined:                       # not a variant set, or the fetch failed
            for i in insts:
                c = comps.get(i.get("componentId"), {})
                for part in (c.get("name") or "").split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        axes[k.strip()].add(v.strip())
        if axes:
            total = 1
            for vals in axes.values():
                total *= len(vals)
            print("     axes: " + " x ".join(f"{k}({'|'.join(sorted(v))})" for k, v in axes.items())
                  + f"   -> {total} defined" + (f", {len(defined)} in the set" if defined else ""))

        # component properties, including the non-variant ones a fill dump misses
        props = defaultdict(set)
        for i in insts:
            for k, v in (i.get("componentProperties") or {}).items():
                props[k.split("#")[0]].add(str(v.get("value")))
        nonvariant = {k: v for k, v in props.items() if k not in axes}
        if nonvariant:
            print("     properties: " + ", ".join(f"{k}={'/'.join(sorted(v))}" for k, v in nonvariant.items()))

        # which variants are actually instantiated, and with what paint
        seen = {}
        for i in insts:
            c = comps.get(i.get("componentId"), {})
            key = c.get("name", "?")
            vis = i.get("visible") is not False
            fills = [rgba(f.get("color")) for f in (i.get("fills") or [])
                     if f.get("visible") is not False and f.get("type") == "SOLID"]
            bv = i.get("boundVariables") or {}
            bound = []
            for prop, ref in bv.items():
                for r in (ref if isinstance(ref, list) else [ref]):
                    vid = r.get("id")
                    if not vid:
                        continue
                    res = vars_.resolve(vid)
                    if a.mode and isinstance(res, dict) and a.mode in res:
                        res = {a.mode: res[a.mode]}
                    bound.append(f"{prop}={vars_.name(vid)} {json.dumps(res, ensure_ascii=False)}")
            rec = seen.setdefault(key, {"n": 0, "visible": 0, "fills": set(), "bound": set()})
            rec["n"] += 1
            rec["visible"] += 1 if vis else 0
            rec["fills"].update(fills)
            rec["bound"].update(bound)

        for k in sorted(seen):
            r = seen[k]
            vtag = f"{r['visible']}/{r['n']} visible"
            print(f"       - {k:44} {vtag}")
            if r["fills"]:
                print(f"           fill: {', '.join(sorted(r['fills']))}")
            for bnd in sorted(r["bound"]):
                print(f"           bound {bnd}")

        if a.unused and defined:
            missing = set(defined) - set(seen)
            if missing:
                print(f"     NOT instantiated in this frame ({len(missing)}): " + "; ".join(sorted(missing)))
        print()

    print("  Every variant above is a state the build has to account for, or deliberately decline.")
    print("  A variant that exists in the design and nowhere in the CSS is a defect by default.")
    return 0


if __name__ == "__main__":
    sys.exit(kit_config.run(main))
