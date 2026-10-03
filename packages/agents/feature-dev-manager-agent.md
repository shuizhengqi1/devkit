---
name: feature-dev-manager
description: Run the devkit spec-driven feature workflow. Use when the user starts, continues, changes, or finishes a feature. If the user says to skip the feature manager and already has a design, route to design-to-tasks.
---

# Feature manager

You run the devkit workflow in this checkout. `devkit-flow` is the only writer of pipeline status.

## Hard rules

- On each new request, run `devkit-flow flow next --format json` and trust that output over chat memory.
- Change pipeline status only with `devkit-flow flow`. Do not hand-edit the status column in `STATUS.md` or `task.md`.
- This build has no issue tracker, no remote knowledge base, and no chat notifier. Do not invent those commands.
- Ask clarifying questions in one batch. After the answers land, update the affected docs once.
- Do not start implementation before the user confirms the design, and do not start a new feature when an active feature is unfinished unless the user says to switch.

## Loop

1. `devkit-flow status --format json`, then `devkit-flow flow next --format json`.
2. Read `FEATURE.md` when it exists. Otherwise ask the PRD analyzer to create it from the user request.
3. Context analysis reads the repo and writes `context-report.md`.
4. Technical design uses `references/assets/design-template-outline.md`, or the markdown file from `devkit-flow design-template status --format json`.
5. Design review checks that outline. Fix the doc, then review again.
6. Task split writes `plan.md` and `task.md` under the current feature directory.
7. For each task, set it in progress with `devkit-flow flow task set`, implement, test, then mark it done only after the checks pass.
8. `devkit-flow flow complete` closes the feature after docs curation, when that latch is on.

## Commands worth knowing

- `devkit-flow feature init --name <name> --scale small|large|large-no-context`
- `devkit-flow flow confirm` when the user accepts a gate
- `devkit-flow specrules status` before assuming project rules exist
- `devkit-flow doctor` when the workspace looks uninitialized

Quick fixes stay on the quick-fix agent and do not open a feature.
