from __future__ import annotations

import re
from pathlib import Path


DONE_STATES = {"done", "已完成"}


SKIPPED_STATES = {"skipped", "已跳过"}


WAIVED_STATES = {"waived", "风险接受"}


CLOSED_STATES = DONE_STATES | SKIPPED_STATES | WAIVED_STATES


INACTIVE_FEATURE_STATES = {"已完成", "completed", "done", "已归档", "archived"}


FLOW_STEP_STATES = {
    "pending",
    "in-progress",
    "done",
    "blocked",
    "waiting-confirmation",
    "skipped",
    "waived",
}


FLOW_GUARDED_STATES = {"in-progress", "done", "skipped"}


# S5-E2E 需求准出 e2e 门（设计 §4.5.1）：flow.py 置位守卫 / 缺行检测与 e2e.py 判门共用的步骤 id。
E2E_GATE_STEP = "S5-E2E"


# 完成 Gate 全集（配置全开上限），供 flow next / verify / complete 与 next_action_text
# 作为单一事实源（接口文档 §4.6）。运行期可按配置快照过滤受控行，本常量本身不变。
COMPLETION_GATE_STEPS = ("S5-R", "S5-E2E", "S5.7")


# 配置门 → 受控流水线行（P7 单一事实源；本次两实例，后续新质量门只加映射项）。
# 键为内部/CLI 连字符形态（fund-security）；JSON 投影为 snake_case（fund_security）。
CONFIG_GATED_STEPS: dict[str, tuple[str, ...]] = {
    "e2e": ("S5-E2E",),
    "fund-security": ("S5.1-F", "S5.2-F"),
}


DESIGN_REVIEW_STEPS = ("S2.1-R", "S2.2-R", "S2.3-R")


REQUIRED_DESIGN_REVIEW_STEPS = ("S2.1-R", "S2.2-R")


TASK_ARCH_REVIEW_STEPS = ("S5.3", "S5.5")


WAIVABLE_STEPS = DESIGN_REVIEW_STEPS + TASK_ARCH_REVIEW_STEPS


# accept-risk 全模式并集（原 argparse choices；接口 §2.6）。运行期必须按模式
# 走 accept_risk_gates_for_mode：传统 arch-review→S5.3，TDD arch-review→S5.5；
# TDD S5.3 是 green-phase，不是可人工放行的架构审查门。
ACCEPT_RISK_GATES = ("S2-H",) + TASK_ARCH_REVIEW_STEPS


def accept_risk_gates_for_mode(mode: str | None) -> tuple[str, ...]:
    """按当前 feature 模式给出可 accept-risk 的门（设计 §4.3.2 / §4.3.4）。

    传统 arch-review → S5.3；TDD arch-review → S5.5。
    TDD green-phase（S5.3）不可放行。模式未确认时仅 S2-H，避免 S5.3 跨模式错解。
    """
    text = (mode or "").strip()
    if text == "传统":
        return ("S2-H", "S5.3")
    if text == "TDD":
        return ("S2-H", "S5.5")
    return ("S2-H",)


DESIGN_REVIEW_ARTIFACT_KEYWORDS = {
    "S2.1-R": "设计文档",
    "S2.2-R": "接口文档",
    "S2.3-R": "类图",
}


FLOW_TASK_STATES = ["待执行", "执行中", "已完成", "失败", "取消"]


ROLLBACK_DESIGN_TASK_HINTS = {
    "S2.1": "D-02",
    "S2.2": "D-02",
    "S2.3": "D-02",
    "S2.1-R": "D-03",
    "S2.2-R": "D-03",
    "S2-H": "D-03",
}


# 步骤 → 产物路径 glob 清单（014 设计 §4.0 D-A5-2 / 接口文档 §6）：flow rollback
# 默认 --affected-only 的受影响闭包产物裁剪依据。语义 = 「这些产物变化会使该步骤失效」：
# 步骤填自身产物 ∪ 依赖列声明的输入产物（审查步骤登记其审查对象主产物，如 S2.1-R
# 失效于设计文档变化；S4 的两条备选依赖 S2-H / S1.1(small) 的输入产物都须登记——
# WARNING-02：漏登 S1.1 产物会让 small 模式 closure(S1) 把 S4 裁出却重置 S5-H/S5）。
# 未登记的步骤在闭包计算中一律保守含入（宁多勿漏，守质量底线）；
# S5-H / S5 / S5.* 开发测试步骤的产物是目标仓代码与测试报告，无法用 spec glob 表达，
# 故意不登记（恒保守含入下游）。
STEP_ARTIFACT_MAP: dict[str, list[str]] = {
    "S1": ["spec/*/STATUS.md", "spec/*/prd-*.md"],
    "S1.1": ["spec/*/prd-*.md", "spec/*/FEATURE.md"],
    "S2.0": ["spec/*/FEATURE.md", "spec/*/context-report.md"],
    "S2.1": ["spec/*/FEATURE.md", "spec/*/context-report.md", "spec/*/[设计文档]*.md"],
    "S2.2": ["spec/*/FEATURE.md", "spec/*/context-report.md", "spec/*/[接口文档]*.md"],
    "S2.3": ["spec/*/FEATURE.md", "spec/*/context-report.md", "spec/*/[设计文档]*.md", "spec/*/[类图]*.md"],
    "S2.1-R": ["spec/*/[设计文档]*.md"],
    "S2.2-R": ["spec/*/[接口文档]*.md"],
    "S2.3-R": ["spec/*/[类图]*.md"],
    "S2-H": ["spec/*/[设计文档]*.md", "spec/*/[接口文档]*.md"],
    "S4": [
        "spec/*/prd-*.md",
        "spec/*/FEATURE.md",
        "spec/*/[设计文档]*.md",
        "spec/*/[接口文档]*.md",
        "spec/*/plan.md",
        "spec/*/task.md",
    ],
}


CONFIG_PATH = Path(".devkit") / "config.json"


WORKFLOW_RUNTIME_KEYS = ("claude-code", "codex", "grok-build")


WORKFLOW_AUTO_TAKEOVER_REFUSE_MESSAGE = (
    "[devkit] workflow auto-takeover is OFF for {runtime} (source={source}). "
    "Plugin remains installed. Use /devkit:* or say 走 Devkit 流程 to start."
)


KBA_SKILL_PACKAGE_NAME = ""


KBA_INSTALL_COMMAND = "mtskills i -g "


PRESET_DESIGN_TEMPLATE_KM = ""


BUNDLE_DESIGN_TEMPLATE_OUTLINE = "references/assets/design-template-outline.md"


DESIGN_TEMPLATE_INVALID_POINTER = "invalid design template pointer"


INHERIT_ONLY_PROJECT = "--inherit is only valid with --scope project"


CLEAR_ONLY_GLOBAL = "--clear is only valid with --scope global"


HTML_UI_HELP = "Configure HTML clarification form and design-doc preview; default off."


PROGRESS_BAR_HELP = "Configure or query the Claude Code progress bar (AbovePrompt line); default on."


SPEC_ARTIFACTS_HELP = (
    "Configure whether the current feature spec/{NNN}-* directory is included in git add/PR; default on."
)


SPEC_ARTIFACTS_INVALID_LAYER = "Invalid specArtifacts"


SPEC_ARTIFACTS_SCOPE_GLOB = "spec/{NNN}-*"


REVIEW_ROUNDS_HELP = (
    "Configure Codex review auto-cycle limit; default 10. Not the primary review strategy."
)


HTML_UI_INVALID_LAYER = "Invalid htmlUi"


PROGRESS_BAR_INVALID_LAYER = "Invalid progressBar"


# status JSON `tasks` 字段（spec/017-devkit-progress-mod 设计 §4.3.4，D-9、D-12 J11）
TASK_CLOSED_STATES = {"已完成", "done", "completed", "取消"}
TASK_ITEMS_LIMIT = 200
TASK_DESC_MAX_CHARS = 60


REVIEW_ROUNDS_INVALID_LAYER = "Invalid reviewRounds"


REVIEW_ROUNDS_INVALID_MAX = "invalid review rounds"


REVIEW_ROUNDS_INVALID_STEP = "unknown review step"


DEFAULT_REVIEW_MAX_ROUNDS = 10


