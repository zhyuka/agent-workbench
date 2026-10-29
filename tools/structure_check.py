"""Does a new component match the N-1 already in its class? Peer conformance over BUILT markup.

  python tools/structure_check.py <class-glob> --root dist [--expect uniform] [--depth 2] [--ancestors N]

EXIT. 0 measured (or UNIFORM with --expect uniform), 1 SPLIT with --expect uniform, or no build
folder, or no .html under it; 2 the glob matched nothing.

WHY THIS EXISTS. Most checks are one of four shapes, and none of them is this one:

  item vs a CONSTANT            a contrast threshold
  item vs ITS OWN referent      a page vs its design frame, verify_ledger (its prior hash)
  item vs ONE NAMED other       ado_rest audit (its parent item)
  COVERAGE / set difference     class_check

Coverage is not conformance. A coverage count can tell you 22 of 22 files contain a string; it cannot
tell you the 22 strings are the SAME SHAPE. That gap ships real defects: most page containers put
the gutter on the inner element, a few put it on the section and keep the gutter-inclusive
max-width, and those few sit outboard of every other band on a wide screen. Each of them matches its
own design frame perfectly, so every other check passes.

DIRECTION MATTERS. The qa-audit skill's verification.md covers the other direction: an EDIT touches
a shared path, did it reach the whole class. This covers a NEW INSTANCE joining an existing class:
does it match the ones already there. Same numerator/denominator contract, opposite arrow.

IT RUNS ON dist/, NOT src/. The artifact a person actually loads. A source file can look uniform and
render differently once a media query or a scoped-style hash lands.

WHAT IT CANNOT DO. It cannot tell you the majority is RIGHT - only that the class is SPLIT. A
unanimous class prints as unanimous, which is not the same as correct.

POINT IT AT A NAMED CLASS. A blanket sweep over every class in the build is noise: a glob such as
`*-label` or `*-cell` returns thousands of instances in many shapes, because those wrap
CONTENT and content legitimately varies in child count. The signal is in small cross-page structural
families - a button, an accordion control, a card shell, a section container - where every instance
is supposed to be the same component. Same contract as class_check: you name the class, the tool
supplies the denominator.
"""
import argparse
import fnmatch
import os
import re
import sys
from collections import defaultdict
from html.parser import HTMLParser

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}

# Astro/Svelte scoped styles append a build hash to every class. Two identical components get
# different hashes, so leaving them in makes every element its own bucket - the check would report
# a 100% split and be useless. Same for trailing digits on generated ids.
HASH = re.compile(r"^(astro-[a-z0-9]{6,}|svelte-[a-z0-9]{6,}|css-[a-z0-9]{6,})$", re.I)


class Node:
    __slots__ = ("tag", "classes", "children", "parent", "line")

    def __init__(self, tag, classes, line):
        self.tag, self.classes, self.line = tag, classes, line
        self.children, self.parent = [], None


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#doc", [], 0)
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = [c for c in (d.get("class") or "").split() if not HASH.match(c)]
        n = Node(tag, cls, self.getpos()[0])
        n.parent = self.stack[-1]
        self.stack[-1].children.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return


def walk(n):
    yield n
    for c in n.children:
        yield from walk(c)


def prefix_of(glob, hit):
    """The text the glob's leading `*` consumed. For glob '*-acc-btn' and hit 'home-acc-btn'
    that is 'home'.

    This matters more than it looks. Sibling pages often namespace their classes by page - home-,
    about-, docs- - so three byte-identical accordions fingerprint as three different shapes purely
    because their CHILD class names carry the page prefix. Without this, the tool reports every
    per-page-namespaced component as a split and its output becomes noise."""
    if not glob.startswith("*"):
        return None
    tail = glob[1:]
    return hit[:-len(tail)] if tail and hit.endswith(tail) else None


def shape(n, depth, root=False, pfx=None):
    """Canonical structural fingerprint of an element's own subtree.

    The MATCHED element's class list is reduced to the token that matched. Keeping its co-classes
    made one shared component report 14 shapes across 24 instances, because `ui-btn` legitimately
    appears as `ctaf-btn ui-btn`, `hero-cta ui-btn` and so on - modifiers are the point of a shared
    component, not evidence against it. Descendant classes ARE kept: a button whose icon wrapper
    differs from its siblings is exactly the divergence this looks for."""
    if depth < 0:
        return ""
    kids = " ".join(shape(c, depth - 1, pfx=pfx) for c in n.children
                    if c.tag not in ("script", "style"))
    cls = n.classes
    if pfx:
        cls = [c[len(pfx):] if c.startswith(pfx) else c for c in cls]
    own = "" if root else ",".join(sorted(cls))
    return f"{n.tag}[{own}]({kids})" if kids else f"{n.tag}[{own}]"


