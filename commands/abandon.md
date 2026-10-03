---
description: Archive (abandon) the current Devkit feature so it leaves active detection.
disable-model-invocation: true
allowed-tools: Bash(devkit-flow *)
---

Run `devkit-flow flow next` in the current workspace.
If the global `devkit-flow` command is unavailable, stop and ask the user to install it; do not fall back to a project-local executable.

Show the current feature and breakpoint to the user, and ask for explicit
confirmation that the feature should be archived (abandoned). This slash
command itself is NOT the user's authorization — only an explicit user reply
in chat counts as confirmation.

Only after the user explicitly confirms, run
`flow archive --reason <short reason>` (same path resolution as above; use
the user arguments as the reason, or ask for one if empty):

`$ARGUMENTS`

After archiving succeeds, tell the user the feature is archived and will no
longer appear in active feature detection, and that its files remain on disk
under its `spec/00x-{feature}/` directory. This is only **逻辑归档** and will not
consolidate the knowledge base or clean any files（不会清理 `spec/`）. If the user
wants historical content整理或可选清理, tell them to continue by direct dialogue
or `/devkit:archive`; that separate transaction has its own knowledge and cleanup
approvals.
