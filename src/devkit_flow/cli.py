from __future__ import annotations

from pathlib import Path
from typing import Any
from devkit_flow.cliargs import parse_args
from devkit_flow.config import command_review_gate_configure, command_review_gate_status, command_context_source_configure, command_context_source_status, command_quality_gate_configure, ones_config_summary, ones_follow_up_sync
from devkit_flow.e2e import command_e2e_configure, command_e2e_status, evaluate_e2e_gate
from devkit_flow.errors import DevkitFlowError
from devkit_flow.flow import command_feature_init, command_feature_next_id, command_flow_accept_risk, command_flow_archive, command_flow_complete, command_flow_confirm, command_flow_migrate_steps, command_flow_mode, command_flow_next, command_flow_rollback, command_flow_set, command_flow_task_ensure_small, command_flow_task_set, command_flow_verify, command_template
from devkit_flow.flow_batch import command_flow_batch_set
from devkit_flow.lint import command_lint
from devkit_flow.parse_util import parse_status_file, status_path_for_feature
from devkit_flow.project_detect import command_project_detect
from devkit_flow.rule_sources import command_project_rules
from devkit_flow.review import command_design_template_configure, command_design_template_status, command_review_rounds_check, command_review_rounds_configure, command_review_rounds_reset, command_review_rounds_status, command_review_rounds_tick
from devkit_flow.review_result import command_review_result_verify
from devkit_flow.session_binding import is_takeover, record_takeover
from devkit_flow.session_registry import command_sessions_list, latest_marker
from devkit_flow.setup import command_doctor, command_setup
from devkit_flow.status_cmd import command_status
from devkit_flow.switches import command_dashboard_configure, command_dashboard_status, command_docs_curator_configure, command_docs_curator_status, command_html_ui_configure, command_html_ui_status, command_progress_bar_configure, command_progress_bar_status, command_spec_artifacts_configure, command_spec_artifacts_status, command_specrules_catalog_configure, command_specrules_catalog_status, command_specrules_configure, command_specrules_refresh, command_specrules_status
from devkit_flow.textio import compact_error, emit
from devkit_flow.workflow_auto import command_workflow_configure, command_workflow_status


_INTENT_EVENT_TYPES = frozenset(
    {"ones_follow_up", "write_workflow_report", "open_local_html_report", "quality_collect"}
)


def _takeover_for(command: str, *, require_branch_match: bool = False):
    """017：flow next 接手记录闭包（D-2 分支门控在 record_takeover 内）；吞掉一切异常。"""
    def _record(workspace: Path, feature: dict[str, Any]) -> None:
        try:
            record_takeover(workspace, feature, command=command, require_branch_match=require_branch_match)
        except Exception:  # noqa: BLE001
            pass

    return _record


def _side_effects_for(args: Any, *, command: str):
    def _bound(data: dict[str, Any], feature: dict[str, Any] | None) -> dict[str, Any]:
        return _dispatch_write_side_effects(args, data, feature, command=command)

    return _bound


def _cli_runtime_context(workspace: Path) -> Any:
    from devkit_flow.telemetry import RuntimeContext, env_session_ref
    marker = None
    try:
        marker = env_session_ref()
    except Exception:
        marker = None
    if not isinstance(marker, dict) or not marker.get("runtime"):
        try:
            marker = latest_marker(workspace)
        except Exception:
            marker = None
    if not isinstance(marker, dict) or not marker.get("runtime"):
        return None
    session_id = marker.get("session_id")
    return RuntimeContext(
        name=str(marker.get("runtime")),
        session_id=session_id if isinstance(session_id, str) else None,
        attribution="attributed" if session_id else "unattributed",
    )


def _cli_feature_ref(feature: dict[str, Any] | None) -> Any:
    from devkit_flow.telemetry import FeatureRef
    if feature is None:
        return None
    return FeatureRef(dir=feature.get("feature_dir"), name=feature.get("feature"))


def _cli_ones_identity_allowed(workspace: Path, ones_sync_active: bool) -> bool:
    del workspace, ones_sync_active
    return False


def _cli_will_explicit_ones_sync(command: str, events: list) -> bool:
    """D-P5：仅显式同步路径允许 identity 走 external-cli；queued 主路径零子进程。"""
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "ones_follow_up":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if command == "flow complete" or bool(payload.get("sync_tasks")):
            return True
    return False