def build_roots(dist):
    """The folders a build's pages live in, and a note naming the pages that are not there.

    A static build is one flat folder. When some routes render on request, a server build (for
    example `astro build` with a server adapter) writes client/ (the static pages and assets) and
    server/ (the handler that renders the rest), and reading the build folder as one tree would
    give every route a /client/ prefix. A project that renders its on-request pages to static HTML
    can put them in <build>/rendered/, which is read after client/ when it exists. A build that
    empties its folder first leaves no stale rendered/ behind. <build>/client reads the same as
    <build>."""
    dist = os.path.normpath(dist)
    up = os.path.dirname(dist) or "."
    if os.path.basename(dist) == "client" and os.path.isdir(os.path.join(up, "server")):
        dist = up
    client, rendered = os.path.join(dist, "client"), os.path.join(dist, "rendered")
    if not os.path.isdir(client):
        return [dist], None
    if os.path.isdir(rendered):
        return [client, rendered], None
    if not os.path.isdir(os.path.join(dist, "server")):
        return [client], None
    return [client], (f"NOT READ: the pages {dist}/server renders on request. {dist}/rendered does not "
                      f"exist; render those pages to static HTML in it to have them read.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("glob", help="class-name glob, e.g. '*-inner' or 'ui-btn'")
    ap.add_argument("--root", default="dist")
    ap.add_argument("--depth", type=int, default=2, help="how many levels of children to fingerprint")
    ap.add_argument("--expect", choices=["uniform"], default=None,
                    help="without this the run only measures: exit 0, or 2 when the glob matches "
                         "nothing, 1 when there is no dist or no .html")
    ap.add_argument("--ancestors", type=int, default=0, help="levels of ancestor tag/class to include")
    a = ap.parse_args()

    if not os.path.isdir(a.root):
        sys.exit(f"no directory at {a.root}. Build first.")

    # (path, its route-relative name). On a split build a path taken relative to the build folder
    # starts with client/, and the on-request pages are only in rendered/.
    roots, note = build_roots(a.root)
    if note:
        print(f"  WARNING: {note}\n")
    pages, seen = [], set()
    for top in roots:
        for dirpath, _, names in os.walk(top):
            for nm in names:
                if nm.endswith(".html"):
                    p = os.path.join(dirpath, nm)
                    rel = os.path.relpath(p, top).replace("\\", "/")
                    if rel not in seen:
                        seen.add(rel)
                        pages.append((p, rel))
    if not pages:
        sys.exit(f"no .html under {a.root}. A check that cannot fail is not a gate.")

    buckets = defaultdict(list)
    seen_shapes = {}
    for p, rel in pages:
        try:
            t = Tree()
            t.feed(open(p, encoding="utf-8", errors="replace").read())
        except Exception:
            continue
        for n in walk(t.root):
            hit = next((c for c in n.classes if fnmatch.fnmatch(c, a.glob)), None)
            if not hit:
                continue
            anc = []
            q = n.parent
            for _ in range(a.ancestors):
                if not q or q.tag == "#doc":
                    break
                anc.append(f"{q.tag}[{','.join(sorted(q.classes))}]")
                q = q.parent
            head = (" > ".join(reversed(anc)) + " >> ") if anc else ""
            sig = head + shape(n, a.depth, root=True, pfx=prefix_of(a.glob, hit))
            buckets[sig].append((rel, hit, n.line))
            seen_shapes[sig] = hit

    total = sum(len(v) for v in buckets.values())
    if total == 0:
        print(f"  '{a.glob}' matched NOTHING under {a.root}/")
        print("  A selector matching zero elements is a FAIL, not a pass.")
        if note:
            print("  The on-request pages were NOT READ (see the warning above).")
        return 2

    ranked = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    print(f"  '{a.glob}'  {total} instance(s) across {len(pages)} page(s)  ->  {len(ranked)} distinct shape(s)\n")

    for i, (sig, members) in enumerate(ranked):
        pct = round(len(members) * 100 / total)
        names = sorted({m[1] for m in members})
        head = "MAJORITY" if i == 0 and len(ranked) > 1 else ("uniform" if len(ranked) == 1 else "minority")
        print(f"  [{len(members):>3} / {pct:>3}%] {head}   {', '.join(names[:6])}"
              + (f"  (+{len(names)-6} more)" if len(names) > 6 else ""))
        if i > 0 or len(ranked) == 1:
            for f, cls, ln in sorted(members)[:8]:
                print(f"          {f}:{ln}  .{cls}")
            if len(members) > 8:
                print(f"          ... and {len(members)-8} more")
        if i == 0 and len(ranked) > 1:
            print(f"          shape: {sig[:150]}")
        print()

    if len(ranked) > 1:
        print(f"  {len(ranked[0][1])} of {total} share the majority shape; "
              f"{total - len(ranked[0][1])} do not.")

    unread = "  The on-request pages were NOT READ (see the warning above)." if note else ""
    if a.expect != "uniform":
        print("\n  This is a measurement, not a gate. Re-run with --expect uniform to make it fail.")
        if unread:
            print(unread)
        return 0
    if len(ranked) == 1:
        print(f"  UNIFORM: all {total} instances share one shape.")
        print("  Uniform is not the same as correct - it means nothing here disagrees.")
        if unread:
            print(unread)
        return 0
    print("\n  SPLIT: the minority above either matches the majority, or the difference is stated.")
    if unread:
        print(unread)
    return 1


if __name__ == "__main__":
    sys.exit(main())
