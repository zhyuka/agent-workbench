#!/usr/bin/env python3
"""html_clip.py - put rendered HTML on the macOS clipboard so it pastes into a chat app, mail client or
rich-text editor as real formatting (tables, links, lists), not as tags.

  python3 html_clip.py <file.html>

The pasteboard gets two flavours: public.html, which rich-text targets use, and a plain-text
fallback for targets that take only text. macOS needs no descriptor header (Windows' CF_HTML needs
byte offsets). Uses osascript, so it runs on any python3 with no extra packages. macOS only.

Prints the plain-text fallback. Read it: if tags or entities leak into it, the HTML is malformed
and the paste will fail the same way. Then prints the clipboard's flavours as macOS reports them.
Exit 0 clipped, 1 bad input, 2 osascript failed.
"""
import html
import re
import subprocess
import sys

if any(a in ("-h", "--help") for a in sys.argv[1:]):
    print(__doc__.strip())
    raise SystemExit(0)


def plain_text(fragment):
    """Tags to text lines. Decode entities AFTER stripping tags, or they survive as literal "&quot;"."""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", fragment)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(tr|p|h[1-6]|div|li)>", "\n", t)
    t = re.sub(r"(?i)</t[dh]>", " | ", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    lines = (re.sub(r"\s+\|\s*$", "", ln).strip() for ln in t.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 1
    try:
        fragment = open(sys.argv[1], encoding="utf-8").read().strip()
    except OSError as e:
        print(f"cannot read {sys.argv[1]}: {e}", file=sys.stderr)
        return 1
    doc = f"<html><body>{fragment}</body></html>"
    plain = plain_text(fragment)
    script = ("set the clipboard to {«class HTML»:«data HTML%s», «class utf8»:«data utf8%s»}"
              % (doc.encode("utf-8").hex().upper(), plain.encode("utf-8").hex().upper()))
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if p.returncode:
        print(f"osascript failed: {p.stderr.strip()}", file=sys.stderr)
        return 2
    print("--- plain-text fallback ---")
    print(plain)
    info = subprocess.run(["osascript", "-e", "clipboard info"], capture_output=True, text=True)
    print(f"--- clipboard now holds: {info.stdout.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
