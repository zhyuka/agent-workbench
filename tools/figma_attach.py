#!/usr/bin/env python
"""figma_attach.py - the steps the REST API cannot do: edit a Figma comment, put pictures in it, turn
"@Last, First" into a mention that notifies, and resolve a thread. All of a batch's steps run in one
browser.

    attach  <file_key> <comment_id> <picture> [<picture> ...] [--again] [--wait-user SECONDS]
    edit    <file_key> <comment_id> --text-file <file.txt> [<picture> ...] [--replace] [--wait-user SECONDS]
              --replace takes off the pictures the comment holds, in the editor, before adding the new
              ones, and checks the thread then shows exactly the new ones (for a remade picture, so
              the old one does not stay beside it)
    mention <file_key> <comment_id> [<comment_id> ...]
              each "@Last, First" in each comment becomes a mention chip, all in one browser
    resolve <file_key> <comment_id> [<comment_id> ...] [--wait-user SECONDS]
              resolves each thread (give its root's id), all in one browser; see RESOLVE
    probe   <file_key> <comment_id> [...] [--picker "Last, First"]
              opens each thread and its editor in one browser and cancels; --picker types "@First" and
              says whom it would pick. Saves nothing: the live check after a Figma change
    selftest                                planted cases, no network, no browser

ONE BROWSER PER BATCH. A tool that launches, signs in to and deletes a fresh browser for every UI
step is slow and fragile. run_jobs() takes every step of a batch (pictures, edits, mentions,
resolves, probes), refuses them all before any browser opens if one is refused, runs them in one
browser from signed_in_browser.py, and checks each over REST once the browser is gone.
figma_post.py batch and new/reply hand it everything they posted.

MENTION. REST posts "@Last, First" as plain text and notifies nobody; the Plugin API and the Figma MCP
have no comments. In the editor the tool selects the plain "@Last, First", types "@First", and picks
the one option that reads the whole handle, is not a guest, and has no other option with the same
surname; anything else cancels with nothing saved. After Save the markdown form of the comment
(?as_md=true) must drop the "@": mentions made in the UI read that way, and ones typed over REST keep
it. A mention added this way notifies the person.

RESOLVE. REST cannot resolve a comment; only the web app can. From one REST read of the file, it
refuses every id, before any browser opens, when one id is not in the file, is a reply (resolve takes
the thread's root), is a thread already resolved, has no text to find it by on the page (a
picture-only root), or is a teammate's thread whose newest comment is a teammate's. You resolve your
own threads and moot ones; a thread a teammate is still discussing is theirs. Then, in one browser,
it opens each thread and clicks "Mark as resolved" in the header of the open thread that holds the
root's text, and waits for that header to read "Mark as unresolved" (opening the link again if the
thread closed). The Comments panel draws the same "Mark as resolved" on every card, so the click is
scoped to the open thread, never the page. A screenshot per thread goes to
~/.cache/agent-workbench/figma_attach/<id>-resolve.png. Once the browser is gone it reads REST again
and prints RESOLVED <id> or NOT RESOLVED <id>: <why> for each, from resolved_at; it also names any
other thread in the file that turned resolved during the run. Exit 0 only when REST shows every id
resolved and no other thread changed.

EDIT. REST cannot edit a comment (PUT is 404), so without this every approved rewrite of your own
comment comes back to you as a hand step. `edit` does that step: the same browser and sign-in as
`attach`, Comment actions -> Edit on the comment, a check that the editor holds the comment's
current text, the whole text replaced with the file's (paragraphs split on a blank line, typed with
Shift+Enter), a check that the editor now holds exactly the file's text and still holds every
attachment it had, then the pictures if any, Save, and a check over REST that the comment's message
is the file's text. It refuses before any browser opens when the comment is someone else's, when it
has replies (an edit would change what they answer; reply after them instead), when the text is
empty, unchanged with no picture, or holds an @ (the editor turns it into a mention picker), when it
keeps a wrapped quote's line breaks (drafts_store.wrapped_refusal; the same words with other line
breaks count as a change, since Figma shows every break), or when pictures are given and the text
does not end "Before and after:". After an edit of a FIX note, run figma_post.py fixnote to stamp
it. When a drafts file is configured (drafts_store.py), the EDIT draft the edit used leaves it.

WHY. A FIX note's before/after picture goes in the comment itself, attached every time in the
browser rather than handed back as a manual step. REST takes message, comment_id and client_meta
only, so the picture goes in through the web app. figma_post.py new and reply run this right after
they post a FIX note with --picture, and print the manual step only when it did not attach.

HOW. Reads the comment over REST and refuses (below) before any browser opens. Then launches one
browser for the batch (signed_in_browser.session: Playwright, Edge or Chrome per its config, a
visible window, a fresh profile in the system temp dir) and opens the first comment's link. On
Figma's own login page it types the token owner's email (GET /v1/me) into the email field and
presses the button; single sign-on, if your account has it, then finishes from the browser's saved
identity-provider session. It types nothing else anywhere: never a password and never into an
identity provider's page. A password field, a code, an account picker, or a sign-in still
unfinished a minute after the email, means you finish it in that window, which waits --wait-user
seconds (default 180) and then gives up. Figma's "desktop app or installed fonts" prompt is closed
with Escape, never Continue. Then: Comment actions -> Edit on that comment, a check that the editor
holds the comment's text and ends "Before and after:", the pictures through the editor's file input,
Save, and a check that the thread shows that many more attached images. A screenshot of the result
goes to ~/.cache/agent-workbench/figma_attach/. The next comment opens in the same window. The browser
closes and the profile is deleted at the end of the batch, since it held the session cookies; a
killed run's profile is reaped by pid at the next launch.
Each attach is stamped in ~/.cache/agent-workbench/figma_attachments.json.

REFUSES (exit 1, no browser opened, nothing changed)
  - the comment is not the token owner's: only its author can edit it
  - its text does not end "Before and after:" (figma_post.py new and reply add it)
  - no picture, more than 5, a file that is missing or not .jpg/.jpeg/.png/.gif, or one already
    stamped on this comment (--again attaches it once more)
  - and, with the browser open (exit 1, nothing saved): an editor whose text is not the comment's, or
    a comment that already holds pictures, unless --again (run on a wrong comment id, it would add a
    picture to another FIX note)

EXIT 0 attached and checked, 1 refused, 2 did not run (no Playwright or browser, the API failed, the
sign-in was not finished, or a control was not found because Figma changed its page).

BLIND TO what the pictures show: it counts attached images, not which ones, so the check is "held +
N", not "these N". It cannot tell a right comment id from a wrong one that also ends "Before and
after:" and holds no pictures; the caller's id is trusted. The sign-in check reads the
page (fields, a picker's text), so a sign-in page it has never seen reads as "still loading" until
the minute is up, and then waits for you. `resolve` finds the open thread by the first 60
characters of its root's text, so two threads that open the same way cannot be told apart (it
stops, nothing clicked); it does not read the thread first, so whether the thread is done is the
caller's call; and a thread resolved by someone else during the run reads as one more change to look
at, not as this tool's. Figma's comment UI selectors (aria-labels such as "Comment actions", "Mark as
resolved") are read off the live page and break when Figma changes it: `probe` is the check.
"""
import contextlib, io, json, os, re, shutil, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

API = "/v1"   # a Figma REST path; _get sends it through figma_client.py
LABEL = "Before and after:"
HOME = os.path.expanduser("~")
STAMPS = os.path.join(HOME, ".cache", "agent-workbench", "figma_attachments.json")
PROOF = os.path.join(HOME, ".cache", "agent-workbench", "figma_attach")
KINDS = (".jpg", ".jpeg", ".png", ".gif")
MAX_PICTURES = 5          # the editor's file input: "Upload up to 5 images and GIFs"
EMAIL_WAIT = 60           # seconds after the email for single sign-on to come back
EDIT_WAIT = 90            # seconds for the file and its comment thread to load once signed in
RESOLVE_PAGE_WAIT = 15    # seconds for the open thread's header to read "Mark as unresolved" after the click
RESOLVE_API_WAIT = 20     # seconds for REST to show resolved_at once the browser is gone


def _get(path):
    """Through figma_client.py beside this file: paced with every other tool, and a 429 waited out.
    No token exits with figma_auth's message."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import figma_auth
    import figma_client
    return figma_client.get(API + path, token=figma_auth.token(), tool="figma_attach", timeout=120)


def norm(s):
    """Text as the editor and the API both write it: the UI draws x as a multiplication sign and
    straight quotes curly, and the editor's paragraphs add blank lines."""
    s = (s or "").replace("×", "x").replace("‘", "'").replace("’", "'")
    s = s.replace("“", '"').replace("”", '"').replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def face(s):
    """Text as the page shows it: norm(), less every "@" and every list marker. A mention chip reads
    "Last, First" in the thread and the editor while REST's plain text reads "@Last, First", so a
    match must drop the @ on both sides. A list item reads without its "* " or "1. " on the page,
    which draws the marker, so a root that holds a list is otherwise never found."""
    return re.sub(r"\s+", " ", norm(LIST_MARK.sub("", s or "")).replace("@", "")).strip()


def refusal(comment, me_id, pictures, stamped=(), again=False):
    """Refusal text or None, from the API's view of the comment and the files. No browser."""
    if comment is None:
        return "REFUSED: that comment is not in this file."
    who = (comment.get("user") or {})
    if who.get("id") != me_id:
        return (f"REFUSED: comment {comment.get('id')} is {who.get('handle')}'s. Only its author can edit it, "
                "and the Figma token is someone else's.")
    if not norm(comment.get("message")).endswith(LABEL):
        return (f"REFUSED: comment {comment.get('id')} does not end \"{LABEL}\", where the picture goes. "
                "figma_post.py new and reply add it; for an older note, edit the text first.")
    if not pictures:
        return "REFUSED: no picture given."
    return picture_refusal(comment, pictures, stamped, again)


def picture_refusal(comment, pictures, stamped=(), again=False):
    """Refusal text or None for the files themselves: how many, that each exists and is a picture, and
    that none is already stamped on this comment."""
    if len(pictures) > MAX_PICTURES:
        return f"REFUSED: {len(pictures)} pictures; a comment takes up to {MAX_PICTURES}."
    for p in pictures:
        if not os.path.isfile(p):
            return f"REFUSED: {p} is not a file."
        if not p.lower().endswith(KINDS):
            return f"REFUSED: {p} is not a .jpg, .png or .gif."
    dup = [os.path.basename(p) for p in pictures if os.path.basename(p) in set(stamped)]
    if dup and not again:
        return (f"REFUSED: {', '.join(dup)} is already attached to comment {comment.get('id')} "
                f"(stamped in {STAMPS}). --again attaches it once more.")
    return None


