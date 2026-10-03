---
name: spec-archive-manager
description: Use when a repository has completed or legacy spec directories that need knowledge-base consolidation, archival, optional cleanup, status inspection, or Git-backed recovery.
---

# Spec Archive Manager

历史 spec 归档是一个可恢复事务：先扫描和收敛知识，再独立确认清理，最后才改变源目录。所有状态、配置、审批和删除都由全局 `devkit-flow` 执行；Skill 负责理解用户意图、生成摘要、调用 `docs-curator`，以及展示确认信息。

## 快速参考

| 用户意图 | 动作 |
|---|---|
| “看看哪些可以归档” | `devkit-flow spec archive scan --format json` |
| “归档这个 spec” | plan → stage → knowledge-base approval → verify → apply |
| “归档后也清理源文件” | 完成 apply 后单独展示目标，再批准 cleanup 并 prune |
| “只保留 Git 历史” | cleanup 与 git-only-cleanup 两个 Gate 都通过后 prune |
| “继续上次归档” | `devkit-flow spec archive status --format json`，从 receipt 的下一步继续 |
| “恢复误清理内容” | `devkit-flow spec archive restore --archive-id <id>` |

## 执行契约

0. **docs-curator 门闩**：生成知识库草稿前，先读 `devkit-flow docs-curator status --format json`（或总 `status --format json` 的 `docs_curator`）。`enabled=false` 时不调用 skill，写出 `skipped-disabled` + `source`，不得假装已同步；`operation: archive` 在关闭时不写工作区 `docs/`。`enabled=true` 时再按下方契约调用，`operation = `archive``。
1. 运行 `devkit-flow spec archive scan --format json`。只自动处理 `eligible`；`review-required` 必须把识别依据展示给用户，得到明确选择后才可在 plan 中使用 `--include-review-required`；`blocked` 不得绕过。
2. 若用户通过对话修改默认策略，将意图映射到 `devkit-flow spec archive configure`。不得直接编辑 `.devkit/config.json`，也不得要求用户编辑 JSON。
3. 为每个已选 feature 运行 `devkit-flow spec archive plan --feature <feature> --format json`，保存返回的 `archive_id`。`$ARGUMENTS` 或命令入口只表达选择与选项，不等于任何删除授权。
4. 在 receipt 的 staging 目录生成：
   - `archive/ARCHIVE-SUMMARY.md`：目标、关键裁定、最终结果、仍有效链接、恢复信息；
   - compact 模式需要保留的其他 `archive/**` 文件；
   - 知识库草稿：仅当门闩开启时调用 `docs-curator`，传入 `operation = archive`、`archive_summary_path`、`source_manifest`、`knowledge_output_root` 和 `sync_to_template_probe: false`。docs-curator 只能写入 staging，不能直接改工作区 `docs/`；确实无需更新时返回结构化 no-op 原因。门闩关闭则跳过调用并回传 `skipped-disabled`，不得假装已同步。
5. 运行 `devkit-flow spec archive stage --archive-id <id>`；知识库为 no-op 时使用 `--knowledge-noop-reason <reason>`。随后按有效配置处理 `knowledge-base` Gate：
   - `ask`：展示将写入/替换的知识库文件和 no-op 理由，等待用户明确确认，再运行 `devkit-flow spec archive approve --archive-id <id> --gate knowledge-base`；
   - `auto`：CLI 可自动记录此 Gate，但仍要向用户汇报将应用的内容；
   - 知识库收敛不可跳过，只能是有草稿或有结构化 no-op。
6. 运行 `devkit-flow spec archive verify --archive-id <id>`，再运行 `devkit-flow spec archive apply --archive-id <id>`。apply 先应用知识库和紧凑归档，不代表用户批准删除原始 spec。
7. 根据 retention 决定后续：
   - `keep`：不清理源内容，直接进入 finalize；
   - `compact`：列出 prune 的精确路径、Git 可恢复提交和目标状态；只有用户在本轮对话明确同意后，运行 `devkit-flow spec archive approve --archive-id <id> --gate cleanup`；
   - `git-only`：除 cleanup 外，必须再次解释“归档副本也会删除，仅保留 Git 恢复”，获得独立确认后运行 `devkit-flow spec archive approve --archive-id <id> --gate git-only-cleanup`。
8. Gate 完整后运行 `devkit-flow spec archive prune --archive-id <id>`，最后运行 `devkit-flow spec archive finalize --archive-id <id>`。任何步骤失败时保留 receipt，运行 `devkit-flow spec archive status --archive-id <id> --format json` 汇报可恢复断点。

## 不可绕过的安全边界

- 不得直接删除、移动或覆盖 `spec/` 内容；不得用 shell、文件工具或 Git 清理命令代替 `prune`。
- 不得直接编辑 `.devkit/config.json`；配置只能经 `devkit-flow spec archive configure`。
- 不得把 Skill/command 的触发本身当作 cleanup 或 git-only-cleanup 授权。
- 不得清理未跟踪、已修改、无法从 receipt 记录的 Git commit 恢复，或 verify 后发生变化的文件。
- `flow archive` 只表示 feature 的逻辑归档（离开活跃检测），不做历史内容整理；历史归档必须走 `spec archive` 事务。
- 文档等远端知识库不随本流程自动更新。`sync_to_template_probe: false` 是默认值；如需同步，必须在本地归档事务之外单独确认。

## 示例

用户说“把 012-payment 归档，知识库先让我确认，源文件暂时保留”时：scan 后 plan 该 feature，生成摘要并调用 docs-curator 写 staging，stage 后展示知识库 diff，收到确认才批准 `knowledge-base`，verify/apply，因 retention=keep 不执行 cleanup，最后 finalize。全程不直接改配置或删除 spec。

## 常见错误

- 把“归档”直接理解成删除：先区分逻辑归档、内容归档与清理。
- 每个文件问一次：知识库改动合并为一次确认，cleanup 再单独一次确认。
- 为了继续而重新 plan：先查 status；receipt 已存在时从已记录断点恢复。
- 源文件变化后继续 apply/prune：重新 verify，失败则停止并报告变化，不得覆盖。
