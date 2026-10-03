# Codex Agent Overrides

Keyed by agent filename stem. `{agent}.append.md` is appended to the Codex
worker body. Shared `packages/agents/*.md` stay unchanged so Claude/Grok/Kimi/Pi
do not inherit Codex single-pass review rules.

`feature-dev-manager.append.md` is **not** a worker override. The Codex plugin
builder appends it only to the Codex copy of
`packages/skills/feature-dev-manager/SKILL.md`. Shared SKILL source and
Grok/Pi/Claude/Kimi skill projections must not contain `review-rounds check`
or `review-rounds tick`.
