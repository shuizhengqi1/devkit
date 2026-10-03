# README 架构维护 - 参考

本 skill 独立运行，仅依赖项目内以下约定文件：

- **README 模板**：项目根目录下 [docs/README-template.md](../../docs/README-template.md)
- **规则入口**：[specrules/rules/index.md](../../../specrules/rules/index.md)（先加载任务前置必加载，再按设计阶段或开发阶段追加）
- **项目结构等规范**：`specrules/00_general/` 等（按 `specrules/rules/index.md` 的统一入口回查）

维护本 skill 时，若上述路径或结构变更，请按 SKILL.md 末尾「自更新说明」更新技能内引用。
