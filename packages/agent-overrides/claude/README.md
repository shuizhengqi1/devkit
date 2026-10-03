# Claude Plugin Agent Overrides

This directory contains Claude-specific overrides that are applied when building
plugin runtime agents into `.claude-plugin/agents/`.

Override files are optional and keyed by agent filename stem:

- `{agent}.json`
  - `frontmatter`: object merged into the shared agent frontmatter
  - `body_replacements`: ordered exact-string replacements applied to the body
- `{agent}.append.md`
  - extra markdown appended to the rendered body

Example:

```json
{
  "frontmatter": {
    "tools": "Read, Grep, Glob, Bash",
    "skills": ["docs-curator"]
  },
  "body_replacements": [
    {
      "find": "由 dev-task-executor-agent 调用时：",
      "replace": "由 feature-dev-manager-agent 调用时："
    }
  ]
}
```

Notes:

- Claude override 里的 `skills` 仍写短名即可，例如 `mermaid_chart_generator`
- `scripts/build_plugin_agents.py` 会在生成 `.claude-plugin/agents/*.md` 时，自动把这些 skill 改写成 `devkit::{skill}`
