from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from devkit_flow.constants import FEEDBACK_REPORTER_FIELD_NAME, FEEDBACK_SUBTYPE_NAME


@dataclass(frozen=True)
class ConfigResolution:
    effective: dict[str, Any]
    global_config: dict[str, Any]
    project_config: dict[str, Any]
    global_exists: bool
    project_exists: bool
    source: str


@dataclass(frozen=True)
class WorkflowRuntimeSwitch:
    runtime: str
    enabled: bool
    source: str
    default_enabled: bool = True


@dataclass(frozen=True)
class AutoReviewSwitch:
    enabled: bool
    source: str
    default_enabled: bool = True


@dataclass(frozen=True)
class GateDecision:
    outcome: str
    skip_reason: str = ""
    error_code: str = ""


@dataclass(frozen=True)
class FeedbackIdentity:
    reporter: str = ""
    available: bool = False
    source: str = ""


@dataclass
class FeedbackSchema:
    subtype_id: str
    subtype_name: str = FEEDBACK_SUBTYPE_NAME
    reporter_variable: str = ""
    reporter_field_name: str = FEEDBACK_REPORTER_FIELD_NAME
    reporter_id: Any = None
    reporter_type: str = "component_user"
    reporter_multiple: bool = True


@dataclass(frozen=True)
class TakeoverTrigger:
    trigger_kind: str
    raw_text: str = ""


@dataclass(frozen=True)
class TakeoverDecision:
    outcome: str
    trigger_kind: str
    enabled: bool
    source: str
    reason: str
    user_message: str = ""


@dataclass(frozen=True)
class DocsCuratorSwitch:
    enabled: bool
    source: str
    default_enabled: bool = True


@dataclass(frozen=True)
class DashboardSwitch:
    enabled: bool
    source: str
    default_enabled: bool = True


@dataclass(frozen=True)
class HtmlUiSwitch:
    enabled: bool
    source: str
    default_enabled: bool = False


@dataclass(frozen=True)
class ProgressBarSwitch:
    enabled: bool
    source: str
    default_enabled: bool = True


@dataclass(frozen=True)
class SpecArtifactsSwitch:
    enabled: bool
    source: str
    default_enabled: bool = True
    scope_glob: str = "spec/{NNN}-*"


@dataclass(frozen=True)
class UserStopHookEntry:
    command: str
    kind: str


@dataclass(frozen=True)
class UserStopHookScan:
    user_hooks_path: str
    plugin_like_count: int
    third_party_count: int
    file_modified: bool
    check_status: str
    message: str
    fix: str = ""
    entries: tuple[UserStopHookEntry, ...] = ()


@dataclass(frozen=True)
class SpecrulesSwitch:
    enabled: bool = False
    source: str = "none"
    default_enabled: bool = False


@dataclass(frozen=True)
class SpecrulesResolution:
    enabled: bool = False
    source: str = "none"
    default_enabled: bool = False
    effective_state: str = "disabled"
    repo_url: str | None = None
    repo_url_source: str = "none"
    configured_repo_url: str | None = None
    illegal: bool = False
    illegal_reason: str | None = None
    ignored_global_repo_url: bool = False
    workspace_tree: str = "none"
    cleanup_needed: bool = False


@dataclass(frozen=True)
class GitUrl:
    raw: str = ""
    normalized: str = ""
    scheme: str = "file"


@dataclass(frozen=True)
class ReviewRoundsConfig:
    max_rounds: int
    source: str
    default_max_rounds: int = 10


@dataclass(frozen=True)
class DesignTemplatePointer:
    pointer: str | None
    kind: str
    source: str


@dataclass(frozen=True)
class TemplateProbeResult:
    available: bool
    method: str
    detail: str = ""


@dataclass(frozen=True)
class DesignTemplateConfig:
    configured: bool
    pointer: str | None
    kind: str  # none | markdown | km
    source: str  # default | global | project
    resolved_path: str | None


@dataclass(frozen=True)
class DesignTemplateResolution:
    configured: bool
    pointer: str | None
    kind: str
    source: str
    resolved_path: str | None
    template_probe: TemplateProbeResult
    effective_mode: str
    effective_pointer: str
    bundle_outline: str
    fallback_reason: str | None
    content_status: str
    content_checked_at: str | None = None
    content_source: str = "live"  # cache | live | unknown


@dataclass(frozen=True)
class CuratorSkipDecision:
    skipped: bool
    reason: str
    source: str
    operation: str


@dataclass(frozen=True)
class SreIntent:
    raw_text: str
    matched: bool
    matched_term: str | None
    exclude_reason: str | None


@dataclass(frozen=True)
class SreRouteDecision:
    outcome: str
    entry_kind: str
    reason: str = ""


@dataclass(frozen=True)
class E2ESourceConfig:
    """e2e 源有效配置（接口文档 §5；项目层按根键整体覆盖全局层后的解析结果）。"""

    schema_version: int
    source_type: str
    path: str
    gate_file: str
    source: str  # global | project


@dataclass(frozen=True)
class QualityGateReport:
    """quality-gate.json 门契约文件（接口文档 §6）；外部执行方产出，devkit 只读。"""

    schema_version: int
    run_id: str
    feature: str
    result: str
    started_at: str
    finished_at: str
    summary: dict[str, Any]
    failures: tuple[dict[str, Any], ...]
    report_url: str


@dataclass(frozen=True)
class E2EGateVerdict:
    """e2e 门判定（接口文档 §6 真值表）：verdict 值域仅 pass | fail | blocked 三值；
    未配置 = blocked + reason=not-configured，不引入第四态。report 仅 gate 文件
    合法加载后存在（blocked 时可为 None）。"""

    verdict: str
    reason: str = ""
    failures: tuple[dict[str, Any], ...] = ()
    report_url: str = ""
    report: QualityGateReport | None = None


@dataclass(frozen=True)
class FeatureSnapshot:
    """过程态快照（设计 §4.3.2 共享采集层产物；dashboard.py 与 report.py 共同消费）。

    全部字段来自 feature 目录既有结构化文件的只读采集；e2e_gate 为 W3/T11 接入的
    E2EGateVerdict（类图 §5 中 QualityGateReport 经 verdict.report 嵌入；采集失败
    降级为 blocked + reason，不阻断快照，与 D-5 fail-soft 同精神）。
    config_gates 为配置门快照（设计 §4.2.5）；类型为 PipelineConfigSnapshot | None，
    此处用 Any 避免 models → pipeline_gates 拓扑边。默认空（None）。
    """

    feature: dict[str, Any]
    title: str
    pipeline: list[dict[str, str]]
    design_tasks: list[dict[str, str]]
    dev_tasks: list[dict[str, str]]
    progress: dict[str, str]
    breakpoint: dict[str, Any] | None
    next_action: str
    dependencies_satisfied: bool
    dependencies: list[dict[str, Any]]
    waiting_on: list[str]
    blockers: dict[str, Any]
    clarification: dict[str, Any]
    ones_outbox: dict[str, int] | None
    ones_space_id: str
    e2e_gate: E2EGateVerdict | None = None
    config_gates: Any | None = None


@dataclass(frozen=True)
class ReviewGateTiming:
    """Effective review timing and immutable diagnostics for the raw config layers."""

    timing: str
    source: str
    default_timing: str = "dev-once"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PrReviewSwitch:
    """``prReview.enabled`` 摘要。全量生效值在 ``pr_review_config.PrReviewConfig``。"""

    enabled: bool
    source: str
    default_enabled: bool = False
