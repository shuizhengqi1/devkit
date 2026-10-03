---
description: Continue exactly one Devkit feature workflow step from the current STATUS.md state.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow flow next --format json` in the current
workspace. If the global `devkit-flow` command is unavailable, stop and ask the user to install it; do not fall back to a project-local executable. Optionally also run
`status --format json` (same path resolution) as supplementary ONES/config
context.

Use the returned `flow next` breakpoint state as the only workflow anchor.
If there is no active feature, or multiple active features, stop and ask the
user for the feature to use. If the current step is `blocked` or `waiting-confirmation`,
show that gate or blocker and stop.

Otherwise load and follow the `devkit:feature-manager` Skill in the current main thread to continue
exactly one workflow step for the selected `feature_dir`. Pass the
`flow next` JSON and any user arguments:

`$ARGUMENTS`

The manager Skill may call worker agents, but it must keep the same file
ownership rules and update `STATUS.md` / `task.md` before moving to another
step.