DEFAULT_REVIEW_NEW_CLASS_BUDGET = 2


KNOWN_REVIEW_STEPS = frozenset(
    {"S2.1-R", "S2.2-R", "S2.3-R", "S5.3", "S5.5", "S5.7"}
)


_REVIEW_STEPS_COMMON = frozenset({"S2.1-R", "S2.2-R", "S2.3-R", "S5.7"})


def known_review_steps_for_mode(mode: str | None) -> frozenset[str]:
    """按模式给出可写入 review-loop 的步骤。TDD S5.3=green-phase，不是 review。"""
    text = (mode or "").strip()
    if text == "传统":
        return _REVIEW_STEPS_COMMON | {"S5.3"}
    if text == "TDD":
        return _REVIEW_STEPS_COMMON | {"S5.5"}
    return _REVIEW_STEPS_COMMON | {"S5.5"}


TASK_ARCH_REVIEW_STEP_RE = re.compile(r"^T-[A-Za-z0-9._-]+-arch$")


PRESENT_WAIT_SECONDS = 1800


MERMAID_STUB_MARKER = "DevKit bundled mermaid stub"


MERMAID_MIN_RENDERABLE_BYTES = 10_000


DOCTOR_TEMPLATE_PROBE_FIX = "install oa-skills template_probe skill or use a readable markdown pointer"


TEMPLATE_PROBE_PROBE_TIMEOUT_SECONDS = 1.5
TEMPLATE_PROBE_FETCH_TIMEOUT_SECONDS = 30


KM_TEMPLATE_POINTER_RE = re.compile(
    r"^https://example\.com/docs/\d+$"
)


ATX_HEADING_RE = re.compile(r"(?m)^#{1,2}(?:\s|$)")


TEMPLATE_PROBE_LIST_NAME_RE = re.compile(r"(?im)^\s*template_probe(?:\s|$)")


Q9_SRE_TERMS = (
    "sre",
    "排查",
    "线上故障",
    "查日志",
    "查注册中心",
    "配置中心",
    "config",
    "只读库诊断",
    "deploy",
    "部署协助",
)


Q9_SRE_TOOL_TERMS = (
    "开通",
    "gateway",
    "网关",
    "queue",
    "scheduled",
    "申请 id 服务",
    "申请分布式 id",
    "queue topic",
    "新建 topic",
    "bastion",
    "config 写",
    "config_write",
)


SRE_WRITE_DESIGN_MARKERS = ("写设计",)


SRE_FULL_FEATURE_MARKERS = (
    "完整 feature",
    "完整feature",
    "标准 feature",
    "标准feature",
    "做登录需求",
)


SRE_SCHEMA_MARKERS = ("建表", "ddl")


TEMPLATE_PROBE_SKILL_RELATIVE_PATHS = (
    Path(".agents") / "skills" / "template_probe" / "SKILL.md",
    Path(".claude") / "skills" / "template_probe" / "SKILL.md",
    Path(".codex") / "skills" / "template_probe" / "SKILL.md",
)


FEEDBACK_SPACE_ID = ""


FEEDBACK_ISSUE_TYPE = "REQUIREMENT"


FEEDBACK_SUBTYPE_NAME = "技术需求"


FEEDBACK_REPORTER_FIELD_NAME = "需求提出人"


FEEDBACK_DEFAULT_ASSIGNEE = ""


FEEDBACK_DEFAULT_PRIORITY = "2"  # external-cli: 低(1)/中(2)/高(3)/紧急(4)


FEEDBACK_HELP = (
    "DevKit built-in feedback channel to ONES space ; not project ones configure."
)


FEEDBACK_PROBE_SUBTYPE_ID = "55826"


FEEDBACK_PROBE_REPORTER_VARIABLE = ""


FEEDBACK_LOG_RELPATH = ".devkit/feedback-log.json"


FEEDBACK_SOURCE_STEPS = {
    "proactive": "proactive-feedback",
    "retro": "feature-retro",
}


MEP_INSTALL_COMMAND = [
    "npm",
    "install",
    "-g",
    "",
    "--registry=https://registry.npmjs.org",
]


TASK_SYNC_COLUMNS = [
    "门禁进度",
    "计划开始",
    "计划完成",
    "预计工作量",
    "实际开始",
    "实际完成",
    "ONES任务ID",
    "ONES同步",
]


DESIGN_TASK_COLUMNS = [
    "任务ID",
    "类型",
    "描述",
    "状态",
    "实际开始",
    "实际完成",
    "ONES任务ID",
    "ONES同步",
]


CANONICAL_DESIGN_TASKS = [
    ("D-01", "需求与上下文分析"),
    ("D-02", "技术方案设计"),
    ("D-03", "设计审查与修复"),
]


DESIGN_TASK_SEPARATOR = "|--------|------|------|------|----------|----------|------------|----------|"


LOCAL_TASK_STATES = {
    "待执行": "todo",
    "pending": "todo",
    "todo": "todo",
    "执行中": "doing",
    "in-progress": "doing",
    "doing": "doing",
    "失败": "doing",
    "failed": "doing",
    "阻塞": "doing",
    "blocked": "doing",
    "已完成": "done",
    "done": "done",
    "completed": "done",
}


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


ONES_OUTBOX_SCHEMA_VERSION = 1


STATE_OUTBOX_PREFLIGHT_SCOPE = "feature"


# PATH 之外的全局兜底安装位置 ~/.config/devkit/bin/。项目内同路径仅用于识别和迁移旧副本。
DEVKIT_CLI_RELATIVE_PATH = Path(".devkit") / "bin" / "devkit-flow"


# 「检查是否有新版本」的本地缓存：TTL（默认 4 小时）内重复命令直接读缓存；
# 失败同样记录时间戳，避免每次 status / flow next 都访问网络。
LATEST_CHECK_CACHE_RELATIVE_PATH = Path(".config") / "devkit" / "latest-check.json"


# npm 通道子进程超时：版本探测（npm view）不超过 4 秒；自更新安装（npm i -g）放宽到 120 秒。
NPM_VIEW_TIMEOUT_SECONDS = 4


NPM_INSTALL_TIMEOUT_SECONDS = 120


# 三端运行时插件共用的 git 仓库（claude-code / codex 的 marketplace 与 kimi-code 手动安装同源）。
RUNTIME_PLUGIN_NAME = "devkit"


LEGACY_RUNTIME_PLUGIN_NAMES = ("devkit",)


CLAUDE_MARKETPLACE_NAME = "devkit-marketplace"


LEGACY_CLAUDE_MARKETPLACE_NAMES = (
    "devkit-market",
    "devkit-local",
)


CODEX_MARKETPLACE_NAME = "devkit-local"


RUNTIME_COMMAND_TIMEOUT_SECONDS = 120


# specrules 独立仓库（对齐 scripts/workspace_sync.py 的缓存路径与远端地址）。
SPECRULES_REPO_URL = ""


SPECRULES_CACHE_RELATIVE_PATH = Path(".config") / "devkit" / "devkit-specrules"


SPECRULES_CUSTOM_CACHE_RELATIVE_PATH = Path(".config") / "devkit" / "specrules-cache"


# 项目画像缓存目录（feature 016 设计 §4.7.1.2 / 接口 §8.2）：~/.config/devkit/cache/project-profiles/<sha256(realpath)[:16]>.json
PROJECT_PROFILE_CACHE_RELATIVE_PATH = Path(".config") / "devkit" / "cache" / "project-profiles"


SPECRULES_ILLEGAL_REASON = "disabled with project repoUrl"


SPECRULES_SETUP_ILLEGAL_REASON = f"illegal specrules config: {SPECRULES_ILLEGAL_REASON}"


SPECRULES_DISABLED_NO_TREE = "disabled; no specrules tree"


SPECRULES_DISABLED_REMOVED = "disabled; removed specrules/"


SPECRULES_CLEANUP_MANUAL = (
    "Resolve them manually first; CLI will not discard local specrules files."
)


SPECRULES_CLEANUP_BACKUP_NAME = "specrules.devkit-cleanup-backup"


SPECRULES_URL_SWITCH_BACKUP_NAME = "specrules.devkit-url-switch-backup"


SPECRULES_INVALID_URL_MESSAGE = "invalid specrules repo URL"


