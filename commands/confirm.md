---
description: Confirm the pending human gate of the current Devkit feature workflow.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow flow next` in the current workspace.
If the global `devkit-flow` command is unavailable, stop and ask the user to install it; do not fall back to a project-local executable.

If the reported breakpoint step is not `waiting-confirmation`, tell the user
there is no pending human gate and stop.

Otherwise show the gate summary (step id, gate note, and what confirming
unlocks) to the user and ask for explicit confirmation. This slash command
itself is NOT the user's authorization — only an explicit user reply in chat
counts as confirmation.

Only after the user explicitly confirms, run `flow confirm` (same path
resolution as above) and show the result. Then load and follow the
`devkit:feature-manager` Skill in the current main thread to continue the workflow
from the newly unblocked step for the selected `feature_dir`, honoring any
user arguments:

`$ARGUMENTS`
