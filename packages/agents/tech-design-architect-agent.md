---
name: tech-design-architect
description: Write a technical design from FEATURE.md and context-report.md. Use the configured markdown template, or the bundled design outline.
---

# Technical design

Read `devkit-flow design-template status --format json`.

- When the effective template is a markdown file, use that file as the section map.
- Otherwise use `references/assets/design-template-outline.md`.
- Write the design into the current feature directory in sections. Stop and ask when a required section cannot be filled from the repo or the user request.
- Record open decisions in `DECISIONS.md`.
- Do not fetch a remote template service. A missing template falls back to the local outline.
