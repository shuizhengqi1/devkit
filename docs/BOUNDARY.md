# Public export boundary

Name: devkit
License: MIT
Public version: 0.1.0
Hosts in this tree: Claude Code, Codex, Grok Build
History: this directory is a new tree. It does not carry the private upstream git history.

## In this tree

- `src/devkit_flow/` local workflow CLI, with telemetry replaced by a no-op
- `spec/template/` feature status templates
- `commands/` except feedback and sre
- `hooks/claude-code/` and `hooks/codex/`
- `packages/agents/` and `packages/skills/` after the drops below
- `packages/agent-overrides/claude/` and `packages/agent-overrides/codex/`
- `references/assets/design-template-outline.md` written for this tree
- `examples/specrules/` starter layout

Specrules default to off. Set a git URL with `devkit-flow specrules configure` when a project has rules.

User-level state (global config, update check, specrules cache, runtime marketplace, session registry) is `~/.config/devkit`. Project state stays in `<repo>/.devkit/`. This tree does not read or write `~/.devkit`.

## Omitted

These paths from the upstream are not copied, because they call internal systems or carry internal identifiers:

- `src/devkit_flow/ones.py`, `ones_sync.py`
- `src/devkit_flow/bridge*.py` (SSO cookies and chat bridge)
- `src/devkit_flow/feedback.py`, `sre.py`
- `packages/agents/sre-agent.md`, `apidoc-checker-agent.md`
- `packages/skills/devkit-feedback/`, `gate-notify/`, `devkit-release-manager/`, `feature-retro/`
- `commands/feedback.md`, `commands/sre.md`
- upstream `docs/releases/`, `spec/001-*`, `spec/015-*`, `spec/016-*`
- `runtime-plugins/` generated copies
- the bundled `bin/devkit-flow` from upstream

The manager, technical-design, and design-review prompts in this tree are public replacements. They keep the local state machine and drop remote template, issue-tracker, and notification steps.

## Ports

| Port | Public behavior |
|---|---|
| Issue tracker | off; local STATUS.md is the record |
| Design template source | markdown file or the bundled outline |
| Notifier | off |
| Telemetry | off; no identity and no network |
| External knowledge | off |
| Identity | git user only, when a command needs a name |

## Before GitHub

Run `python3 scripts/check_boundary.py`. Keep the repository private until the copyright holder in `LICENSE` is confirmed and a secret scan of this tree is clean. Do not push the private upstream history.
