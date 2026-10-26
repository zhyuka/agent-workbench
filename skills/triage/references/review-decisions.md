# triage: what a decision on a draft does to the files (step 1)

The drafts file of Figma comments and the file of messages waiting to be sent hold open items only,
and each records an item's status in the item itself. The posting rules are the `figma-writes`
skill's; this is what each decision changes.

- When the user says to post the drafts, make each one an item in one `items.json` (a new draft
  `"kind": "new"`, a reply `"reply"`, an edit of the user's own comment `"edit"`) and run
  `python "${CLAUDE_PLUGIN_ROOT}/tools/figma_post.py" batch <items.json> --check`, then the same
  without `--check`. Posting deletes the draft's block once Figma has the comment (`drafts_store.py`);
  when the post prints that none was removed, the text was reworded, so delete the block it names
  yourself.
- A draft the user cuts, *Drop*: remove the block and add a row to the `## Cut: do not re-raise`
  table in the drafts file: the item, its node id in backticks, the note as Why. `figma_post.py new`
  refuses that node until the row is deleted. *Rewrite*: apply the new text.
- A message, walked in chat one at a time as in steps 3 to 5: *Sent* or *Don't send* → delete its
  block. The record is the channel, and no Resolved list is kept.