def _cli_consume_ones_follow_up(
    workspace: Path,
    feature: dict[str, Any] | None,
    data: dict[str, Any],
    event: dict[str, Any],
    *,
    command: str,
) -> None:
    del workspace, feature, event, command
    data["ones_sync"] = {"status": "skipped", "reason": "public-build"}
    return
    from devkit_flow.ones import enqueue_state_outbox, require_enabled_ones_config
    from devkit_flow.ones_sync import sync_feature_to_ones
    if feature is None:
        return
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    task_ids = list(payload.get("task_ids") or [])
    sync_tasks = bool(payload.get("sync_tasks"))
    # W3：意图事件 command="" 占位，显式同步边界用 dispatcher 的真实 command。
    explicit = command == "flow complete" or sync_tasks
    if explicit:
        try:
            ones = require_enabled_ones_config(workspace)
        except DevkitFlowError as error:
            data["ones_sync"] = {"status": "failed", "error": compact_error(error)}
            return
        # D-3：显式同步必须用落盘后的 STATUS 快照（complete 已把状态写成「已完成」）。
        sync_feature = feature
        try:
            sync_feature = parse_status_file(
                status_path_for_feature(workspace, feature), workspace
            )
        except Exception:
            sync_feature = feature
        try:
            sync_data, _exit_code = sync_feature_to_ones(
                workspace,
                sync_feature,
                ones,
                task_ids,
                sync_tasks=sync_tasks,
                reconcile=False,
            )
        except DevkitFlowError as error:
            data["ones_sync"] = {"status": "failed", "error": compact_error(error)}
            return
        result: dict[str, Any] = {
            "status": sync_data["status"],
            "reconcile_status": sync_data.get("reconcile_status", "skipped"),
            "sync_tasks": sync_tasks,
        }
        if sync_data.get("errors"):
            result["errors"] = sync_data["errors"]
        data["ones_sync"] = result
        return
    try:
        enqueue_state_outbox(
            workspace, feature, {"scope": "requirement", "action": "sync"}
        )
    except Exception as error:
        data["ones_sync"] = {"status": "failed", "error": compact_error(error)}
        return
    data["ones_sync"] = {"status": "queued", "reason": "outbox"}


def _cli_consume_write_report(
    workspace: Path,
    feature: dict[str, Any] | None,
    data: dict[str, Any],
) -> None:
    from devkit_flow.report import write_workflow_report
    if feature is None:
        return
    status_path = status_path_for_feature(workspace, feature)
    completed_feature = parse_status_file(status_path, workspace)
    report = write_workflow_report(workspace, completed_feature)
    report["opened"] = False
    report["open_error"] = None
    data["report"] = report


def _cli_consume_open_report(data: dict[str, Any]) -> None:
    from devkit_flow.html_ui import open_local_html_report
    report = data.get("report")
    if not isinstance(report, dict):
        return
    absolute = report.get("absolute_path")
    if not absolute:
        return
    opened, open_error = open_local_html_report(Path(absolute))
    report["opened"] = opened
    report["open_error"] = open_error


def _cli_consume_quality_collect(
    workspace: Path,
    feature: dict[str, Any] | None,
    data: dict[str, Any],
    event: dict[str, Any],
) -> None:
    """R21（014，接口 §4.4）：`flow set --status done` 顺带解析质量事件的意图消费点。

    fail-safe：采集异常不阻断 flow 命令主路径（exit code 不变），摘要挂
    data["quality_collect"] 供 JSON 输出观测；telemetry 未启用时采集侧按 skipped 记账。
    """
    from devkit_flow.telemetry import collect_feature_quality_events

    feature_ref = _cli_feature_ref(feature)
    if feature_ref is None:
        return
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    command = str(event.get("command") or "flow set")
    try:
        summary = collect_feature_quality_events(workspace, feature_ref, command=command)
        warnings = summary.pop("warnings", [])
        data["quality_collect"] = {
            "status": "ok",
            "step_id": payload.get("step_id"),
            **summary,
            "warning": "; ".join(str(item) for item in warnings) or None,
        }
    except Exception as error:
        data["quality_collect"] = {"status": "failed", "error": compact_error(error)}


