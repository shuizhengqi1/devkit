---
description: Run the Devkit feature completion flow (task 8), generate its report, and mark it completed.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow flow verify` and
`devkit-flow flow next` in the current workspace.
If the global `devkit-flow` command is unavailable, stop and ask the user to
install it; do not fall back to a project-local executable.

If `flow verify` reports open issues or `flow next` shows unfinished steps,
list them to the user and stop to ask whether to proceed anyway. Do not start
the completion flow without explicit user confirmation.

After the user explicitly confirms, load and follow the
`devkit:feature-manager` Skill in the current main thread to run task 8
(completion) flow for the selected `feature_dir`: final review, then
`docs-curator` (`operation: feature_done`) **before** `flow complete`, then
`readme-architecture-curator` and `feature-retro`. Before invoking
`docs-curator`, read `devkit-flow docs-curator status --format json` (or
`docs_curator` on the top-level `status --format json`). If `enabled=false`,
do not call the skill; record `skipped-disabled` plus `source` and
不得假装已同步. If enabled, 门闩开启时不得跳过. Do not call
`docs-curator` from `dev-task-executor-agent`. The
`devkit-flow flow complete` call generates and opens the HTML report by default
and must run **after** docs-curator.
Honoring any user arguments:

`$ARGUMENTS`

At the end of the completion flow, the manager runs `flow complete` (same path
resolution as above) **after** docs-curator to mark the feature header as
completed and return the report path/statistics. If it fails validation, show
the error and stop instead of forcing it. If automatic opening fails, keep
completion successful and show the returned report path for manual opening.
