# Agent Workbench

Agent Workbench is the setup I use with Claude Code every day, generalized so anyone can install it: 9 skills, 5 agents and 41 Python tools. Most of it exists because an agent that grades its own work tends to pass it. So there's a reviewer that tries to beat a plan before you see it, a grader that doesn't know who made what, a search over everything you've already decided, and gates that refuse a duplicate ticket or a comment posted without reading the thread first.

It is a Claude Code plugin. Install it from a session:

```
/plugin marketplace add zhyuka/agent-workbench
/plugin install agent-workbench@agent-workbench
```

The tools need Python 3.9 or later. A few drive a browser and also need Playwright (`pip install playwright`, then `playwright install chromium`). Skills call the tools as `python "${CLAUDE_PLUGIN_ROOT}/tools/<name>.py"`. You can also run any tool yourself from a clone. Every tool answers `--help`, and most have a `--selftest` that runs offline.

## Skills

| Skill | Use it for |
|---|---|
| `recall` | "Have we seen this before?" One search over your Claude Code session logs, git, notes and memory, plus Figma comments and Azure DevOps when you've set a token. Run it before you ask the user something they may have answered already |
| `open-question` | File a question only the user can answer into the project's ledger (`.claude/open-questions.md`) instead of guessing |
| `triage` | Work the open questions down one at a time and record each decision |
| `reconcile` | Bring a workspace back to a clean baseline: catch up on what changed, verify, report at most 12 findings, then do only what's approved |
| `humanize` | Cut AI tells from prose someone else will read |
| `ambition` | Rewrite a prompt into a deeper version of the task, not a bigger one |
| `qa-audit` | A measured QA pass on a built web page before it ships. Report-only |
| `figma-writes` | What goes wrong when writing to a Figma file or FigJam board, before any write |
| `ado` | The Azure DevOps work-item lifecycle: search before creating, write it up properly, resolve when the code ships, reconcile the board |

## Agents

| Agent | What it does |
|---|---|
| `challenger` | Tries to beat a recommendation before the user sees it. Give it the problem, the hard rules and the recommendation, not your reasoning |
| `blind-grader` | Grades shuffled, anonymised items against a rubric. Mix in a known-good and a known-bad control |
| `cold-reader` | Reads a draft as one named first-time reader and says where they got lost |
| `page-qa` | Runs the `qa-audit` skill on one built page, report-only |
| `section-reviewer` | Reviews one built section against its design frame, the way the designer would |

## Tools

All of them are in [`tools/`](tools). By area:

- **Claude Code logs and memory.** `cc_logs` finds every session log on the machine, backups included. `log_extract` pulls out everything you typed, with a real denominator. `session_close` says whether a session can be archived: what blocks it, and what it only lists. `ledger` and `digest` track every request you made and its state. Also `tool_time`, `skill_use`, `skill_lint`, `recall`, `recall_index`, `seals`, `pushback_score`, `weekly_metrics`, `agent_gate_stale` and `statusline_model`.
- **Checks that report N of M.** `class_check` counts how many files in a class got a shared change. `verify_ledger` answers "was it verified?" from a file. Also `workspace_audit`, and `doclint`, a docs linter and pre-commit gate.
- **Figma.** `figma_client` is one REST client with a shared budget and a pause on 429. `figma_comments` reads a thread before you write to it. `figma_variants` lists a component set's variant matrix, its properties and its variable bindings per mode. Also `figma_post`, `figma_attach`, `figma_family`, `figma_evidence`, `figma_auth` and `drafts_store`.
- **Page checks.** `backdrop_group_check` finds frosted elements an ancestor's opacity breaks, and dead tap spots in cards. `sliver_sweep` finds rounded boxes whose fill leaks as a ring at the corners. `structure_check` checks that every instance of a class in the built HTML has the same shape. `shot_gpu` takes a GPU-on full-page screenshot.
- **Azure DevOps.** `ado` refuses a create that hasn't searched for duplicates. `ado_rest` and `ado_write` use REST with a PAT, so nothing waits on an `az` prompt.
- **Upkeep.** `scheduled_audits` runs the read-only audits daily. Also `upkeep`, `backup_cc_logs`, `desktop_mirror`, `archive`, `pw_temp`, `signed_in_browser`, `html_clip` and `transcript_check`.

The tools that grade something share one exit-code rule: 0 clean, 1 findings, 2 could not run. A tool that couldn't run never reports clean.

### Calling a tool from outside the plugin

While the plugin is enabled, Claude Code puts its [`bin/`](bin) folder on the Bash PATH. So a skill or script in your own project can run any tool without knowing where the plugin is installed:

    agent-workbench class_check 'SiteFooter' --glob '**/*.astro' --expect all
    agent-workbench --list

`agent-workbench <tool> [args...]` runs `tools/<tool>.py` with `python3`, or `python` if there's no `python3`, and exits with the tool's own code. An unknown tool exits 2 with a one-line message. Inside the plugin's own skills, `python "${CLAUDE_PLUGIN_ROOT}/tools/<tool>.py"` still works.

## Settings

Project values, such as an Azure DevOps project or a Figma file key, go in `.claude/agent-workbench.json`, never in a tool. Tokens come from environment variables or the OS keychain. Every key is listed in [docs/config.md](docs/config.md). A tool that needs a key you haven't set stops and names it.

## Templates

- [`templates/CLAUDE.md`](templates/CLAUDE.md) holds working agreements to copy into `~/.claude/CLAUDE.md`. Edit them to fit how you work.
- [`templates/writing-instructions.md`](templates/writing-instructions.md) is how to write a skill, an agent or a rule so a model follows it.
- [`templates/launchd/`](templates/launchd) has a macOS example that runs the daily audits at login and at 08:00.

## Maintenance

This repo is a generalized copy of a private setup that keeps changing, and I port each change here. [`scripts/leak_scan.py`](scripts/leak_scan.py) runs before every commit. It refuses token shapes and a private list of names, hosts and file ids, which is kept outside the repo.

Agent Workbench has one maintainer, Derek Zhou ([@zhyuka](https://github.com/zhyuka)). Issues are welcome. MIT licensed.
