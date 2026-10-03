# Skills: Shared Agent Skill Definitions

This package contains reusable skill definitions shared by Claude Code, Codex,
and Kimi Code runtime adapters.

## Structure

- `skill/skills/` - Skill definitions
  - `apidoc-checker-agent/` - Check API documentation completeness
  - `design-doc-reviewer-agent/` - Review design documents
  - `devkit-feedback/` - Submit proactive DevKit feedback to ONES space 
  - `devkit-release-manager/` - Prepare devkit/plugin releases (maintainer-only; not shipped in consumer plugins)
  - `enum_design_agent/` - Legacy enum ritual (canonical relic; omitted from consumer plugins; use specrules/rules/index.md §4)
  - `feature-dev-manager/` - Codex main-thread Devkit SDD entrypoint
  - `mermaid_chart_generator/` - Generate mermaid charts
  - `object-conversion-checker/` - Legacy conversion checklist (canonical relic; omitted from consumer plugins; use specrules object conversion row)
  - `readme-architecture-curator/` - Curate README architecture sections
  - `rules-curator/` - Maintain specrules consistency
  - `spec-archive-manager/` - Consolidate, archive, clean, and restore historical specs
  - `work_item_splitter/` - Legacy scheduling WBS table (canonical relic; omitted from consumer plugins; S4 uses design-to-tasks-agent)

## Version

See `VERSION` file for current version.

## Usage

Skills are synchronized to a target workspace `.agents/skills/` directory when
the selected runtime supports the shared Agent Skills convention. Claude and
Codex plugins reference compatible canonical directories directly. The
`feature-dev-manager` Skill is the Codex-only main-thread entrypoint. Kimi
worker-role Skills are generated separately under `.kimi-plugin/skills/`, and
the Kimi manifest excludes the Codex-only manager to prevent entrypoint
collisions. `devkit-release-manager` stays in this canonical tree for
devkit maintainers and is omitted from consumer plugin
payloads (Claude, Codex, Kimi, Pi, Grok). `enum_design_agent`,
`object-conversion-checker`, and `work_item_splitter` also stay in this
canonical tree but are omitted from consumer payloads; agents discover enum
and conversion rules via `specrules/rules/index.md` §4.