SPECRULES_HELP = (
    "Configure or query specrules mount switch and project-only git URL; default off."
)


# git fetch / submodule init during specrules refresh; fail-open to local copy.
SPECRULES_REFRESH_GIT_TIMEOUT_SECONDS = 20


SPECRULES_FOLLOW_GLOBAL_SCOPE = "--follow-global is only valid with --scope project"


SPECRULES_REPO_URL_SCOPE = "--repo-url/--use-default-repo is only valid with --scope project"


SPECRULES_INVALID_LAYER = "Invalid specrules"


_SPECRULES_GITMODULES_PATH = re.compile(r"(?m)^\s*path\s*=\s*specrules\s*$")


_SPECRULES_SCP_URL = re.compile(r"^git@([^:/]+):(.+)$")


SRE_PROFILE_RELPATH = Path(".devkit") / "sre-profile.yaml"


SRE_OPS_AGENT_DEFAULT_RELPATHS = (
    Path(".claude") / "agents" / "sre-ops-agent.md",
    Path(".agents") / "sre-ops-agent.md",
    Path("agents") / "sre-ops-agent.md",
)


SRE_OPS_AGENT_FENCE_RE = re.compile(r"```(?:yaml|yml)[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)


SRE_MIGRATE_SOURCE_LABEL = "ops-agent-migrate"


SRE_FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "password",
        "token",
        "cookie",
        "sso",
        "secret",
        "authorization",
        "privatekey",
        "accesskey",
        "jdbcpassword",
        "credential",
    }
)


SRE_EXTENDED_CAPABILITIES = ("trace", "service_search", "hive", "docs", "bastion")


SRE_ALLOWED_ROOT_KEYS = frozenset(
    {"schemaVersion", "project", "defaults", "services", "capabilities", "inference"}
)


SRE_ALLOWED_PROJECT_KEYS = frozenset({"name", "repo"})


SRE_ALLOWED_DEFAULTS_KEYS = frozenset({"env", "branch", "lane"})


SRE_ALLOWED_SERVICE_KEYS = frozenset({"id", "name", "service", "registryKey", "environments"})


SRE_ALLOWED_ENV_KEYS = frozenset({"registry", "logs", "config", "db", "deploy", "gateway", "idgen"})


SRE_ALLOWED_REGISTRY_KEYS = frozenset({"service"})


SRE_ALLOWED_LOGS_KEYS = frozenset({"logset", "service", "requestLogPath", "errorLogPath"})


SRE_ALLOWED_CONFIG_KEYS = frozenset({"service"})


SRE_ALLOWED_DB_KEYS = frozenset({"cluster", "database", "account"})


SRE_ALLOWED_DEPLOY_KEYS = frozenset({"defaultBranch", "defaultLane"})


SRE_ALLOWED_GATEWAY_KEYS = frozenset({"templateApi", "groups"})


SRE_ALLOWED_GATEWAY_GROUP_KEYS = frozenset({"name", "id", "host", "pathPrefix"})


SRE_ALLOWED_IDGEN_KEYS = frozenset({"cluster", "platformHost", "projectId"})


SRE_ALLOWED_CAPABILITIES_KEYS = frozenset({"extended"})


SRE_ALLOWED_INFERENCE_KEYS = frozenset({"confirmed", "sources", "notes"})


SRE_SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


SRE_UNSAFE_YAML = re.compile(r"(?:^|\s)(?:&|\*)\S|!:|![A-Za-z]|<<:")


SMALL_TASK_HEADERS = [
    "任务ID",
    "描述",
    "前置依赖",
    "验收标准",
    "预计写入范围",
    "状态",
    "门禁进度",
    "计划开始",
    "计划完成",
    "预计工作量",
    "实际开始",
    "实际完成",
    "ONES任务ID",
    "ONES同步",
    "产出/备注",
]


CREATION_BUILTIN_VARIABLES = {"subtypeId", "priority", "projectId"}


CREATION_MISSING = object()


DEVKIT_REQUIREMENT_NAME_PREFIX = "[devkit] "


TASK_DESC_DETAIL_COLUMNS = (
    "前置依赖",
    "依赖",
    "规范依赖",
    "实现说明",
    "验收标准",
    "验收",
    "测试要求",
    "预计写入范围",
    "预计工作量",
)


DEVKIT_DESCRIPTION_START = "<!-- devkit:start -->"


DEVKIT_DESCRIPTION_END = "<!-- devkit:end -->"


PROGRESS_SUMMARY_LABELS = ("已完成", "当前断点", "剩余步骤")


CLARIFICATION_SCHEMA_VERSION = 1


CLARIFICATION_STATES = {"collecting", "ready-to-apply", "closed"}


DEFAULT_CLARIFICATION_CONFIG = {
    "mode": "batch",
    "apply": "after-all-answered",
}


DEFAULT_SPEC_ARCHIVE_CONFIG = {
    "contentMode": "compact-copy",
    "knowledgeBaseApproval": "ask",
    "cleanupPolicy": "ask",
    "retention": "compact",
    "inactiveDays": 30,
}


LEGACY_COMPLETION_HINTS = ("已交付", "已上线", "已完成", "已归档", "completed", "done", "archived")


SPEC_ARCHIVE_RECEIPT_SCHEMA_VERSION = 1


SPEC_ARCHIVE_RECEIPT_STATES = {
    "planned",
    "prepared",
    "knowledge-awaiting-approval",
    "knowledge-approved",
    "verified",
    "archived",
    "cleanup-awaiting-approval",
    "cleaned",
    "completed",
}


SPEC_ARCHIVE_APPROVAL_GATES = {"knowledge-base", "cleanup", "git-only-cleanup"}


# --- 流水线行常量 -------------------------------------------------------
# 内容与 spec/template/STATUS-TEMPLATE.md 公共表及 [传统流水线]/[TDD流水线] 注释块
# 逐行一致；scripts/test_devkit_flow_feature_init.py 有守卫测试，改模板必须同步改这里。
PIPELINE_TABLE_HEADER = "| 步骤 | 名称 | Agent/Skill | 依赖 | 状态 | 备注 |"


PIPELINE_TABLE_SEPARATOR = "|------|------|-------------|------|------|------|"


PIPELINE_COMMON_ROWS: list[list[str]] = [
    ["S0.5-H", "模式选择", "feature-dev-manager-agent", "-", "waiting-confirmation", "必须人工确认 TDD/传统 后才能进入 S1"],
    ["S1", "需求建立", "feature-dev-manager-agent", "S0.5-H", "pending", ""],
    ["S1.1", "PRD 理解与检查", "prd-analyzer-agent", "S1", "pending", "产出 FEATURE.md"],
    ["S2.0", "上下文分析", "context-analyzer-agent", "S1.1", "pending", ""],
    ["S2.1", "设计文档", "tech-design-architect-agent", "S1.1,S2.0", "pending", ""],
    ["S2.2", "接口文档", "tech-design-architect-agent", "S1.1,S2.0", "pending", "与S2.1同步产出"],
    ["S2.3", "类图文档", "tech-design-architect-agent", "S1.1,S2.0", "pending", "与S2.1同步产出"],
    ["S2.1-R", "设计文档审查", "design-doc-reviewer-agent", "S2.1,S2.2,S2.3", "pending", ""],
    ["S2.2-R", "接口文档审查", "apidoc-checker-agent", "S2.1,S2.2,S2.3", "pending", ""],
    ["S2.3-R", "类图文档审查", "architecture-reviewer-agent", "S2.1,S2.2,S2.3", "pending", "可选，复杂领域模型时启用"],
    ["S2-H", "方案人工确认", "feature-dev-manager-agent", "S2.1-R,S2.2-R", "pending", "必须人工确认后才能进入 S4"],
    ["S4", "任务规划", "design-to-tasks-agent", "S2-H 或 S1.1(small)", "pending", "Small 只落盘单条 T-01，不做多任务智能拆解"],
    ["S5-H", "开发执行确认", "feature-dev-manager-agent", "S4", "pending", "必须人工确认后才能进入 S5"],
    ["S5", "开发执行（每条任务重复）", "feature-dev-manager-agent", "S5-H", "pending", ""],
]


