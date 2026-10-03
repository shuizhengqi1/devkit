# {Feature} 状态

**状态**: 开发中 | 已完成 | 已归档
**ONES需求ID**:
**ONES同步**: pending | creating | synced | retry: {错误摘要}
**规模**: small | large | large-no-context
**模式**: 传统 | TDD
**分支**: feature/{feature} | {当前分支名（未创建独立分支时）}
**创建时间**: yyyy-mm-dd
**最后更新**: yyyy-mm-dd
**TraceID**: tx_<uuid>

## 流水线

<!-- 公共头部（S0.5-H 到 S5）：两种模式均使用 -->
<!-- agent 在任务 0.5 模式确认后，将下方 [传统流水线] 或 [TDD流水线] 之一追加到此表格末尾，另一套不写入 -->
| 步骤 | 名称 | Agent/Skill | 依赖 | 状态 | 备注 |
|------|------|-------------|------|------|------|
| S0.5-H | 模式选择 | feature-dev-manager-agent | - | waiting-confirmation | 必须人工确认 TDD/传统 后才能进入 S1 |
| S1   | 需求建立 | feature-dev-manager-agent | S0.5-H | pending | |
| S1.1 | PRD 理解与检查 | prd-analyzer-agent | S1 | pending | 产出 FEATURE.md |
| S2.0 | 上下文分析 | context-analyzer-agent | S1.1 | pending | |
| S2.1 | 设计文档 | tech-design-architect-agent | S1.1,S2.0 | pending | |
| S2.2 | 接口文档 | tech-design-architect-agent | S1.1,S2.0 | pending | 与S2.1同步产出 |
| S2.3 | 类图文档 | tech-design-architect-agent | S1.1,S2.0 | pending | 与S2.1同步产出 |
| S2.1-R | 设计文档审查 | design-doc-reviewer-agent | S2.1,S2.2,S2.3 | pending | |
| S2.2-R | 接口文档审查 | apidoc-checker-agent | S2.1,S2.2,S2.3 | pending | |
| S2.3-R | 类图文档审查 | architecture-reviewer-agent | S2.1,S2.2,S2.3 | pending | 可选，复杂领域模型时启用 |
| S2-H | 方案人工确认 | feature-dev-manager-agent | S2.1-R,S2.2-R | pending | 必须人工确认后才能进入 S4 |
| S4   | 任务规划 | design-to-tasks-agent | S2-H 或 S1.1(small) | pending | Small 只落盘单条 T-01，不做多任务智能拆解 |
| S5-H | 开发执行确认 | feature-dev-manager-agent | S4 | pending | 必须人工确认后才能进入 S5 |
| S5   | 开发执行（每条任务重复） | feature-dev-manager-agent | S5-H | pending | |

<!-- [传统流水线] 用户选传统模式时，将以下行追加到上方表格：
条件行：S5.1-F（fund-security）与 S5-E2E（e2e）配置关闭时不生成，开启后由 flow migrate-steps 补行。
| S5.1 | 实现代码 | dev-task-executor-agent | S5 | pending | [传统] |
| S5.1-F | 资金安全风险分析 | fund-security-analyzer | S5.1 | pending | [传统] 风险关闭后才能进入单元测试/Review |
| S5.2 | 单元测试 | unit-test-agent | S5.1-F | pending | [传统] ≥80% 覆盖率 |
| S5.3 | 架构审查 | architecture-reviewer-agent | S5.2 | pending | [传统] |
| S5-R | 受影响模块全量回归 | feature-dev-manager-agent | S5,S5.3 | pending | 不带测试选择器，Small 不得豁免 |
| S5-E2E | 需求准出 e2e 门 | feature-dev-manager-agent | S5-R | pending | devkit 只读 quality-gate.json 判门；无跨进程面须 skipped + 书面理由 |
| S5.7 | Feature 级综合审查 | architecture-reviewer-agent | S5-E2E | pending | 单位=feature 而非任务；跨切片一致性 + 整体性判断 |
-->

<!-- [TDD流水线] 用户选 TDD 模式时，将以下行追加到上方表格：
条件行：S5.2-F（fund-security）与 S5-E2E（e2e）配置关闭时不生成，开启后由 flow migrate-steps 补行。
| S5.1 | Red Phase（测试先行） | tdd-test-agent | S5 | pending | [TDD] 编写失败测试 + 骨架类，等待用户确认 |
| S5.2 | 实现代码 | dev-task-executor-agent | S5.1 | pending | [TDD] 使测试通过 |
| S5.2-F | 资金安全风险分析 | fund-security-analyzer | S5.2 | pending | [TDD] 风险关闭后才能进入 Green/Review |
| S5.3 | Green Phase（验证通过） | tdd-test-agent | S5.2-F | pending | [TDD] 运行测试确认全绿，等待用户确认 |
| S5.4 | Coverage Analysis（覆盖率） | tdd-test-agent | S5.3 | pending | [TDD] ≥80%，不足则补测试 |
| S5.5 | 架构审查 | architecture-reviewer-agent | S5.4 | pending | [TDD] |
| S5.6 | Refactor（可选） | tdd-test-agent | S5.5 | pending | [TDD] 重构后测试仍需全绿 |
| S5-R | 受影响模块全量回归 | feature-dev-manager-agent | S5,S5.6 | pending | 不带测试选择器，Small 不得豁免 |
| S5-E2E | 需求准出 e2e 门 | feature-dev-manager-agent | S5-R | pending | devkit 只读 quality-gate.json 判门；无跨进程面须 skipped + 书面理由 |
| S5.7 | Feature 级综合审查 | architecture-reviewer-agent | S5-E2E | pending | 单位=feature 而非任务；跨切片一致性 + 整体性判断 |
-->

## 设计任务

| 任务ID | 类型 | 描述 | 状态 | 实际开始 | 实际完成 | ONES任务ID | ONES同步 |
|--------|------|------|------|----------|----------|------------|----------|
| D-01 | 设计 | 需求与上下文分析 | 待执行 | | | | pending |
| D-02 | 设计 | 技术方案设计 | 待执行 | | | | pending |
| D-03 | 设计 | 设计审查与修复 | 待执行 | | | | pending |

<!-- 由 devkit-flow flow 命令自动维护，禁止手工编辑 -->
## 进度概要

- 已完成: 无
- 当前断点: S0.5-H（模式选择）
- 剩余步骤: S0.5-H → S1 → S1.1 → S2.0 → S2.1 → S2.1-R → S2.2 → S2.2-R → S2.3 → S2.3-R → S2-H → S4 → S5-H → S5 → S5.x（模式确认后填入）→ S5-R（受影响模块全量回归）→ S5-E2E（需求准出 e2e 门，skipped 须书面理由）→ S5.7（Feature 级综合审查；S5-R 与 S5.7 均不可跳过）

## 状态值说明

- `pending`：未开始
- `in-progress`：执行中
- `done`：已完成
- `blocked`：被阻塞
- `waiting-confirmation`：等待人工确认
- `skipped`：已跳过
- `waived`：设计审查遗留风险已由用户明确接受；仅允许通过 `flow accept-risk --gate S2-H --reason ... --confirmed` 写入，S5-R/S5.7 不适用
