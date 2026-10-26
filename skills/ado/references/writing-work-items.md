# 4. Writing work-item content - applies at every phase

The detail behind `SKILL.md` §4. These rules govern any body or comment you write, whether creating,
resolving or replying. Read them before your first write, not after.

## Every design reference is a clickable URL

A bare node id is not a reference. Writing `123:456`, `node 123:456` or `Figma comment 987654321`
cites nothing a person can open: the reader has to already know which file it means, and nobody
does a month later. The URL is mechanical, so there is no excuse for the short form:

```
https://www.figma.com/design/{fileKey}/{FileName}?node-id={node-with-HYPHENS}#{commentId}   <- a discussion
https://www.figma.com/design/{fileKey}/{FileName}?node-id={node-with-HYPHENS}               <- a design only
```

- **A discussion link carries the thread's ROOT `#{commentId}` and the node the THREAD is pinned
  to**, not the frame the new text is about. When an open thread exists on the subject, link the
  thread. A comment search tool (such as `figma_comments.py search <subject>`) finds it on any node
  and prints the link.
- **The node id uses HYPHENS in a URL** (`123-456`) though Figma and its REST API report it with a
  colon (`123:456`). Convert it; a colon in the query string resolves to the file root.
  `ado_rest.py audit` flags the colon form (`figma-colon-nodeid`).
- Keep the node id visible in the link TEXT as well, so the reference survives being pasted
  somewhere the link does not render.
- Same rule for ADO cross-references: `_workitems/edit/{id}/`, not a naked `#1234`, when the item is
  the thing the reader needs to open.
- **A well-formed URL is not a working one. Resolve every node id you write against the API**:
  `GET api.figma.com/v1/files/{key}/nodes?ids=...` with your Figma token (a few dozen ids a call),
  and treat a missing `document` as dead. Design files churn, and a dead link silently opens the file
  root and looks like it worked. **Pick the file per citation, not per field**: when one body cites
  both a design file and a board, decide each node's file on its own.

This rule belongs in every write path, not in one build skill. Scoped to one path, it goes to zero
the moment any other path files a ticket.

## Body versus comment

The **body is the living spec**: when requirements change on a not-yet-started item, **rewrite
Description/AC to the clean current truth** (the revision history is the diff). Comments are for
watchers and for the resolution record. Do not pile a new comment per change; consolidate or delete
your own redundant comments.

**An error nobody has read yet is fixed in place, as if it had always been right.** No
"corrected", "this first said" or "I was wrong" line, in a body, a comment or a design comment. A
value that later work replaced is a real change and keeps a "replaced by #N" note. Once someone has
replied to the wrong text or acted on it, do not edit it; ask the user how to correct it.

## Comments are short

- **Target under ~700 characters**: one measure, and one you can check with `len()` before posting.
  Outcome first, then at most 4-6 short lines or a tight numbered list mirroring the reviewer's own
  numbering. `ado_write.py comment` warns over that length; `ado_rest.py audit` reports
  `comment-too-long`.
- **The detail belongs in the BODY, not the comment.** If you are tempted to explain the mechanism,
  the alternatives and the measurements in a comment, that material is the living spec: put it in
  Description/ReproSteps/AC and let the comment point at it.
- Keep the numbers that carry the claim ("0px spread", "38px on 3 heroes x 2 viewports") and cut the
  narration around them.
- No hint of an autonomous run (the `humanize` skill, "Leave out how the text was made").

## One comment per thread - edit it, do not stack a second one

The reviewer should open the item and find **one** message, not a running log. Updating, correcting
or extending what you already said means **patching the existing comment** (`ado_write.py editcmt`).

**The rule is for un-read threads only.** Once a teammate has replied, your earlier comments are
**the context they replied to**. Consolidating them rewrites history someone has already acted on,
and their reply is left answering something no longer on screen. So:

- **Nobody has replied yet** -> patch your existing comment rather than stacking a second one.
- **Someone has replied** -> leave every earlier comment alone, including your own, and add your
  reply after theirs. A question, their answer and your close-out is a THREAD, not a pile.
- A count of your own comments is therefore not the test. **Read the thread order**: consecutive
  un-answered comments of yours are a pile; comments separated by someone else's are a conversation.

**This is a mechanism, not a rule to remember: `ado_write.py comment` refuses** when the newest live
comment on the item is already yours. Written rules lost under load, so the precondition is enforced
the way `ado.py` enforces the dup-search on `new`.

- **The test** is the newest live comment: yours -> REFUSE; a teammate's -> ALLOW; none -> ALLOW.
- **It matches on the author GUID, never the name.** The signed-in user's provider display name can
  be an email while a comment's `createdBy.displayName` is a formatted name; comparing names never
  matches, and the gate silently always allows.
- **`--stack "<reason>"` is the escape hatch**, and the reason is required and printed. A gate with
  no override gets routed around instead of used.

**Handle the lost update by MERGING, not by refusing to patch.** ADO comments have no optimistic
concurrency, so whoever saves last wins silently: a person editing the same comment in the browser
from an older copy wipes your patch, and from their side the update simply never arrived. The fix
is **not** a second comment:

1. **Re-read the comment immediately before patching**, not from a copy you fetched earlier.
2. **Diff it against what you last wrote.** If someone added or changed anything, KEEP their words
   and weave your update around them. An inline question they added gets **answered in place**, not
   replaced.
3. Patch, then confirm both your change and their edits survived (`editcmt` reads back your text;
   check theirs by eye).
4. If you already stacked a redundant comment, **delete it** (`ado_write.py delcmt`) once its content
   is merged. Your own just-posted comment is yours to remove.

**Never rewrite someone ELSE's comment** to change what it says; reply after it instead. Comments
posted from the user's own account are the user's, and editing them on the user's behalf is
expected.

Corollary for the read path: comments can come back **unsorted**, so the last in the list is not
necessarily the newest. Sort by `createdDate` or `id` before picking one, or you will patch a comment
from last week.

## @mentions need an identity anchor

Plain `@Name`, and especially `@&lt;Lastname, Firstname&gt;`, renders as literal text and notifies
nobody. The real markup is:

```html
<a href="#" data-vss-mention="version:2.0,{GUID}">@Display Name</a>
```

Get the GUID from any existing comment's `createdBy.id`, or a work item's `System.AssignedTo` id; no
extra API is needed. Verify after posting that the stored text contains `data-vss-mention`
(`editcmt`/`comment` compare mentions in the read-back). Only mention someone when the comment
actually needs their attention; a status update addressed to nobody should have no mention at all.

## Read the attachments first

If a work item carries screenshots or attached files, read them before implementing. They usually
hold the actual spec; a text description of a screenshot is not the screenshot. `ado_rest.py show
<id>` downloads pasted images and attached files and prints their local paths.
