---
description: Diagnose Devkit feature workflow state consistency without modifying any files.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow flow verify` and
`devkit-flow status --format json` in the current workspace.
If the global `devkit-flow` command is unavailable, stop and ask the user to
install it; do not fall back to a project-local executable.

Present every issue reported by `flow verify` one by one (status
contradictions, progress-summary drift, branch mismatch, ONES retry backlog,
missing artifacts), enriched with the status JSON as ONES/config context.
For each issue, give a concrete fix suggestion — e.g. which `flow set`,
`flow rollback`, `flow confirm`, or manual file edit would resolve it.

This command is read-only: do not modify any file, do not run any
state-changing `devkit-flow` subcommand, and do not auto-apply the suggested
fixes. If no issues are found, say so and show the progress summary.
