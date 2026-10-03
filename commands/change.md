---
description: Route a Devkit feature change request through the existing change workflow.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow status --format json` in the current workspace.
If the global `devkit-flow` command is unavailable, stop and ask the user to install it; do not fall back to a project-local executable.

Use the returned feature status as the workflow anchor. If there is no selected
active feature, ask the user which feature the change belongs to.

Then load and follow the `devkit:feature-manager` Skill in the current main thread to handle this
change request for the selected `feature_dir`:

`$ARGUMENTS`

The manager must route through the existing change-impact workflow, preserve
file ownership boundaries, and update `CHANGELOG.md` / `STATUS.md` before any
next step proceeds.