def _cli_attach_report_message(data: dict[str, Any]) -> None:
    report = data.get("report")
    if not isinstance(report, dict) or "output" not in report:
        return
    suffix = f" 可视化报告已生成: {report['output']}。"
    if report.get("opened"):
        suffix += " 已在默认浏览器打开。"
    elif report.get("open_error"):
        suffix += f" 未能自动打开（{report['open_error']}），请手动打开该文件。"
    message = data.get("message") or ""
    data["message"] = f"{message}{suffix}"


def _dispatch_write_side_effects(
    args: Any, data: dict[str, Any], feature: dict[str, Any] | None, *, command: str
) -> dict[str, Any]:
    """写命令副作用唯一编排点（设计 §4.4.1）：摘 `_events`、telemetry、ONES、report。"""
    from devkit_flow.telemetry import TelemetryEmitter, schedule_background_flush
    # 017 progress-mod-focus：成功路径记录 feature 接手（写命令不看分支）；fail-safe，不影响后续副作用。
    try:
        if is_takeover(command, data, subcommand=getattr(args, "flow_clarify_command", None)):
            record_takeover(Path.cwd(), feature, command=command)
    except Exception:  # noqa: BLE001
        pass
    del args  # 签名契约保留；事件已编码 no_open / command 边界。
    result = dict(data)
    events = result.pop("_events", None)
    if not isinstance(events, list):
        events = []
    workspace = Path.cwd()
    feature_ref = _cli_feature_ref(feature)
    wrote_report = False
    enqueued_any = False
    flushed_any = False
    emitter = None
    emitter_key = None
    explicit_ones = _cli_will_explicit_ones_sync(command, events)
    failed_event_ids: list[str] = []
    saw_receipt_events = any(
        isinstance(event, dict) and isinstance(event.get("event_id"), str) and event.get("event_id")
        for event in events
    )
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type in _INTENT_EVENT_TYPES:
            if event_type == "ones_follow_up":
                _cli_consume_ones_follow_up(
                    workspace, feature, result, event, command=command
                )
            elif event_type == "write_workflow_report":
                _cli_consume_write_report(workspace, feature, result)
                wrote_report = True
            elif event_type == "open_local_html_report":
                _cli_consume_open_report(result)
            elif event_type == "quality_collect":
                _cli_consume_quality_collect(workspace, feature, result, event)
            continue
        try:
            event_command = event.get("command") or command
            is_flush = bool(event.get("flush"))
            # D-5：flush=true / 显式同步走完整身份；其余仅读缓存。禁止 cache-only 泄漏进快照。
            full_identity = explicit_ones or is_flush
            ones_sync_active = bool(event.get("ones_sync_active", True)) and full_identity
            cache_only = not full_identity
            key = (event_command, ones_sync_active, cache_only)
            if emitter is None or emitter_key != key:
                emitter = TelemetryEmitter(
                    workspace,
                    command=str(event_command),
                    runtime=_cli_runtime_context(workspace),
                    ones_identity_allowed=_cli_ones_identity_allowed(
                        workspace, ones_sync_active
                    ),
                    identity_cache_only=cache_only,
                )
                emitter_key = key
            if is_flush:
                trigger = event.get("trigger") or "manual"
                emitter.flush_with_snapshot_safe(feature_ref, trigger=str(trigger))
                flushed_any = True
            else:
                event_payload = event.get("payload")
                if not isinstance(event_payload, dict):
                    event_payload = {}
                # D-11/R2-N2（批次 1 审查 MAJOR-01 收口）：回放 flow_emit_event 写入的
                # event["trace"]（v2 可选段，dict 形态），由 _build_envelope 注入信封；
                # 缺省/None 时行为不变。
                # 016 T-10（§4.7.2.3 第 9 步）：flow batch-set 事件携带确定性 event_id（uuid5），原样透传；
                # 其余事件不带该键，调用形态与此前一致（event_id 缺省 → uuid4）。
                event_id = event.get("event_id")
                if isinstance(event_id, str) and event_id:
                    # D-57②：带确定性 event_id 的批量事件用检查型入队，失败计入回执（不得视为成功）。
                    enqueue_error = emitter.enqueue_event_checked(
                        str(event_type), event_payload, feature_ref, trace=event.get("trace"),
                        event_id=event_id,
                    )
                    if enqueue_error is not None:
                        failed_event_ids.append(event_id)
                else:
                    emitter.enqueue_event_safe(
                        str(event_type), event_payload, feature_ref, trace=event.get("trace")
                    )
                enqueued_any = True
        except Exception:
            event_id = event.get("event_id")
            if isinstance(event_id, str) and event_id:
                failed_event_ids.append(event_id)
    if saw_receipt_events:
        # D-57②：派发回执（私有键，flow batch-set 在 emit 前取走）；单步命令的事件不带 event_id，输出形状不变。
        result["_dispatch_receipts"] = {"telemetry_failed_event_ids": failed_event_ids}
    # D-10：只入队未 flush 的写命令，按节流条件异步拉起唯一后台 flusher（主路径不等待、不触网）。
    if enqueued_any and not flushed_any:
        try:
            schedule_background_flush(workspace)
        except Exception:
            pass
    if wrote_report:
        _cli_attach_report_message(result)
    return result


