---
description: Show the current Devkit feature workflow status from STATUS.md.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow flow next` and
`devkit-flow flow verify` in the current workspace.
If the global `devkit-flow` command is unavailable, stop and ask the user to
install it; do not fall back to a project-local executable.

Report the result as the source of truth for the current feature, current
breakpoint step, dependency satisfaction, gate/blocker state, progress, and
any consistency issues found by `flow verify`. Do not infer workflow progress from
chat history. If multiple active features are reported, ask the user to choose
one before continuing.
