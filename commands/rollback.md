---
description: Roll back the current Devkit feature workflow to a target step and rerun from there.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

The user arguments must contain the target step id to roll back to:

`$ARGUMENTS`

If no step id is given, ask the user which step to roll back to.

Run `devkit-flow flow next` in the current workspace.
If the global `devkit-flow` command is unavailable, stop and ask the user to install it; do not fall back to a project-local executable.

Show the current breakpoint and warn the user that rolling back will reset the
target step and all later steps to `pending`. Ask for explicit confirmation
before proceeding.

Only after the user explicitly confirms, run
`flow rollback --step <id> --reason <short reason>` (same path resolution as
above, using the confirmed step id; ask the user for the reason if it is not
obvious from the arguments).

Then load and follow the `devkit:feature-manager` Skill in the current main thread to rerun the
workflow starting from the rolled-back step for the selected `feature_dir`.
The manager must keep the same file ownership rules and update `STATUS.md` /
`task.md` as steps proceed.