def _dispatch_sre(args: Any) -> int | None:
    from devkit_flow.sre import command_sre_configure, command_sre_init, command_sre_migrate_profile, command_sre_status

    if args.sre_command == "status":
        return command_sre_status(args)
    if args.sre_command == "init":
        return command_sre_init(args)
    if args.sre_command == "configure":
        return command_sre_configure(args)
    if args.sre_command == "migrate-profile":
        return command_sre_migrate_profile(args)
    return None


def _dispatch_html_ui(args: Any) -> int | None:
    from devkit_flow.dashboard import refresh_dashboard_after_write
    from devkit_flow.html_ui import command_html_ui_present, command_html_ui_preview

    if args.html_ui_command == "status":
        return command_html_ui_status(args)
    if args.html_ui_command == "configure":
        return command_html_ui_configure(args)
    if args.html_ui_command == "present":
        return command_html_ui_present(args, dashboard_refresh=refresh_dashboard_after_write, side_effects=_side_effects_for(args, command="html-ui present"))
    if args.html_ui_command == "preview":
        return command_html_ui_preview(args)
    return None


def _dispatch_self_update(args: Any) -> int:
    from devkit_flow.selfupdate import command_self_update

    return command_self_update(args)


def _dispatch_runtime_family(args: Any) -> int | None:
    """runtime / migrate / plugin / upgrade：全部由 runtime 模块承载（白名单 lazy import）。"""
    from devkit_flow.runtime import command_migrate_legacy_workspace, command_plugin_install, command_runtime_check, command_runtime_install, command_runtime_recover, command_runtime_rollback, command_runtime_session_end, command_runtime_uninstall, command_runtime_update, command_session_mark, command_upgrade

    if args.command == "upgrade":
        return command_upgrade(args)
    if args.command == "session-mark":
        return command_session_mark(args)
    if args.command == "runtime":
        if args.runtime_command == "update":
            return command_runtime_update(args)
        if args.runtime_command == "check":
            return command_runtime_check(args)
        if args.runtime_command == "session-end":
            return command_runtime_session_end(args)
        if args.runtime_command == "install":
            return command_runtime_install(args)
        if args.runtime_command == "uninstall":
            return command_runtime_uninstall(args)
        if args.runtime_command == "rollback":
            return command_runtime_rollback(args)
        if args.runtime_command == "recover":
            return command_runtime_recover(args)
    if args.command == "migrate":
        if args.migrate_command == "legacy-workspace":
            return command_migrate_legacy_workspace(args)
    if args.command == "plugin":
        if args.plugin_command == "install":
            return command_plugin_install(args)
    return None


def _dispatch_telemetry(args: Any) -> int | None:
    from devkit_flow.telemetry import command_telemetry_collect, command_telemetry_configure, command_telemetry_flush, command_telemetry_intervention, command_telemetry_login, command_telemetry_span, command_telemetry_status

    if args.telemetry_command == "configure":
        return command_telemetry_configure(args)
    if args.telemetry_command == "status":
        return command_telemetry_status(args)
    if args.telemetry_command == "login":
        return command_telemetry_login(args)
    if args.telemetry_command == "flush":
        return command_telemetry_flush(args)
    if args.telemetry_command == "span":
        return command_telemetry_span(args)
    if args.telemetry_command == "intervention":
        return command_telemetry_intervention(args)
    if args.telemetry_command == "collect":
        return command_telemetry_collect(args)
    return None


