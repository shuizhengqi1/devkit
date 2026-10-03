from __future__ import annotations


# 工作区极简路由文件模板：只在目标缺失时写入，已存在绝不覆盖。
WORKSPACE_ROUTING_TEMPLATE = """# {filename}

本项目使用 Devkit SDD 流程。全局 `devkit-flow` 是唯一入口。

- 所有 feature / 修复 / 设计请求：先运行 `devkit-flow flow next` 获取当前断点与建议动作。
- 自动接管受 `devkit-flow workflow status` 约束。`workflow` 是自动接管开关，不是流水线状态机 `flow`。
- 流水线与任务状态流转只允许通过 `devkit-flow flow` 命令完成，禁止手工编辑 STATUS.md / task.md 的状态列。
- spec 模板通过 `devkit-flow template <name>` 获取。开发规范索引见 `specrules/rules/index.md`。
- 项目级补充规范见 `project-rules/project-rules.md`（若存在）。
- 设计模板默认使用 `references/assets/design-template-outline.md`，也可以把 markdown 文件配成设计模板。
"""


LEGACY_WORKSPACE_ROUTING_TEMPLATE = WORKSPACE_ROUTING_TEMPLATE


PRE_WORKFLOW_LEGACY_WORKSPACE_ROUTING_TEMPLATE = WORKSPACE_ROUTING_TEMPLATE


PROJECT_RULES_TEMPLATE = """# 项目级规范补充

<!-- 在此维护本项目特有的规范约定，作为 specrules/ 的补充。 -->
<!-- agent 会在加载 specrules/rules/index.md 之后追加加载本文件。 -->
"""