def edit_refusal(comment, me_id, text, pictures, replies, stamped=(), replace=False):
    """Refusal text or None for `edit`, from the API's view of the comment, the new text and the files.
    No browser. `replies` is how many comments in the file answer this one. With `replace` the pictures
    already on the comment come off, so one stamped there may go in again."""
    if comment is None:
        return "REFUSED: that comment is not in this file."
    who = (comment.get("user") or {})
    if who.get("id") != me_id:
        return (f"REFUSED: comment {comment.get('id')} is {who.get('handle')}'s. Only its author can edit it, "
                "and the Figma token is someone else's.")
    if replies:
        return (f"REFUSED: comment {comment.get('id')} has {replies} repl{'y' if replies == 1 else 'ies'}. An edit "
                "would change what they answer; reply after them instead (figma_post.py reply).")
    new = norm(text)
    if not new:
        return "REFUSED: the new text is empty."
    if "@" in new:
        return "REFUSED: the new text holds an @, which the editor turns into a mention picker. Type a mention by hand."
    try:
        import drafts_store
        wrapped = drafts_store.wrapped_refusal(text)
    except ImportError:
        wrapped = None
    if wrapped:
        return wrapped
    # The same words with other line breaks is a change: Figma shows every break (a reflow).
    if new == norm(comment.get("message")) and not pictures \
            and text.strip().count("\n") == (comment.get("message") or "").strip().count("\n"):
        return "REFUSED: the new text is the comment's text already, and no picture is given. Nothing to do."
    if pictures and not new.endswith(LABEL):
        return f"REFUSED: pictures go after \"{LABEL}\", and the new text does not end with it."
    if replace and not pictures:
        return "REFUSED: --replace takes the comment's pictures off for new ones, and none is given."
    return picture_refusal(comment, pictures, () if replace else stamped) if pictures else None


def editor_matches(editor_text, message):
    """True when the open editor holds this comment: the same opening and the closing label."""
    e, m = face(editor_text), face(message)
    return e.endswith(LABEL) and len(e) >= 20 and e[:80] == m[:80]


# A line's list marker. The editor draws a numbered or bulleted line as a list item, whose number is
# not in its text, while REST keeps "1. ", so an edit of a numbered comment is otherwise refused.
LIST_MARK = re.compile(r"(?m)^[ \t]*(?:\d+[.)]|[*•\-])[ \t]+")


def editor_holds(editor_text, message):
    """True when the open editor holds this comment's text, label or not: `edit` opens comments that
    predate the label (an older FIX note may end "Please confirm and resolve."). List markers are
    left out on both sides."""
    e, m = face(LIST_MARK.sub("", editor_text or "")), face(LIST_MARK.sub("", message or ""))
    return len(e) >= 20 and e[:80] == m[:80]


