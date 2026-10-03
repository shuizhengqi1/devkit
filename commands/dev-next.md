---
description: Execute the next Devkit development task through the existing manager/worker agents.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow status --format json` in the current workspace.
If the global `devkit-flow` command is unavailable, stop and ask the user to install it; do not fall back to a project-local executable.

Use the returned feature status as the workflow anchor. If `S5-H` has not been
confirmed, stop at the development execution gate unless the user arguments
explicitly confirm development execution:

`$ARGUMENTS`

If development execution is allowed, load and follow the
`devkit:feature-manager` Skill in the current main thread to execute the next
eligible task from `task.md` for the selected `feature_dir`. The manager keeps
ownership of `task.md` updates and routes implementation, unit test/TDD, docs,
and architecture review through the existing agents.
