---
description: Route a clarification through the Devkit feature workflow's task-3 clarification flow.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow flow next` and `devkit-flow flow clarify status --format json`
in the current workspace.
If the global `devkit-flow` command is unavailable, stop and ask the user to install it; do not fall back to a project-local executable.

Use the returned breakpoint state as the workflow anchor. If there is no
active feature, or multiple active features, stop and ask the user for the
feature to clarify.

Then load and follow the `devkit:feature-manager` Skill in the current main thread to handle this
clarification through its task 3 batch flow for the selected
`feature_dir`:

`$ARGUMENTS`

If there is a collecting batch, treat `$ARGUMENTS` as answers or additions to
that batch. If no batch exists, collect all currently discoverable questions
before opening one. 回答收集期间只允许使用 `flow clarify add/answer/status`；
不得立即调用 `change-impact-analyzer-agent`、修改正文、回退步骤或重跑审查。
Only after every required item is answered may the manager apply the batch,
record all answers in `question-clarification.md`, run one impact-analysis and
follow-up cycle, and close it. If the arguments are empty, show the current
batch or ask for the clarification content first.
