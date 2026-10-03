---
description: Quickly fix a clear bug or failing command without starting the full feature workflow.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow status --format json` in the current workspace.
If the global `devkit-flow` command is unavailable, stop and ask the user to install it; do not fall back to a project-local executable.

Use the returned status only as context. Do not block quick fix execution just
because there is no active feature or because an active feature is waiting at a
gate. If multiple active features exist, pass that status to the quick-fix
agent so it can avoid touching unrelated work.

If the user arguments are empty, ask for the failing command, error message, or
short bug description. Otherwise ask
`devkit:quick-fix-agent` to handle this quick fix request:

`$ARGUMENTS`

Pass the status JSON and current workspace path to the agent. The agent should
make the smallest safe change, run targeted verification, and avoid creating a
full `spec/00x-{feature}/` workflow. If it returns `escalation_needed`, show the
reason and ask the user whether to upgrade to the standard
`devkit:feature-manager` workflow in the current main thread.