def paragraphs(text):
    """The text as the editor takes it: paragraphs split on a blank line, each a list of its lines."""
    return [[ln.strip() for ln in p.strip().split("\n")] for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def classify(info):
    """What the tab is showing, from _probe's dict: ready, figma-email, needs-user or wait."""
    host = urllib.parse.urlparse(info.get("url") or "").netloc
    if info.get("actions") and host.endswith("figma.com"):
        return "ready"
    if info.get("password") or info.get("code") or info.get("picker") or info.get("consent"):
        return "needs-user"
    if host.endswith("figma.com") and info.get("email"):
        return "figma-email"
    if host and not host.endswith("figma.com") and info.get("email"):
        return "needs-user"   # an identity provider asking for an account: never typed into
    return "wait"


PROBE = """() => {
  const q = s => !!document.querySelector(s);
  const body = document.body ? document.body.innerText.slice(0, 4000) : '';
  return {
    url: location.href,
    actions: document.querySelectorAll('button[aria-label="Comment actions"]').length,
    email: q('input[type=email],input[name=email],input[name=loginfmt]'),
    password: q('input[type=password]'),
    code: q('input[name=otc],input[autocomplete="one-time-code"]'),
    picker: /pick an account/i.test(body),
    consent: /permissions requested|accept/i.test(body) && !/figma\\.com$/.test(location.host),
    fonts: /desktop app or installed fonts/i.test(body),
  };
}"""

PICK = """(prefix) => {
  const norm = s => (s || '').replace(/\\u00d7/g, 'x').replace(/[\\u2018\\u2019]/g, "'")
    .replace(/[\\u201c\\u201d]/g, '"').replace(/@/g, '').replace(/\\s+/g, ' ').trim();
  const all = [...document.querySelectorAll('button[aria-label="Comment actions"]')];
  const hits = [];
  all.forEach((b, i) => {
    let a = b.parentElement;
    for (let d = 0; d < 12 && a; d++, a = a.parentElement) {
      if (a.querySelectorAll('button[aria-label="Comment actions"]').length > 1) break;
      if (norm(a.innerText).includes(prefix)) { hits.push(i); break; }
    }
  });
  return hits;
}"""


def _probe(pg):
    try:
        return pg.evaluate(PROBE)
    except Exception:
        return {"url": pg.url}


def lost_comment(url, target):
    """True when the tab is on the file but without the comment's #id, which the sign-in redirect
    can drop: the file loads on its own and the thread never opens."""
    frag = urllib.parse.urlparse(target).fragment
    u = urllib.parse.urlparse(url or "")
    return bool(frag) and u.netloc.endswith("figma.com") and "/design/" in u.path and u.fragment != frag


def sign_in(pg, email, wait_user, now=time.time, sleep=time.sleep, target=None):
    """Wait for the comment to be on screen, typing the email once on Figma's login page and opening
    the comment's link again once if the sign-in redirect dropped its #id. 'ready', or why not."""
    typed_at, user_since, start, reopened = None, None, now(), False
    while True:
        info = _probe(pg)
        state = classify(info)
        if info.get("fonts"):
            pg.keyboard.press("Escape")
        if state == "ready":
            return "ready"
        if target and not reopened and lost_comment(info.get("url"), target):
            pg.goto(target, wait_until="domcontentloaded", timeout=90000)
            reopened, start = True, now()
            print("  signed in; the redirect dropped the comment from the link, so it was opened again")
            continue
        if state == "figma-email" and typed_at is None:
            pg.locator('input[type=email],input[name=email]').first.fill(email)
            pg.get_by_role("button", name=re.compile(r"^(log in|continue|next)", re.I)).first.click()
            typed_at = now()
            print("  signing in: typed the account's email on Figma's login page; single sign-on does the rest")
        stuck = (state == "needs-user" or (typed_at is not None and now() - typed_at > EMAIL_WAIT)
                 or (typed_at is None and now() - start > EDIT_WAIT))
        if stuck and user_since is None:
            user_since = now()
            print(f"  SIGN-IN NEEDS YOU: finish it in the browser window that opened ({info.get('url', '')[:60]}). "
                  f"Waiting {wait_user}s.")
        if user_since is not None and now() - user_since > wait_user:
            return f"the sign-in was not finished within {wait_user}s (last page {info.get('url', '')[:80]})"
        sleep(1)


def nearest(boxes, anchor_box, within=400):
    """The box closest to anchor_box, or None: the menu item that just opened beside its button.
    Boxes come from the menu items, because a text locator lands on Figma's <i18n-text> wrapper,
    which has no box, so a text locator finds no Edit even with the menu open."""
    best, dist = None, None
    for b in boxes:
        d = abs(b["x"] - anchor_box["x"]) + abs(b["y"] - anchor_box["y"])
        if dist is None or d < dist:
            best, dist = b, d
    return best if dist is not None and dist < within else None


REMOVE = '[aria-label^="Remove attachment ("]'   # one per attachment in the editor, every one listed
# The thread draws three thumbnails and a "+N" tile for the rest, so its <img> count stops at 3
# (a 4th picture goes in and the count reads "0 more"). The editor's remove buttons, and
# the thumbnails' "(k / M)" labels, count them all.
TOTAL = """(prefix) => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  let m = 0;
  for (const e of document.querySelectorAll('[aria-label*="uploaded media file ("]')) {
    const g = /\\((\\d+) \\/ (\\d+)\\)/.exec(e.getAttribute('aria-label'));
    if (g) m = Math.max(m, +g[2]);
  }
  const imgs = document.querySelectorAll('img[alt="Thumbnail for uploaded media file"]').length;
  const more = [...document.querySelectorAll('*')].filter(e => e.childElementCount === 0 && /^\\+\\d+$/.test(norm(e.textContent)))
    .reduce((s, e) => s + parseInt(norm(e.textContent).slice(1), 10), 0);
  return Math.max(m, imgs + more);
}"""


def held_refusal(held, wanted, again):
    """Refusal text or None for a comment that already holds `held` attachments in its editor."""
    if held and not again:
        return (f"REFUSED: the comment already holds {held} attachment(s); nothing saved. Check it is the right "
                f"comment, then --again adds {wanted} more.")
    if held + wanted > MAX_PICTURES:
        return f"REFUSED: the comment holds {held} attachment(s); {wanted} more passes the limit of {MAX_PICTURES}."
    return None


def open_editor(pg, comment, holds=editor_matches):
    """Comment actions -> Edit on `comment`. (0, editor, cancel) when the editor that opened holds its
    text by `holds`, else (1 or 2, None, None) after printing why; a refusal cancels the editor."""
    msg = comment["message"]
    hits = pg.evaluate(PICK, face(msg)[:60])
    if len(hits) != 1:
        print(f"DID NOT RUN: found {len(hits)} comments on the page matching this one's text; expected 1.")
        return 2, None, None
    btn = pg.locator('button[aria-label="Comment actions"]').nth(hits[0])
    btn.click()
    pg.wait_for_timeout(700)
    from playwright.sync_api import Error as PlaywrightError
    # The word sits in Figma's <i18n-text> element, which has no box of its own; its menuitem has one.
    loc = pg.locator('[role=menuitem]', has_text=re.compile(r"^\s*Edit\s*$"))
    try:
        loc.first.wait_for(state="visible", timeout=5000)
    except PlaywrightError:
        pass
    boxes = [b for b in (loc.nth(i).bounding_box() for i in range(loc.count())) if b]
    edit = nearest(boxes, btn.bounding_box())
    if edit is None:
        os.makedirs(PROOF, exist_ok=True)
        shot = os.path.join(PROOF, f"{comment['id']}-no-edit.png")
        pg.screenshot(path=shot)
        print(f"DID NOT RUN: the comment's menu has no Edit next to it (button at {btn.bounding_box()}, "
              f"visible Edit texts at {boxes}). Screenshot: {shot}")
        return 2, None, None
    pg.mouse.click(edit["x"] + edit["width"] / 2, edit["y"] + edit["height"] / 2)
    pg.wait_for_timeout(1200)
    eds = pg.locator("[contenteditable=true]")
    ed = next((eds.nth(i) for i in range(eds.count())
               if face(eds.nth(i).inner_text()).startswith(face(msg)[:40])), None)
    if ed is None or not holds(ed.inner_text(), msg):
        print("REFUSED: the editor that opened does not hold this comment's text; nothing saved.")
        cancel = pg.get_by_role("button", name="Cancel", exact=True)
        if cancel.count():
            cancel.first.click()
        return 1, None, None
    return 0, ed, (lambda: pg.get_by_role("button", name="Cancel", exact=True).first.click())


def edit_and_attach(pg, comment, pictures, again=False):
    """0 attached and checked, 1 refused (wrong comment, or it already holds pictures), 2 a control was
    missing or the count did not rise."""
    code, ed, cancel = open_editor(pg, comment)
    if code:
        return code
    held = pg.locator(REMOVE).count()
    why = held_refusal(held, len(pictures), again)
    if why:
        print(why)
        cancel()
        return 1
    return stage_and_save(pg, comment, pictures, held, cancel)


# The editor that has focus, and its text: the one `edit` just cleared and typed into.
FOCUSED = "() => { const e = document.activeElement; return e && e.isContentEditable ? e.innerText : null; }"
SELECT_ALL = "Meta+A" if sys.platform == "darwin" else "Control+A"


def edit_text(pg, comment, text, pictures, replace=False):
    """Replace the comment's text with `text`, add `pictures`, save. 0 saved, 1 refused (wrong comment,
    or too many attachments), 2 a control was missing or the editor did not take the text. Nothing is
    saved unless the editor holds exactly `text` and every attachment it opened with, or, with
    `replace`, none of them."""
    code, ed, cancel = open_editor(pg, comment, holds=editor_holds)
    if code:
        return code
    held = pg.locator(REMOVE).count()
    if (0 if replace else held) + len(pictures) > MAX_PICTURES:
        print(f"REFUSED: the comment holds {held} attachment(s); {len(pictures)} more passes the limit of "
              f"{MAX_PICTURES}. Nothing saved.")
        cancel()
        return 1
    ed.click()
    pg.keyboard.press(SELECT_ALL)
    pg.keyboard.press("Backspace")
    pg.wait_for_timeout(400)
    left = pg.evaluate(FOCUSED)
    if left is None or norm(left):
        print(f"DID NOT RUN: the editor did not clear (it holds {norm(left or '')[:60]!r}); nothing saved.")
        cancel()
        return 2
    for i, par in enumerate(paragraphs(text)):
        if i:
            pg.keyboard.press("Shift+Enter")
            pg.keyboard.press("Shift+Enter")
        for j, line in enumerate(par):
            if j:
                pg.keyboard.press("Shift+Enter")
            pg.keyboard.insert_text(line)
    pg.wait_for_timeout(600)
    got = pg.evaluate(FOCUSED)
    if got is None or norm(got) != norm(text):
        print(f"DID NOT RUN: after typing, the editor holds {norm(got or '')[:80]!r}, not the new text; "
              "nothing saved.")
        cancel()
        return 2
    if pg.locator(REMOVE).count() != held:
        print(f"DID NOT RUN: the editor held {held} attachment(s) and now holds {pg.locator(REMOVE).count()}; "
              "nothing saved.")
        cancel()
        return 2
    if replace and held:
        # The remove button shows only while the pointer is over its thumbnail (a plain click waits
        # out its timeout), so hover the thumbnail that holds it first.
        for _ in range(held):
            rm = pg.locator(REMOVE).first
            holder = rm.locator("xpath=ancestor::*[.//img][1]")
            if holder.count():
                holder.first.hover(force=True)
            pg.wait_for_timeout(400)
            rm.click(force=True, timeout=5000)
            pg.wait_for_timeout(600)
        if pg.locator(REMOVE).count():
            print(f"DID NOT RUN: removing the {held} attachment(s) the comment held left "
                  f"{pg.locator(REMOVE).count()} in the editor; nothing saved.")
            cancel()
            return 2
        print(f"  took off the {held} picture(s) the comment held")
        held = 0
    return stage_and_save(pg, comment, pictures, held, cancel, exact=replace)


def stage_and_save(pg, comment, pictures, held, cancel, exact=False):
    """Add `pictures` to the open editor, which holds `held` already, and press Save. 0 when the thread
    then shows held + N attached images (exactly that many with `exact`), 2 when a control was missing
    or the count did not rise. With no pictures it only saves; the caller checks the saved text."""
    if not pictures:
        pg.get_by_role("button", name="Save", exact=True).click()
        pg.wait_for_timeout(2000)
        return 0
    inp = pg.locator('input[type=file][accept*="image"]')
    if inp.count() != 1:
        print(f"DID NOT RUN: {inp.count()} image inputs on the page while editing; expected 1.")
        cancel()
        return 2
    inp.set_input_files(pictures)
    staged = held
    for _ in range(max(8, 4 * len(pictures))):
        pg.wait_for_timeout(1000)
        staged = pg.locator(REMOVE).count()
        if staged >= held + len(pictures):
            break
    if staged != held + len(pictures):
        print(f"DID NOT ATTACH: the editor holds {staged} attachment(s) after adding {len(pictures)} to {held}; "
              "nothing saved.")
        cancel()
        return 2
    pg.get_by_role("button", name="Save", exact=True).click()
    after = 0
    want = held + len(pictures)
    for _ in range(30):
        pg.wait_for_timeout(1000)
        after = pg.evaluate(TOTAL, "")
        if (after == want) if exact else (after >= want):
            break
    os.makedirs(PROOF, exist_ok=True)
    shot = os.path.join(PROOF, f"{comment['id']}.png")
    pg.screenshot(path=shot)
    if after < want or (exact and after != want):
        print(f"DID NOT ATTACH: saved, but the thread shows {after} attached image(s), not "
              f"{'exactly ' if exact else ''}{want}. Screenshot: {shot}")
        return 2
    print(f"  ATTACHED {len(pictures)} picture(s) to comment {comment['id']}: its editor held {held} and "
          f"{staged} before saving; the thread shows {after}. Screenshot: {shot}")
    return 0


def _stamps():
    try:
        return json.load(open(STAMPS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_stamps(d):
    os.makedirs(os.path.dirname(STAMPS), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STAMPS))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STAMPS)


def _stamp(key, cid, pictures):
    d = _stamps()
    d.setdefault(key, {}).setdefault(str(cid), []).append([time.time(), [os.path.basename(p) for p in pictures]])
    _save_stamps(d)


def _read(key, md=False):
    """(comments, me) over REST, or (None, None) after printing why. md=True reads the markdown form,
    where only a mention chip drops its "@"."""
    try:
        return (_get(f"/files/{key}/comments" + ("?as_md=true" if md else "")).get("comments", []),
                _get("/me"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"DID NOT RUN: the Figma API failed: {e}")
        return None, None


def _comment_url(key, by_id, comment):
    """The link that opens the comment's thread: its root's id, on the node the root is pinned to."""
    root = comment
    while root.get("parent_id") in by_id:
        root = by_id[root["parent_id"]]
    node = (root.get("client_meta") or {}).get("node_id")
    return (f"https://www.figma.com/design/{key}/f" + (f"?node-id={node.replace(':', '-')}" if node else "")
            + f"#{root['id']}")


def attach(key, cid, pictures, again=False, wait_user=180):
    """Attach `pictures` to comment `cid` in file `key`: run_jobs with one job. Exit code: 0, 1 or 2."""
    return run_jobs([{"key": key, "cid": cid, "pictures": pictures, "again": again}], wait_user)[0]


def edit(key, cid, text, pictures=(), wait_user=180, replace=False):
    """Replace comment `cid`'s text with `text` and attach `pictures` (in place of the ones it holds,
    with `replace`): run_jobs with one job. Exit code: 0, 1 or 2; 0 only when the API then returns the
    new text."""
    return run_jobs([{"key": key, "cid": cid, "text": text, "pictures": list(pictures), "replace": replace}],
                    wait_user)[0]


def mention(key, cids, wait_user=180):
    """Turn each "@<Last, First>" in each comment into a mention chip, all in one browser. Exit code."""
    comments, _ = _read(key)
    if comments is None:
        return 2
    known = {(c.get("user") or {}).get("handle") for c in comments} - {None}
    by_id = {c["id"]: c for c in comments}
    jobs = [{"key": key, "cid": c, "mentions": handles_in((by_id.get(str(c)) or {}).get("message"), known)}
            for c in cids]
    return run_jobs(jobs, wait_user)[0]


def _failed(pg, e, name="failed-step"):
    """Print a browser step's Playwright error with where it stopped and a screenshot. Returns 2."""
    lines = str(e).splitlines()
    where = next((l.strip() for l in lines if "locator(" in l or "get_by_role(" in l), "")
    shot = os.path.join(PROOF, f"{name}.png")
    try:
        os.makedirs(PROOF, exist_ok=True)
        pg.screenshot(path=shot)
    except Exception:
        shot = "none"
    print(f"DID NOT RUN: the browser step failed: {lines[0]}" + (f"\n  at {where}" if where else "")
          + f"\n  screenshot: {shot}")
    return 2


def figma_session(url, email, wait_user):
    """One signed-in browser on Figma for a whole batch (signed_in_browser.session): it opens `url`,
    signs in by typing `email` on Figma's own login page, and is gone when the `with` ends."""
    import signed_in_browser
    return signed_in_browser.session(url, ready=lambda pg: sign_in(pg, email, wait_user, target=url),
                                     wait_user=wait_user)


# --- mentions ------------------------------------------------------------------------------------
# REST posts "@Last, First" as plain text and notifies nobody, and the Plugin API and the MCP have no
# comments at all. The web editor turns a picked name into a mention chip, and a chip added by an
# edit notifies the person. Over REST the plain message reads "@Last, First" either way; the markdown
# form (?as_md=true) drops the "@" only for a chip. The selftest's pairs have the shape real comments
# read back in.

def handles_in(text, known):
    """The known handles written as "@<handle>" in text, in order of first use. An @ inside a word or
    an email address is not one."""
    found, taken = [], []
    for h in sorted(known, key=len, reverse=True):     # the longer name claims its span first
        for m in re.finditer(r"(?<![\w.])@" + re.escape(h) + r"(?![\w])", text or ""):
            if not any(a <= m.start() < b for a, b in taken):
                taken.append(m.span())
                found.append((m.start(), h))
    seen, out = set(), []
    for _, h in sorted(found):
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def stray_ats(text, known):
    """Each "@word" in text that does not start a known handle: a mention the tool cannot make."""
    out = []
    for m in re.finditer(r"(?<![\w.])@(\S[^\n]{0,40})", text or ""):
        if not any(m.group(1).startswith(h) for h in known):
            out.append("@" + m.group(1).split()[0])
    return out


def mention_state(plain, md, handle):
    """'chip', 'text' (typed, notifies nobody) or 'absent', from a comment's plain and markdown text."""
    at = "@" + handle
    if at not in (plain or "") and handle not in (plain or ""):
        return "absent"
    if at in (md or ""):
        return "text"
    return "chip" if handle in (md or "") else "absent"


def pick_option(rows, handle):
    """(row, None) for the one picker option that is this person, or (None, why). An option must read
    the whole handle and not be a guest, and no other option may carry the same surname."""
    options = [r for r in rows if (r.get("role") or "") == "option"]
    surname = handle.split(",")[0].strip()
    same = [r for r in options if re.search(r"\b" + re.escape(surname) + r",", r.get("text") or "")]
    exact = [r for r in same if re.search(r"(?<![\w])" + re.escape(handle) + r"(?![\w])", r["text"])
             and "(guest)" not in r["text"]]
    if len(same) != 1 or len(exact) != 1:
        return None, (f"the picker shows {len(exact)} option(s) that are {handle} and {len(same)} with the "
                      f"surname {surname}, among {len(options)}; expected exactly one of each")
    return exact[0], None


def query_for(handle):
    """What to type after the @ to bring the person up: the first name of "Last, First"."""
    return handle.split(",", 1)[1].split()[0] if "," in handle else handle.split()[0]


def mention_refusal(comment, me_id, handles, replies):
    """Refusal text or None for turning each "@handle" in a comment into a mention chip. No browser."""
    if comment is None:
        return "REFUSED: that comment is not in this file."
    if (comment.get("user") or {}).get("id") != me_id:
        return (f"REFUSED: comment {comment.get('id')} is {(comment.get('user') or {}).get('handle')}'s. Only its "
                "author can edit it.")
    if replies:
        return (f"REFUSED: comment {comment.get('id')} has replies; an edit would change what they answer. "
                "Mention the person in a reply instead.")
    if not handles:
        return f"REFUSED: comment {comment.get('id')} names nobody as @<Last, First>."
    missing = [h for h in handles if "@" + h not in (comment.get("message") or "")]
    if missing:
        return f"REFUSED: comment {comment.get('id')} does not hold {', '.join('@' + h for h in missing)}."
    return None


# Select the first "@<handle>" (and one space after it) in the focused editor's text, across text
# nodes; returns the selected string, or null.
SELECT_AT = """(at) => {
  const ed = document.activeElement;
  if (!ed || !ed.isContentEditable) return null;
  const walk = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
  const nodes = []; let all = '';
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    let p = n.parentElement, chip = false;
    for (; p && p !== ed; p = p.parentElement) if (p.getAttribute('contenteditable') === 'false') chip = true;
    if (chip) continue;
    nodes.push([n, all.length]); all += n.data;
  }
  let i = all.indexOf(at);
  if (i < 0) return null;
  let j = i + at.length;
  if (all[j] === ' ' || all[j] === '\\u00a0') j += 1;
  const at_ = k => { for (let x = nodes.length - 1; x >= 0; x--) if (nodes[x][1] <= k) return [nodes[x][0], k - nodes[x][1]]; };
  const [sn, so] = at_(i), [en, eo] = at_(j);
  const r = document.createRange(); r.setStart(sn, so); r.setEnd(en, eo);
  const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  return s.toString();
}"""

# Every visible element outside the focused editor that reads `word`: the picker's rows, with role.
PICKER = """(word) => {
  const ed = document.activeElement, out = [], re = new RegExp(word, 'i');
  for (const el of document.querySelectorAll('[role=option],[role=menuitem],[role=listitem],li,button,[role=button]')) {
    if (ed && ed.contains(el)) continue;
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!re.test(t)) continue;
    const b = el.getBoundingClientRect();
    if (b.width < 2 || b.height < 2) continue;
    out.push({role: el.getAttribute('role'), text: t.slice(0, 160), x: b.x, y: b.y, w: b.width, h: b.height});
  }
  return out;
}"""

# The focused editor's text and its mention chips.
CHIPS = """() => {
  const e = document.activeElement;
  if (!e || !e.isContentEditable) return null;
  const chips = [...e.querySelectorAll('[data-mention-user-id]')].map(n => n.innerText);
  return {text: e.innerText, chips};
}"""


def _redact(s):
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", s or "")


def add_mentions(pg, comment, handles):
    """Turn each "@handle" in the open comment into a mention chip picked from the editor's list, then
    Save. 0 saved, 1 refused (the picker did not show exactly this person), 2 a control was missing or
    the editor's text changed. Nothing is saved unless the editor then holds the comment's own text
    with each "@handle" a chip, and every attachment it opened with."""
    code, ed, cancel = open_editor(pg, comment, holds=editor_holds)
    if code:
        return code
    held = pg.locator(REMOVE).count()

    def stop(why, code):
        os.makedirs(PROOF, exist_ok=True)
        shot = os.path.join(PROOF, f"{comment['id']}-mention-stopped.png")
        try:
            pg.screenshot(path=shot)
        except Exception:
            shot = "none"
        print(f"{'REFUSED' if code == 1 else 'DID NOT RUN'} on {comment['id']}: {why}. Nothing saved. Screenshot: {shot}")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        try:
            cancel()
        except Exception:
            pass
        return code
    want = comment["message"]
    for h in handles:
        ed.click()
        pg.wait_for_timeout(200)
        got = pg.evaluate(SELECT_AT, "@" + h)
        if not got or norm(got) != norm("@" + h):
            return stop(f"the editor holds no plain \"@{h}\" to turn into a mention (selected {got!r})", 2)
        pg.keyboard.type("@" + query_for(h), delay=90)
        rows = []
        for _ in range(12):
            pg.wait_for_timeout(500)
            rows = pg.evaluate(PICKER, re.escape(query_for(h)))
            if any((r.get("role") or "") == "option" for r in rows):
                break
        print(f"  {comment['id']}: picker for @{query_for(h)}: {[_redact(r['text'])[:60] for r in rows if r.get('role') == 'option']}")
        row, why = pick_option(rows, h)
        if row is None:
            return stop(why, 1)
        pg.mouse.click(row["x"] + row["w"] / 2, row["y"] + row["h"] / 2)
        pg.wait_for_timeout(800)
        want = want.replace("@" + h, h, 1)
        state = pg.evaluate(CHIPS) or {}
        if norm(state.get("text")) != norm(want):
            pg.keyboard.type(" ")            # the chip took the space after it
            pg.wait_for_timeout(300)
            state = pg.evaluate(CHIPS) or {}
        if norm(state.get("text")) != norm(want):
            return stop(f"after picking, the editor reads {_redact(norm(state.get('text')))[:100]!r}, not the "
                        "comment's text", 2)
    state = pg.evaluate(CHIPS) or {}
    if sorted(state.get("chips") or []) != sorted(handles):
        return stop(f"the editor's chips are {state.get('chips')}, not {handles}", 2)
    if pg.locator(REMOVE).count() != held:
        return stop(f"the editor held {held} attachment(s) and now holds {pg.locator(REMOVE).count()}", 2)
    pg.get_by_role("button", name="Save", exact=True).click()
    pg.wait_for_timeout(2500)
    os.makedirs(PROOF, exist_ok=True)
    shot = os.path.join(PROOF, f"{comment['id']}-mention.png")
    pg.screenshot(path=shot)
    print(f"  {comment['id']}: saved with {len(handles)} mention chip(s); screenshot {shot}")
    return 0


def wait_for_comment(pg, comment, secs=EDIT_WAIT):
    """True once exactly one comment on the page reads this one's text (after a goto in the session)."""
    start = time.time()
    while time.time() - start < secs:
        info = _probe(pg)
        if info.get("fonts"):
            pg.keyboard.press("Escape")
        try:
            if len(pg.evaluate(PICK, face(comment["message"])[:60])) == 1:
                return True
        except Exception:
            pass
        pg.wait_for_timeout(1000)
    return False


# --- resolve ----------------------------------------------------------------------------------------
# REST has no resolve (POST and DELETE only), so it is the open thread's header checkmark. Read off a
# live file: the open thread is a region labelled "Currently expanded comment thread...", its header
# holds Thread actions, "Mark as resolved" and Close comment, and a resolved thread opened by link shows
# "Mark as unresolved" there. The Comments panel draws a "Mark as resolved" on every card too, so
# nothing outside the open thread's region is ever clicked. The thread can close on the click before
# its header redraws, so a page that never shows "Mark as unresolved" opens the link again, and REST
# decides either way.
THREAD_REGION = '[role=region][aria-label^="Currently expanded comment thread"]'
RESOLVE_BTN = 'button[aria-label="Mark as resolved"]'
UNRESOLVE_BTN = 'button[aria-label="Mark as unresolved"]'


def wait_for_thread(pg, comment, secs=EDIT_WAIT):
    """True once the open thread holding this root's text shows (thread_target finds it, open or
    resolved). A resolve waits on the region it acts on, not on wait_for_comment's "Comment actions"
    lookup, which can find nothing on a teammate's thread whose text the open region holds."""
    start = time.time()
    while time.time() - start < secs:
        try:
            got = pg.evaluate(OPEN_THREADS, [THREAD_REGION, RESOLVE_BTN, UNRESOLVE_BTN])
            if thread_target(got, comment["message"])[1]:
                return True
        except Exception:
            pass
        pg.wait_for_timeout(1000)
    return False


# Each open thread region: its text and how many of each control its header holds.
OPEN_THREADS = """([region, resolve, unresolve]) => [...document.querySelectorAll(region)].map(r => ({
  text: r.innerText || '', resolve: r.querySelectorAll(resolve).length, unresolve: r.querySelectorAll(unresolve).length}))"""


def resolve_refusal(comment, comments, me_id):
    """Refusal text or None for resolving the thread `comment` is the root of, from the API's view of
    the file's comments. No browser."""
    if comment is None:
        return "REFUSED: that comment is not in this file."
    cid = comment.get("id")
    if comment.get("parent_id"):
        return (f"REFUSED: comment {cid} is a reply in thread {comment['parent_id']}. Resolve applies to a "
                f"thread: give its root, {comment['parent_id']}.")
    if comment.get("resolved_at"):
        return f"REFUSED: thread {cid} is already resolved ({comment['resolved_at'][:16]}). Nothing to do."
    if not face(comment.get("message")):
        return (f"REFUSED: thread {cid} has no text, so the open thread cannot be told from another on the "
                "page. Resolve it in the Figma UI.")
    if not me_id:
        return "REFUSED: GET /v1/me gave no id, so whose threads these are cannot be told."
    thread = [comment] + [c for c in comments if c.get("parent_id") == cid]
    last = max(thread, key=lambda c: c.get("created_at") or "")
    root_by, last_by = comment.get("user") or {}, last.get("user") or {}
    if root_by.get("id") != me_id and last_by.get("id") != me_id:
        return (f"REFUSED: thread {cid} is {root_by.get('handle')}'s and {last_by.get('handle')} wrote its newest "
                f"comment ({(last.get('created_at') or '')[:10]}). A thread a teammate is still discussing is "
                "theirs to resolve; answer it instead.")
    return None


def resolve_jobs(key, cids):
    """One resolve job per thread id, in the order given, each id once."""
    jobs, seen = [], set()
    for c in cids:
        if str(c) not in seen:
            seen.add(str(c))
            jobs.append({"key": key, "cid": str(c), "resolve": True})
    return jobs


def resolve(key, cids, wait_user=180):
    """Resolve each thread in `cids`, all in one browser, then read REST. Exit code: 0 every one
    resolved over REST, 1 refused (nothing changed), 2 not all resolved."""
    return run_jobs(resolve_jobs(key, cids), wait_user)[0]


def thread_target(threads, message):
    """(index, state, why) for the one open thread whose text holds `message`'s opening: state 'open'
    (its header holds one "Mark as resolved") or 'resolved' (one "Mark as unresolved"), else None
    with why."""
    prefix = face(message)[:60]
    hits = [i for i, t in enumerate(threads) if prefix and prefix in face(t.get("text"))]
    if len(hits) != 1:
        return None, None, f"{len(hits)} open thread(s) of {len(threads)} hold this comment's text; expected 1"
    t = threads[hits[0]]
    if t.get("unresolve") == 1 and not t.get("resolve"):
        return hits[0], "resolved", None
    if t.get("resolve") == 1 and not t.get("unresolve"):
        return hits[0], "open", None
    return None, None, (f"the open thread holds {t.get('resolve')} \"Mark as resolved\" and {t.get('unresolve')} "
                        "\"Mark as unresolved\"; expected one of them")


def resolve_thread(s, url, comment, wait=None):
    """Click the open thread's own "Mark as resolved" and wait for its header to read "Mark as
    unresolved", opening the link again if the thread closed. 0 shown resolved, 2 a control was
    missing or the page never showed it. REST decides afterwards either way."""
    pg, cid = s.page, comment["id"]
    wait = RESOLVE_PAGE_WAIT if wait is None else wait
    os.makedirs(PROOF, exist_ok=True)
    shot = os.path.join(PROOF, f"{cid}-resolve.png")

    def look():
        return thread_target(pg.evaluate(OPEN_THREADS, [THREAD_REGION, RESOLVE_BTN, UNRESOLVE_BTN]), comment["message"])
    i, state, why = look()
    if state != "open":
        pg.screenshot(path=shot)
        if state == "resolved":
            print(f"  {cid}: the thread already shows as resolved; nothing clicked. Screenshot: {shot}")
            return 0
        print(f"DID NOT RUN on {cid}: {why}; nothing clicked. Screenshot: {shot}")
        return 2
    pg.locator(THREAD_REGION).nth(i).locator(RESOLVE_BTN).click(timeout=10000)
    how, start = "its header now reads \"Mark as unresolved\"", time.time()
    while state != "resolved" and time.time() - start < wait:
        pg.wait_for_timeout(500)
        _, state, why = look()
    if state != "resolved":
        s.goto(url)                       # the thread may close on resolve: open it again and read it there
        if wait_for_thread(pg, comment):
            _, state, why = look()
            how = "opened again, its header reads \"Mark as unresolved\""
    pg.screenshot(path=shot)
    if state != "resolved":
        print(f"DID NOT SHOW RESOLVED on {cid}: clicked \"Mark as resolved\", but {why or 'the thread never showed as resolved'}. "
              f"Screenshot: {shot}")
        return 2
    print(f"  {cid}: clicked the open thread's \"Mark as resolved\"; {how}. Screenshot: {shot}")
    return 0


def _check_resolves(jobs, res, read, before, sleep=time.sleep):
    """Over REST, once the browser is gone: resolved_at for every resolve job, whatever its browser
    step returned (a click can land though the page never showed it), printed as RESOLVED or NOT
    RESOLVED per id. Updates `res`. Returns 2 when another thread in those files turned resolved
    during the run, which nobody asked for (the click may have landed there), else 0."""
    idx = [i for i, j in enumerate(jobs) if j.get("resolve")]
    if not idx:
        return 0
    keys = sorted({jobs[i]["key"] for i in idx})
    now, start = {}, time.time()
    while True:
        for k in keys:
            got = read(k)[0]
            if got is not None:
                now[k] = {c["id"]: c for c in got}
        if all((now.get(jobs[i]["key"], {}).get(jobs[i]["cid"]) or {}).get("resolved_at") for i in idx) \
                or time.time() - start >= RESOLVE_API_WAIT:
            break
        sleep(2)
    for i in idx:
        j = jobs[i]
        c = now.get(j["key"], {}).get(j["cid"])
        if c and c.get("resolved_at"):
            print(f"RESOLVED {j['cid']}: REST resolved_at {c['resolved_at']}"
                  + ("" if res[i] == 0 else "; the page did not show it, the API does") + ".")
            res[i] = 0
            continue
        why = ("the API read failed" if j["key"] not in now else "the comment is no longer in the file" if c is None
               else f"REST resolved_at is still empty {RESOLVE_API_WAIT}s after the browser closed"
               + ("" if res[i] == 0 else "; the browser step did not finish, see above"))
        print(f"NOT RESOLVED {j['cid']}: {why}.")
        res[i] = 2
    asked, stray = {(jobs[i]["key"], jobs[i]["cid"]) for i in idx}, 0
    for k in keys:
        was = {c["id"]: c for c in before.get(k) or []}
        for cid, c in (now.get(k) or {}).items():
            if (k, cid) not in asked and c.get("resolved_at") and cid in was and not was[cid].get("resolved_at"):
                print(f"ALSO RESOLVED during this run, not asked for: thread {cid} ({(c.get('user') or {}).get('handle')}'s, "
                      f"{c['resolved_at']}). Someone else may have resolved it, or the click landed there; look at it.")
                stray = 2
    return stray


# --- one browser for every browser step of a batch ---------------------------------------------------

def job_refusal(job, comments, me_id, stamps):
    """Refusal text or None for one job, from the API's view. No browser. A job is a dict: key, cid,
    and one of: pictures (attach), text (+ pictures, replace: edit), mentions (handles), resolve, probe."""
    cid = str(job["cid"])
    by_id = {c["id"]: c for c in comments}
    comment = by_id.get(cid)
    replies = sum(1 for c in comments if c.get("parent_id") == cid)
    stamped = [n for _, names in stamps.get(job["key"], {}).get(cid, []) for n in names]
    if job.get("probe"):
        return None if comment else "REFUSED: that comment is not in this file."
    if job.get("resolve"):
        return resolve_refusal(comment, comments, me_id)
    if "text" in job:
        return edit_refusal(comment, me_id, job["text"], job.get("pictures", []), replies, stamped,
                            job.get("replace", False))
    if job.get("mentions"):
        return mention_refusal(comment, me_id, job["mentions"], replies)
    return refusal(comment, me_id, job.get("pictures", []), stamped, job.get("again", False))


def run_jobs(jobs, wait_user=180, session=None, read=None):
    """Run every job's browser step in ONE signed-in browser, in order, then check each over REST.
    Refuses the whole batch, with no browser opened, when any job is refused. Returns
    (exit code, {cid: code}): 0 all done and checked, 1 refused, 2 some step did not run.
    `session` and `read` are the selftest's stubs."""
    read = read or _read
    session = session or figma_session
    jobs = [dict(j, cid=str(j["cid"]), pictures=[os.path.abspath(p) for p in j.get("pictures", [])]) for j in jobs]
    views = {}
    for j in jobs:
        if j["key"] not in views:
            views[j["key"]] = read(j["key"])
            if views[j["key"]][0] is None:
                return 2, {}
    stamps = _stamps()
    whys = [(j, job_refusal(j, views[j["key"]][0], views[j["key"]][1].get("id"), stamps)) for j in jobs]
    if any(w for _, w in whys):
        for j, w in whys:
            if w:
                print(f"  {j['cid']}: {w}")
        print(f"REFUSED: {sum(1 for _, w in whys if w)} of {len(jobs)} job(s) refused; no browser opened, nothing changed.")
        return 1, {}
    me = views[jobs[0]["key"]][1]
    if not me.get("email"):
        print("DID NOT RUN: GET /v1/me gave no email to sign in with.")
        return 2, {}
    import signed_in_browser
    from playwright.sync_api import Error as PlaywrightError
    res = [2] * len(jobs)          # one result per job; a comment can carry a picture job and a mention job
    urls = []
    for j in jobs:
        by_id = {c["id"]: c for c in views[j["key"]][0]}
        j.setdefault("before", by_id[j["cid"]].get("message"))
        urls.append((j, by_id[j["cid"]], _comment_url(j["key"], by_id, by_id[j["cid"]])))
    launches = signed_in_browser.LAUNCHES
    try:
        with session(urls[0][2], me["email"], wait_user) as s:
            for i, (j, comment, url) in enumerate(urls):
                try:
                    if i:
                        s.goto(url)
                    if not (wait_for_thread if j.get("resolve") else wait_for_comment)(s.page, comment):
                        print(f"DID NOT RUN on {j['cid']}: the thread did not show this comment within {EDIT_WAIT}s.")
                        continue
                    if j.get("probe"):
                        if j.get("picker"):
                            res[i] = probe_picker(s.page, comment, j["picker"])
                        else:
                            code, _, cancel = open_editor(s.page, comment, holds=editor_holds)
                            if not code:
                                cancel()
                                print(f"  {j['cid']}: probe: the thread loaded and its editor opened; cancelled.")
                            res[i] = code
                    elif j.get("resolve"):
                        res[i] = resolve_thread(s, url, comment)
                    elif "text" in j:
                        res[i] = edit_text(s.page, comment, j["text"], j["pictures"], j.get("replace", False))
                        res[i] = res[i] or saved_view_refusal(s, url, j["text"], j["cid"])
                    elif j.get("mentions"):
                        res[i] = add_mentions(s.page, comment, j["mentions"])
                        res[i] = res[i] or saved_view_refusal(s, url, comment["message"], j["cid"])
                    else:
                        res[i] = edit_and_attach(s.page, comment, j["pictures"], j.get("again", False))
                except PlaywrightError as e:
                    res[i] = _failed(s.page, e, f"{j['cid']}-failed")
    except signed_in_browser.NotReady as e:
        print(f"DID NOT RUN: {e}.")
        for j in jobs:
            if j.get("resolve"):
                print(f"NOT RESOLVED {j['cid']}: the browser did not get to it, so nothing was clicked.")
        return 2, {j["cid"]: 2 for j in jobs}
    print(f"  one browser for {len(jobs)} job(s): {signed_in_browser.LAUNCHES - launches} launch(es).")
    stray = _check_resolves(jobs, res, read, {k: v[0] for k, v in views.items()})
    worst = max(_check_jobs(jobs, res, read), stray)
    codes = {}
    for j, r in zip(jobs, res):
        codes[j["cid"]] = max(codes.get(j["cid"], 0), r)
    return worst, codes


# The text of the thread view's comment that reads like `prefix`, as the page drew it from the server.
THREAD = """(prefix) => {
  const norm = s => (s || '').replace(/\\u00d7/g, 'x').replace(/[\\u2018\\u2019]/g, "'")
    .replace(/[\\u201c\\u201d]/g, '"').replace(/@/g, '').replace(/\\s+/g, ' ').trim();
  for (const b of document.querySelectorAll('button[aria-label="Comment actions"]')) {
    let a = b.parentElement;
    for (let d = 0; d < 12 && a; d++, a = a.parentElement) {
      if (a.querySelectorAll('button[aria-label="Comment actions"]').length > 1) break;
      if (norm(a.innerText).includes(prefix)) return a.innerText;
    }
  }
  return null;
}"""


def saved_view_refusal(s, url, expected, cid):
    """0, or 2 after printing why: the thread opened again shows the whole of `expected`. The editor's
    own view after Save is the page's, not the server's, so the check reloads."""
    s.goto(url)
    if not wait_for_comment(s.page, {"message": expected}):
        print(f"DID NOT KEEP THE TEXT on {cid}: opened again, the thread shows no comment reading like the saved text.")
        return 2
    got = s.page.evaluate(THREAD, face(expected)[:60]) or ""
    if face(expected) not in face(got):
        print(f"DID NOT KEEP THE TEXT on {cid}: opened again, the thread reads {face(got)[-100:]!r}, not the whole "
              "saved text. Look at the comment.")
        return 2
    print(f"  {cid}: opened again, the thread shows the whole saved text.")
    return 0


def probe_picker(pg, comment, handle):
    """Open the comment's editor, type "@<first name>" at its end, read the picker, pick nothing, and
    cancel. 0 when the picker shows exactly this person, 1 when it does not."""
    code, ed, cancel = open_editor(pg, comment, holds=editor_holds)
    if code:
        return code
    ed.click()
    pg.keyboard.press("Meta+ArrowDown" if sys.platform == "darwin" else "Control+End")
    pg.keyboard.type(" @" + query_for(handle), delay=90)
    rows = []
    for _ in range(12):
        pg.wait_for_timeout(500)
        rows = pg.evaluate(PICKER, re.escape(query_for(handle)))
        if any((r.get("role") or "") == "option" for r in rows):
            break
    row, why = pick_option(rows, handle)
    print(f"  {comment['id']}: probe picker for @{query_for(handle)}: "
          f"{[_redact(r['text'])[:60] for r in rows if r.get('role') == 'option']} -> "
          + (f"would pick {_redact(row['text'])[:60]!r}" if row else why) + "; cancelled, nothing saved.")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)
    cancel()
    return 0 if row else 1


def _check_jobs(jobs, res, read, wait_api=20):
    """Over REST, after the browser closed: stamp attached pictures, check an edit's saved text, and
    check each mention is a chip. Updates `res` (one result per job) and returns the worst."""
    for i, j in enumerate(jobs):
        if res[i] != 0 or j.get("probe") or j.get("resolve"):     # _check_resolves reads those
            continue
        if j["pictures"] and "text" not in j:
            _stamp(j["key"], j["cid"], j["pictures"])
        if "text" in j:
            if j["pictures"] and j.get("replace"):
                d = _stamps()
                d.setdefault(j["key"], {})[j["cid"]] = []
                _save_stamps(d)
            if j["pictures"]:
                _stamp(j["key"], j["cid"], j["pictures"])
            saved, start = None, time.time()
            while time.time() - start < wait_api:
                now, _ = read(j["key"])
                saved = next((c for c in now or [] if c["id"] == j["cid"]), None)
                if saved and norm(saved.get("message")) == norm(j["text"]):
                    break
                time.sleep(2)
            if not (saved and norm(saved.get("message")) == norm(j["text"])):
                print(f"DID NOT CHECK: saved, but the API's text for {j['cid']} is "
                      f"{norm((saved or {}).get('message'))[:80]!r}, not the new text. Look at the comment.")
                res[i] = 2
                continue
            print(f"  EDITED comment {j['cid']}: the API returns the new text"
                  + (f", and {len(j['pictures'])} picture(s) went in" if j["pictures"] else "") + ".")
            try:   # the EDIT draft leaves the drafts file, when one is configured, now that the comment holds it
                import drafts_store
                gone = drafts_store.remove_posted(j["text"], comment_id=j["cid"])
            except ImportError:
                gone = None
            if gone:
                print(gone)
        if j.get("mentions"):
            # The edit changes nothing but the chip, so both REST forms must still hold the whole text the
            # comment had before it. A plain read that matches can be the copy from before the Save while
            # the markdown read is the copy after it, so both are read, each in full.
            plain = {c["id"]: c for c in (read(j["key"])[0] or [])}
            md = {c["id"]: c for c in (read(j["key"], md=True)[0] or [])}
            p, m = plain.get(j["cid"], {}).get("message"), md.get(j["cid"], {}).get("message")
            for h in j["mentions"]:
                st = mention_state(p, m, h)
                print(f"  {j['cid']}: @{h} is a {st} over REST" + ("" if st == "chip" else ": NOT a mention"))
                if st != "chip":
                    res[i] = 2
            whole = face(j.get("before") or "")
            if whole and (face(p) != whole or face(m) != whole):
                print(f"DID NOT KEEP THE TEXT on {j['cid']}: before the edit it read {len(whole)} characters; over "
                      f"REST it now reads {len(face(p))} (plain) and {len(face(m))} (markdown), ending "
                      f"{face(m)[-60:]!r}. Look at the comment.")
                res[i] = 2
    return max(res or [0])


def selftest():
    results = []

    def case(name, ok):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    td = tempfile.mkdtemp(prefix="figma-attach-selftest-")
    pic = os.path.join(td, "sheet.jpg")
    open(pic, "wb").write(b"\xff\xd8\xff")
    mine = {"id": "9", "user": {"id": "me", "handle": "Owner"},
            "message": "FIX (done in Figma): the crop. Please confirm and resolve.\n\nBefore and after:"}
    theirs = dict(mine, user={"id": "other", "handle": "Designer"})
    unlabelled = dict(mine, message="FIX (done in Figma): the crop. Please confirm and resolve.")
    case("the token owner's comment ending in the label, with a real picture: passes", refusal(mine, "me", [pic]) is None)
    case("someone else's comment: refused", "Only its author" in (refusal(theirs, "me", [pic]) or ""))
    case("a comment without the closing label: refused", LABEL in (refusal(unlabelled, "me", [pic]) or ""))
    case("a comment not in the file: refused", refusal(None, "me", [pic]) is not None)
    case("no picture: refused", refusal(mine, "me", []) is not None)
    case("six pictures: refused", refusal(mine, "me", [pic] * 6) is not None)
    case("a missing file: refused", refusal(mine, "me", [pic + "x"]) is not None)
    txt = pic.replace(".jpg", ".txt")
    open(txt, "w").close()
    case("a .txt file: refused", "not a .jpg" in (refusal(mine, "me", [txt]) or ""))
    case("a picture already stamped on the comment: refused", "--again" in (refusal(mine, "me", [pic], ["sheet.jpg"]) or ""))
    case("... and passes with --again", refusal(mine, "me", [pic], ["sheet.jpg"], again=True) is None)
    ed = "FIX (done in Figma): the crop. Please confirm and resolve.\n\n\n\n\nBefore and after:"
    case("the editor's text with the editor's blank lines matches its comment", editor_matches(ed, mine["message"]))
    case("a 390×835 drawn by the UI matches the API's 390x835",
         editor_matches("FIX: layer 390×835 trimmed, as on desktop, see the crop. Before and after:",
                        "FIX: layer 390x835 trimmed, as on desktop, see the crop.\n\nBefore and after:"))
    case("another comment's editor does not match",
         not editor_matches("FIX (done in Figma): the badge. Please confirm and resolve. Before and after:", mine["message"]))
    case("an editor without the label does not match",
         not editor_matches("FIX (done in Figma): the crop. Please confirm and resolve.", mine["message"]))
    file_url = "https://www.figma.com/design/K/f?node-id=1-2#9"
    case("the file with the comment open: ready", classify({"url": file_url, "actions": 1}) == "ready")
    case("Figma's email-only login page: type the email",
         classify({"url": "https://www.figma.com/email_only?cont=x", "email": True}) == "figma-email")
    case("Figma's login page asking for a password: the user's",
         classify({"url": "https://www.figma.com/login", "email": True, "password": True}) == "needs-user")
    case("the blank identity-provider redirect: wait",
         classify({"url": "https://idp.example.com/x/saml2"}) == "wait")
    case("an identity provider asking for an account: the user's, never typed into",
         classify({"url": "https://idp.example.com/x", "email": True}) == "needs-user")
    case("an account picker: the user's", classify({"url": "https://idp.example.com/x", "picker": True}) == "needs-user")
    case("a code prompt: the user's", classify({"url": "https://idp.example.com/x", "code": True}) == "needs-user")
    case("the file still loading: wait", classify({"url": file_url, "actions": 0}) == "wait")

    class Page:   # sign_in against a scripted tab
        def __init__(self, states):
            self.states, self.typed, self.clicked = list(states), [], 0
            self.keyboard = type("K", (), {"press": lambda s, k: None})()
        def evaluate(self, js):
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        def locator(self, sel):
            page = self
            class L:
                first = property(lambda s: s)
                def fill(s, text): page.typed.append(text)
            return L()
        def get_by_role(self, role, name=None):
            page = self
            class B:
                first = property(lambda s: s)
                def click(s): page.clicked += 1
            return B()
        url = ""
    clock = [0.0]
    tick = lambda: clock[0]
    def step(_):
        clock[0] += 1
    login = {"url": "https://www.figma.com/email_only", "email": True}
    redirect = {"url": "https://idp.example.com/x"}
    loaded = {"url": file_url, "actions": 1}
    pg = Page([login, redirect, redirect, loaded])
    case("sign_in types the email once on Figma's page, waits out the redirect, and reports ready",
         sign_in(pg, "a@b.c", 5, tick, step) == "ready" and pg.typed == ["a@b.c"] and pg.clicked == 1)
    clock[0] = 0.0
    pg = Page([login, login, redirect])
    got = sign_in(pg, "a@b.c", 5, tick, step)
    case("a sign-in that never finishes gives up after the user's wait, having typed the email once",
         got != "ready" and "not finished" in got and pg.typed == ["a@b.c"])
    clock[0] = 0.0
    dropped = {"url": "https://www.figma.com/design/K/f?node-id=1-2", "actions": 0}
    pg = Page([login, redirect, dropped, loaded])
    gone = []
    pg.goto = lambda u, **kw: gone.append(u)
    case("signed in on the file without the comment's #id: the comment's link is opened again, once",
         sign_in(pg, "a@b.c", 5, tick, step, target=file_url) == "ready" and gone == [file_url])
    case("the file with the comment's #id is not reopened", not lost_comment(file_url, file_url))
    btn = {"x": 918, "y": 258}
    menu = {"x": 926, "y": 296, "width": 60, "height": 20}
    case("the Edit drawn beside the button is picked over one far away",
         nearest([{"x": 80, "y": 700, "width": 30, "height": 16}, menu], btn) == menu)
    case("no Edit near the button: none picked", nearest([{"x": 80, "y": 700, "width": 30, "height": 16}], btn) is None)
    case("a comment that already holds 3 pictures: refused without --again (a wrong id would put a picture "
         "on another FIX note)", "already holds 3" in (held_refusal(3, 1, False) or ""))
    case("... and with --again, adding 1 to 3 passes", held_refusal(3, 1, True) is None)
    case("adding 3 to 3 passes the limit of 5 even with --again", held_refusal(3, 3, True) is not None)
    case("an empty comment takes 2", held_refusal(0, 2, False) is None)
    clock[0] = 0.0
    pg = Page([{"url": "https://idp.example.com/x", "password": True}])
    got = sign_in(pg, "a@b.c", 3, tick, step)
    case("a password prompt is never typed into", got != "ready" and pg.typed == [])
    old = dict(mine, message="FIX (done in Figma): the crop. Please confirm and resolve.")
    new = "FIX (done in Figma): the crop, and the header. Please confirm and resolve.\n\nBefore and after:"
    case("edit: the token owner's comment, no replies, new text: passes", edit_refusal(old, "me", new, [], 0) is None)
    case("edit: ... with a picture and the label: passes", edit_refusal(old, "me", new, [pic], 0) is None)
    case("edit: someone else's comment: refused", "Only its author" in (edit_refusal(theirs, "me", new, [], 0) or ""))
    case("edit: a comment with a reply: refused", "1 reply" in (edit_refusal(old, "me", new, [], 1) or ""))
    case("edit: empty text: refused", "empty" in (edit_refusal(old, "me", "  \n ", [], 0) or ""))
    case("edit: text with an @: refused", "mention" in (edit_refusal(old, "me", new.replace("crop", "@crop"), [], 0) or ""))
    case("edit: the same text and no picture: refused", "Nothing to do" in (edit_refusal(old, "me", old["message"], [], 0) or ""))
    wrapped_old = dict(old, message="FIX (done in Figma): These cards are 450 tall now, on desktop and phone, so\n"
                                    "the longest title fits on two lines.\n\nBefore and after:")
    case("edit: the same words with the wrapping taken out: passes",
         edit_refusal(wrapped_old, "me", wrapped_old["message"].replace("so\nthe", "so the"), [], 0) is None)
    case("edit: a new text that keeps a quote's wrapping: refused",
         "mid-paragraph" in (edit_refusal(old, "me", wrapped_old["message"] + " More.", [], 0) or ""))
    case("edit: a picture but no closing label: refused", LABEL in (edit_refusal(old, "me", old["message"] + " More.", [pic], 0) or ""))
    case("edit: six pictures: refused", edit_refusal(old, "me", new, [pic] * 6, 0) is not None)
    case("edit: a picture already stamped on the comment: refused", "--again" in (edit_refusal(old, "me", new, [pic], 0, ["sheet.jpg"]) or ""))
    case("edit --replace: the same picture name, remade, passes: the old one comes off",
         edit_refusal(old, "me", new, [pic], 0, ["sheet.jpg"], replace=True) is None)
    case("edit --replace with no picture: refused", "none is given" in (edit_refusal(old, "me", new, [], 0, replace=True) or ""))
    case("edit: a comment not in the file: refused", edit_refusal(None, "me", new, [], 0) is not None)
    case("edit: the editor of a comment with no label is found by its text", editor_holds(old["message"], old["message"]))
    case("edit: another comment's editor is not", not editor_holds("FIX (done in Figma): the badge. Please confirm.", old["message"]))
    listed = ("FIX (done in Figma): Every photo card now follows this set.\n1. The card descriptions use "
              "text/secondary, set once in the card component.\n2. The gradients end at the bottom edge.")
    case("edit: a numbered comment's editor, which draws the list without its numbers, is found",
         editor_holds(listed.replace("1. ", "").replace("2. ", ""), listed))
    case("edit: a numbered comment's editor with the numbers typed in is found", editor_holds(listed, listed))
    case("edit: a different numbered comment is not",
         not editor_holds(listed.replace("Every photo card", "Every hero card"), listed))
    case("edit: paragraphs split on a blank line, lines kept",
         paragraphs("A one.\n\nB two\nB three.\n\n\nBefore and after:") == [["A one."], ["B two", "B three."], ["Before and after:"]])

    # Mentions. The plain/markdown pairs have the shape a file's comments read back in: an
    # "@<handle>" made in the UI is a chip (markdown drops the @), one typed over REST is text (it keeps it).
    known = {"Rivera, Ana", "Okafor, Lee", "Owner, Token Q.", "Berg, Tomas", "Rivera"}
    case("mention: a chip made in the UI reads as a chip",
         mention_state("@Rivera, Ana  can you ensure that the copy on the v3 page matches this?",
                       "Rivera, Ana  can you ensure that the copy on the v3 page matches this?\n", "Rivera, Ana") == "chip")
    case("mention: an @ posted over REST reads as text, not a mention",
         mention_state("@Berg, Tomas Please underline the business function and industry tags",
                       "@Berg, Tomas Please underline the business function and industry tags", "Berg, Tomas") == "text")
    case("mention: the page's chip text finds the comment REST writes with the @",
         editor_holds("Q: Okafor, Lee what's the final body copy for the three cards?",
                      "Q: @Okafor, Lee what's the final body copy for the three cards?"))
    case("mention: ... and another comment still does not match",
         not editor_holds("Q: Okafor, Lee what should the fourth row's label say?",
                          "Q: @Okafor, Lee what's the final body copy for the three cards?"))
    case("mention: a comment without the name reads absent", mention_state("Please match.", "Please match.", "Rivera, Ana") == "absent")
    case("mention: handles found in order, the longer name over its prefix",
         handles_in("FIX: @Rivera, Ana please match. CC: @Owner, Token Q.", known) == ["Rivera, Ana", "Owner, Token Q."])
    case("mention: an email address is not a mention", handles_in("mail rivera@example.com", known) == [])
    case("mention: an @ before nobody known is a stray", stray_ats("ask @Nobody, Here please", known) == ["@Nobody,"])
    case("mention: a known handle is not a stray", stray_ats("FIX: @Rivera, Ana please", known) == [])
    picker = [{"role": "button", "text": "Lee Sandbox"},
              {"role": "option", "text": "D Okafor, Lee lee.okafor@example.com"},
              {"role": "option", "text": "L Lee Arego (guest) b@x.com"},
              {"role": "option", "text": "L lee.malings (guest) c@x.com"}]
    row, _ = pick_option(picker, "Okafor, Lee")
    case("picker: the one non-guest option with the whole handle is picked",
         row is not None and "Okafor, Lee" in row["text"])
    row, why = pick_option(picker + [{"role": "option", "text": "D Okafor, Lee (guest) d@x.com"}], "Okafor, Lee")
    case("picker: a second Okafor, even a guest, refuses", row is None and "surname" in why)
    case("picker: no option for the person refuses", pick_option(picker, "Rivera, Ana")[0] is None)
    case("picker: a guest with the whole handle refuses",
         pick_option([{"role": "option", "text": "Rivera, Ana (guest) s@x.com"}], "Rivera, Ana")[0] is None)
    case("picker: a button that reads the name is not an option",
         pick_option([{"role": "button", "text": "Rivera, Ana"}], "Rivera, Ana")[0] is None)
    case("picker: the first name is what gets typed", query_for("Owner, Token Q.") == "Token" and query_for("Rivera, Ana") == "Ana")
    posted = dict(mine, message="FIX: @Rivera, Ana please match the copy.")
    case("mention job: the owner's comment holding @handle passes", mention_refusal(posted, "me", ["Rivera, Ana"], 0) is None)
    case("mention job: a comment with a reply refuses", mention_refusal(posted, "me", ["Rivera, Ana"], 1) is not None)
    case("mention job: a handle the text does not hold refuses", mention_refusal(posted, "me", ["Berg, Tomas"], 0) is not None)
    case("mention job: someone else's comment refuses", mention_refusal(dict(posted, user={"id": "x"}), "me", ["Rivera, Ana"], 0) is not None)

    # Resolve. A planted dump in REST's own shape: a root's parent_id is "", and only a root carries
    # resolved_at (on a live file, no reply did).
    me_u, mate = {"id": "me", "handle": "Owner"}, {"id": "t1", "handle": "Designer"}

    def cm(i, by, msg, at, parent="", resolved=None):
        return {"id": i, "parent_id": parent, "user": by, "message": msg, "created_at": at,
                "resolved_at": resolved, "client_meta": {"node_id": "1:2"}}
    dump = [cm("40", me_u, "Header: the light header comes with the nav redesign.", "2026-08-13T21:56:08Z"),
            cm("41", me_u, "This one's closed out.", "2026-08-28T19:48:23Z", parent="40"),
            cm("42", mate, "Q: should this band be 120 tall?", "2026-10-20T10:00:00Z"),
            cm("43", mate, "And the phone one too?", "2026-10-21T10:00:00Z", parent="42"),
            cm("44", mate, "Q: is this copy final?", "2026-10-20T11:00:00Z"),
            cm("45", me_u, "Yes, it shipped.", "2026-10-22T09:00:00Z", parent="44"),
            cm("46", me_u, "FIX (done in Figma): the crop.", "2026-10-20T12:00:00Z"),
            cm("47", mate, "Looks right, one question on the phone crop.", "2026-10-23T12:00:00Z", parent="46"),
            cm("48", me_u, "Q: which header is current?", "2026-09-01T00:00:00Z", resolved="2026-09-02T00:00:00Z"),
            cm("49", me_u, "", "2026-10-01T00:00:00Z")]
    rv = {c["id"]: c for c in dump}
    rr = lambda cid, me="me": resolve_refusal(rv.get(cid), dump, me)
    case("resolve: an id not in the file: refused", "not in this file" in (rr("99") or ""))
    case("resolve: a reply's id: refused, naming its thread's root", "give its root, 40" in (rr("41") or ""))
    case("resolve: a thread already resolved: refused", "already resolved" in (rr("48") or ""))
    case("resolve: a teammate's thread a teammate wrote last in: refused", "theirs to resolve" in (rr("42") or ""))
    case("resolve: a root with no text to find it by: refused", "no text" in (rr("49") or ""))
    case("resolve: no token owner id: refused", "/v1/me" in (rr("40", me=None) or ""))
    case("resolve: the owner's thread the owner wrote last in: passes", rr("40") is None)
    case("resolve: a teammate's thread the owner answered last (a moot one): passes", rr("44") is None)
    case("resolve: the owner's own thread a teammate wrote last in: passes", rr("46") is None)
    case("resolve: the job list holds one job per id, in order, each id once",
         resolve_jobs("K", ["40", "44", "40", 46]) == [{"key": "K", "cid": c, "resolve": True} for c in ("40", "44", "46")])
    regions = [{"text": "Comment\nOwner\n2 months ago\nHeader: the light header comes with the nav\n"
                        "redesign.\nThis one's closed out.", "resolve": 1, "unresolve": 0},
               {"text": "Comment\nOwner\nQ: is this copy final?\nYes, it shipped.", "resolve": 1, "unresolve": 0}]
    case("resolve page: the open thread holding the root's text is the one clicked, not another",
         thread_target(regions, rv["40"]["message"])[:2] == (0, "open")
         and thread_target(regions, rv["44"]["message"])[:2] == (1, "open"))
    case("resolve page: no open thread holds the text: nothing clicked", thread_target(regions[1:], rv["40"]["message"])[1] is None)
    case("resolve page: a header reading \"Mark as unresolved\" shows the thread resolved",
         thread_target([dict(regions[0], resolve=0, unresolve=1)], rv["40"]["message"])[1] == "resolved")
    case("resolve page: an open thread with no resolve control: nothing clicked",
         thread_target([dict(regions[0], resolve=0)], rv["40"]["message"])[1] is None)
    listed = {"id": "50", "message": "live on the branch:\n* Container: 1376 max-width + 32px side margins."}
    shown = [{"text": "Comment\nOwner\nlive on the branch: \nContainer: 1376 max-width + 32px side margins.",
              "resolve": 1, "unresolve": 0}]
    case("resolve page: a root whose list marker the page draws as a bullet is still found",
         thread_target(shown, listed["message"])[1] == "open")

    class _Pg:                      # a page that shows the open thread on its third look
        def __init__(self, regions):
            self.n, self.regions = 0, regions

        def evaluate(self, js, args):
            self.n += 1
            return self.regions if self.n >= 3 else []

        def wait_for_timeout(self, ms):
            pass
    case("resolve wait: returns once the open thread holding the root shows, whoever wrote it",
         wait_for_thread(_Pg(shown), listed, secs=5) is True)
    case("resolve wait: a page that never shows it gives up", wait_for_thread(_Pg([]), listed, secs=0.01) is False)

    # One browser per batch. A stub session counts launches the way signed_in_browser does; the steps
    # are stubbed, so this checks the batch's shape, not Figma's page.
    import signed_in_browser
    saved = {n: globals()[n] for n in ("edit_and_attach", "add_mentions", "wait_for_comment", "_stamp",
                                       "saved_view_refusal", "resolve_thread", "RESOLVE_API_WAIT", "wait_for_thread")}
    c1, c2, c3 = (dict(mine, id=i) for i in ("21", "22", "23"))
    c3["message"] = "Q: @Rivera, Ana which is current?"
    view = ([c1, c2, c3], {"id": "me", "email": "a@b.c"})
    opened, steps = [], []

    class FakePage:
        def screenshot(self, path): pass

    @contextlib.contextmanager
    def fake_session(url, email, wait):
        signed_in_browser.LAUNCHES += 1
        opened.append(url)
        yield type("S", (), {"page": FakePage(), "goto": lambda s, u: opened.append(u)})()
    try:
        globals().update(edit_and_attach=lambda pg, c, p, a=False: steps.append(("attach", c["id"])) or 0,
                         add_mentions=lambda pg, c, h: steps.append(("mention", c["id"])) or 0,
                         wait_for_comment=lambda pg, c, secs=0: True, wait_for_thread=lambda pg, c, secs=0: True,
                         _stamp=lambda *a: None,
                         saved_view_refusal=lambda s, u, e, c: 0)
        reads = lambda key, md=False: (view[0] if not md else [dict(c3, message="Q: Rivera, Ana which is current?")]
                                       + [c1, c2], view[1])
        jobs = [{"key": "K", "cid": "21", "pictures": [pic]}, {"key": "K", "cid": "22", "pictures": [pic]},
                {"key": "K", "cid": "23", "mentions": ["Rivera, Ana"]}]
        before = signed_in_browser.LAUNCHES
        code, codes = run_jobs(jobs, session=fake_session, read=reads)
        case("batch: 3 jobs run in ONE browser, each comment opened once, and the chip checks over REST",
             code == 0 and signed_in_browser.LAUNCHES - before == 1 and len(opened) == 3
             and steps == [("attach", "21"), ("attach", "22"), ("mention", "23")])
        before, steps[:] = signed_in_browser.LAUNCHES, []
        for j in jobs[:2]:
            run_jobs([j], session=fake_session, read=reads)
        case("batch: the old way, one call per comment, counts one launch each (the count catches it)",
             signed_in_browser.LAUNCHES - before == 2)
        before, steps[:] = signed_in_browser.LAUNCHES, []
        code, _ = run_jobs(jobs + [{"key": "K", "cid": "99", "pictures": [pic]}], session=fake_session, read=reads)
        case("batch: one refused job refuses them all, with no browser opened and no step run",
             code == 1 and signed_in_browser.LAUNCHES == before and steps == [])
        c4 = dict(mine, id="24", message="FIX (done in Figma): the crop, @Rivera, Ana.\n\nBefore and after:")
        both = lambda key, md=False: ([dict(c4, message=c4["message"].replace("@Rivera", "Rivera"))] if md else [c4], view[1])
        globals().update(edit_and_attach=lambda pg, c, p, a=False: 2)
        code, codes = run_jobs([{"key": "K", "cid": "24", "pictures": [pic]},
                                {"key": "K", "cid": "24", "mentions": ["Rivera, Ana"]}],
                               session=fake_session, read=both)
        case("batch: a picture that failed is not hidden by a mention that passed on the same comment",
             code == 2 and codes["24"] == 2)
        # Two paragraphs, a chip in the first: the Save must keep the second (a stale plain read can pass
        # while the markdown one is short).
        two = dict(mine, id="25", message="FIX: @Rivera, Ana please match.\n\nQ: which is current?")
        kept = lambda key, md=False: ([dict(two, message="FIX: Rivera, Ana please match.\n\nQ: which is current?")]
                                      if md else [two], view[1])
        short = lambda key, md=False: ([dict(two, message="FIX: Rivera, Ana please match.")] if md else [two], view[1])
        code, _ = run_jobs([{"key": "K", "cid": "25", "mentions": ["Rivera, Ana"]}], session=fake_session, read=kept)
        case("two paragraphs: a chip that kept the second paragraph passes", code == 0)
        stale = lambda key, md=False: short(key, md) if md else kept(key, md)   # plain still the old, whole copy
        code, _ = run_jobs([{"key": "K", "cid": "25", "mentions": ["Rivera, Ana"]}], session=fake_session, read=stale)
        case("two paragraphs: a markdown read missing the second paragraph fails, though plain still reads whole",
             code == 2)
        typed = lambda key, md=False: (view[0], view[1])     # markdown keeps the @: no chip
        code, codes = run_jobs(jobs[2:], session=fake_session, read=typed)
        case("batch: a mention the API still shows as typed text fails the check", code == 2 and codes["23"] == 2)

        # Resolve in the batch's one browser, the page step stubbed; REST read first as `dump`, then as
        # resolved for the ids given.
        page = {}
        globals().update(resolve_thread=lambda s, u, c, wait=None: steps.append(("resolve", c["id"])) or page.get(c["id"], 0),
                         RESOLVE_API_WAIT=0)

        def seq(*ids):
            calls = []

            def read(key, md=False):
                calls.append(key)
                done = set(ids) if len(calls) > 1 else set()
                return [dict(c, resolved_at="2026-10-26T03:00:00Z") if c["id"] in done else c for c in dump], view[1]
            return read

        def run(cids, read, sess=fake_session):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                got = run_jobs(resolve_jobs("K", cids), session=sess, read=read)
            return got + (out.getvalue(),)
        before, steps[:], opened[:] = signed_in_browser.LAUNCHES, [], []
        code, codes, out = run(["40", "44", "46"], seq("40", "44", "46"))
        case("resolve: 3 threads in ONE browser, each opened once, and REST shows all 3 resolved: exit 0",
             code == 0 and signed_in_browser.LAUNCHES - before == 1 and len(opened) == 3
             and steps == [("resolve", c) for c in ("40", "44", "46")]
             and all(f"RESOLVED {c}: REST resolved_at" in out for c in ("40", "44", "46")))
        globals()["wait_for_comment"] = lambda pg, c, secs=0: False    # the edit step's lookup finds nothing
        steps[:] = []
        code, codes, out = run(["44"], seq("44"))
        case("resolve: waits on the open thread, not the edit step's lookup (a teammate's thread)",
             steps == [("resolve", "44")])
        globals()["wait_for_comment"] = lambda pg, c, secs=0: True
        code, codes, out = run(["40", "44", "46"], seq("40", "44"))
        case("resolve: one thread REST still shows open fails, by id, and the others pass",
             code == 2 and codes == {"40": 0, "44": 0, "46": 2} and "NOT RESOLVED 46:" in out)
        code, codes, out = run(["40", "44"], seq("40", "44", "42"))
        case("resolve: another thread turning resolved during the run is named and fails the run",
             code == 2 and "ALSO RESOLVED" in out and "thread 42" in out)
        before, steps[:] = signed_in_browser.LAUNCHES, []
        code, codes, out = run(["40", "42"], seq("40"))
        case("resolve: one refused id refuses them all, with no browser opened and nothing clicked",
             code == 1 and signed_in_browser.LAUNCHES == before and steps == [] and "theirs to resolve" in out)
        page["44"] = 2
        code, codes, out = run(["40", "44"], seq("40", "44"))
        case("resolve: a page that never showed it resolved is still RESOLVED when REST shows resolved_at",
             code == 0 and "RESOLVED 44: REST resolved_at" in out and "the page did not show it" in out)

        @contextlib.contextmanager
        def no_sign_in(url, email, wait):
            raise signed_in_browser.NotReady("the sign-in was not finished")
            yield
        code, codes, out = run(["40"], seq("40"), sess=no_sign_in)
        case("resolve: a sign-in that never finished prints NOT RESOLVED with nothing clicked",
             code == 2 and "NOT RESOLVED 40: the browser did not get to it" in out)
    finally:
        globals().update(saved)
    shutil.rmtree(td, ignore_errors=True)
    print(f"{sum(results)} of {len(results)} planted cases behave as expected.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if args[0] == "selftest":
        sys.exit(selftest())
    if args[0] == "attach" and len(args) >= 4:
        again = "--again" in args
        rest = [a for a in args[1:] if a != "--again"]
        wait = 180
        if "--wait-user" in rest:
            i = rest.index("--wait-user")
            wait = int(rest[i + 1])
            del rest[i:i + 2]
        sys.exit(attach(rest[0], rest[1], rest[2:], again=again, wait_user=wait))
    if args[0] == "edit" and "--text-file" in args and len(args) >= 5:
        replace = "--replace" in args
        rest = [a for a in args[1:] if a != "--replace"]
        wait = 180
        if "--wait-user" in rest:
            i = rest.index("--wait-user")
            wait = int(rest[i + 1])
            del rest[i:i + 2]
        i = rest.index("--text-file")
        text = open(rest[i + 1], encoding="utf-8").read()
        del rest[i:i + 2]
        sys.exit(edit(rest[0], rest[1], text, rest[2:], wait_user=wait, replace=replace))
    if args[0] == "mention" and len(args) >= 3:
        sys.exit(mention(args[1], args[2:]))
    if args[0] == "resolve" and len(args) >= 3:
        rest, wait = list(args[1:]), 180
        if "--wait-user" in rest:
            i = rest.index("--wait-user")
            wait = int(rest[i + 1])
            del rest[i:i + 2]
        if len(rest) >= 2:
            sys.exit(resolve(rest[0], rest[1:], wait_user=wait))
    if args[0] == "probe" and len(args) >= 3:
        rest, who = list(args[1:]), None
        if "--picker" in rest:
            i = rest.index("--picker")
            who = rest[i + 1]
            del rest[i:i + 2]
        sys.exit(run_jobs([{"key": rest[0], "cid": c, "probe": True, "picker": who} for c in rest[1:]])[0])
    print(__doc__)
    sys.exit(2)