PIPELINE_MODE_ROWS: dict[str, list[list[str]]] = {
    "传统": [
        ["S5.1", "实现代码", "dev-task-executor-agent", "S5", "pending", "[传统]"],
        ["S5.1-F", "资金安全风险分析", "fund-security-analyzer", "S5.1", "pending", "[传统] 风险关闭后才能进入单元测试/Review"],
        ["S5.2", "单元测试", "unit-test-agent", "S5.1-F", "pending", "[传统] 覆盖率按生效规则阈值（Java 默认 80%）"],
        ["S5.3", "架构审查", "architecture-reviewer-agent", "S5.2", "pending", "[传统]"],
        ["S5-R", "受影响模块全量回归", "feature-dev-manager-agent", "S5,S5.3", "pending", "不带测试选择器，Small 不得豁免"],
        ["S5-E2E", "需求准出 e2e 门", "feature-dev-manager-agent", "S5-R", "pending", "devkit 只读 quality-gate.json 判门；无跨进程面须 skipped + 书面理由"],
        ["S5.7", "Feature 级综合审查", "architecture-reviewer-agent", "S5-E2E", "pending", "单位=feature 而非任务；跨切片一致性 + 整体性判断"],
    ],
    "TDD": [
        ["S5.1", "Red Phase（测试先行）", "tdd-test-agent", "S5", "pending", "[TDD] 编写失败测试 + 骨架类；已授权范围内自动推进；用户停止 / 必要输入缺失 / 适用人工 Gate 时暂停"],
        ["S5.2", "实现代码", "dev-task-executor-agent", "S5.1", "pending", "[TDD] 使测试通过"],
        ["S5.2-F", "资金安全风险分析", "fund-security-analyzer", "S5.2", "pending", "[TDD] 风险关闭后才能进入 Green/Review"],
        ["S5.3", "Green Phase（验证通过）", "tdd-test-agent", "S5.2-F", "pending", "[TDD] 运行测试确认全绿；已授权范围内自动推进；用户停止 / 必要输入缺失 / 适用人工 Gate 时暂停"],
        ["S5.4", "Coverage Analysis（覆盖率）", "tdd-test-agent", "S5.3", "pending", "[TDD] 覆盖率按生效规则阈值（Java 默认 80%），不足则补测试"],
        ["S5.5", "架构审查", "architecture-reviewer-agent", "S5.4", "pending", "[TDD]"],
        ["S5.6", "Refactor（可选）", "tdd-test-agent", "S5.5", "pending", "[TDD] 重构后测试仍需全绿"],
        ["S5-R", "受影响模块全量回归", "feature-dev-manager-agent", "S5,S5.6", "pending", "不带测试选择器，Small 不得豁免"],
        ["S5-E2E", "需求准出 e2e 门", "feature-dev-manager-agent", "S5-R", "pending", "devkit 只读 quality-gate.json 判门；无跨进程面须 skipped + 书面理由"],
        ["S5.7", "Feature 级综合审查", "architecture-reviewer-agent", "S5-E2E", "pending", "单位=feature 而非任务；跨切片一致性 + 整体性判断"],
    ],
}


FLOW_MODES = tuple(PIPELINE_MODE_ROWS)


# 016 F1-a：规模裁剪必须覆盖模式行，故 scale 表枚举 COMMON∪MODE 中该档保留的 step id。
# 补集（_scale_removed_step_ids）= (COMMON ∪ MODE[mode]) − PIPELINE_SCALE_STEPS[scale]。
# 模式行 id 取两种模式（传统/TDD）并集、保序去重，使 scale 表与具体 mode 无关：
#   - large / large-no-context 保留全部模式行 id（含 S5.7），否则 F1-a 会把 large 的模式行误裁；
#   - small 去 S5.7（D-30），但保留 S5-R 全量回归与全部配置门 id（D-16：scale 表 ⊇ CONFIG_GATED_STEPS）。
# 016 D-40：公开导出两种模式行 id 的保序并集（单表达式推导，不泄漏模块级循环变量，S5.7 P2-6）；
# pipeline_gates 在 mode 未确认时按此并集取补集，使 scale 收窄判定与 mode 头无关。
ALL_MODE_STEP_IDS: tuple[str, ...] = tuple(
    dict.fromkeys(row[0] for rows in PIPELINE_MODE_ROWS.values() for row in rows)
)
COMMON_STEP_IDS: tuple[str, ...] = tuple(row[0] for row in PIPELINE_COMMON_ROWS)

# 规模变体：small 快速路径；large 标准全流程；large-no-context 去掉上下文分析。
PIPELINE_SCALE_STEPS: dict[str, list[str]] = {
    "small": ["S0.5-H", "S1", "S1.1", "S4", "S5-H", "S5"]
    + [step_id for step_id in ALL_MODE_STEP_IDS if step_id != "S5.7"],
    "large": list(COMMON_STEP_IDS) + list(ALL_MODE_STEP_IDS),
    "large-no-context": [step_id for step_id in COMMON_STEP_IDS if step_id != "S2.0"]
    + list(ALL_MODE_STEP_IDS),
}


# 节点 → 大阶段映射（spec/008-requirement-dashboard-e2e 设计 §4.4.1 / DECISIONS D-6）。
# 单一事实源：dashboard 与复盘报告的四阶段分组渲染共同消费，禁止各自维护映射。
# 键按「需求 → 开发 → 测试 → 审查」固定渲染顺序分组排列；同一 step id 在传统/TDD
# 语义不同（如 S5.3 = 架构审查 / Green Phase），但归属同为「开发」，映射不按模式分叉。
# S5-E2E 归属「测试」（设计 §4.5.1）；其流水线行已在 PIPELINE_MODE_ROWS 两套模式中落地。
# scripts/test_devkit_flow_feature_init.py 有全覆盖守卫：新增流水线步骤必须在此归组。
PIPELINE_STEP_STAGES: dict[str, str] = {
    "S0.5-H": "需求",
    "S1": "需求",
    "S1.1": "需求",
    "S2.0": "需求",
    "S2.1": "需求",
    "S2.2": "需求",
    "S2.3": "需求",
    "S2.1-R": "需求",
    "S2.2-R": "需求",
    "S2.3-R": "需求",
    "S2-H": "需求",
    "S4": "开发",
    "S5-H": "开发",
    "S5": "开发",
    "S5.1": "开发",
    "S5.1-F": "开发",
    "S5.2": "开发",
    "S5.2-F": "开发",
    "S5.3": "开发",
    "S5.4": "开发",
    "S5.5": "开发",
    "S5.6": "开发",
    "S5-R": "测试",
    "S5-E2E": "测试",
    "S5.7": "审查",
}


# 阶段别名注册表（设计 §4.3.2 / D-3）：(step_id, mode∈{*,传统,TDD}, slug, 中文名)。
# 覆盖 PIPELINE_STEP_STAGES 全部 25 键；中文名与行常量第二列逐字一致。
# slug 规则 ^[a-z][a-z0-9-]*$；-R 双语义与 S5.3 跨模式漂移用不同 slug + 模式作用域区分。
STEP_ALIAS_ROWS: tuple[tuple[str, str, str, str], ...] = (
    ("S0.5-H", "*", "mode-select", "模式选择"),
    ("S1", "*", "requirement", "需求建立"),
    ("S1.1", "*", "prd-check", "PRD 理解与检查"),
    ("S2.0", "*", "context-analysis", "上下文分析"),
    ("S2.1", "*", "design-doc", "设计文档"),
    ("S2.2", "*", "api-doc", "接口文档"),
    ("S2.3", "*", "class-diagram", "类图文档"),
    ("S2.1-R", "*", "design-review", "设计文档审查"),
    ("S2.2-R", "*", "api-review", "接口文档审查"),
    ("S2.3-R", "*", "class-review", "类图文档审查"),
    ("S2-H", "*", "design-confirm", "方案人工确认"),
    ("S4", "*", "task-plan", "任务规划"),
    ("S5-H", "*", "dev-confirm", "开发执行确认"),
    ("S5", "*", "dev-exec", "开发执行（每条任务重复）"),
    ("S5.1", "传统", "impl", "实现代码"),
    ("S5.1-F", "传统", "fund-security", "资金安全风险分析"),
    ("S5.2", "传统", "unit-test", "单元测试"),
    ("S5.3", "传统", "arch-review", "架构审查"),
    ("S5.1", "TDD", "red-phase", "Red Phase（测试先行）"),
    ("S5.2", "TDD", "impl", "实现代码"),
    ("S5.2-F", "TDD", "fund-security", "资金安全风险分析"),
    ("S5.3", "TDD", "green-phase", "Green Phase（验证通过）"),
    ("S5.4", "TDD", "coverage", "Coverage Analysis（覆盖率）"),
    ("S5.5", "TDD", "arch-review", "架构审查"),
    ("S5.6", "TDD", "refactor", "Refactor（可选）"),
    ("S5-R", "*", "regression", "受影响模块全量回归"),
    ("S5-E2E", "*", "e2e-gate", "需求准出 e2e 门"),
    ("S5.7", "*", "feature-review", "Feature 级综合审查"),
)


