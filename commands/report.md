---
description: Generate and open the current Devkit feature workflow HTML report.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow flow next --format json` in the current workspace to identify
the selected feature. If there is no active feature or more than one active
feature, ask the user which feature report to generate.

Run `devkit-flow flow report --feature <feature> --format json`, using the
selected feature name. Then open the returned absolute report path with the
platform's default HTML viewer (`open` on macOS, `xdg-open` on Linux, or
`start` on Windows). Report the generated path and the step/task statistics.
