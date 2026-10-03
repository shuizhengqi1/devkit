---
description: Inspect resumable historical spec archive transactions without changing them.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Load the active `spec-archive-manager` Skill, then run the read-only command
`devkit-flow spec archive status --format json`. If `$ARGUMENTS` identifies an
archive id, pass it as `--archive-id`:

`$ARGUMENTS`

Summarize each transaction's feature, state, approvals, source verification,
next safe action, cleanup scope, and restore commit. This command is 只读: do
not approve, apply, prune, finalize, or restore until the user explicitly asks
to resume that action in the conversation.
