# devkit

Spec-driven development for coding agents. `devkit-flow` keeps feature state on disk. Claude Code, Codex, and Grok Build share that state machine.

Version 0.1.0 is the public core. It is licensed under MIT.

## Install

From a checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
devkit-flow --help
```

Or run it in place:

```bash
python3 bin/devkit-flow --help
```

## Start a feature

```bash
cd your-repo
devkit-flow setup
devkit-flow doctor
devkit-flow feature init --name demo --scale small
devkit-flow flow next
```

Pipeline status changes only through `devkit-flow flow`. Do not hand-edit the status column in `STATUS.md` or `task.md`.

## What 0.1.0 includes

- Local feature lifecycle: init, next, confirm, set, task, complete, archive
- Design template from a markdown file, with `references/assets/design-template-outline.md` as the default outline
- Optional specrules from a git URL you configure
- Agents and skills for the local workflow
- Host notes for Claude Code, Codex, and Grok Build

## What 0.1.0 leaves out

Issue trackers, remote knowledge bases, chat notifications, and phone-home telemetry are ports. This build ships those ports as off. See [docs/BOUNDARY.md](docs/BOUNDARY.md).

User-level config and caches live in `~/.config/devkit`. Project config stays in `your-repo/.devkit/config.json`. This build does not read or write `~/.devkit`.

## Source

The public git repository is <https://github.com/shuizhengqi1/devkit>. The command is `devkit-flow`. The npm package is `@h-devkit/devkit-flow`:

```bash
npm i -g @h-devkit/devkit-flow
```

This checkout is not on npm yet. The pip project name stays `devkit-flow`.

The copyright line in `LICENSE` is `devkit contributors`.