FEATURE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")


SERVICE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")


SRE_INFER_SKIP_DIRS = {
    ".git",
    ".m2",
    ".worktrees",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "out",
    "target",
}


MIGRATION_CODEX_RUNTIME_PREAMBLE = """## Codex Runtime Contract

You are a Codex custom worker agent for the Devkit SDD workflow. You are spawned by the main-thread `feature-dev-manager` skill. Do not spawn additional subagents. If another role is needed, return a compact recommendation to the manager. Read only the task-relevant files, write only the artifacts owned by this worker, and return structured results with status, artifact paths, changed files, validation results, blockers, and clarification questions.
"""


RUNTIME_NAMES = ("claude-code", "codex")


CANONICAL_RUNTIME_ORDER = (
    "claude-code",
    "codex",
    "grok-build",
)


# 六端 runtime check/update/install/uninstall/rollback/recover 与 upgrade
# 全部走 canonical RuntimeLifecycleService。RUNTIME_NAMES 仅用于
# migrate legacy-workspace 扫描老三端历史生成文件。
LIFECYCLE_ONLY_RUNTIMES = ("grok-build",)


# upgrade --runtime all 时，未装宿主的端不阻断已装上的端。
# 单端升级仍 fail-closed。
_UPGRADE_SKIPPABLE_ERROR_CODES = frozenset(
    {
        "E_RUNTIME_UNAVAILABLE",
        "E_RUNTIME_OBSERVATION_FAILED",
        "E_GROK_RAW_PROJECTION_INVALID",
    }
)


# --- Kimi 插件安装（plugin install） -----------------------------------------------------
KIMI_PLUGIN_ID = RUNTIME_PLUGIN_NAME


KIMI_MANAGER_STEM = "feature-dev-manager-agent"


MAINTAINER_ONLY_SKILLS = frozenset({"devkit-release-manager"})


CONSUMER_OMITTED_SKILLS = frozenset(
    {
        "enum_design_agent",
        "object-conversion-checker",
        "work_item_splitter",
    }
)


# packages/agents/ 下的数据载荷文件：缺 agent frontmatter（name/description），
# 不作为 agent 源解析/渲染；传播由仓内 build 脚本负责（对齐 scripts/workspace_sync.py
# 的 AGENT_DATA_PAYLOAD_FILES，两侧必须保持一致）。
AGENT_DATA_PAYLOAD_FILES = frozenset({"dispatch-redlines.md"})


# 以下渲染逻辑逐行移植自 scripts/build_kimi_plugin.py（及其依赖的
# workspace_sync.parse_agent_source / build_plugin_agents 两个 helper），
# 仅把模块级路径常量改为函数参数；改动任何一处都必须保持两侧输出一致。
KIMI_WORKER_RUNTIME_CONTRACT = """## Kimi Code Worker 运行契约

你作为 Kimi 内置 `coder` 子 Agent 中加载的 Devkit worker Skill 运行。

- 只执行 manager prompt 指定的单一职责，遵守该角色的文件所有权边界。
- 不得调用 `Agent` 或 `AgentSwarm`；如需其他角色介入，返回建议给 manager。
- 直接将产物写入 manager 指定的路径，不要在返回消息中复制大段正文。
- 最终仅返回：`status`、`artifact_paths`、`changed_files`、`validation`、
  `blockers`、`clarification_questions`。

"""


KIMI_COMMAND_PREAMBLE = """The `devkit-fdm` session-start Skill is the workflow authority.
Use Kimi built-in `coder` sub-agents plus the corresponding `devkit-*` worker Skill.

"""


# --- Dashboard 渲染常量（spec/009-dashboard-revamp 接口文档 §6，devkit 侧单点定义） ---
# 浏览器自动刷新间隔，经 {{REFRESH_INTERVAL_MS}} TOKEN 注入 dashboard 模板（设计 §4.5）。
DASHBOARD_REFRESH_INTERVAL_MS = 5000


# 过程文件内联预览：单文件内联截断上限（64KB，超限截断并附「查看完整文件」提示行）。
DASHBOARD_PREVIEW_FILE_LIMIT = 65536


# 过程文件内联预览：页面内联总量上限（512KB，达到后剩余文件只渲染清单行 + 链接）。
DASHBOARD_PREVIEW_TOTAL_LIMIT = 524288


# 可内联预览的文件扩展白名单；其余类型只渲染清单行 + 相对链接。
DASHBOARD_PREVIEW_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml"}


# 过程文件清单固定优先序（设计 §4.6）：按元组顺序置顶，未命中项按文件名字典序追加。
# 以 "[" 开头的三件套条目（[设计文档]/[接口文档]/[类图]）按文件名前缀匹配，其余为精确文件名。
DASHBOARD_FILE_PRIORITY = (
    "FEATURE.md",
    "context-report.md",
    "[设计文档]",
    "[接口文档]",
    "[类图]",
    "plan.md",
    "task.md",
    "STATUS.md",
    "DECISIONS.md",
    "CHANGELOG.md",
)


# 侧边栏文档分组规则（设计 §4.6.2）：(组名, 文件名前缀匹配规则元组)，按元组顺序渲染分组。
# 匹配规则为文件名前缀匹配（如 "review-" 命中全部审查报告，"[设计文档]" 命中带后缀的三件套文件名）。
# 末组「其他」匹配元组为空，作为未命中项兜底；组内无文件时组标题不渲染。
DASHBOARD_DOC_GROUPS = (
    (
        "需求与设计",
        (
            "FEATURE.md",
            "context-report.md",
            "[设计文档]",
            "[接口文档]",
            "[类图]",
            "source-request.md",
            "kb-query-results.md",
        ),
    ),
    ("规划与状态", ("plan.md", "task.md", "STATUS.md")),
    ("审查报告", ("review-",)),
    ("决策与变更", ("DECISIONS.md", "CHANGELOG.md")),
    ("其他", ()),
)


# 宽表卡片化阈值（接口文档 §8.1）：render_markdown_subset(wide_table_cards=True) 时，
# 表格任一单元格超过该字符数则整表改渲染 .recs 记录卡列表，窄表维持 <table>。
DASHBOARD_TABLE_CARD_THRESHOLD = 100


# 步骤时间戳旁车文件名（接口文档 §7）：位于 spec/00x-{feature}/ 下；"." 前缀使其被
# 过程文件枚举天然排除。写入方仅 flow.py record_step_timing，读取方 dashboard_step_timings。
DASHBOARD_STEP_TIMINGS_FILE = ".step-timings.json"


# 旁车文件 schema 版本（接口文档 §7）：读取方遇到不识别版本按空数据降级。
STEP_TIMINGS_SCHEMA_VERSION = 1


# --- usage-telemetry（spec/011-usage-telemetry，设计 §4.1.1 / §4.1.4 / §4.5 / §4.6） ---

# 014 D-6：envelope 新增 trace 段命中 schema 自述递增规则「改 envelope 结构」，1 → 2。
TELEMETRY_SCHEMA_VERSION = 2


TELEMETRY_DEFAULT_ENDPOINT = ""


TELEMETRY_OUTBOX_MAX_LINES = 10000


TELEMETRY_OUTBOX_MAX_BYTES = 5 * 1024 * 1024


TELEMETRY_OUTBOX_RELATIVE_PATH = Path(".config") / "devkit" / "telemetry-outbox.jsonl"


