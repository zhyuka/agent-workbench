# Settings

The tools read settings from two JSON files and merge them key by key, with the project's file winning:

- `~/.claude/agent-workbench.json` holds yours: folders on your machine, plus the project to use when you work outside any project folder.
- `<project>/.claude/agent-workbench.json` holds the project's and is committed with it. The tools use the nearest one, found by walking up from the folder you run in.

`$AGENT_WORKBENCH_CONFIG` names a single file to use in place of the project's. `python tools/kit_config.py` shows where it looked and what it found.

Every key is optional, and a tool reads only the keys it uses. A tool that needs a key neither file has stops with exit 2 and names the key. There are no built-in defaults for a project, so a missing setting never reads from someone else's project or files into it.

Tokens never go in these files. `FIGMA_TOKEN`, `ADO_PAT` and the like come from the environment, or from the OS keychain under the service `agent-workbench` (see `kit_config.py --help`).

## Keys, by the tool that reads them

| Key | Read by | What it is |
|---|---|---|
| `ado.org`, `ado.project` | ado, ado_rest, ado_write | Azure DevOps organization URL and project |
| `ado.title_prefix` | ado | work-item titles read `<prefix> // {Page - Section} // {what}`; unset, any title passes |
| `ado.search_stop` | ado | extra words the duplicate search ignores |
| `ado.frozen_branch` | ado_rest audit | a branch nobody may propose merging to; unset, that check prints "did not run" |
| `ado.backlog_iteration` | ado_rest audit | the dateless "someday" iteration |
| `ado.phase_suffixes`, `ado.decision_tag` | ado_rest | title endings that mark a planned pair; the tag that marks an open question (default `decision`) |
| `figma.files`, `figma.default_file` | figma tools, ado | `{"<file key>": {"kind": "design" or "board", "name": "<label>"}}`; the file a bare node id refers to |
| `figma.mirror` | figma_client | `{"<file key>": ["<page name>", ...]}` to keep a local REST mirror; off until set |
| `figma.drafts` | drafts_store, figma_post | path of a drafts file of Figma comments (or `$FIGMA_DRAFTS`) |
| `recall.notes`, `recall.transcripts`, `recall.repos` | recall | folders searched as your notes and meeting transcripts; git clones to search (default: the repo you run in). `seen_before.notes` / `.transcripts` / `.site` are read as fallbacks |
| `recall.index_dir` | recall, recall_index | folder of the logs index (default `~/.cache/agent-workbench/recall_logs`); keep it outside any folder a cloud drive or mirror job copies |
| `logs.root`, `logs.backups` | cc_logs and the tools built on it | where to walk for Claude Code logs (default `~`); backup folders whose copies count as backups |
| `session_close.drafts` | session_close | file names or path endings that count as drafts files; listed, never blocking, and a promise naming one has a home |
| `skill_lint.topics`, `skill_lint.startup_max` | skill_lint | facts with one owning file; the word budget for what loads at startup |
| `skill_use.rules`, `skill_use.aliases` | skill_use | which skill must load before which risky step |
| `tool_time.tools` | tool_time | extra folders of tools to time |
| `agent_gate.suites` | agent_gate_stale | folder of eval suites |
| `metrics.*` (`log_roots`, `project_folders`, `skip_folders`, `repo`, `branch`, `ticket_re`, `check_dir`, `tsv`) | weekly_metrics | inputs of the weekly process measures |
| `doclint.*` (`scope`, `names`, `names_file`, `people_glob`, `people_prefix`, `people_file`, `exempt_names`, `exempt_parts`) | doclint | which docs it reads and the people's names it refuses |
| `ledger.*` (`dir`, `rulings`, `rulings_also`, `ado_cache`, `skip_cwd`, `skip_first`, ...) | ledger, digest, recall (`ledger.dir`) | where the request ledger lives and what else it reads |
| `transcripts.*` (`dir`, `aliases`, `aliases_file`, `ambiguous`) | transcript_check | the transcripts folder and the spellings of your product names |
| `workspace.*` (`root`, `not_ours`, `docs`, `map`, `skip`, `ado_docs`, ...) | workspace_audit, archive, upkeep, desktop_mirror | your working folder and what in it the audits judge |
| `backup.folder` | backup_cc_logs, desktop_mirror, upkeep | a folder a cloud drive syncs, or an external disk |
| `mirror.*` (`source`, `name`, `exclude`, `worktrees`, `sentinels`, `extra_repos`, `log`) | desktop_mirror, scheduled_audits | the one-way copy of your working folder |
| `upkeep.*` (`repos`, `integration`, `worktrees`, `dist_parents`, `pyc_dirs`, `setup_repos`) | upkeep | what the daily cleanup may prune |
| `scheduled_audits.*` (`extra`, `agent_prefix`, `exit_matters`, `log`, `watchdog`) | scheduled_audits | extra audits to run, and which scheduled jobs to watch |
| `signed_in_browser.*` (`channel`, `no_profile_under`, `temp_root`, `reap`) | signed_in_browser, figma_attach | `msedge` or `chrome`, and where a signed-in profile may not live |
| `ask_rules.settings` | ask_rules | settings files whose permissions.ask rules must prompt on each of the plugin's outward writes and on no read |
| `net_lines.facts` | net_lines | files, relative to the repo root, left out of the net-lines count: records that grow as facts arrive |

Each tool's `--help` describes its keys in more detail.
