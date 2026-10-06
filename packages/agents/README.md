# Agents: Agent Definitions for IDEs

This package contains canonical Agent role definitions shared by Cursor,
Claude Code, and Kimi Code adapters.

## Structure

Agent definitions with platform-agnostic frontmatter:

- `design-to-tasks-agent.md` - Convert design documents to task lists
- `dev-task-executor-agent.md` - Execute development tasks
- `feature-dev-manager-agent.md` - Manage feature development workflow
- `quick-fix-agent.md` - Quickly fix clear bugs without the full feature workflow
- `sre-agent.md` - Diagnose runtime issues and assist deploy without changing business code
- `prd-analyzer-agent.md` - Analyze PRD quality and generate FEATURE.md
- `change-impact-analyzer-agent.md` - Analyze clarification/change impact and update CHANGELOG routing
- `tech-design-architect-agent.md` - Create technical design documents
- `architecture-reviewer-agent.md` - Four-layer DDD architecture compliance auditor (cross-cutting topics via the entry index's declared loading protocol, e.g. root package §4 trigger table)
- `apidoc-checker-agent.md` - API documentation annotation completeness checker
- `design-doc-reviewer-agent.md` - Design document compliance reviewer (project template)

## Version

See `VERSION` file for current version.

## Usage

Agent definitions are rendered during sync to target workspace
`.cursor/agents/` or legacy `.claude/agents/` directories based on the detected
IDE. Claude plugin agents are generated into `.claude-plugin/agents/` by
`scripts/build_plugin_agents.py`. Kimi Code has no project custom-Agent target;
`scripts/build_kimi_plugin.py` renders the same roles as isolated inline Skills
under `.kimi-plugin/skills/`.