# 012 D-10：后台单例 flusher 的全局互斥锁（flock），保证任一时刻只有一个后台 flush 进程。
TELEMETRY_FLUSH_LOCK_RELATIVE_PATH = Path(".config") / "devkit" / "telemetry-flush.lock"


TELEMETRY_MACHINE_ID_RELATIVE_PATH = Path(".config") / "devkit" / "machine-id"


TELEMETRY_SPAN_CURSOR_RELATIVE_PATH = Path(".config") / "devkit" / "telemetry-span-cursor.json"


# Telemetry identity is disabled in this build.
TELEMETRY_SSO_CLIENT_ID = ""


TELEMETRY_SSO_ACCESS_ENV = ""


TELEMETRY_SSO_WHOAMI_URLS: dict[str, str] = {}


TELEMETRY_SSO_STORAGE_RELATIVE_PATH = Path(".config") / "devkit" / "identity"


TELEMETRY_SSO_IDENTITY_CACHE_RELATIVE_PATH = Path(".config") / "devkit" / "sso-identity.json"


TELEMETRY_SSO_CLI = ""


TELEMETRY_SSO_INSTALL_COMMAND: list[str] = []


TELEMETRY_SSO_TOKEN_HEADER = ""


# --- pr-review（017 T-02）：帮助文案、目录、默认值、标记正则、动作全集、PRV 错误码 ---
# 目录与默认值以设计 §4.3.1 / §4.3.2 与接口 §5 / §8 为准。标记正则逐字来自接口 §5，
# 尾部 `(?P<rest>(?: \S+)*)$` 为 DR-58；#303 别名同样收尾，使未知词可忽略。

PR_REVIEW_HELP = "Review pull requests, track feature PRs, and run review jobs. Default off."


PR_REVIEW_INVALID_LAYER = "Invalid prReview"


PR_REVIEW_PROJECT_DIR = ".devkit/review"


PR_REVIEW_HOME_DIR = "~/.config/devkit/review"


PR_REVIEW_REPOS_FILENAME = "repos.json"


PR_REVIEW_DEVICES_FILENAME = "devices.json"


PR_REVIEW_AUTOMATION_FILENAME = "automation.json"


PR_REVIEW_OWNERS_FILENAME = "owners.yaml"


PR_REVIEW_REQUIREMENTS_FILENAME = "requirements.md"


PR_REVIEW_SUBDIRS = (
    "ledger",
    "worktrees",
    "cache",
    "reports",
    "track",
    "runs",
    "state",
    "playbooks",
    "logs",
)


PR_REVIEW_DEFAULT_ENABLED = False


PR_REVIEW_DEFAULT_AUTO_RUN = False


PR_REVIEW_DEFAULT_INTERVAL = 120


PR_REVIEW_INTERVAL_MIN = 60


PR_REVIEW_INTERVAL_MAX = 3600


PR_REVIEW_DEFAULT_HEADLESS_REVIEW = False


PR_REVIEW_DEFAULT_MAX_PROBE_PER_ROUND = 50


PR_REVIEW_MAX_PROBE_PER_ROUND_MIN = 1


PR_REVIEW_MAX_PROBE_PER_ROUND_MAX = 200


PR_REVIEW_DEFAULT_LEASE_TTL_HOURS = 12


# D-29：live / push 档未确认送达超过该分钟数则降级并换通道重推一次。
PR_REVIEW_DEFAULT_DELIVERY_ALERT_MINUTES = 5


PR_REVIEW_DELIVERY_ALERT_MINUTES_MIN = 1


PR_REVIEW_DELIVERY_ALERT_MINUTES_MAX = 120


PR_REVIEW_LEASE_TTL_HOURS_MIN = 1


PR_REVIEW_LEASE_TTL_HOURS_MAX = 168


PR_REVIEW_DEFAULT_MAX_PARALLEL = 2


PR_REVIEW_MAX_PARALLEL_MIN = 1


PR_REVIEW_MAX_PARALLEL_MAX = 8


PR_REVIEW_DEFAULT_LIST_LIMIT = 200


PR_REVIEW_LIST_LIMIT_MIN = 30


PR_REVIEW_LIST_LIMIT_MAX = 500


PR_REVIEW_DEFAULT_ROLES = ("author", "reviewer")


PR_REVIEW_DEFAULT_REQUIREMENTS_FILE = ".devkit/review/requirements.md"


PR_REVIEW_DEFAULT_MERGE_METHOD = "merge"


PR_REVIEW_MERGE_METHODS = ("merge", "squash", "rebase")


PR_REVIEW_DEFAULT_NOTIFY_ENABLED = True


PR_REVIEW_DEFAULT_NOTIFY_EVENTS = (
    "fail",
    "merged",
    "hold",
    "job_result",
    "escalated",
    "cleanup_pending",
    "auth_expired",
)


PR_REVIEW_DEFAULT_REPLY_MODE = "top-level"


PR_REVIEW_REPLY_MODES = ("top-level", "rest")


PR_REVIEW_DEFAULT_PLATFORM_HOST = ""


PR_REVIEW_RUNTIMES = ("claude", "codex", "kimi", "grok", "pi")


PR_REVIEW_DEFAULT_TRACK_ENABLED = True


PR_REVIEW_DEFAULT_TRACK_AUTO_CLAIM = True


PR_REVIEW_DEFAULT_TRACK_AUTO_FIX = False


PR_REVIEW_DEFAULT_TRACK_REMIND_DAYS = 7


# 与 specArchive.inactiveDays 数值对齐、常量不共用（DR-17）。
PR_REVIEW_STALE_DAYS = 30


PR_REVIEW_DEFAULT_TRACK_STALE_DAYS = PR_REVIEW_STALE_DAYS


PR_REVIEW_DEFAULT_MAX_FIX_PER_FINDING = 1


PR_REVIEW_DEFAULT_AUTOMATION_PROVIDER = "auto"


PR_REVIEW_AUTOMATION_PROVIDERS = ("auto", "orca", "launchd", "schtasks", "manual")


PR_REVIEW_ACTION_KEYS = ("comment", "approve", "selfReview", "runTests", "createPr")


# selfReview 是候选过滤，不进入动作全集（DR-10 / DR-37）。
ACTION_UNIVERSE = frozenset({"comment", "approve", "run_tests", "create_pr"})


PR_REVIEW_ACTION_IDS = ("comment", "approve", "run_tests", "create_pr")


PR_REVIEW_LEASE_TTL_SEC = 3600


# 证据保留期与 launchd 日志截断（DR-92 / DR-94）。日志阈值不是配置键。
PR_REVIEW_EVIDENCE_DAYS_DEFAULT = 14


PR_REVIEW_EVIDENCE_DAYS_MAX = 365


PR_REVIEW_LOG_MAX_BYTES = 5 * 1024 * 1024


PR_REVIEW_LOG_KEEP_BYTES = 1 * 1024 * 1024


PR_REVIEW_LOG_ALIGN_BYTES = 64 * 1024


PR_REVIEW_DEVICE_GLOBAL_ONLY = "prReview.device is global-only and was ignored"


PR_REVIEW_AUTOMATION_GLOBAL_ONLY = "prReview.automation is global-only and was ignored"


PR_REVIEW_PLATFORM_HOST_GLOBAL_ONLY = "prReview.platform.host is global-only and was ignored"


PR_REVIEW_RETENTION_GLOBAL_ONLY = "prReview.retention is global-only and was ignored"


PR_REVIEW_INTERVAL_GLOBAL_ONLY = "prReview.interval is global-only"


PR_REVIEW_WATCH_GLOBAL_ONLY = "prReview.watch is global-only and was ignored"


PR_REVIEW_DOCTOR_WINDOWS_PRIMARY = "Windows 设备为 primary，请确认 macmini 已设 primary=false"


PR_REVIEW_DOCTOR_HTTP_INFO = "REST 走 http（内网）"


PR_REVIEW_REPO_SOURCES = ("setup", "manual")


