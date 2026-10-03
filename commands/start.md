---
description: Start a Devkit feature while keeping the existing manager-agent workflow.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow status --format json` in the current workspace.
If the global `devkit-flow` command is unavailable, stop and ask the user to install it; do not fall back to a project-local executable.

If an active feature already exists, show it and ask whether the new request
should join that feature or create a new one. If multiple active features exist,
ask the user to choose. If no active feature exists, load and follow the
`devkit:feature-manager` Skill in the current main thread to start a new feature
from the user request below.

`$ARGUMENTS`

The manager Skill must use the existing feature startup workflow and write
`STATUS.md` / `FEATURE.md` anchors before proceeding.
