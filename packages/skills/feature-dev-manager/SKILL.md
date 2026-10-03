---
name: feature-dev-manager
description: Use when starting, continuing, changing, or completing a spec-backed feature with devkit-flow.
---

# Feature manager

Follow `packages/agents/feature-dev-manager-agent.md`.

Before any feature command, run `devkit-flow flow next --format json` when the CLI is available. Pipeline status is written only by `devkit-flow flow`.

Design input is a local markdown outline. Spec rules come from the git URL configured for this project, and stay off when that URL is empty.