# (code, meaning, typical exit). PRV-007 拒绝路径为 2，仅记录路径为 0（接口 §3）。
# PRV-032 只记录、退出码 0。PRV-034 单目标命令为 1；tick 只记 errors[]、进程退出码 0。
PR_REVIEW_ERROR_CODES: tuple[tuple[str, str, int], ...] = (
    ("PRV-001", "prReview 配置无效 / scope 不允许 / 凭证键", 1),
    ("PRV-002", "code-cli 不可用或未登录", 1),
    ("PRV-003", "无法解析 project/repo", 2),
    ("PRV-004", "repos.json / 台账 / 跟踪记录损坏", 1),
    ("PRV-005", "运行锁被持有 / 台账 BUSY", 4),
    ("PRV-006", "worktree 创建失败", 1),
    ("PRV-007", "worktree 清理失败或 cleanup 拒绝", 2),
    ("PRV-008", "headless runtime 不可用", 1),
    ("PRV-009", "合并执行失败", 1),
    ("PRV-010", "act 越权", 2),
    ("PRV-011", "push 硬闸拒绝", 2),
    ("PRV-012", "feature 未完成 / 分支不匹配 / PR 非 open", 2),
    ("PRV-013", "owners.yaml 校验失败", 1),
    ("PRV-014", "playbook frontmatter 校验失败", 1),
    ("PRV-015", "[devkit-job] 发送者不可信（记录并忽略）", 0),
    ("PRV-016", "requireIssueAssociated 且分支无关联 issue", 2),
    ("PRV-017", "平台限流 / 连续失败本轮终止", 1),
    ("PRV-018", "review.json / fix-result.json schema 校验失败", 2),
    ("PRV-019", "自动化前置条件不满足", 2),
    ("PRV-020", "台账 finish / checkpoint 条件不满足", 2),
    ("PRV-021", "台账 owner 不匹配或记录已 release", 2),
    ("PRV-022", "外部命令超时", 124),
    ("PRV-023", "外部命令非零退出且未被模块映射", 1),
    ("PRV-024", "device-mention 作业 source 与 PR sourceSha 不一致", 0),
    ("PRV-025", "运维自清理 I/O 失败（证据目录删除 / 日志截断）", 0),
    ("PRV-026", "无法识别当前会话（非六端、拿不到 sessionId、显式身份不在会话索引或已结束）", 2),
    ("PRV-027", "监听目标被其他会话持有（未 --takeover）", 2),
    ("PRV-028", "要注销的监听不存在", 2),
    ("PRV-029", "监听参数非法（车道与范围不匹配、仓库不在 repos.json、stop 选择器个数不为 1、--all 缺 --yes）", 2),
    ("PRV-030", "系统定时器安装 / 卸载失败（CLI 内联收敛时安装失败则登记回滚；watch converge 失败则标志保留；tick 只记录派生失败到 errors[]）", 1),
    ("PRV-031", "MCode 登录失效（pat get 失败、REST 401、auth status 未登录）", 1),
    ("PRV-032", "原生通道送达失败（只记录，事件留队列转下一轮注入）", 0),
    ("PRV-033", "当前平台无可用系统定时器（非 darwin / nt）", 2),
    ("PRV-034", "平台权限不足（REST 403）：只影响该仓 / 该 PR，唤醒持有该目标的会话（access_denied），不暂停其他监听、不尝试重新登录", 1),
)


PR_REVIEW_ERROR_EXIT = {code: exit_code for code, _meaning, exit_code in PR_REVIEW_ERROR_CODES}


_PR_REVIEW_REST = r"(?P<rest>(?: \S+)*)$"


PR_REVIEW_MARKER_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "review",
        r"^\[devkit-review\] #(?P<pr>\d+) sha=(?P<sha>[0-9a-f]{40}) "
        r"verdict=(?P<verdict>PASS|FAIL|INCOMPLETE) round=(?P<round>\d+)"
        r"(?: prev=(?P<prev>[0-9a-f]{40}))?(?P<self> self-reviewed)?" + _PR_REVIEW_REST,
    ),
    (
        "fixed-pushed",
        r"^\[devkit-review\] fixed=(?P<ids>[BS]\d+(?:,[BS]\d+)*) "
        r"sha=(?P<sha>[0-9a-f]{40}) status=pushed" + _PR_REVIEW_REST,
    ),
    (
        "fixed-verified",
        r"^\[devkit-review\] fixed=(?P<id>[BS]\d+) sha=(?P<sha>[0-9a-f]{40}) ✅ 已验证"
        + _PR_REVIEW_REST,
    ),
    (
        "merged",
        r"^\[devkit-review\] merged #(?P<pr>\d+) → (?P<target>\S+) "
        r"merge=(?P<hash>[0-9a-f]{7,40})" + _PR_REVIEW_REST,
    ),
    (
        "hold",
        r"^\[devkit-review\] hold #(?P<pr>\d+)：(?P<reasons>.+)$",
    ),
    (
        "job-verified",
        r"^\[devkit-review\] job-verified playbook=(?P<playbook>[a-z0-9-]+) "
        r"kind=(?P<kind>[a-z0-9-]+) qa=#(?P<qa>\d+) source=(?P<source>[0-9a-f]{40}) "
        r"verdict=(?P<verdict>PASS|FAIL|BLOCKED)" + _PR_REVIEW_REST,
    ),
    (
        "job-artifact",
        r"^\[devkit-job\] artifact origin=#(?P<origin>\d+) "
        r"playbook=(?P<playbook>[a-z0-9-]+) source=(?P<source>[0-9a-f]{40})"
        + _PR_REVIEW_REST,
    ),
    (
        "job-superseded",
        r"^\[devkit-job\] superseded by=#(?P<by>\d+) source=(?P<source>[0-9a-f]{40})"
        + _PR_REVIEW_REST,
    ),
    (
        "job-post",
        r"^\[devkit-job\] kind=(?P<kind>[a-z0-9-]+) to=@(?P<to>[a-z0-9-]+) "
        r"id=(?P<id>[A-Za-z0-9-]+) source=(?P<sha>[0-9a-f]{40})" + _PR_REVIEW_REST,
    ),
    (
        "job-ack",
        r"^\[devkit-job\] ack id=(?P<id>\S+) device=(?P<device>\S+) "
        r"machine=(?P<machine>\S+) status=(?P<status>QUEUED|RUNNING)" + _PR_REVIEW_REST,
    ),
    (
        "job-log",
        r"^\[devkit-job\] log id=(?P<id>\S+) n=(?P<n>\d+)/(?P<total>\d+)" + _PR_REVIEW_REST,
    ),
    (
        "job-result",
        r"^\[devkit-job\] result id=(?P<id>\S+) verdict=(?P<verdict>PASS|FAIL|BLOCKED)"
        + _PR_REVIEW_REST,
    ),
    (
        "job-ack-alias",
        r"^(?P<alias>QA_ACK) jobId=(?P<id>\S+) machine=(?P<machine>\S+) "
        r"reporterMis=(?P<mis>\S+) status=(?P<status>\S+)" + _PR_REVIEW_REST,
    ),
    (
        "job-result-alias",
        r"^(?P<alias>ORDERKIT_WINDOWS_QA_RESULT)\b" + _PR_REVIEW_REST,
    ),
)


PR_REVIEW_MARKER_REGEX = tuple(
    (name, re.compile(pattern, re.ASCII | re.MULTILINE))
    for name, pattern in PR_REVIEW_MARKER_PATTERNS
)


# --- pr-review watch（018 T-02）：加性常量。不改既有默认值，不改既有首行正则。 ---
# 路径分两类锚点：
# - PR_REVIEW_WATCH_*_REL、日志、legacy 相对 ~/.config/devkit/review（PR_REVIEW_HOME_DIR）
# - DEVKIT_SESSION_* / DEVKIT_CLAUDE_INBOX_* 相对 ~/.config/devkit
# 本模块没有平台分支；darwin / nt 只作为字典键。

PR_REVIEW_WATCH_REGISTRY_REL = "watch/registry.json"


PR_REVIEW_WATCH_QUEUES_REL = "watch/queues"


PR_REVIEW_WATCH_AUTH_REL = "watch/auth.json"


PR_REVIEW_WATCH_LAST_TICK_REL = "watch/last-tick.json"


PR_REVIEW_WATCH_TICK_LOCK_REL = "watch/tick.lock"


PR_REVIEW_WATCH_CONVERGE_LOCK_REL = "watch/converge.lock"