def _dispatch_bridge(args: Any) -> int | None:
    """bridge 命令族（on/off/status/configure + 隐藏 daemon）：白名单 lazy import（仿 telemetry 族）。"""
    from devkit_flow.bridge import command_bridge_configure, command_bridge_daemon, command_bridge_off, command_bridge_on, command_bridge_status

    if args.bridge_command == "on":
        return command_bridge_on(args)
    if args.bridge_command == "off":
        return command_bridge_off(args)
    if args.bridge_command == "status":
        return command_bridge_status(args)
    if args.bridge_command == "configure":
        return command_bridge_configure(args)
    if args.bridge_command == "daemon":
        return command_bridge_daemon(args)
    return None


def _dispatch_spec_archive(args: Any) -> int | None:
    from devkit_flow.spec_archive import command_spec_archive_apply, command_spec_archive_approve, command_spec_archive_configure, command_spec_archive_finalize, command_spec_archive_plan, command_spec_archive_prune, command_spec_archive_restore, command_spec_archive_scan, command_spec_archive_stage, command_spec_archive_status, command_spec_archive_verify

    if args.spec_archive_command == "scan":
        return command_spec_archive_scan(args)
    if args.spec_archive_command == "configure":
        return command_spec_archive_configure(args)
    if args.spec_archive_command == "plan":
        return command_spec_archive_plan(args)
    if args.spec_archive_command == "status":
        return command_spec_archive_status(args)
    if args.spec_archive_command == "stage":
        return command_spec_archive_stage(args)
    if args.spec_archive_command == "approve":
        return command_spec_archive_approve(args)
    if args.spec_archive_command == "verify":
        return command_spec_archive_verify(args)
    if args.spec_archive_command == "apply":
        return command_spec_archive_apply(args)
    if args.spec_archive_command == "prune":
        return command_spec_archive_prune(args)
    if args.spec_archive_command == "finalize":
        return command_spec_archive_finalize(args)
    if args.spec_archive_command == "restore":
        return command_spec_archive_restore(args)
    return None


def _dispatch_ones(args: Any) -> int | None:
    from devkit_flow.dashboard import refresh_dashboard_after_write
    from devkit_flow.ones import command_ones_bootstrap, command_ones_comment, command_ones_configure, command_ones_discover, command_ones_doctor, command_ones_link, command_ones_list, command_ones_push_descriptions, command_ones_recover_create
    from devkit_flow.ones_sync import command_ones_reconcile, command_ones_sync

    if args.ones_command == "configure":
        return command_ones_configure(args)
    if args.ones_command == "doctor":
        return command_ones_doctor(args)
    if args.ones_command == "discover":
        return command_ones_discover(args)
    if args.ones_command == "list":
        return command_ones_list(args)
    if args.ones_command == "bootstrap":
        return command_ones_bootstrap(args, dashboard_refresh=refresh_dashboard_after_write)
    if args.ones_command == "link":
        return command_ones_link(args, dashboard_refresh=refresh_dashboard_after_write)
    if args.ones_command == "recover-create":
        return command_ones_recover_create(args, dashboard_refresh=refresh_dashboard_after_write)
    if args.ones_command == "sync":
        return command_ones_sync(args, dashboard_refresh=refresh_dashboard_after_write)
    if args.ones_command == "comment":
        return command_ones_comment(args, dashboard_refresh=refresh_dashboard_after_write)
    if args.ones_command == "reconcile":
        return command_ones_reconcile(args)
    if args.ones_command == "push-descriptions":
        return command_ones_push_descriptions(args, dashboard_refresh=refresh_dashboard_after_write)
    return None


