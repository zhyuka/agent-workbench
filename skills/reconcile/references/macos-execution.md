# reconcile: deleting and moving on macOS

- Nothing guards a delete, and `rm` has no undo. Print the resolved path, look at it, then delete.
- `mv` within one volume is a rename: atomic, no copy. Across volumes it copies then deletes, so
  check the destination is complete and the source is gone. In a folder a cloud drive syncs, a file
  may be a cloud-only placeholder, and reading it forces a download.
- The default APFS volume is case-insensitive: `mv` and `git mv` make a case-only rename in one step;
  `mkdir -p` of a case variant silently reuses the existing folder.
- A file that will not delete is usually flagged locked: `ls -lO` shows `uchg`; clear it with
  `chflags -R nouchg <path>`. Git's read-only object files delete fine; macOS has no 260-character
  path limit.
- macOS lets you delete a running server's files, and the process keeps going. Stop it first:
  `pgrep -fl <folder>` or `lsof +D <folder>` finds it.
- Before restoring a folder that vanished, check it was not disposable scrap.