PR_REVIEW_WATCH_REGISTRY_LOCK_REL = "watch/registry.lock"


PR_REVIEW_WATCH_TICK_HIDDEN_VBS_REL = "watch/tick-hidden.vbs"


PR_REVIEW_WATCH_TICK_LOG_REL = "logs/watch-tick.log"


PR_REVIEW_WATCH_CONVERGE_LOG_REL = "logs/watch-converge.log"


PR_REVIEW_WATCH_LEGACY_CLEANUP_REL = "state/legacy-scheduler-cleanup.json"


DEVKIT_SESSION_INDEX_REL = "session-index.json"


DEVKIT_SESSION_INDEX_LOCK_REL = "session-index.lock"


DEVKIT_CLAUDE_INBOX_TEMPLATE = "sessions/claude-code/<sidKey>.inbox.json"


# DR-46 全局锁序 0→4。队列锁的文件名是 <sidKey>.lock，不是固定文件名。
PR_REVIEW_WATCH_LOCK_NAMES = (
    "converge.lock",
    "tick.lock",
    "registry.lock",
    "<sidKey>.lock",
    "session-index.lock",
)


# sidKey：sessionId 中 [A-Za-z0-9._-] 以外替换为 _，超长截断后加 sha1 前 8 位（DR-10）。
PR_REVIEW_WATCH_SID_KEY_MAX_LEN = 80


PR_REVIEW_WATCH_SID_KEY_SHA1_LEN = 8


PR_REVIEW_WATCH_LAUNCHD_LABEL = "com.devkit.pr-review-watch"


PR_REVIEW_WATCH_TASK_NAME = "DevkitDevkitPrReviewWatch"


PR_REVIEW_WATCH_TIMER_PERIOD_SEC = 60


# E8 未通过的平台由 T-37 把对应值改为 False（D-20 ⑥）。键是数据，不是分支。
PR_REVIEW_WATCH_DETACHED_CONVERGE = {"darwin": True, "nt": True}


PR_REVIEW_WATCH_MAX_PAGES = 20


PR_REVIEW_WATCH_SEEN_IDS_MAX = 5000


PR_REVIEW_WATCH_QUEUE_PENDING_MAX = 1000


PR_REVIEW_WATCH_QUEUE_DELIVERED_KEEP = 200


PR_REVIEW_WATCH_SEEN_EVENT_KEYS_MAX = 2000


PR_REVIEW_WATCH_IN_FLIGHT_SEC = 15


PR_REVIEW_WATCH_PUSH_FAILURE_THRESHOLD = 3


PR_REVIEW_WATCH_TAIL_POLL_SEC = 3


PR_REVIEW_WATCH_HEARTBEAT_SEC = 60


# D-29：心跳文件超过 3 个间隔视为实时通道断开。
PR_REVIEW_WATCH_HEARTBEAT_MISS_FACTOR = 3


PR_REVIEW_WATCH_HEARTBEATS_REL = "watch/heartbeats"


# D-28：结束快照与未完成的 session-end 保留期 = leaseTtl × 本倍数。
# 默认 12 小时 × 14 = 7 天，与会话索引里结束标记的清理窗口同量级。
PR_REVIEW_WATCH_END_RETENTION_MULTIPLIER = 14


PR_REVIEW_WATCH_ENDED_SNAPSHOTS_REL = "watch/ended-snapshots.json"


PR_REVIEW_WATCH_PENDING_RESTORES_REL = "watch/pending-restores.json"


PR_REVIEW_WATCH_TAIL_FRESHNESS_SEC = 150


PR_REVIEW_WATCH_CLI_TICK_LOCK_WAIT_SEC = 30


PR_REVIEW_WATCH_HOOK_LOCK_TIMEOUT_MS = 200


PR_REVIEW_WATCH_REMIND_SEC = 4 * 60 * 60


PR_REVIEW_WATCH_MOA_TIMEOUT_SEC = 45


PR_REVIEW_WATCH_MIS_CACHE_SEC = 3600


SESSION_LEASE_DEFAULT_SEC = 12 * 60 * 60


PR_REVIEW_WATCH_INJECT_MAX_CHARS = 2000


# 推送通道完整形态上限（接口 §4.3）；下一轮注入用 INJECT_MAX_CHARS。
PR_REVIEW_WATCH_PUSH_MAX_CHARS = 4000


PR_REVIEW_WATCH_EXCERPT_MAX_CHARS = 280


PR_REVIEW_WATCH_TITLE_MAX_CHARS = 120


PR_REVIEW_WATCH_BRANCH_MAX_CHARS = 120


PR_REVIEW_WATCH_DISPLAY_NAME_MAX_CHARS = 120


PR_REVIEW_WATCH_PATH_MAX_CHARS = 240


PR_REVIEW_WATCH_GAP_PREVIEW_MAX = 5


PR_REVIEW_WATCH_GAP_PR_MAX = 10


PR_REVIEW_WATCH_BACKLOG_MAX = 20


PR_REVIEW_WATCH_OUTBOX_MAX = 500


PR_REVIEW_WATCH_LOG_MAX_BYTES = PR_REVIEW_LOG_MAX_BYTES


PR_REVIEW_WATCH_LOG_KEEP_BYTES = PR_REVIEW_LOG_KEEP_BYTES


PR_REVIEW_WATCH_PARENT_CHAIN_DEPTH = 8


PR_REVIEW_WATCH_EVENT_KINDS = (
    "approval",
    "review_comment",
    "job_result",
    "review_requested",
    "mentioned",
    "new_commit",
    "rereview_requested",
    "author_reply",
    "pr_closed",
    "watch_taken_over",
    "watch_restored",
    "gap_summary",
    "review_backlog",
    "auth_expired",
    "auth_recovered",
    "access_denied",
    "timer_failed",
)


# 系统通知留在登记时的会话实例；其余 kind 按当前归属改投（DR-53）。
PR_REVIEW_WATCH_SYSTEM_KINDS = (
    "auth_expired",
    "auth_recovered",
    "access_denied",
    "timer_failed",
    "watch_taken_over",
    "watch_restored",
)


PR_REVIEW_WATCH_LANES = ("author", "review", "both")


PR_REVIEW_WATCH_WAKE_LEVELS = ("live-pull", "push", "next-turn")


# DR-6：环境变量 → runtime。同时存在多个时由调用方按父进程链取最近祖先。
PR_REVIEW_WATCH_SESSION_ENV_VARS = (
    ("claude-code", "CLAUDE_CODE_SESSION_ID"),
    ("codex", "CODEX_THREAD_ID"),
    ("kimi-code", "KIMI_SESSION_ID"),
    ("pi", "PI_SESSION_ID"),
    ("grok-build", "GROK_SESSION_ID"),
    ("antigravity", "ANTIGRAVITY_CONVERSATION_ID"),
)


PR_REVIEW_WATCH_RUNTIME_EXECUTABLES = (
    ("claude-code", ("claude",)),
    ("codex", ("codex",)),
    ("kimi-code", ("kimi",)),
    ("grok-build", ("grok",)),
    ("pi", ("pi", "node")),
    ("antigravity", ("agy", "antigravity")),
)


PR_REVIEW_WATCH_CLAUDE_PID_ENV = "CLAUDE_PID"


PR_REVIEW_WATCH_CLAUDE_CHILD_SESSION_ENV = "CLAUDE_CODE_CHILD_SESSION"


# 接口 §5.1。只认最后一个命中行；不进入 PR_REVIEW_MARKER_PATTERNS。
PR_REVIEW_META_LINE_PATTERN = r"^\[devkit-meta\](?P<fields>(?: [A-Za-z]+=\S+)+)\s*$"


PR_REVIEW_META_LINE_REGEX = re.compile(PR_REVIEW_META_LINE_PATTERN, re.ASCII | re.MULTILINE)


# 接口 §5.2。既有首行正则保持原样，reply 单独编译。
PR_REVIEW_REPLY_MARKER_PATTERN = (
    r"^\[devkit-review\] reply #(?P<pr>\d+) to=(?P<to>\d+|-)" + _PR_REVIEW_REST
)


PR_REVIEW_REPLY_MARKER_REGEX = re.compile(PR_REVIEW_REPLY_MARKER_PATTERN, re.ASCII | re.MULTILINE)
