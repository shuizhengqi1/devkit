---
description: Consolidate knowledge and archive selected historical specs with optional, separately approved cleanup.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Load the active `spec-archive-manager` Skill and follow its transaction contract.
This command is only a convenient entrypoint; 日常使用也可以在当前 agent 的对话框里直接对话，例如“整理并归档历史 spec”。

Start with `devkit-flow spec archive scan --format json`. Interpret `$ARGUMENTS`
as a candidate selection or archive option and show any ambiguity in one batch:

`$ARGUMENTS`

The command invocation and its arguments 不等于删除授权. Knowledge-base
approval follows the effective configuration, while cleanup and git-only
cleanup always keep their independent explicit Gate. Never delete, move, or
rewrite spec files directly; use only the `devkit-flow spec archive` transaction
commands defined by the Skill.

If no candidate is selected, present eligible and review-required candidates
with the reason for each classification, then wait for one selection. Continue
the selected transaction through plan, staged summary and docs-curator
convergence. Stop only at a required user Gate or a reported blocker.
