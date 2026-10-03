---
version: 1
top_n: 10
updated_at: 2026-09-07
source_features:
  - historical-review
  - historical-review
  - historical-review
  - historical-review
  - historical-review
  - historical-review
  - historical-review
  - historical-review
  - historical-review
  - historical-review
  - historical-review
  - historical-review
---

# 派发红线清单（dispatch-redlines）

> 用途：feature-dev-manager-agent 派发 worker / reviewer 任务时，**必须**将本清单 top_n 条全文带入派发 prompt；
> worker 实现前逐条对照预计写入范围自检，reviewer 将其作为既有检查项之外的前置红线复核。
> 首版由本仓 `spec/*/review-*.md` 历史 finding 归类生成（见 frontmatter `source_features`）；
> 后续由 feature-retro 环节增量追加，并同步提升 `version` 与 `updated_at`。
>
> 频次口径：`finding` = 种子报告中作为 finding / 警告 / 缺口实发的报告份数；
> `固定核查` = 种子报告中作为每轮架构审查固定执行项出现的报告份数（本仓为 Python 工具链，Java 专项 finding 实发为 0，
> 列为派发红线是因为其在业务目标仓高发且每轮审查必查，应前置到派发与 lint 拦截）。
> 排序规则：先 finding 类按频次降序，后固定核查类按频次降序；同类同频次按 id 升序。
>
> 静态可校验：`static_check_id` 与 `devkit-flow lint` 规则 id 一一对应（命名 / 分层 / DI 三组，WARNING 不阻断、BLOCKER 必须清零）；
> `none` 表示暂无静态规则，由 reviewer 兜底。

| id | pattern（违规模式） | static_check_id | 出处（种子 review 文件） | 频次 |
|----|----------------------|-----------------|---------------------------|------|
| RL-01 | 设计文档必填/卡控章节缺口：干系方表角色不全、技术安全「安全要求清单」未逐条评估、容量评估未立节、背景缺 PRD/FEATURE 链接、排期缺负责人/责任角色 | none | historical-review；historical-review；historical-review；historical-review；historical-review；historical-review；historical-review；historical-review | 8（finding） |
| RL-02 | 审查/测试报告产物缺席：适用时必须落盘报告（不适用则落 N/A 结论报告），缺文件导致下游门禁与复审误读 | none | historical-review（INFO-04）；historical-review（INFO-02）；historical-review（m-8）；historical-review（INFO-02）；historical-review（INFO-01）；historical-review（INFO-03） | 6（finding） |
| RL-03 | 契约文档与实现/裁定漂移：取值域、措辞、版本口径在文档间或文档与代码间自相矛盾（含 DECISIONS 裁定与设计正文打架） | none | historical-review（MAJOR-01）；historical-review（M-2）；historical-review；historical-review；historical-review | 5（finding） |
| RL-04 | 审查扫描范围失控：扫全库、把 docs/ 历史快照当审查对象、未与 Git diff 对账导致漏文件或多文件 | none | historical-review；historical-review；historical-review；historical-review | 4（finding） |
| RL-05 | 契约源改动后传播产物未再生成：packages/ 或源 CLI 变更未同步再生成全部宿主产物（plugins/ 与 runtime-plugins/），投影过期漏同步 | none | historical-review（WARNING）；historical-review（WARNING-1/W1）；historical-review | 3（finding） |
| RL-06 | 门禁/基线事实源缺误用防护：默认 fail-open、刷新脚本不校验来源一致性，脏产物可污染基线导致门禁被放宽而非收紧 | none | historical-review（W2）；historical-review；historical-review | 3（finding） |
| RL-07 | 复核只做差异检查：前轮 pass 结论直接带入、只查上轮列出的问题点（每轮必须全量重审，修改可能引入新问题） | none | historical-review；historical-review | 2（finding） |
| RL-08 | 数据对象命名错层：DTO/BO/DO/Entity 后缀出现在错误层、集合命名未用 list 后缀 | naming.data-object-suffix | historical-review；historical-review；historical-review（每轮架构审查 §3.2 固定执行，共 40 份） | 40（固定核查） |
| RL-09 | 跨层 import / 依赖方向违规：App 直调 DAO、Domain 依赖 api 包、Infra 依赖 BO/DTO、App 层使用 @Transactional | layers.cross-layer-import | historical-review；historical-review；historical-review（每轮架构审查 §3.1 固定执行，共 38 份） | 38（固定核查） |
| RL-10 | Spring Bean 宿主 new / `new Foo(this, ...)` / lazy factory：拆出协作类绕开容器、以宿主引用解循环（须 Spring 管理 + `@Resource` 字段注入） | di.bean-host-new | historical-review（门闩来源 feature）；historical-review；historical-review；historical-review | 9（固定核查） |

## 维护规则

1. 条目总数超过 `top_n` 时按上述排序规则截取前 top_n 条。
2. 新增条目必须给出可回溯的种子出处（review 文件 + finding 编号）；无出处的条目不得进入清单。
3. 条目被 `devkit-flow lint` 规则覆盖时，把 `static_check_id` 从 `none` 改为对应规则 id，并在 CHANGELOG 记录对齐关系。
4. 本文件随 packages/ build 传播到全部宿主产物；改动后必须再生成并通过 build 契约测试。
