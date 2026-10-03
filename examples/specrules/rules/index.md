# Example specrules

This directory is a starter. Point a project at your own git repository:

```bash
devkit-flow specrules configure --scope project --enable --repo-url git@github.com:you/specrules.git
```

## How agents should read rules

1. Read this index.
2. Load only the rule files that match the task.
3. Prefer the project's code over a generic example.

## Suggested rule files

- `testing.md` — how to run and where tests live
- `style.md` — naming and layout the repository already uses
- `architecture.md` — boundaries that reviewers should enforce
