# 对象转换与数据类规范 — 参考

本 skill 的检查依据来自项目规范，此处仅做引用与补充说明。

## 规范文档

| 内容 | 路径 |
|------|------|
| 对象转换（分层、Converter、禁止项） | [specrules/03_coding/object_conversion_standards.md](../../../specrules/03_coding/object_conversion_standards.md) |
| 数据对象命名与层级 | [specrules/00_general/naming/data_object_naming.md](../../../specrules/00_general/naming/data_object_naming.md) |
| 应用层 BO/DO 转换 | [specrules/00_general/architecture/app_layer_standards.md](../../../specrules/00_general/architecture/app_layer_standards.md) |
| 用户接口层 DTO↔BO | [specrules/00_general/architecture/ui_layer_standards.md](../../../specrules/00_general/architecture/ui_layer_standards.md) |

## 转换职责速查

- **DTO/VO ↔ BO**：只在 starter 的 Converter 里做，静态方法，命名如 `xxDTO2xxBO`、`xxBO2xxVO`。
- **BO → DO**：简单放在 BO（`toXxxDO()`、`static fromXxxDO(XxxDO do)`），复杂用 `application/.../converter/` 的静态 Converter。
- **DO → Entity/Cache/外部 DTO**：简单放在 DO（`toXxxEntity()`、`static fromXxxEntity(...)`），复杂用 `domain/.../converter/` 的静态 Converter。
- **禁止**：Converter 用 @Component/注入；任何地方通过「服务实现类的静态/实例方法」做 BO→DO 或 DO→其他转换。

## @Data 与 Lombok 使用说明

- 项目内 消息队列 DTO、部分 Request/Response、领域 Request 已使用 `@Data`（及 `@Builder`、`@AllArgsConstructor`、`@NoArgsConstructor`）。
- 纯数据载体类（DTO/VO/BO/DO/Entity/消息体）在无特殊框架约束时，推荐统一使用 `@Data` 去掉手写 getter/setter。
- 若类上已有 `@Data`，则不应再保留手写 `getXxx()`/`setXxx()`，避免重复与冲突。
- 使用 @Data 后，**BO/DO 上的业务转换方法**（如 `toXxxDO()`、`fromXxxDO()`、`toXxxEntity()`）仍保留在类内，不删除。

## 规则入口

按规则入口加载时，先看 [specrules/rules/index.md](../../../../specrules/rules/index.md) 的「任务前置必加载」，再进入「开发阶段规则集合」中的对象转换与分层实现规则；若涉及接口边界，再补充横切专题中的相关约束。