def _dispatch_flow(args: Any) -> int | None:
    """flow 写命令族：dashboard 注入 callable 与 clarify/report/dashboard 适配层为白名单 lazy import。"""
    from devkit_flow.dashboard import command_flow_dashboard, refresh_dashboard_after_write
    from devkit_flow.html_ui import command_flow_clarify
    from devkit_flow.report import command_flow_report

    if args.flow_command == "next":
        return command_flow_next(args, takeover=_takeover_for("flow next", require_branch_match=True))
    if args.flow_command == "set":
        return command_flow_set(args, dashboard_refresh=refresh_dashboard_after_write, gate_guard=evaluate_e2e_gate, side_effects=_side_effects_for(args, command="flow set"))
    if args.flow_command == "batch-set":
        return command_flow_batch_set(args, dashboard_refresh=refresh_dashboard_after_write, gate_guard=evaluate_e2e_gate, side_effects=_side_effects_for(args, command="flow batch-set"))
    if args.flow_command == "accept-risk":
        return command_flow_accept_risk(args, dashboard_refresh=refresh_dashboard_after_write, side_effects=_side_effects_for(args, command="flow accept-risk"))
    if args.flow_command == "task":
        if args.flow_task_command == "set":
            return command_flow_task_set(args, dashboard_refresh=refresh_dashboard_after_write, side_effects=_side_effects_for(args, command="flow task set"))
        if args.flow_task_command == "ensure-small":
            return command_flow_task_ensure_small(args, dashboard_refresh=refresh_dashboard_after_write, side_effects=_side_effects_for(args, command="flow task ensure-small"))
    if args.flow_command == "confirm":
        return command_flow_confirm(args, dashboard_refresh=refresh_dashboard_after_write, gate_guard=evaluate_e2e_gate, side_effects=_side_effects_for(args, command="flow confirm"))
    if args.flow_command == "rollback":
        return command_flow_rollback(args, dashboard_refresh=refresh_dashboard_after_write, side_effects=_side_effects_for(args, command="flow rollback"))
    if args.flow_command == "complete":
        return command_flow_complete(args, dashboard_refresh=refresh_dashboard_after_write, side_effects=_side_effects_for(args, command="flow complete"))
    if args.flow_command == "archive":
        return command_flow_archive(args, dashboard_refresh=refresh_dashboard_after_write, side_effects=_side_effects_for(args, command="flow archive"))
    if args.flow_command == "clarify":
        return command_flow_clarify(args, dashboard_refresh=refresh_dashboard_after_write, side_effects=_side_effects_for(args, command="flow clarify"))
    if args.flow_command == "verify":
        return command_flow_verify(args)
    if args.flow_command == "mode":
        return command_flow_mode(args, dashboard_refresh=refresh_dashboard_after_write, side_effects=_side_effects_for(args, command="flow mode"))
    if args.flow_command == "migrate-steps":
        return command_flow_migrate_steps(args, dashboard_refresh=refresh_dashboard_after_write, side_effects=_side_effects_for(args, command="flow migrate-steps"))
    if args.flow_command == "report":
        return command_flow_report(args)
    if args.flow_command == "dashboard":
        return command_flow_dashboard(args)
    return None


