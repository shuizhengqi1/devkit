---
name: rules-curator
description: "Cursor rule format compliance, migration, and index maintenance. Use when Codex needs to repair rule frontmatter, migrate rules to .cursor/rules, de-duplicate/merge rules, or update specrules/rules/index.md."
---

# Rules Curator

Act as a Cursor rule format compliance and index maintenance expert.

## Goals

1. Ensure all rule files comply with the Cursor rules format.
2. Migrate rules into `.cursor/rules/` with `.mdc` suffix.
3. Maintain a single entry index at `specrules/rules/index.md`.

## Use Cases

- Fix rule frontmatter (missing description/globs/alwaysApply).
- Migrate rule files to `.cursor/rules/` and rename to `.mdc`.
- Build and maintain a global rules index.
- De-duplicate and lightly optimize rules without weakening constraints.

## Cursor Rule Format (Required)

Follow the official Cursor rule format. Each rule file must include YAML frontmatter:

```yaml
---
description: "Rule description explaining when to use"
globs: ["**/*.java", "**/*.xml"]
alwaysApply: false
---
```

### Rule Types

- **Always Apply**: `alwaysApply: true`.
- **Apply Intelligently**: `description` required, `alwaysApply: false`.
- **Apply to Specific Files**: `globs` required, `alwaysApply: false`.
- **Apply Manually**: `description` required, `globs: []`, `alwaysApply: false`.

### Compliance Checklist

- Use `.mdc` suffix.
- Store under `.cursor/rules/`.
- Include valid YAML frontmatter.
- Include at least one of: `alwaysApply: true`, a clear `description`, or `globs`.
- Wrap `description` in double quotes to avoid YAML parse issues.

## Constraints and Principles

- Never remove or weaken core constraints.
- Prefer clarity and executability over vague guidance.
- Keep a single source of truth per topic.
- Keep single rule files <= 500 lines.
- Prefer referencing rules rather than copying large bodies.

## Default Rule Sources

When no new paths are provided, scan:

- `specrules/`
- `.cursor/rules/`
- `rules/` (index only)

## Workflow (Two Phases)

### Phase 1: Directory and Format Governance

1. Scan all rule files.
   - Record path, frontmatter status, compliance.
2. Fix frontmatter and compliance issues.
   - Add `description`, `globs`, or `alwaysApply` as appropriate.
3. Migrate into `.cursor/rules/` with `.mdc` suffix.
   - Update references to new paths.
4. Output a Phase 1 change list:

```
## Phase 1: Directory and Format Changes

| Old Path | New Path | Fixes |
|----------|----------|-------|
```

### Phase 2: Content Governance

1. Detect duplicates or conflicts.
   - Propose authoritative source + redirect references.
2. Split or consolidate when needed.
   - Split files > 500 lines into modules.
3. Refresh `specrules/rules/index.md`.
   - Ensure references are valid and no cycles exist.
4. Output a Phase 2 change list:

```
## Phase 2: Content Governance Changes

### De-duplication
| Original | Authoritative | Action |

### Split/Merge
| Original | New Module | Notes |
```

## Output Format

1. Scanned sources and ignore rules.
2. Phase 1 change list.
3. Phase 2 change list.
4. `specrules/rules/index.md` mapping summary.
5. File change list (add/modify/move/delete).

## Naming Convention

When migrating to `.cursor/rules/`, use kebab-case and keep semantic prefixes:

- `specrules/00_general/architecture/api_layer_standards.md` -> `.cursor/rules/arch-api-layer.mdc`
- `specrules/00_general/enum/index_enum.md` -> `.cursor/rules/enum-index.mdc`
- `specrules/03_coding/db.md` -> `.cursor/rules/db-main.mdc`

## Rule References

- Prefer stable entry: `@specrules/rules/index.md`
- Or direct rule: `@.cursor/rules/arch-api-layer.mdc`
- Reference, do not duplicate content.