def main() -> int:
    args = parse_args()
    try:
        result: int | None = None
        if args.command == "status":
            return command_status(args)
        if args.command == "review-gate":
            if args.review_gate_command == "configure":
                return command_review_gate_configure(args)
            if args.review_gate_command == "status":
                return command_review_gate_status(args)
        if args.command == "quality-gate":
            if args.quality_command == "configure":
                return command_quality_gate_configure(args)
        if args.command == "context-source":
            if args.context_source_command == "status":
                return command_context_source_status(args)
            if args.context_source_command == "configure":
                return command_context_source_configure(args)
        if args.command == "e2e":
            if args.e2e_command == "configure":
                return command_e2e_configure(args)
            if args.e2e_command == "status":
                return command_e2e_status(args)
        if args.command == "workflow":
            if args.workflow_command == "status":
                return command_workflow_status(args)
            if args.workflow_command == "configure":
                return command_workflow_configure(args)
        if args.command == "feedback":
            if args.feedback_command == "status":
                return command_feedback_status(args)
            if args.feedback_command == "configure":
                return command_feedback_configure(args)
            if args.feedback_command == "preview":
                return command_feedback_preview(args)
            if args.feedback_command == "submit":
                return command_feedback_submit(args)
        if args.command == "sre":
            result = _dispatch_sre(args)
        if args.command == "docs-curator":
            if args.docs_curator_command == "status":
                return command_docs_curator_status(args)
            if args.docs_curator_command == "configure":
                return command_docs_curator_configure(args)
        if args.command == "dashboard":
            if args.dashboard_command == "status":
                return command_dashboard_status(args)
            if args.dashboard_command == "configure":
                return command_dashboard_configure(args)
        if args.command == "specrules":
            if args.specrules_command == "status":
                return command_specrules_status(args)
            if args.specrules_command == "configure":
                return command_specrules_configure(args)
            if args.specrules_command == "refresh":
                return command_specrules_refresh(args)
            if args.specrules_command == "catalog":
                if args.specrules_catalog_command == "status":
                    return command_specrules_catalog_status(args)
                if args.specrules_catalog_command == "configure":
                    return command_specrules_catalog_configure(args)
        if args.command == "design-template":
            if args.design_template_command == "status":
                return command_design_template_status(args)
            if args.design_template_command == "configure":
                return command_design_template_configure(args)
        if args.command == "html-ui":
            result = _dispatch_html_ui(args)
        if args.command == "progress-bar":
            if args.progress_bar_command == "status":
                return command_progress_bar_status(args)
            if args.progress_bar_command == "configure":
                return command_progress_bar_configure(args)
        if args.command == "spec-artifacts":
            if args.spec_artifacts_command == "status":
                return command_spec_artifacts_status(args)
            if args.spec_artifacts_command == "configure":
                return command_spec_artifacts_configure(args)
        if args.command == "review-rounds":
            if args.review_rounds_command == "status":
                return command_review_rounds_status(args)
            if args.review_rounds_command == "configure":
                return command_review_rounds_configure(args)
            if args.review_rounds_command == "check":
                return command_review_rounds_check(args)
            if args.review_rounds_command == "tick":
                return command_review_rounds_tick(args)
            if args.review_rounds_command == "reset":
                return command_review_rounds_reset(args)
        if args.command == "review-result":
            if args.review_result_command == "verify":
                return command_review_result_verify(args)
        if args.command == "template":
            return command_template(args)
        if args.command == "lint":
            return command_lint(args)
        if args.command == "project":
            if args.project_command == "detect":
                return command_project_detect(args)
            if args.project_command == "rules":
                return command_project_rules(args)
        if args.command == "self-update":
            return _dispatch_self_update(args)
        if args.command in {"upgrade", "runtime", "migrate", "plugin", "session-mark"}:
            result = _dispatch_runtime_family(args)
        if args.command == "setup":
            return command_setup(args)
        if args.command == "doctor":
            return command_doctor(args)
        if args.command == "sessions":
            if args.sessions_command == "list":
                return command_sessions_list(args)
        if args.command == "telemetry":
            result = _dispatch_telemetry(args)
        if args.command == "bridge":
            result = _dispatch_bridge(args)
        if args.command == "feature":
            if args.feature_command == "next-id":
                return command_feature_next_id(args)
            if args.feature_command == "init":
                return command_feature_init(args, side_effects=_side_effects_for(args, command="feature init"))
        if args.command == "spec":
            if args.spec_command == "archive":
                result = _dispatch_spec_archive(args)
        if args.command == "ones":
            result = _dispatch_ones(args)
        if args.command == "flow":
            result = _dispatch_flow(args)
        if result is not None:
            return result
        raise AssertionError(f"unhandled command: {args.command}")
    except DevkitFlowError as error:
        output_format = getattr(args, "format", "text")
        data = {"status": "error", "message": compact_error(error)}
        error_code = getattr(error, "error_code", None)
        if error_code:
            data["error_code"] = error_code
        emit(data, output_format)
        return 1


# 源码树 cli.py 直接运行入口（016 T-07 修复 T-V3 blocker-1）：缺本 guard 时
# `python cli.py ...` 仅执行模块定义即静默退出 0（daemon 再拉起 / telemetry 后台
# flush 的 argv0=cli.py 形态因此 no-op）。bundle 构建时本块由 bundler 剥离（产物入口
# 唯一，见 scripts/build_devkit_flow_bundle.py ENTRY_POINT 与入口块剥离口径）。
if __name__ == "__main__":
    raise SystemExit(main())
