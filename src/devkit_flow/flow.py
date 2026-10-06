from __future__ import annotations

import argparse
import copy
import fnmatch
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from devkit_flow.embedded import embedded_templates
from devkit_flow.config import load_config_file, load_devkit_config, ones_config_summary, ones_follow_up_sync
from devkit_flow.constants import CLARIFICATION_SCHEMA_VERSION, CLOSED_STATES, CONFIG_PATH, COMPLETION_GATE_STEPS, CONFIG_GATED_STEPS, DASHBOARD_STEP_TIMINGS_FILE, DESIGN_REVIEW_ARTIFACT_KEYWORDS, DESIGN_REVIEW_STEPS, DONE_STATES, E2E_GATE_STEP, FEATURE_NAME_PATTERN, FLOW_GUARDED_STATES, FLOW_MODES, FLOW_STEP_STATES, FLOW_TASK_STATES, ONES_OUTBOX_SCHEMA_VERSION, PIPELINE_MODE_ROWS, PIPELINE_SCALE_STEPS, PROGRESS_SUMMARY_LABELS, REQUIRED_DESIGN_REVIEW_STEPS, ROLLBACK_DESIGN_TASK_HINTS, SKIPPED_STATES, SMALL_TASK_HEADERS, STEP_ARTIFACT_MAP, STEP_TIMINGS_SCHEMA_VERSION, TASK_ARCH_REVIEW_STEPS, TELEMETRY_OUTBOX_RELATIVE_PATH, WAIVABLE_STEPS, WAIVED_STATES, accept_risk_gates_for_mode
from devkit_flow.errors import DevkitFlowError
from devkit_flow.project_detect import ProfileCache, ProjectDetector
from devkit_flow.rule_sources import RULES_BINDING_STALE, BindingReceipt, RuleSourceCatalog, RuleSourceSelector
from devkit_flow.parse_util import append_pipeline_history_rows, ensure_design_tasks, feature_name_from_dir, find_features, find_task_row, markdown_cell, next_feature_id, parse_design_task_table, parse_pipeline_history, parse_pipeline_table, parse_status_file, parse_task_table, render_status_template, report_clarification_stats, report_human_intervention_count, save_design_task_table, save_pipeline_rows, save_task_table, select_feature, set_status_field, small_task_row, status_path_for_feature, task_path_for_feature, write_new_small_task_file
from devkit_flow.pipeline_gates import (
    DependencyRewrite,
    PipelineConfigSnapshot,
    canonical_effective_rows,
    effective_pipeline_rows,
    expected_completion_gate_steps,
    missing_required_rows,
    pipeline_config_snapshot,
    scale_narrowed_step_ids,
)
from devkit_flow.step_alias import ALIAS_ROWS, StepAliasRow, StepResolution, attach_step_resolution, feature_pipeline_mode, resolve_step
from devkit_flow.textio import atomic_write_text, attach_dashboard_warning, compact_error, emit, emit_update_notice, flow_status_write_lock, maybe_check_latest_update


def command_feature_next_id(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    next_id = next_feature_id(workspace)
    emit(
        {
            "status": "ok",
            "message": next_id,
            "next_id": next_id,
            "directory": f"spec/{next_id}-<feature>",
        },
        args.format,
    )
    return 0


def flow_select_feature(workspace: Path, args: argparse.Namespace) -> dict[str, Any] | None:
    """Select the target feature for flow commands; emits and returns None on failure."""
    selected = select_feature(find_features(workspace), args.feature)
    if selected["status"] != "ok":
        emit(selected, args.format)
        return None
    return selected


def dependency_alternatives(depends_on: str) -> list[list[str]]:
    """Split a dependency cell into 或-separated alternatives of comma-separated ids."""
    text = depends_on.strip()
    if not text or text == "-":
        return []
    alternatives: list[list[str]] = []
    for alternative in re.split(r"\s*或\s*", text):
        ids = [
            cleaned
            for part in alternative.split(",")
            if (cleaned := re.sub(r"[（(][^)）]*[)）]", "", part).strip())
        ]
        alternatives.append(ids)
    return alternatives


def dependency_report(
    rows: list[dict[str, Any]], depends_on: str
) -> tuple[bool, list[dict[str, Any]]]:
    state_by_id = {row["cells"][0]: row["cells"][4] for row in rows}
    alternatives = dependency_alternatives(depends_on)
    detail: list[dict[str, Any]] = []
    for alternative in alternatives:
        steps = []
        for dep_id in alternative:
            state = state_by_id.get(dep_id)
            steps.append(
                {
                    "id": dep_id,
                    "state": state if state is not None else "missing",
                    "satisfied": state in CLOSED_STATES if state is not None else False,
                }
            )
        detail.append(
            {
                "steps": steps,
                "satisfied": bool(steps) and all(step["satisfied"] for step in steps),
            }
        )
    satisfied = not detail or any(alternative["satisfied"] for alternative in detail)
    return satisfied, detail


def pipeline_breakpoint(
    table: dict[str, Any], skip_pending_ids: frozenset[str] | set[str] = frozenset()
) -> dict[str, Any] | None:
    """第一条未关闭的流水线行。016 D-40：skip_pending_ids（scale 收窄遗留行集）中仍为 pending 的行
    不作为断点——它们将在 flow complete 自动归档（D-32），不应被 flow next / dashboard 建议派发。
    缺省空集时行为与此前完全一致。"""
    return next(
        (
            row
            for row in table["rows"]
            if row["cells"][4] not in CLOSED_STATES
            and not (row["cells"][0] in skip_pending_ids and row["cells"][4] == "pending")
        ),
        None,
    )


def feature_scale(feature: dict[str, Any], *, allow_empty: bool = True) -> str:
    """STATUS.md `规模` 头的唯一运行期解析口径（016 D-40）：strip + lower；读命令空值默认 large
    （allow_empty=True，与 master 读行为一致），写命令 migrate-steps 传 allow_empty=False 使空值
    fail-closed（016 D-42：不得按 large 插行）；非法取值抛结构化 DevkitFlowError（与 migrate-steps
    同文案），禁止裸 KeyError 穿透。读命令一律经此，不得各自 `(... or "large").strip()` 后直接下标
    PIPELINE_SCALE_STEPS。dashboard/snapshot 采集层保留 _normalize_scale fail-soft（辅助产物，D-5），
    但大小写口径与本函数一致（同 lower）。"""
    raw = str((feature.get("summary") or {}).get("scale") or "").strip()
    if not raw and not allow_empty:
        raise DevkitFlowError("STATUS.md 规模无效: (空)。")
    scale = raw.lower() or "large"
    if scale not in PIPELINE_SCALE_STEPS:
        raise DevkitFlowError(f"STATUS.md 规模无效: {raw or '(空)'}。")
    return scale


def feature_resolved_mode(table: dict[str, Any], feature: dict[str, Any]) -> str | None:
    """`模式` 头的运行期解析：S0.5-H 已 done 且头 ∈ FLOW_MODES 才视为已确认，否则 None。
    与 migrate-steps / priority_missing_gate_ids 同口径（016 D-40：消除 complete/verify 直接取原始头
    的第四种口径）。None 时 scale 收窄判定按两种模式并集算（pipeline_gates._scale_removed_step_ids）。"""
    header_mode = str((feature.get("summary") or {}).get("mode") or "").strip()
    return _migrate_steps_mode(table, header_mode)


def scale_narrowed_pending_ids(
    table: dict[str, Any], scale: str, mode: str | None
) -> tuple[frozenset[str], list[str]]:
    """(scale 收窄遗留 id 全集, 表内「遗留 ∧ pending」行 id 保序列表)。
    next / confirm / verify / complete / snapshot 共用同一谓词（D-32 / D-40 同源）。"""
    narrowed = scale_narrowed_step_ids(scale, mode)
    pending = [
        row["cells"][0]
        for row in table["rows"]
        if row["cells"][0] in narrowed and row["cells"][4] == "pending"
    ]
    return narrowed, pending


def task_arch_review_steps_for_mode(mode: str | None) -> tuple[str, ...]:
    """当前模式下的任务级架构审查步骤：传统 → S5.3，TDD → S5.5；模式未确认时两者都算
    （fail-closed：宁可多拦，不把 TDD 的 S5.3 Green Phase 与传统的 S5.3 架构审查混淆放行）。"""
    arch = tuple(
        step_id for step_id in accept_risk_gates_for_mode(mode) if step_id in TASK_ARCH_REVIEW_STEPS
    )
    return arch or TASK_ARCH_REVIEW_STEPS


def _task_arch_deferred_without_s57_message(steps: Sequence[str], *, scale: str | None = None) -> str:
    # 文案总长必须 ≤160 字符：CLI 出口 textio.compact_error 一律截到 160（S5.7 Round-2 apidoc B-1）。
    gate = steps[0] if steps else "S5.5|S5.3"
    for where in ((f"{scale} 档，D-30" if scale else "未补行"), "D-30"):
        text = (
            f"{', '.join(steps)} 任务级架构审查不得 skipped：流水线无 S5.7 行（{where}），递延无落点（N8）。"
            f"须 per-task 置 done，或 in-progress 后 flow accept-risk --gate {gate} --task-id … --reason … --confirmed。"
        )
        if len(text) <= 160:
            return text
    return text  # 不可达（"D-30" 变体恒 ≤160），保守兜底


def progress_summary_values(table: dict[str, Any]) -> dict[str, str]:
    rows = [row["cells"] for row in table["rows"]]
    completed = [cells[0] for cells in rows if cells[4] in DONE_STATES]
    waived = [cells[0] for cells in rows if cells[4] in WAIVED_STATES]
    open_rows = [cells for cells in rows if cells[4] not in CLOSED_STATES]
    # 与 snapshot.step_display_label 同口径：名称（Sx）（接口 §2.7 / 设计 §4.3.4）。
    # 不从 snapshot 导入：bundle 单文件无 devkit_flow 包。
    current = (
        f"{(open_rows[0][1] or '-').strip() or '-'}（{(open_rows[0][0] or '-').strip() or '-'}）"
        if open_rows
        else "无"
    )
    remaining = " → ".join(cells[0] for cells in open_rows) or "无"
    closed = [*completed, *(f"{step_id}(waived)" for step_id in waived)]
    return {
        "已完成": ", ".join(closed) or "无",
        "当前断点": current,
        "剩余步骤": remaining,
    }


def rewrite_progress_summary(path: Path, values: dict[str, str]) -> None:
    atomic_write_text(path, render_progress_summary_text(path.read_text(encoding="utf-8"), values))


def render_progress_summary_text(text: str, values: dict[str, str]) -> str:
    """016 T-09（§4.7.2.3）：内存版「进度概要」受管区改写，返回全文（不落盘）。"""
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == "## 进度概要"),
        None,
    )
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(["", "## 进度概要", ""])
        lines.extend(f"- {label}: {values[label]}" for label in PROGRESS_SUMMARY_LABELS)
        return "\n".join(lines) + "\n"
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].strip().startswith("## ")
        ),
        len(lines),
    )
    for label in PROGRESS_SUMMARY_LABELS:
        replacement = f"- {label}: {values[label]}"
        for index in range(start + 1, end):
            if re.match(rf"^-\s*{re.escape(label)}\s*[:：]", lines[index].strip()):
                lines[index] = replacement
                break
        else:
            lines.insert(end, replacement)
            end += 1
    return "\n".join(lines) + "\n"


def read_progress_summary(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped == "## 进度概要"
            continue
        if not in_section:
            continue
        match = re.match(r"^-\s*(已完成|当前断点|剩余步骤)\s*[:：]\s*(.*)$", stripped)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def flow_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def flow_today() -> str:
    return datetime.now().astimezone().date().isoformat()


def flow_outbox_counts(workspace: Path, feature: dict[str, Any]) -> dict[str, int] | None:
    """ONES-SYNC.json 未清空条目计数（只读、fail-safe；损坏/schema 不识别 → None）。

    核心层不得 import ones（设计 §3.2 规则 1），此处为 outbox 文件格式的本地只读副本。
    """
    path = workspace / feature["feature_dir"] / "ONES-SYNC.json"
    if not path.exists():
        return {
            "state_outbox_queued": 0,
            "state_outbox_retry": 0,
            "state_outbox_needs_input": 0,
            "comment_pending": 0,
            "comment_retry": 0,
        }
    try:
        outbox = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(outbox, dict) or outbox.get("version") != ONES_OUTBOX_SCHEMA_VERSION:
            return None
        state_entries = outbox.get("stateOutbox", [])
        comment_events = outbox.get("commentEvents", [])
        if not isinstance(state_entries, list) or not isinstance(comment_events, list):
            return None
    except (OSError, ValueError):
        return None
    return {
        # 012 D-9：queued（从未尝试、等待补发）与 retry 分列，让 flow next / dashboard 可见待补发数量。
        "state_outbox_queued": sum(
            1 for entry in state_entries if isinstance(entry, dict) and entry.get("status") == "queued"
        ),
        "state_outbox_retry": sum(
            1 for entry in state_entries if isinstance(entry, dict) and entry.get("status") == "retry"
        ),
        "state_outbox_needs_input": sum(
            1 for entry in state_entries if isinstance(entry, dict) and entry.get("status") == "needs_input"
        ),
        "comment_pending": sum(
            1 for event in comment_events if isinstance(event, dict) and event.get("status") == "pending"
        ),
        "comment_retry": sum(
            1 for event in comment_events if isinstance(event, dict) and event.get("status") == "retry"
        ),
    }


def _no_ones_sync(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "no_ones_sync", False))


def _flow_ones_config_or_none(workspace: Path) -> dict[str, Any] | None:
    """require_enabled_ones_config 的 core 等价只读判定（config 层），不完整/未启用/异常 → None。

    核心层不得 import ones（设计 §3.2 规则 1）；必填键清单与 ones.py 保持一致。
    """
    try:
        resolution = load_devkit_config(workspace)
    except DevkitFlowError:
        return None
    ones = resolution.effective.get("ones")
    if resolution.source == "none" or not isinstance(ones, dict) or not ones.get("enabled"):
        return None
    missing = [
        key
        for key in ("spaceId", "requirementSubtypeId", "designTaskSubtypeId", "taskSubtypeId")
        if not str(ones.get(key, "")).strip()
    ]
    if missing:
        return None
    return ones


def flow_ones_follow_up(
    workspace: Path,
    feature: dict[str, Any],
    task_ids: list[str] | None = None,
    *,
    skip: bool = False,
    sync_tasks: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """ONES follow-up 决策（core 内聚）：返回 (ones_sync 字段值, ones_follow_up 意图事件)。

    本地可判定的分支保持既有 JSON 契约不变：无需求 ID / 未启用 → (None, None)；
    --no-ones-sync → skipped/flag；follow_up_sync=manual → skipped/config。
    真实同步转为 ones_follow_up 意图事件（不触网），由 cli 装配层消费后决定
    显式同步（flow complete / sync_tasks=True）或 outbox 入队（queued，设计 D-P5）。

    Follow-up never runs full-table reconcile. Pipeline commands pass
    ``sync_tasks=False`` so only the requirement is pushed; ``flow task set``
    keeps ``sync_tasks=True`` with the selected task ids.
    """
    if not str(feature["summary"].get("ones_requirement_id", "")).strip():
        return None, None
    if not ones_config_summary(workspace)["enabled"]:
        return None, None
    if skip:
        return {"status": "skipped", "reason": "flag"}, None
    ones = _flow_ones_config_or_none(workspace)
    if ones is None:
        return None, None
    if ones_follow_up_sync(ones) == "manual":
        return {"status": "skipped", "reason": "config"}, None
    intent = _flow_event(
        "ones_follow_up",
        {"task_ids": list(task_ids or []), "sync_tasks": bool(sync_tasks)},
        command="",
    )
    return None, intent


def append_cell_note(row: dict[str, str], column: str, text: str) -> None:
    existing = row.get(column, "").strip()
    row[column] = f"{existing}；{text}" if existing else text


# R7 周期模型的活跃态（设计 §4.7.1）：置位为活跃态开周期；置位前为活跃态才允许闭合。
STEP_TIMING_ACTIVE_STATES = {"in-progress", "waiting-confirmation", "blocked"}


# 会话归属读取（record_step_timing 的 session_ref 数据源）：session_registry/telemetry
# 同语义的只读副本——核心层不得 import 外围模块（设计 §3.2 规则 1）；registry 为
# append-only JSONL，此处只读且 fail-safe（任何异常 → 未命中，绝不抛出）。
_FLOW_ENV_SESSION_BINDINGS = (
    ("CLAUDE_CODE_SESSION_ID", "claude-code"),
    ("GROK_SESSION_ID", "grok-build"),
    ("ANTIGRAVITY_CONVERSATION_ID", "antigravity"),
)
_FLOW_SESSION_REGISTRY_FILE = "session-registry.jsonl"
_FLOW_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _flow_session_registry_path() -> Path:
    return Path.home() / ".config" / "devkit" / _FLOW_SESSION_REGISTRY_FILE


def _flow_normalize_workspace(workspace: Path | str) -> str:
    path = Path(workspace).expanduser()
    try:
        return str(path.resolve())
    except OSError:
        return os.path.normpath(os.path.abspath(str(path)))


def _flow_iter_session_markers(workspace: Path | str | None = None) -> list[dict[str, Any]]:
    """按文件顺序读取 marker；坏行跳过，文件缺失返回空列表。workspace 非空时按归一化路径过滤。"""
    try:
        path = _flow_session_registry_path()
        if not path.is_file():
            return []
        wanted = _flow_normalize_workspace(workspace) if workspace is not None else None
        markers: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                marker = json.loads(line)
            except ValueError:
                continue  # 坏行跳过（截断/半行写入不污染读取）
            if not isinstance(marker, dict):
                continue
            if wanted is not None and marker.get("workspace") != wanted:
                continue
            markers.append(marker)
        return markers
    except Exception:
        return []


def _flow_marker_ts(marker: dict[str, Any]) -> datetime:
    value = marker.get("ts") if isinstance(marker, dict) else None
    if not isinstance(value, str) or not value.strip():
        return _FLOW_EPOCH
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) is None:
            return _FLOW_EPOCH
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return _FLOW_EPOCH
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _flow_env_session_marker() -> dict[str, Any] | None:
    """读宿主会话 env，全 registry 按 session_id 反查 marker（D-41）；无命中 → None。"""
    try:
        wanted: list[tuple[str, str]] = []
        for env_name, runtime in _FLOW_ENV_SESSION_BINDINGS:
            session_id = os.environ.get(env_name, "").strip()
            if session_id:
                wanted.append((session_id, runtime))
        if not wanted:
            return None
        markers = _flow_iter_session_markers()
        for session_id, expected_runtime in wanted:
            matched = [
                marker
                for marker in markers
                if isinstance(marker, dict) and marker.get("session_id") == session_id
            ]
            if not matched:
                continue
            same_runtime = [
                marker
                for marker in matched
                if marker.get("runtime") == expected_runtime
            ]
            pool = same_runtime or matched
            return max(pool, key=_flow_marker_ts)
        return None
    except Exception:
        return None


def flow_session_ref(workspace: Path) -> dict[str, Any]:
    """查中央 session registry 作为 cycle 归属（fail-safe，永不抛出）。

    优先宿主会话 env 反查（D-41），回落本 workspace 最新 marker；
    命中 → {runtime, session_id, attribution: "registry-latest"}；
    未命中（如手工终端执行）→ {attribution: "unattributed"}。
    """
    marker = _flow_env_session_marker()
    if not isinstance(marker, dict):
        markers = _flow_iter_session_markers(workspace)
        marker = markers[-1] if markers else None
    if marker:
        return {
            "runtime": marker.get("runtime"),
            "session_id": marker.get("session_id"),
            "attribution": "registry-latest",
        }
    return {"attribution": "unattributed"}


# 模块级别名会在运行时求值；bundle shebang 走系统 python3（macOS 3.9），
# 不得使用 PEP 604 `X | None`（3.10+）。from __future__ import annotations 不覆盖赋值。
WriteSideEffects = Callable[[dict[str, Any], Optional[dict[str, Any]]], dict[str, Any]]


def apply_write_side_effects(
    data: dict[str, Any],
    feature: dict[str, Any] | None,
    side_effects: WriteSideEffects | None,
) -> dict[str, Any]:
    """cli 装配层注入缝（D-1）：emit 前消费 `_events`。未注入时原样返回（测试缝，D-2）。"""
    if side_effects is None:
        return data
    return side_effects(data, feature)


def _flow_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    command: str,
    ones_sync_active: bool = True,
    flush: bool = False,
    trigger: str | None = None,
) -> dict[str, Any]:
    """构建领域事件（设计 §4.3.3 `_events` 进程内契约，唯一事实源 §4.4.1）。

    type/payload 与既有埋点逐位一致；command/ones_sync_active 原样保留为事件属性，
    供 cli 装配层重建 TelemetryEmitter 信封（runtime/identity gate）时逐位回放。
    flush=true 的事件由装配层在 enqueue 后同步 flush（trigger=gate-flush/complete-flush，D-P5）。
    """
    return {
        "type": event_type,
        "payload": payload,
        "flush": bool(flush),
        "trigger": trigger,
        "command": command,
        "ones_sync_active": bool(ones_sync_active),
    }


def flow_emit_event(
    workspace: Path,
    feature: dict[str, Any] | None,
    event_type: str,
    payload: dict[str, Any],
    *,
    command: str,
    ones_sync_active: bool = True,
    trace: Any = None,
) -> dict[str, Any]:
    """事件构建（原位替代原直发埋点）：返回事件 dict，不再触达 telemetry。

    flow.py 内部点位收集进返回 dict 的 `_events`（设计 §4.3.3）；html_ui 的 clarify
    点位（#10/#11）经本函数拿到事件对象后挂入 `data["_events"]`，由 cli 装配层消费。
    workspace/feature 形参保留以兼容既有调用签名（信封组装已上移至装配层）。
    trace 为 v2 可选段（014 接口 §4.1）：接受 telemetry.TraceContext 或等价 dict，
    序列化为 dict 挂入事件，由装配层在重建信封时注入 envelope；None 时不携带。
    """
    event = _flow_event(
        event_type, payload, command=command, ones_sync_active=ones_sync_active
    )
    if trace is not None:
        to_dict = getattr(trace, "to_dict", None)
        if callable(to_dict):
            event["trace"] = to_dict()
        elif isinstance(trace, dict):
            event["trace"] = dict(trace)
    return event


def flow_flush_snapshot(
    workspace: Path,
    feature: dict[str, Any] | None,
    *,
    trigger: str,
    command: str,
    ones_sync_active: bool = True,
) -> dict[str, Any]:
    """flush 点意图事件：feature_snapshot(trigger) + flush=true（装配层同步 flush，D-P5）。"""
    return _flow_event(
        "feature_snapshot",
        {"trigger": trigger},
        command=command,
        ones_sync_active=ones_sync_active,
        flush=True,
        trigger=trigger,
    )


def _flow_intervention_event(
    workspace: Path,
    feature: dict[str, Any] | None,
    *,
    kind: str,
    source: str,
    target: str | None,
    text: str | None,
    command: str,
    ones_sync_active: bool = True,
) -> dict[str, Any]:
    """intervention 事件（014 R20，接口 §4.2）：外部输入进入 lane。

    payload 四键口径同 T13 `telemetry._intervention_payload`（kind/source/target/
    text_excerpt；text_excerpt 为截断摘录 ≤200 字，只采摘要不采正文，隐私 D-2）。
    200 与 telemetry._INTERVENTION_TEXT_MAX_LEN 同值（核心层不 import telemetry，:591 先例）。
    """
    excerpt = text[:200] if text else None
    return flow_emit_event(
        workspace,
        feature,
        "intervention",
        {
            "kind": kind,
            "source": source,
            "target": target,
            "text_excerpt": excerpt,
        },
        command=command,
        ones_sync_active=ones_sync_active,
        trace=_flow_feature_trace(workspace, feature),
    )


def _flow_status_path(workspace: Path, feature: dict[str, Any]) -> Path:
    """Resolve STATUS.md without requiring `status_file` (feature init 仅有 feature_dir)。"""
    status_file = feature.get("status_file")
    if isinstance(status_file, str) and status_file.strip():
        return workspace / status_file
    feature_dir = feature.get("feature_dir")
    if isinstance(feature_dir, str) and feature_dir.strip():
        return workspace / feature_dir / "STATUS.md"
    return status_path_for_feature(workspace, feature)


def _flow_feature_trace(workspace: Path, feature: dict[str, Any] | None) -> dict[str, Any] | None:
    """STATUS.md 头部 TraceID → v2 trace 段（dict 形态，装配层经 TraceContext.from_value 注入信封）。

    fail-safe：feature 无 TraceID（老格式）或读取异常 → None；trace 段本身 optional
    （014 接口 §4.1），缺省不阻断事件产出。session_id 等其余键由装配层回落补齐。
    """
    try:
        if feature is None:
            return None
        parsed = parse_status_file(_flow_status_path(workspace, feature), workspace)
        trace_id = str((parsed.get("summary") or {}).get("trace_id") or "").strip()
        if not trace_id.startswith("tx_"):
            return None
        return {"transaction_id": trace_id}
    except Exception:
        return None


def _flow_ones_requirement_id(workspace: Path, feature: dict[str, Any] | None) -> str | None:
    """STATUS.md `ONES需求ID`：非空 stripped 值，否则 None。"""
    if feature is None:
        return None
    parsed = parse_status_file(_flow_status_path(workspace, feature), workspace)
    value = str((parsed.get("summary") or {}).get("ones_requirement_id") or "").strip()
    return value or None


def _flow_ones_task_id(
    workspace: Path,
    feature: dict[str, Any] | None,
    task_id: str,
) -> str | None:
    """T-xx / T-PRE-xx → task.md `ONES任务ID`；D-xx → STATUS 设计任务表同列。空则 None。"""
    if feature is None:
        return None
    tid = str(task_id or "").strip()
    if not tid:
        return None
    if tid.startswith("D-"):
        table = parse_design_task_table(_flow_status_path(workspace, feature))
    else:
        table = parse_task_table(task_path_for_feature(workspace, feature))
    value = str(find_task_row(table, tid).get("ONES任务ID") or "").strip()
    return value or None


def _payload_ones_requirement_id(
    workspace: Path, feature: dict[str, Any] | None
) -> str | None:
    """Fail-safe：ONES 读取异常 → null，不改变命令 exit code。"""
    try:
        return _flow_ones_requirement_id(workspace, feature)
    except Exception:
        return None


def _payload_ones_task_id(
    workspace: Path, feature: dict[str, Any] | None, task_id: str
) -> str | None:
    """Fail-safe：ONES 读取异常 → null，不改变命令 exit code。"""
    try:
        return _flow_ones_task_id(workspace, feature, task_id)
    except Exception:
        return None


def _flow_parse_iso_utc(value: Any) -> datetime | None:
    """ISO8601 解析（防御：非字符串/非法格式 → None；naive 按本地时区补齐后归一 UTC）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _outbox_transaction_first_occurred_at(trace_id: str) -> datetime | None:
    """outbox 中该 transaction_id 待发事件的最早 occurred_at（增强/校验手段，D-14）。

    核心层不 import telemetry（同 flow_outbox_counts 不 import ones 的先例），此处为
    outbox 行格式（{attempts, first_enqueued_at, event}）的本地只读副本；append-only
    文件的末行半写只会 JSON 解析失败被跳过。发送成功即出队，happy path 下通常取不到——
    调用方不得以其缺失为 None 依据。
    """
    path = Path.home() / TELEMETRY_OUTBOX_RELATIVE_PATH
    if not path.is_file():
        return None
    earliest: datetime | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            record: Any = json.loads(raw)
        except ValueError:
            continue
        event = record.get("event") if isinstance(record, dict) else None
        trace = event.get("trace") if isinstance(event, dict) else None
        if not isinstance(trace, dict) or trace.get("transaction_id") != trace_id:
            continue
        occurred = _flow_parse_iso_utc(event.get("occurred_at"))
        if occurred is not None and (earliest is None or occurred < earliest):
            earliest = occurred
    return earliest


def _payload_transaction_duration_ms(feature: dict[str, Any]) -> int | None:
    """feature_completed.duration_ms（014 设计 §4.3.4 / 接口 §4.3；D-14 修订事实源）。

    首端事实源 = STATUS.md 头部「创建时间」（与 TraceID 同批落盘、永久可见），
    末端 = 本事件时刻（feature_completed 为该 transaction 末事件）。outbox 同
    transaction 最早事件可取到且更早时用作首端增强；缺它不得导致 None。
    fail-safe 沿用：创建时间缺失/损坏且 outbox 无该 transaction 事件、任何异常 → None。
    """
    try:
        summary = feature.get("summary") or {}
        earliest = _flow_parse_iso_utc(summary.get("created_at"))
        trace_id = str(summary.get("trace_id") or "").strip()
        if trace_id:
            first_event = _outbox_transaction_first_occurred_at(trace_id)
            if first_event is not None and (earliest is None or first_event < earliest):
                earliest = first_event
        if earliest is None:
            return None
        delta_ms = int((datetime.now(timezone.utc) - earliest).total_seconds() * 1000)
        return max(0, delta_ms)
    except Exception:
        return None


def _payload_rework_count(feature_dir: Path) -> int | None:
    """feature_completed.rework_count（014 设计 §4.3.2 / 接口 §4.3）：cycle_index>1 的 step 数。

    缺 cycle_index 的历史 cycle 读侧按序号（1 基位置）回填，免迁移。fail-safe 沿用：
    旁车缺失（存量 feature 正常态）→ 0；文件损坏/schema 不识别/结构不符/异常 → None。
    """
    try:
        path = feature_dir / DASHBOARD_STEP_TIMINGS_FILE
        if not path.exists():
            return 0
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(loaded, dict)
            or loaded.get("schema") != STEP_TIMINGS_SCHEMA_VERSION
            or not isinstance(loaded.get("steps"), dict)
        ):
            return None
        rework = 0
        for entry in loaded["steps"].values():
            cycles = entry.get("cycles") if isinstance(entry, dict) else None
            if not isinstance(cycles, list):
                continue
            for position, cycle in enumerate(cycles, start=1):
                if not isinstance(cycle, dict):
                    continue
                index = cycle.get("cycle_index")
                if not isinstance(index, int) or isinstance(index, bool):
                    index = position
                if index > 1:
                    rework += 1
                    break
        return rework
    except Exception:
        return None


def _emit_feature_completed(
    workspace: Path,
    feature: dict[str, Any],
    status_path: Path,
    rows: list[list[str]],
) -> dict[str, Any] | None:
    """点位 #7：组装 feature_completed 事件（fail-safe：统计异常 → None，不产生事件）。

    clarification/human_interventions 口径对齐 snapshot 报告（A4 对账；统计函数已下沉
    parse_util，snapshot 同源 re-export）；duration_ms/rework_count 自 014 起填实
    （transaction 首末事件差 / cycle_index>1 的 step 数），各自独立 fail-safe，
    不可得置 null（接口 §4.3）。
    """
    try:
        feature_dir = workspace / feature["feature_dir"]
        raw_clar = report_clarification_stats(feature_dir)
        clarification = {
            key: int(raw_clar.get(key, 0) or 0)
            for key in ("items", "answered", "unanswered", "batches")
        }
        pipeline = parse_status_file(status_path, workspace).get("pipeline") or []
        steps = [item for item in pipeline if isinstance(item, dict)]
        human = int(report_human_intervention_count(steps, feature_dir))
        steps_total = len(rows)
        steps_done = sum(1 for cells in rows if cells[4] in DONE_STATES)
        steps_waived = sum(1 for cells in rows if cells[4] in WAIVED_STATES)
        task_path = task_path_for_feature(workspace, feature)
        task_rows = parse_task_table(task_path)["rows"] if task_path.exists() else []
        tasks_total = len(task_rows)
        tasks_done = sum(
            1 for row in task_rows if str(row.get("状态") or "") in {"已完成", "done"}
        )
        mode = str(feature["summary"].get("mode", "") or "").strip() or None
    except Exception:
        return None
    return flow_emit_event(
        workspace, feature, "feature_completed",
        {
            "mode": mode,
            "duration_ms": _payload_transaction_duration_ms(feature),
            "steps_total": steps_total,
            "steps_done": steps_done,
            "steps_waived": steps_waived,
            "tasks_total": tasks_total,
            "tasks_done": tasks_done,
            "clarification": clarification,
            "human_interventions": human,
            "rework_count": _payload_rework_count(feature_dir),
            "ones_requirement_id": _payload_ones_requirement_id(workspace, feature),
        },
        command="flow complete",
        trace=_flow_feature_trace(workspace, feature),  # D-23：v1 事件 trace 注入
    )


def _apply_step_timing_transition(
    cycles: list[Any],
    *,
    feature_dir: Path,
    step_id: str,
    state: str,
    prior_state: str,
    now: str,
    accept_risk: bool = False,
    session_ref: dict[str, Any] | None = None,
) -> bool:
    """record_step_timing 的周期迁移内核（就地修改 ``cycles``，返回是否有变更；只读 STATUS.md 归档区，不写任何文件）。

    016 T-10（D-57④）：抽出为独立函数，供批量操作在预写账本时对 cycles 快照副本**预演**同一迁移，
    得到本批次会闭合的精确周期序号（``predict_step_cycle_closures``），恢复补发只发这些周期。
    """
    mutated = False

    def has_open_cycle() -> bool:
        return bool(cycles) and isinstance(cycles[-1], dict) and cycles[-1].get("ended_at") is None

    def new_cycle(started: str, ended: str | None, ended_by: str | None = None) -> dict[str, Any]:
        cycle: dict[str, Any] = {
            "started_at": started,
            "ended_at": ended,
            "cycle_index": len(cycles) + 1,
        }
        if ended_by is not None:
            cycle["ended_by"] = ended_by
        if session_ref is not None:
            cycle["session"] = session_ref
        return cycle

    def close_open_cycle(ended: str, ended_by: str) -> None:
        cycles[-1]["ended_at"] = ended
        cycles[-1]["ended_by"] = ended_by
        cycles[-1].setdefault("cycle_index", len(cycles))

    def archived_ended_at() -> str | None:
        """已归档未闭合识别（014 §4.3.2 崩溃自愈）：归档区存在本步骤且 cycle_index
        等于开周期序号（缺字段历史 cycle 按序号回填，开周期序号 = 其 1 基位置）的
        归档行时返回其归档时间（缺省回落 now）；否则 None。"""
        if not has_open_cycle():
            return None
        try:
            history = parse_pipeline_history(feature_dir / "STATUS.md")
        except Exception:
            return None
        open_index = str(len(cycles))
        for row in history.get("rows", []):
            cells = row.get("cells") if isinstance(row, dict) else None
            if (
                isinstance(cells, list)
                and len(cells) >= 8
                and str(cells[0]).strip() == step_id
                and str(cells[6]).strip() == open_index
            ):
                return str(cells[7]).strip() or now
        return None

    # 先行自愈（任何埋点先执行）：存在开周期且置位前状态非活跃 = 悬空开周期。
    # 已归档未闭合（rollback 跨文件写崩溃中间态）→ 按归档时间补齐闭合
    # （ended_by=rollback）；无归档信息的悬空开周期 → 沿用废弃兜底（删除，不计耗时）。
    if has_open_cycle() and prior_state not in STEP_TIMING_ACTIVE_STATES:
        archived_at = archived_ended_at()
        if archived_at is not None:
            close_open_cycle(archived_at, "rollback")
        else:
            cycles.pop()
        mutated = True

    if state in STEP_TIMING_ACTIVE_STATES:
        # 活跃态：无开周期（含悬空已愈合）→ 开新周期；活跃态之间迁移不重复开。
        if not has_open_cycle():
            cycles.append(new_cycle(now, None))
            mutated = True
    elif state == "pending":
        # pending = 回到未开始：rollback 归档路径闭合当前开周期（ended_by=rollback，
        # 修既有废弃删除缺陷，回退周期耗时可计量）；无归档信息时沿用废弃兜底；
        # 无开周期时 no-op；已闭合历史保留。
        if has_open_cycle():
            archived_at = archived_ended_at()
            if archived_at is not None:
                close_open_cycle(archived_at, "rollback")
            else:
                cycles.pop()
            mutated = True
    elif state in DONE_STATES or state in SKIPPED_STATES:
        if has_open_cycle():
            close_open_cycle(now, "done")
            mutated = True
        elif not accept_risk:
            # 漏斗终态且无开周期 → 瞬时周期；accept-risk 变体无开周期不记（接口文档 §2.6）。
            cycles.append(new_cycle(now, now, ended_by="done"))
            mutated = True
    # 其余状态（如 waived）不产生埋点事件；调用方本就不应传入。
    return mutated


def record_step_timing(
    feature_dir: Path,
    step_id: str,
    state: str,
    prior_state: str,
    now: str,
    *,
    accept_risk: bool = False,
    session_ref: dict[str, Any] | None = None,
) -> str | None:
    """R7 步骤级时间戳埋点唯一写入函数（设计 §4.7.1，接口文档 §2.6/§7）。

    维护旁车文件 `.step-timings.json` 的周期模型 cycles[]（至多一个开周期且必在末位）。
    cycle 语义单点（014 设计 §4.3.2，D-5）：cycle 条目携带加性字段 cycle_index
    （int，从 1 起）与 ended_by（done/rollback，闭合时写入）；STEP_TIMINGS_SCHEMA_VERSION
    保持 1，缺 cycle_index 的历史 cycle 读侧按序号回填（写侧在已有写入时顺带补齐本步骤）。
    调用点两处：apply_flow_set 漏斗（flow set/confirm/mode）与 flow accept-risk 的
    gate 置 done 直写路径（accept_risk=True 变体：无开周期时不记瞬时周期）。
    session_ref 非 None 时并入新开/瞬时 cycle 的 "session" 键（会话归属，加性字段）。

    rollback 周期闭合（014 D-5，修既有「废弃删除」缺陷）：rollback 归档路径的开周期被
    闭合（ended_by=rollback）而非删除，回退周期耗时可计量。崩溃自愈（WARNING-03：
    STATUS.md 归档与本文件闭合各自原子写、无跨文件事务，中途崩溃留「已归档未闭合」
    中间态）经既有悬空开周期检测识别——归档区存在 (step_id, 开周期序号) 行时按归档时间
    补齐闭合；无归档信息的悬空开周期沿用废弃兜底（存量行为不变）。

    fail-safe：任何读写异常仅返回 warning 字符串（正常返回 None），绝不抛出、
    绝不阻断调用方的状态写入；文件损坏/schema 不识别时按空数据重建。
    """
    try:
        path = feature_dir / DASHBOARD_STEP_TIMINGS_FILE
        payload: dict[str, Any] = {"schema": STEP_TIMINGS_SCHEMA_VERSION, "steps": {}}
        rebuilt = False
        if path.exists():
            try:
                loaded: Any = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                loaded = None
            if (
                isinstance(loaded, dict)
                and loaded.get("schema") == STEP_TIMINGS_SCHEMA_VERSION
                and isinstance(loaded.get("steps"), dict)
            ):
                payload = loaded
            else:
                rebuilt = True  # 损坏文件按空数据重建（设计 §4.7.1 fail-safe）

        steps: dict[str, Any] = payload["steps"]
        entry = steps.get(step_id)
        cycles = entry.get("cycles") if isinstance(entry, dict) else None
        if not isinstance(cycles, list):
            cycles = []
        mutated = _apply_step_timing_transition(
            cycles,
            feature_dir=feature_dir,
            step_id=step_id,
            state=state,
            prior_state=prior_state,
            now=now,
            accept_risk=accept_risk,
            session_ref=session_ref,
        )

        if mutated:
            # 加性回填：本次已有写入时顺带为本步骤缺 cycle_index 的历史 cycle 按序号补齐
            # （读侧回填规则的写侧顺带落地；其它步骤不动，免迁移不变式不变）。
            for position, cycle in enumerate(cycles, start=1):
                if isinstance(cycle, dict):
                    cycle.setdefault("cycle_index", position)

        if isinstance(entry, dict):
            entry["cycles"] = cycles
        elif mutated:
            steps[step_id] = {"cycles": cycles}
        if mutated or rebuilt:
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        if rebuilt:
            return (
                f"步骤时间戳旁车文件损坏或 schema 不识别，已按空数据重建"
                f"（状态写入不受影响）: {path.name}"
            )
        return None
    except Exception as error:  # fail-safe 兜底：任何异常不得阻断状态写入（设计 §4.7.1）
        return f"步骤时间戳记录失败（状态写入已完成，不受影响）: {compact_error(error)}"


def attach_step_timing_warning(data: dict[str, Any], warning: str | None) -> None:
    """埋点 warning 透传（复用 dashboard warning 通道形态：仅失败时追加字段，既有契约不变）。"""
    if warning:
        data["step_timings"] = {"warning": warning}


def _step_cycle_entries(feature_dir: Path, step_id: str) -> list[dict[str, Any]] | None:
    """读 `.step-timings.json` 本步骤 cycles[]（record_step_timing 前后差分的只读基线）。

    文件缺失/schema 不识别/无条目 → []（存量 feature 正常态：无周期即无事件）；
    读取异常 → None（调用方跳过本次事件，fail-safe 不阻断状态写入）。
    """
    try:
        path = feature_dir / DASHBOARD_STEP_TIMINGS_FILE
        if not path.exists():
            return []
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or loaded.get("schema") != STEP_TIMINGS_SCHEMA_VERSION:
            return []
        steps = loaded.get("steps")
        entry = steps.get(step_id) if isinstance(steps, dict) else None
        cycles = entry.get("cycles") if isinstance(entry, dict) else None
        if not isinstance(cycles, list):
            return []
        return [cycle for cycle in cycles if isinstance(cycle, dict)]
    except Exception:
        return None


def emit_step_cycle(
    workspace: Path,
    feature: dict[str, Any] | None,
    step_id: str,
    cycle: dict[str, Any],
    ended_by: str,
    *,
    command: str,
    ones_sync_active: bool = True,
) -> dict[str, Any] | None:
    """step_cycle 事件唯一发射点（014 R19，接口 §4.2/§6；cycle 语义单点 = cycles[]，设计 §4.3.2）。

    事件与 `.step-timings.json` cycles[] 一一对应：cycle_index/started_at/ended_at/ended_by
    逐字段取自 cycle 条目（与 T5 同源同值），duration_ms = ended_at - started_at（毫秒，
    负值钳 0），session_id 取 cycle["session"]["session_id"]（无会话上下文 → null）。
    落点说明：设计 §4.4 把本函数与 flow_emit_event 同列于 telemetry.py 一节，但核心层不
    import telemetry（:591 先例；T3 flow_emit_event 同口径落 flow.py，D-11 已确认该链路），
    事件经 `_events` 契约由 cli 装配层入队。command 为关键字-only 加性参数（装配层信封用）。
    fail-safe：ended_by 越枚举、cycle 缺关键字段或时间戳不可解析 → None（丢事件不丢状态）。
    """
    try:
        if ended_by not in {"done", "rollback", "waived"}:
            return None
        started_at = cycle.get("started_at")
        ended_at = cycle.get("ended_at")
        cycle_index = cycle.get("cycle_index")
        if not isinstance(started_at, str) or not isinstance(ended_at, str):
            return None
        if not isinstance(cycle_index, int) or isinstance(cycle_index, bool) or cycle_index < 1:
            return None
        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
        duration_ms = max(0, int((ended - started).total_seconds() * 1000))
        session = cycle.get("session")
        session_id = session.get("session_id") if isinstance(session, dict) else None
        if not isinstance(session_id, str) or not session_id.strip():
            session_id = None
        return flow_emit_event(
            workspace,
            feature,
            "step_cycle",
            {
                "step_id": step_id,
                "cycle_index": cycle_index,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms,
                "ended_by": ended_by,
                "session_id": session_id,
            },
            command=command,
            ones_sync_active=ones_sync_active,
            trace=_flow_feature_trace(workspace, feature),
        )
    except Exception:
        return None


def predict_step_cycle_closures(
    feature_dir: Path,
    step_id: str,
    before: list[dict[str, Any]] | None,
    state: str,
    prior_state: str,
) -> list[int] | None:
    """016 T-10（D-57④）：对 cycles 快照副本预演本次置位的周期迁移，返回会新闭合的周期序号（0 基，口径与
    ``_step_cycle_events_since`` 的差分一致）。``before`` 为 None（基线读取失败）时返回 None（调用方不发 step_cycle）。"""
    if before is None:
        return None
    simulated = copy.deepcopy(before)
    _apply_step_timing_transition(
        simulated,
        feature_dir=feature_dir,
        step_id=step_id,
        state=state,
        prior_state=prior_state,
        now=flow_now_iso(),
    )
    closed: list[int] = []
    for index, cycle in enumerate(simulated):
        if not isinstance(cycle, dict) or cycle.get("ended_at") is None:
            continue
        if index < len(before) and before[index].get("ended_at") is not None:
            continue
        closed.append(index)
    return closed


def _step_cycle_events_since(
    workspace: Path,
    feature: dict[str, Any] | None,
    step_id: str,
    before: list[dict[str, Any]] | None,
    *,
    command: str,
    ones_sync_active: bool = True,
    cycle_indices: list[int] | None = None,
) -> list[dict[str, Any]]:
    """R19 差分发射：record_step_timing 前快照（before）对照当前 cycles[]，本次新闭合的
    cycle（ended_at 由 None 变有值，或新增的已闭合瞬时周期）各发一条 step_cycle。

    崩溃自愈按归档时间补齐的闭合（ended_by=rollback）同样在差分内；废弃兜底删除的悬空
    开周期从未闭合、不计耗时，不发事件——事件与 cycles[] 一一对应保持不变式成立。
    fail-safe：before 为 None（基线读取失败）或任何异常 → []，绝不阻断状态写入。
    """
    if before is None or feature is None:
        return []
    try:
        after = _step_cycle_entries(workspace / feature["feature_dir"], step_id)
        if after is None:
            return []
        events: list[dict[str, Any]] = []
        for index, cycle in enumerate(after):
            if cycle.get("ended_at") is None:
                continue
            if index < len(before) and before[index].get("ended_at") is not None:
                continue  # 此前已闭合的周期已发过事件
            if cycle_indices is not None and index not in cycle_indices:
                continue  # 016 T-10（D-57④）：只发原批次精确闭合的周期，不把后续周期计入旧批次
            event = emit_step_cycle(
                workspace,
                feature,
                step_id,
                cycle,
                str(cycle.get("ended_by") or ""),
                command=command,
                ones_sync_active=ones_sync_active,
            )
            if event is not None:
                events.append(event)
        return events
    except Exception:
        return []


class FlowGuardError(DevkitFlowError):
    """016 T-10：单步/批量共用守卫的拒绝异常，``guard`` 标注拒绝类别（``invalid`` / ``dependency`` / ``gate``）。

    文案与 2.45.0 逐字一致、不带 ``error_code``（单步 JSON 输出形状不变）；批量入口据 ``guard`` 映射
    FLOW_BATCH_INVALID_MEMBER / FLOW_BATCH_DEPENDENCY_UNMET / FLOW_BATCH_GATE_REQUIRED。
    """

    def __init__(self, message: str, *, guard: str) -> None:
        super().__init__(message)
        self.guard = guard


def validate_flow_step_state(state: str) -> None:
    """流水线步骤目标状态枚举守卫（waived 只走 accept-risk）。"""
    if state in WAIVED_STATES:
        raise FlowGuardError(
            "waived 只能由 flow accept-risk 记录人工风险接受，不能通过 flow set 直接置位。",
            guard="invalid",
        )
    if state not in FLOW_STEP_STATES:
        raise FlowGuardError(
            f"非法状态: {state}（可选: {', '.join(sorted(FLOW_STEP_STATES))}）",
            guard="invalid",
        )


def step_clarify_guard_applies(collecting_batch: dict[str, Any] | None, state: str) -> bool:
    """014 R7（D-18）澄清 collecting 守卫谓词：批次 collecting 且目标为回写类状态时命中。

    纯函数，单步 ``flow set`` 与批量共用；命中后的输出/override 处理由调用方决定。
    """
    return collecting_batch is not None and state in FLOW_GUARDED_STATES


@dataclass
class StepMemberPlan:
    """``plan_step_member`` 的求值结果：守卫已全部通过、尚未写入表。"""

    step_id: str
    row: dict[str, Any]
    prior_state: str
    state: str
    note: str
    forced: bool


def plan_step_member(
    workspace: Path,
    feature: dict[str, Any],
    table: dict[str, Any],
    step_id: str,
    state: str,
    *,
    note: str | None = None,
    confirmed: bool = False,
    force: bool = False,
    reason: str | None = None,
    gate_guard: Callable[[Path], Any] | None = None,
) -> StepMemberPlan:
    """016 T-09（§4.7.2.3）：流水线步骤置位的纯守卫与单元格计算（不写盘、不改 ``table``）。

    守卫顺序与 2.45.0 ``apply_flow_set`` 逐条一致：状态枚举 → 行存在 → 依赖/``--force`` → ``-H`` 需
    ``--confirmed`` → S5-E2E skipped 理由 / done 证据门 → 任务级架构审查无 S5.7 不得 skipped。
    批量入口对内存表副本逐成员调用，与单步共用同一份守卫代码。
    """
    validate_flow_step_state(state)
    row = next((candidate for candidate in table["rows"] if candidate["cells"][0] == step_id), None)
    if row is None:
        raise FlowGuardError(f"流水线中不存在步骤: {step_id}", guard="invalid")

    forced = False
    if state in FLOW_GUARDED_STATES:
        satisfied, detail = dependency_report(table["rows"], row["cells"][3])
        if not satisfied:
            if force and not (reason or "").strip():
                raise FlowGuardError("--force 需要同时提供 --reason 说明强制原因。", guard="invalid")
            if not force:
                unmet = [
                    f"{step['id']}({step['state']})"
                    for alternative in detail
                    for step in alternative["steps"]
                    if not step["satisfied"]
                ]
                raise FlowGuardError(
                    f"步骤 {step_id} 的依赖未满足: {', '.join(unmet)}；"
                    "如确需跳过请使用 --force --reason。",
                    guard="dependency",
                )
            forced = True
    if step_id.endswith("-H") and state in DONE_STATES and not confirmed:
        raise FlowGuardError(
            f"该步骤需要人工确认：{step_id} 是人工 Gate，置 done 必须携带 --confirmed。",
            guard="gate",
        )

    # S5-E2E 置位守卫（设计 §4.5.1，接口文档 §4.5）：在既有依赖/人工 Gate 校验之后追加。
    # gate 证据校验独立于 --force（force 仅豁免步骤依赖检查），防止击穿「宁红勿假绿」。
    if step_id == E2E_GATE_STEP and state in SKIPPED_STATES and not (note or "").strip():
        raise FlowGuardError(
            f"{E2E_GATE_STEP} 置 skipped 必须通过 --note 携带书面理由（无跨进程面说明）。",
            guard="gate",
        )
    if step_id == E2E_GATE_STEP and state in DONE_STATES:
        if gate_guard is None:
            raise FlowGuardError(
                f"{E2E_GATE_STEP} 置 done 需要 e2e 门证据校验，但 gate_guard 未注入"
                "（应由 cli 组装注入 evaluate_e2e_gate）。",
                guard="gate",
            )
        verdict = gate_guard(workspace)
        outcome = getattr(verdict, "verdict", "")
        if outcome != "pass":
            gate_reason = getattr(verdict, "reason", "") or "unknown"
            if outcome == "fail":
                failures = getattr(verdict, "failures", ()) or ()
                detail = "；".join(
                    f"{item.get('caseId', '?')} {item.get('title', '')}: {item.get('message', '')}".strip()
                    for item in failures
                    if isinstance(item, dict)
                )
                report_url = getattr(verdict, "report_url", "") or "-"
                raise FlowGuardError(
                    f"{E2E_GATE_STEP} 门判定 fail（{gate_reason}），拒绝置 done。"
                    f"失败明细：{detail or '（无）'}；报告：{report_url}。",
                    guard="gate",
                )
            raise FlowGuardError(
                f"{E2E_GATE_STEP} 门证据不可用（{gate_reason}），拒绝置 done；"
                "须 verdict=pass 且 failed=0，或以 skipped + 书面理由关闭。",
                guard="gate",
            )

    # 016 D-39（S5.7 P0-1）：任务级架构审查（传统 S5.3 / TDD S5.5）置 skipped 的唯一合法语义是
    # dev-once 递延到 S5.7；流水线不存在 S5.7 行（small 档 D-30，或尚未补行）时递延无落点，等于
    # 零架构审查，违反 FEATURE N8。在置位时刻拒绝（而非等到 complete 才撞墙）；独立于 --force。
    # mode 走 feature_resolved_mode（与 complete/verify/migrate-steps 同一口径，D-40）：未确认时两步都拦（fail-closed）。
    if step_id in task_arch_review_steps_for_mode(feature_resolved_mode(table, feature)) and state in SKIPPED_STATES:
        if not any(candidate["cells"][0] == "S5.7" for candidate in table["rows"]):
            raise FlowGuardError(_task_arch_deferred_without_s57_message([step_id]), guard="gate")

    cells = row["cells"]
    new_note = note if note is not None else (cells[5] if len(cells) > 5 else "")
    if forced:
        append = f"强制置位：{reason.strip()}"
        new_note = f"{new_note}；{append}" if new_note else append
    return StepMemberPlan(
        step_id=step_id,
        row=row,
        prior_state=cells[4],
        state=state,
        note=new_note,
        forced=forced,
    )


def apply_step_member_plan(plan: StepMemberPlan, table: dict[str, Any] | None = None) -> dict[str, Any]:
    """把 ``plan_step_member`` 结果写入内存表行（不落盘），返回被改写的行。

    用法约束（T-09 审查 P2-B）：必须在**同一张表**上 plan → apply 交替执行——先对一个成员 plan 并立即 apply，
    再 plan 下一个成员；不得先批量 plan 再统一 apply，也不得把 plan 套到重新 parse 出的另一张表上。
    传入 ``table`` 时校验 ``plan.row`` 确属该表（身份比较），违反即抛错而非静默丢更新。
    """
    if table is not None and not any(candidate is plan.row for candidate in table["rows"]):
        raise DevkitFlowError(f"内部错误：{plan.step_id} 的置位计划不属于当前流水线表（须同表 plan→apply 交替）。")
    cells = plan.row["cells"]
    cells[4] = plan.state
    while len(cells) < 6:
        cells.append("")
    cells[5] = plan.note
    return plan.row


def flow_step_member_events(
    workspace: Path,
    feature: dict[str, Any],
    step_id: str,
    prior_state: str,
    state: str,
    *,
    trigger: str,
    confirmed: bool,
    no_ones_sync: bool,
    cycles_before: list[dict[str, Any]] | None,
    cycle_indices: list[int] | None = None,
) -> list[dict[str, Any]]:
    """016 T-10：步骤置位后的事件序列（单步漏斗与批量入口共用，保证逐位一致）。

    顺序：step_transition → 本次新闭合周期的 step_cycle → done 时 quality_collect 意图 → waiting-confirmation
    时 intervention(gate)。``cycles_before`` 为 ``record_step_timing`` 之前的 cycles[] 快照。
    """
    # 点位 #1/#12：flow set/confirm/mode 共用漏斗，尾部产出 step_transition 事件（_events 契约）。
    # D-23（014 T16）：v1 feature 上下文事件补 trace 注入（_flow_feature_trace 现成通道，fail-safe）。
    events: list[dict[str, Any]] = [
        flow_emit_event(
            workspace,
            feature,
            "step_transition",
            {
                "step_id": step_id,
                "from_status": prior_state or None,
                "to_status": state,
                "trigger": trigger,
                "confirmed": bool(confirmed),
            },
            command=f"flow {trigger}",
            ones_sync_active=not no_ones_sync,
            trace=_flow_feature_trace(workspace, feature),
        )
    ]
    # R19（点位 #1 漏斗尾部）：本次新闭合周期发 step_cycle（与 cycles[] 一一对应，fail-safe）。
    events.extend(
        _step_cycle_events_since(
            workspace, feature, step_id, cycles_before,
            command=f"flow {trigger}", ones_sync_active=not no_ones_sync,
            cycle_indices=cycle_indices,
        )
    )
    if state == "done":
        # R21（014，接口 §4.4）：step 关闭顺带解析质量事件——quality_collect 意图事件由
        # cli 装配层消费后调用 telemetry 采集（唯一实现在 telemetry.py；flow 核心层不
        # import telemetry，D-21 同例）。side_effects 缺省时意图滞留 _events，不产生副作用。
        events.append(
            _flow_event(
                "quality_collect",
                {"step_id": step_id, "trigger": trigger},
                command=f"flow {trigger}",
                ones_sync_active=not no_ones_sync,
            )
        )
    if state == "waiting-confirmation":
        # R20 gate 类干预：进入人工确认等待点记 intervention（kind=gate, source=human）。
        events.append(
            _flow_intervention_event(
                workspace, feature,
                kind="gate", source="human", target=step_id, text=None,
                command=f"flow {trigger}", ones_sync_active=not no_ones_sync,
            )
        )
    return events


def apply_flow_set(
    workspace: Path,
    feature: dict[str, Any],
    step_id: str,
    state: str,
    *,
    note: str | None = None,
    confirmed: bool = False,
    force: bool = False,
    reason: str | None = None,
    trigger: str = "set",
    dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None,
    gate_guard: Callable[[Path], Any] | None = None,
    no_ones_sync: bool = False,
) -> dict[str, Any]:
    """flow set/confirm/mode 共用漏斗：落盘状态并产出 `_events`。

    注：``dashboard_refresh`` 自 012 D-6 起不再在本函数内触发（dashboard 重渲染由各 command_* 在
    side_effects 之后挂钩），仅为签名兼容保留（D-13 ②，deprecated），调用方无需再传。
    """
    validate_flow_step_state(state)
    status_path = status_path_for_feature(workspace, feature)
    feature_dir = workspace / feature["feature_dir"]
    # 016 T-09（BC-05）：读-改-写 STATUS.md 与 .step-timings.json 全程持锁，与批量入口互斥，
    # 并发单步/批量不丢更新；写入次序（行 → 最后更新 → 进度概要）与 2.45.0 一致。
    with flow_status_write_lock(status_path):
        table = parse_pipeline_table(status_path)
        plan = plan_step_member(
            workspace,
            feature,
            table,
            step_id,
            state,
            note=note,
            confirmed=confirmed,
            force=force,
            reason=reason,
            gate_guard=gate_guard,
        )
        forced = plan.forced
        prior_state = plan.prior_state  # R7 埋点：置位前状态从行内取得（设计 §4.7.1）
        row = apply_step_member_plan(plan, table)
        cells = row["cells"]
        save_pipeline_rows(status_path, table, [row])
        set_status_field(status_path, "最后更新", flow_today())
        updated_table = parse_pipeline_table(status_path)
        rewrite_progress_summary(status_path, progress_summary_values(updated_table))
        # R7 埋点：STATUS.md 写入成功后调用（flow set/confirm/mode 三入口共用漏斗，fail-safe）
        cycles_before = _step_cycle_entries(feature_dir, step_id)
        timing_warning = record_step_timing(
            feature_dir, step_id, state, prior_state, flow_now_iso(),
            session_ref=flow_session_ref(workspace),
        )
    events = flow_step_member_events(
        workspace,
        feature,
        step_id,
        prior_state,
        state,
        trigger=trigger,
        confirmed=confirmed,
        no_ones_sync=no_ones_sync,
        cycles_before=cycles_before,
    )

    data: dict[str, Any] = {
        "status": "ok",
        "message": f"{step_id} 状态已更新为 {state}。",
        "feature": feature["feature"],
        "step": {
            "id": cells[0],
            "name": cells[1],
            "state": state,
            "note": cells[5],
        },
        "forced": forced,
        "progress": progress_summary_values(updated_table),
    }
    ones_field, ones_intent = flow_ones_follow_up(
        workspace, feature, skip=no_ones_sync, sync_tasks=False
    )
    if ones_field is not None:
        data["ones_sync"] = ones_field
    if ones_intent is not None:
        events.append(ones_intent)
    data["_events"] = events
    attach_step_timing_warning(data, timing_warning)
    return data


_MIGRATE_STEPS_HINT = "执行 devkit-flow flow migrate-steps"


def priority_missing_gate_ids(
    table: dict[str, Any],
    feature: dict[str, Any],
    snapshot: PipelineConfigSnapshot,
    expected_gates: tuple[str, ...],
    scale: str | None = None,
) -> list[str]:
    """配置启用但 STATUS 缺失的必需门，供 next 优先于断点催 migrate-steps。

    公开给 snapshot 采集层复用：flow next 与 dashboard hero 的 next_action
    必须喂同一份门集/缺门优先级，否则两侧文案分叉（S5.7 CF-1/CF-2）。
    """
    # 016 D-42：调用方传入已归一的 scale（feature_scale / _normalize_scale），避免此处按原始头精确匹配
    # 导致 `规模: Small` 变体上缺门检测静默失效（S5.7 Round-2 CX2-P1-1）；未传时自行 strip+lower 兜底。
    if scale is None:
        scale = str((feature.get("summary") or {}).get("scale") or "").strip().lower()
    if scale not in PIPELINE_SCALE_STEPS:
        return []
    mode = feature_resolved_mode(table, feature)
    existing = [row["cells"][0] for row in table["rows"]]
    expected = set(expected_gates)
    enabled_gated: set[str] = set()
    for gate_name, step_ids in CONFIG_GATED_STEPS.items():
        if snapshot.gate(gate_name).enabled:
            enabled_gated.update(step_ids)
    return [
        row[0]
        for row in missing_required_rows(existing, scale, mode, snapshot)
        if row[0] in expected or row[0] in enabled_gated
    ]


def next_action_text(
    table: dict[str, Any],
    breakpoint_row: dict[str, Any] | None,
    dependencies_satisfied: bool,
    expected_gates: tuple[str, ...] | None = None,
    missing_priority: Sequence[str] | None = None,
    narrowed_pending: Sequence[str] | None = None,
) -> str:
    """下一步建议判定文案：flow next 与 dashboard hero 共用（唯一事实源，设计 §4.3.2）。

    expected_gates 默认 COMPLETION_GATE_STEPS 全集（纯函数可测）；命令入口传入
    expected_completion_gate_steps(snapshot, scale)，配置关闭的受控行与规模裁剪掉的行不催补
    （设计 §4.2.5 / 016 D-18）。missing_priority 为配置启用但 STATUS 缺失的必需门；非空时优先于常规断点动作。
    narrowed_pending 为 scale 收窄遗留且仍 pending 的行（016 D-32/D-40）：无其它断点时文案指向
    flow complete 自动归档，而不是建议派发一条已失效的行；两侧（flow next / dashboard）必须喂同一份。
    """
    action = "流水线已全部关闭，可执行 flow complete 收尾。"
    if breakpoint_row is None and narrowed_pending:
        action = (
            f"遗留行 {', '.join(narrowed_pending)} 已因规模收窄失效，将在 flow complete 自动归档"
            "（scale-narrowed），可执行 flow complete 收尾。"
        )
    if missing_priority:
        action = (
            f"流水线缺少 {', '.join(missing_priority)} 完成 Gate："
            f"{_MIGRATE_STEPS_HINT} 补行并真实执行，再 flow complete。"
        )
        return action
    if breakpoint_row is None and table["rows"]:
        ids = {row["cells"][0] for row in table["rows"]}
        gates = COMPLETION_GATE_STEPS if expected_gates is None else expected_gates
        missing_completion_gates = [step_id for step_id in gates if step_id not in ids]
        if missing_completion_gates:
            action = (
                f"流水线缺少 {', '.join(missing_completion_gates)} 完成 Gate："
                f"{_MIGRATE_STEPS_HINT} 补行并真实执行，再 flow complete。"
            )
    if breakpoint_row is not None:
        cells = breakpoint_row["cells"]
        state = cells[4]
        if state == "waiting-confirmation":
            action = "等待人工确认；确认后执行 flow confirm。"
        elif state == "blocked":
            note = cells[5] if len(cells) > 5 else ""
            action = f"步骤被阻塞：{note or '（无备注）'}"
        elif state == "in-progress":
            action = "继续执行。"
        elif dependencies_satisfied:
            action = f"依赖已满足，可派发 {cells[2]}。"
        else:
            action = "依赖未满足，不可派发。"
    return action


def command_flow_next(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    status_path = status_path_for_feature(workspace, feature)
    table = parse_pipeline_table(status_path)
    # 016 D-40：scale/mode 走唯一口径；scale 收窄遗留的 pending 行不作为断点（与 dashboard 同源）。
    _scale = feature_scale(feature)
    _mode = feature_resolved_mode(table, feature)
    _narrowed_ids, _narrowed_pending = scale_narrowed_pending_ids(table, _scale, _mode)
    breakpoint_row = pipeline_breakpoint(table, skip_pending_ids=_narrowed_ids)

    current: dict[str, Any] | None = None
    dependencies: list[dict[str, Any]] = []
    dependencies_satisfied = True
    if breakpoint_row is not None:
        cells = breakpoint_row["cells"]
        current = {
            "id": cells[0],
            "name": cells[1],
            "agent": cells[2],
            "state": cells[4],
            "note": cells[5] if len(cells) > 5 else "",
        }
        dependencies_satisfied, dependencies = dependency_report(table["rows"], cells[3])
    snapshot = pipeline_config_snapshot(workspace)
    expected_gates = expected_completion_gate_steps(snapshot, _scale)
    missing_priority = priority_missing_gate_ids(table, feature, snapshot, expected_gates, scale=_scale)
    action = next_action_text(
        table,
        breakpoint_row,
        dependencies_satisfied,
        expected_gates,
        missing_priority,
        _narrowed_pending,
    )

    rows = [row["cells"] for row in table["rows"]]
    completed = [cells[0] for cells in rows if cells[4] in DONE_STATES]
    waived = [cells[0] for cells in rows if cells[4] in WAIVED_STATES]
    remaining = [cells[0] for cells in rows if cells[4] not in CLOSED_STATES]
    ones_sync_field = feature["summary"].get("ones_sync", "")
    data: dict[str, Any] = {
        "status": "ok",
        "message": action,
        "feature": feature["feature"],
        "current_step": current,
        "dependencies": dependencies,
        "dependencies_satisfied": dependencies_satisfied,
        "action": action,
        "progress": {
            "completed": len(completed),
            "remaining": len(remaining),
            "completed_steps": completed,
            "waived_steps": waived,
            "remaining_steps": remaining,
        },
        # 016 D-40：如实透出 scale 收窄遗留且仍 pending 的行（remaining_steps 仍列物理行，不撒谎）。
        "scale_narrowed_pending": _narrowed_pending,
        "ones": {
            "header_sync": ones_sync_field or None,
            "header_sync_retry": ones_sync_field.startswith("retry"),
            "outbox": flow_outbox_counts(workspace, feature),
        },
    }
    update = maybe_check_latest_update()
    if update is not None:
        data["update"] = update
    emit(data, args.format)
    emit_update_notice(update, args.format)
    return 0


def _custom_step_rows(status_path: Path) -> list[StepAliasRow]:
    """014 R15：从 STATUS.md 主表+流水线历史归档区提取存量自定义行（X-* 等未登记进
    静态别名表的步骤 id），供 resolve_step extra_rows 通道；按 step_id casefold 去重，
    已登记的静态 id 不纳入。只读。"""
    known = {row.step_id.casefold() for row in ALIAS_ROWS}
    collected: dict[str, StepAliasRow] = {}
    sources = [row["cells"] for row in parse_pipeline_table(status_path)["rows"]]
    sources.extend(row["cells"] for row in parse_pipeline_history(status_path)["rows"])
    for cells in sources:
        step_id = str(cells[0]).strip()
        if not step_id or step_id.casefold() in known:
            continue
        key = step_id.casefold()
        if key in collected:
            continue
        name = str(cells[1]).strip() if len(cells) > 1 else ""
        collected[key] = StepAliasRow(
            step_id=step_id, mode="*", slug=key, zh_name=name
        )
    return list(collected.values())


def _collecting_clarification_batch(workspace: Path, feature: dict[str, Any]) -> dict[str, Any] | None:
    """014 R7（D-18）：读取 clarification-state.json，仅 collecting 批次触发 guard。

    clarify.py 顶层 import flow.py（clarify 命令复用 flow 漏斗），flow 侧不能反向
    import clarify；此处按 parse_util 同款 fail-safe 口径只读解析——文件缺失、
    JSON 损坏或 schemaVersion 不符一律视为无批次放行（guard 是纪律卡口，不因
    旁车文件异常阻断流水线状态写入）。
    """
    path = workspace / feature["feature_dir"] / "clarification-state.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    # 批次 7 审查 WARNING-01 收口：实现补上 docstring 声称的 schemaVersion 白名单检查
    #（不识别的版本按无批次放行，guard 不因旁车升版阻断状态写入）。
    if payload.get("schemaVersion") != CLARIFICATION_SCHEMA_VERSION:
        return None
    if payload.get("status") != "collecting":
        return None
    return payload


def clarify_collecting_guard_message(batch: dict[str, Any], *, action: str) -> str:
    """014 R7 collecting 守卫拒绝文案（单步 ``_emit_clarify_collecting_guard`` 与批量入口共用，016 T-10）。"""
    batch_id = str(batch.get("batchId") or "")
    return (
        f"澄清批次 {batch_id} 处于 collecting，{action} 属回写类动作，默认拒绝。"
        "请先 `flow clarify apply` / `flow clarify close` 密封批次后再执行；"
        "确需绕过请显式加 `--override-clarify-guard`（override 将记录为外部干预事件）。"
    )


def _emit_clarify_collecting_guard(args: argparse.Namespace, batch: dict[str, Any], *, action: str) -> int:
    """014 R7（接口文档 §3.4/§7）：collecting 期间回写类动作默认拒绝（CLARIFY_COLLECTING_GUARD）。"""
    batch_id = str(batch.get("batchId") or "")
    data = {
        "status": "error",
        "error_code": "CLARIFY_COLLECTING_GUARD",
        "message": clarify_collecting_guard_message(batch, action=action),
        "batch_id": batch_id,
        "recovery_actions": [
            "flow clarify apply",
            "flow clarify close --change-log-id <id>",
        ],
    }
    emit(data, args.format)
    return 1


def command_flow_set(args: argparse.Namespace, *, dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None, gate_guard: Callable[[Path], Any] | None = None, side_effects: WriteSideEffects | None = None) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    # 014 R7 guard（D-18）：批次 collecting 时回写类 set（目标状态 ∈ FLOW_GUARDED_STATES
    # = in-progress/done/skipped，推进/收尾结论）默认拒绝；pending/blocked/
    # waiting-confirmation 属纯簿记放行。
    collecting_batch = _collecting_clarification_batch(workspace, feature)
    guard_applies = step_clarify_guard_applies(collecting_batch, args.status)
    if guard_applies and not getattr(args, "override_clarify_guard", False):
        return _emit_clarify_collecting_guard(args, collecting_batch, action=f"flow set --status {args.status}")
    status_path = status_path_for_feature(workspace, feature)
    resolution = resolve_step(
        args.step,
        mode=feature_pipeline_mode(feature),
        extra_rows=_custom_step_rows(status_path),
    )
    data = apply_flow_set(
        workspace,
        feature,
        resolution.step_id,
        args.status,
        note=args.note,
        confirmed=args.confirmed,
        force=args.force,
        reason=args.reason,
        dashboard_refresh=dashboard_refresh,
        gate_guard=gate_guard,
        no_ones_sync=_no_ones_sync(args),
    )
    attach_step_resolution(data, resolution)
    if guard_applies:
        # override 放行锚点接线（R20，接口 §3.4）：override 动作记 intervention
        #（kind=clarify, source=human, target=batch_id；仅事件，不阻断）。
        data["clarify_guard_override"] = True
        data.setdefault("_events", []).append(
            _flow_intervention_event(
                workspace, feature,
                kind="clarify", source="human",
                target=str(collecting_batch.get("batchId") or "").strip() or None,
                text=f"override clarify guard: flow set --status {args.status}",
                command="flow set", ones_sync_active=not _no_ones_sync(args),
            )
        )
    data = apply_write_side_effects(data, feature, side_effects)
    attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0


def _accept_task_arch_risk(
    args: argparse.Namespace,
    workspace: Path,
    feature: dict[str, Any],
    reason: str,
    *,
    dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None,
    resolution: StepResolution | None = None,
    side_effects: WriteSideEffects | None = None,
) -> int:
    task_id = markdown_cell(getattr(args, "task_id", None) or "")
    if not task_id:
        raise DevkitFlowError("任务级架构风险接受必须携带 --task-id。")
    status_path = status_path_for_feature(workspace, feature)
    # 016 T-10（D-40）：读-检查-写全程持 STATUS 锁，与单步/批量写互斥。
    with flow_status_write_lock(status_path):
        table = parse_pipeline_table(status_path)
        rows_by_id = {row["cells"][0]: row for row in table["rows"]}
        gate = rows_by_id.get(args.gate)
        if gate is None:
            raise DevkitFlowError(f"流水线中不存在人工 Gate: {args.gate}")
        state = gate["cells"][4]
        if state in DONE_STATES:
            raise DevkitFlowError(f"{args.gate} 已完成，无需重复接受风险。")
        if state in WAIVED_STATES:
            raise DevkitFlowError(f"{args.gate} 已记录风险接受。")
        if state not in {"blocked", "in-progress", "waiting-confirmation"}:
            raise DevkitFlowError(
                f"{args.gate} 当前状态为 {state}；只能对已开始且未通过的任务级架构审查接受风险。"
            )
        cells = gate["cells"]
        cells[4] = "waived"
        while len(cells) < 6:
            cells.append("")
        extra = f"人工风险接受：task={task_id}；{reason}"
        cells[5] = f"{cells[5]}；{extra}" if cells[5] else extra
        save_pipeline_rows(status_path, table, [gate])
        set_status_field(status_path, "最后更新", flow_today())
        updated_table = parse_pipeline_table(status_path)
        progress = progress_summary_values(updated_table)
        rewrite_progress_summary(status_path, progress)
    # 点位 #3（D-14）：任务级架构审查 waiver——发 gate_event(arch-waiver, waived) + step_transition。
    waiver_trace = _flow_feature_trace(workspace, feature)  # D-23：v1 事件 trace 注入
    events: list[dict[str, Any]] = [
        flow_emit_event(
            workspace, feature, "gate_event",
            {
                "step_id": args.gate,
                "gate_kind": "arch-waiver",
                "action": "waived",
                "reason_excerpt": reason[:200] if reason else None,
            },
            command="flow accept-risk",
            trace=waiver_trace,
        ),
        flow_emit_event(
            workspace, feature, "step_transition",
            {
                "step_id": args.gate,
                "from_status": state or None,
                "to_status": "waived",
                "trigger": "confirm",
                "confirmed": True,
            },
            command="flow accept-risk",
            trace=waiver_trace,
        ),
        # R20 gate 类干预：人工风险接受记 intervention（kind=gate, source=human）。
        _flow_intervention_event(
            workspace, feature,
            kind="gate", source="human", target=args.gate, text=reason,
            command="flow accept-risk",
        ),
    ]
    data: dict[str, Any] = {
        "status": "ok",
        "message": (
            f"已记录任务级架构风险接受：{args.gate} 置为 waived（task={task_id}）。"
        ),
        "feature": feature["feature"],
        "gate": args.gate,
        "task_id": task_id,
        "waived_reviews": [args.gate],
        "progress": progress,
    }
    data["_events"] = events
    if resolution is not None:
        attach_step_resolution(data, resolution)
    data = apply_write_side_effects(data, feature, side_effects)
    attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0


def review_artifacts(feature_dir: Path, review_id: str) -> list[Path]:
    keyword = DESIGN_REVIEW_ARTIFACT_KEYWORDS[review_id]
    return [
        path
        for path in sorted(feature_dir.glob("*.md"))
        if keyword in path.name
        and path.is_file()
        and path.stat().st_size > 0
        and not path.name.startswith("review-")
    ]


def command_flow_accept_risk(args: argparse.Namespace, *, dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None, side_effects: WriteSideEffects | None = None) -> int:
    if not args.confirmed:
        raise DevkitFlowError("人工风险接受必须携带 --confirmed。")
    reason = markdown_cell(args.reason or "")
    if not reason:
        raise DevkitFlowError("人工风险接受必须通过 --reason 记录用户接受原因。")

    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    mode = feature_pipeline_mode(feature)
    status_path = status_path_for_feature(workspace, feature)
    resolution = resolve_step(
        args.gate,
        mode=mode,
        allowed=accept_risk_gates_for_mode(mode),
        extra_rows=_custom_step_rows(status_path),
    )
    args.gate = resolution.step_id
    if args.gate in TASK_ARCH_REVIEW_STEPS:
        return _accept_task_arch_risk(
            args,
            workspace,
            feature,
            reason,
            dashboard_refresh=dashboard_refresh,
            resolution=resolution,
            side_effects=side_effects,
        )
    feature_dir = workspace / feature["feature_dir"]
    # 016 T-10（D-40）：读-检查-写（含 step timing）全程持 STATUS 锁。
    with flow_status_write_lock(status_path):
        table = parse_pipeline_table(status_path)
        rows_by_id = {row["cells"][0]: row for row in table["rows"]}
        gate = rows_by_id.get(args.gate)
        if gate is None:
            raise DevkitFlowError(f"流水线中不存在人工 Gate: {args.gate}")
        if gate["cells"][4] in DONE_STATES:
            raise DevkitFlowError(f"{args.gate} 已完成，无需重复接受风险。")

        missing_reviews = [step_id for step_id in REQUIRED_DESIGN_REVIEW_STEPS if step_id not in rows_by_id]
        if missing_reviews:
            raise DevkitFlowError(
                f"流水线缺少必要设计审查步骤: {', '.join(missing_reviews)}；不能用风险接受替代缺失的流程定义。"
            )
        open_reviews = [
            rows_by_id[step_id]
            for step_id in DESIGN_REVIEW_STEPS
            if step_id in rows_by_id and rows_by_id[step_id]["cells"][4] not in CLOSED_STATES
        ]
        if not open_reviews:
            raise DevkitFlowError("没有未关闭的设计审查风险；请使用 flow confirm 完成人工确认。")

        artifact_review_ids = {
            *REQUIRED_DESIGN_REVIEW_STEPS,
            *(review["cells"][0] for review in open_reviews),
        }
        for review_id in DESIGN_REVIEW_STEPS:
            if review_id not in artifact_review_ids:
                continue
            if not review_artifacts(feature_dir, review_id):
                keyword = DESIGN_REVIEW_ARTIFACT_KEYWORDS[review_id]
                raise DevkitFlowError(
                    f"{review_id} 对应的{keyword}正文不存在；风险接受不能替代设计产物。"
                )

        for review in open_reviews:
            review_id = review["cells"][0]
            satisfied, detail = dependency_report(table["rows"], review["cells"][3])
            if not satisfied:
                unmet = [
                    f"{step['id']}({step['state']})"
                    for alternative in detail
                    for step in alternative["steps"]
                    if not step["satisfied"]
                ]
                raise DevkitFlowError(
                    f"{review_id} 的设计产物依赖未满足: {', '.join(unmet)}；"
                    "风险接受只能处理审查遗留项，不能跳过设计正文产出。"
                )

        prospective_rows = copy.deepcopy(table["rows"])
        prospective_by_id = {row["cells"][0]: row for row in prospective_rows}
        for review in open_reviews:
            prospective_by_id[review["cells"][0]]["cells"][4] = "waived"
        gate_dependencies_satisfied, detail = dependency_report(
            prospective_rows, gate["cells"][3]
        )
        if not gate_dependencies_satisfied:
            unmet = [
                f"{step['id']}({step['state']})"
                for alternative in detail
                for step in alternative["steps"]
                if not step["satisfied"]
            ]
            raise DevkitFlowError(
                f"{args.gate} 仍有非设计审查依赖未满足: {', '.join(unmet)}；无法接受风险。"
            )

        dirty_rows: list[dict[str, Any]] = []
        review_prior_states = {
            review["cells"][0]: review["cells"][4] for review in open_reviews
        }
        for review in open_reviews:
            cells = review["cells"]
            cells[4] = "waived"
            while len(cells) < 6:
                cells.append("")
            cells[5] = (
                f"{cells[5]}；人工风险接受：{reason}" if cells[5] else f"人工风险接受：{reason}"
            )
            dirty_rows.append(review)
        gate_cells = gate["cells"]
        gate_prior_state = gate_cells[4]  # R7 埋点：gate 置位前状态（accept-risk 变体）
        gate_cells[4] = "done"
        while len(gate_cells) < 6:
            gate_cells.append("")
        gate_cells[5] = (
            f"{gate_cells[5]}；人工确认并接受审查遗留风险：{reason}"
            if gate_cells[5]
            else f"人工确认并接受审查遗留风险：{reason}"
        )
        dirty_rows.append(gate)

        save_pipeline_rows(status_path, table, dirty_rows)
        set_status_field(status_path, "最后更新", flow_today())
        updated_table = parse_pipeline_table(status_path)
        progress = progress_summary_values(updated_table)
        rewrite_progress_summary(status_path, progress)
        # R7 埋点：gate 置 done 直写路径（绕过漏斗），accept-risk 变体——有开周期闭合、无开周期不记；
        # 审查步骤置 waived 不产生埋点事件（遗留开周期按悬空由自愈规则兜底）。
        timing_warning = record_step_timing(
            feature_dir, args.gate, "done", gate_prior_state, flow_now_iso(), accept_risk=True,
            session_ref=flow_session_ref(workspace),
        )
    # 点位 #2：设计审查 waiver——每个被 waived 的审查步骤发 gate_event(review-waiver, waived)
    # 与 step_transition，gate 置 done 发 step_transition（事件原位产出，_events 契约）。
    excerpt = reason[:200] if reason else None
    waiver_trace = _flow_feature_trace(workspace, feature)  # D-23：v1 事件 trace 注入
    events: list[dict[str, Any]] = []
    for review in open_reviews:
        review_id = review["cells"][0]
        events.append(flow_emit_event(
            workspace, feature, "gate_event",
            {
                "step_id": review_id,
                "gate_kind": "review-waiver",
                "action": "waived",
                "reason_excerpt": excerpt,
            },
            command="flow accept-risk",
            trace=waiver_trace,
        ))
        events.append(flow_emit_event(
            workspace, feature, "step_transition",
            {
                "step_id": review_id,
                "from_status": review_prior_states.get(review_id) or None,
                "to_status": "waived",
                "trigger": "confirm",
                "confirmed": True,
            },
            command="flow accept-risk",
            trace=waiver_trace,
        ))
    events.append(flow_emit_event(
        workspace, feature, "step_transition",
        {
            "step_id": args.gate,
            "from_status": gate_prior_state or None,
            "to_status": "done",
            "trigger": "confirm",
            "confirmed": True,
        },
        command="flow accept-risk",
        trace=waiver_trace,
    ))
    # R20 gate 类干预：人工风险接受记 intervention（kind=gate, source=human）。
    events.append(_flow_intervention_event(
        workspace, feature,
        kind="gate", source="human", target=args.gate, text=reason,
        command="flow accept-risk",
    ))
    events.append(flow_flush_snapshot(workspace, feature, trigger="gate-flush", command="flow accept-risk"))
    data: dict[str, Any] = {
        "status": "ok",
        "message": (
            f"已记录人工风险接受：{', '.join(row['cells'][0] for row in open_reviews)} "
            f"置为 waived，{args.gate} 已人工确认完成。"
        ),
        "feature": feature["feature"],
        "gate": args.gate,
        "reason": reason,
        "waived_reviews": [row["cells"][0] for row in open_reviews],
        "progress": progress,
    }
    ones_field, ones_intent = flow_ones_follow_up(
        workspace, feature, skip=_no_ones_sync(args), sync_tasks=False
    )
    if ones_field is not None:
        data["ones_sync"] = ones_field
    if ones_intent is not None:
        events.append(ones_intent)
    data["_events"] = events
    attach_step_timing_warning(data, timing_warning)
    attach_step_resolution(data, resolution)
    data = apply_write_side_effects(data, feature, side_effects)
    attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0


def _confirm_recovery_actions(feature: dict[str, Any], cells: list[str]) -> list[str]:
    """014 R16（接口文档 §3.2/§7）：confirm 被卡时的可执行恢复命令清单。

    set/rollback 恒可执行；accept-risk 仅当断点步骤属于当前模式可风险接受门时给出
    （「适用时」口径与 command_flow_accept_risk 的 allowed 校验同源）。
    """
    step_id = cells[0]
    actions = [
        f"flow set --step {step_id} --status waiting-confirmation",
        f"flow rollback --step {step_id}",
    ]
    if step_id in accept_risk_gates_for_mode(feature_pipeline_mode(feature)):
        actions.append(f"flow accept-risk --gate {step_id} --reason <原因> --confirmed")
    return actions


def command_flow_confirm(args: argparse.Namespace, *, dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None, gate_guard: Callable[[Path], Any] | None = None, side_effects: WriteSideEffects | None = None) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    status_path = status_path_for_feature(workspace, feature)
    # 016 T-10（D-40）：断点检查与置位同处一个临界区（检查移入锁内，消除 TOCTOU）。
    with flow_status_write_lock(status_path):
        table = parse_pipeline_table(status_path)
        # 016 D-40：与 flow next 同一断点谓词（scale 收窄遗留 pending 行不是断点）。
        _narrowed_ids, _narrowed_pending = scale_narrowed_pending_ids(
            table, feature_scale(feature), feature_resolved_mode(table, feature)
        )
        breakpoint_row = pipeline_breakpoint(table, skip_pending_ids=_narrowed_ids)
        if breakpoint_row is None:
            if _narrowed_pending:
                raise DevkitFlowError(
                    f"流水线已无可确认断点；遗留行 {', '.join(_narrowed_pending)} 已因规模收窄失效，"
                    "将在 flow complete 自动归档（scale-narrowed）。"
                )
            raise DevkitFlowError("流水线已无断点，所有步骤均为 done/skipped/waived。")
        cells = breakpoint_row["cells"]
        if cells[4] != "waiting-confirmation":
            recovery_actions = _confirm_recovery_actions(feature, cells)
            hints = [
                "断点行已具备确认条件：flow set 置回 waiting-confirmation 后重试 confirm",
                "断点行需要重做：flow rollback 回退到断点行",
            ]
            if any(action.startswith("flow accept-risk") for action in recovery_actions):
                hints.append("审查遗留项可人工接受：flow accept-risk（需 --reason 与 --confirmed）")
            data = {
                "status": "error",
                "error_code": "CONFIRM_BLOCKED",
                "message": (
                    f"当前断点 {cells[0]} 的状态为 {cells[4]}，不是 waiting-confirmation，无法确认。"
                    f"恢复路径：{'；'.join(hints)}。"
                    f"可执行命令：{'；'.join(recovery_actions)}"
                ),
                "recovery_actions": recovery_actions,
            }
            emit(data, args.format)
            return 1
        step_id = cells[0]
        data = apply_flow_set(
            workspace,
            feature,
            step_id,
            "done",
            confirmed=True,
            trigger="confirm",
            dashboard_refresh=dashboard_refresh,
            gate_guard=gate_guard,
            no_ones_sync=_no_ones_sync(args),
        )
    data["message"] = f"{step_id} 人工确认完成，状态已置为 done。"
    # 点位 #1 confirm：人工 Gate（-H）放行发 gate_event，并作为 flush 点发 gate-flush 快照。
    if step_id.endswith("-H"):
        events = data.setdefault("_events", [])
        events.append(flow_emit_event(
            workspace,
            feature,
            "gate_event",
            {
                "step_id": step_id,
                "gate_kind": "human-gate",
                "action": "confirmed",
                "reason_excerpt": None,
            },
            command="flow confirm",
            trace=_flow_feature_trace(workspace, feature),  # D-23：v1 事件 trace 注入
        ))
        # R20 gate 类干预：人工 Gate 放行记 intervention（kind=gate, source=human）。
        events.append(_flow_intervention_event(
            workspace, feature,
            kind="gate", source="human", target=step_id, text=None,
            command="flow confirm", ones_sync_active=not _no_ones_sync(args),
        ))
        events.append(flow_flush_snapshot(workspace, feature, trigger="gate-flush", command="flow confirm"))
    data = apply_write_side_effects(data, feature, side_effects)
    attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0


def _dependency_downstream(table: dict[str, Any], start_step: str) -> list[str]:
    """依赖列反向传递可达的下游步骤 id（按主表行序）。

    依赖解析复用 dependency_alternatives（或-备选、逗号并列、括号注记剥离同一口径）；
    只计入主表内存在的步骤 id，未识别注记不参与闭包。
    """
    known = {row["cells"][0] for row in table["rows"]}
    dependents: dict[str, list[str]] = {}
    for row in table["rows"]:
        step_id = row["cells"][0]
        for alternative in dependency_alternatives(row["cells"][3]):
            for dep in alternative:
                if dep in known and dep != step_id:
                    dependents.setdefault(dep, []).append(step_id)
    reachable: set[str] = set()
    frontier = [start_step]
    while frontier:
        current = frontier.pop()
        for candidate in dependents.get(current, []):
            if candidate not in reachable:
                reachable.add(candidate)
                frontier.append(candidate)
    return [row["cells"][0] for row in table["rows"] if row["cells"][0] in reachable]


def _artifact_patterns_overlap(left: str, right: str) -> bool:
    """产物 glob 相交启发式（014 R14）：等值或双向 fnmatch 任一命中即判交。

    glob 集合的精确交集不可静态判定；本函数取保守方向（宁可判交不判离），
    与「未登记保守含入」共同守质量底线（设计 §4.0 D-A5-2）。
    """
    return (
        left == right
        or fnmatch.fnmatchcase(left, right)
        or fnmatch.fnmatchcase(right, left)
    )


def compute_affected_closure(table: dict[str, Any], start_step: str, artifact_map: dict[str, list[str]]) -> list[str]:
    """014 R14 受影响闭包（设计 §4.0 D-A5-2 / 接口文档 §6）：目标步骤 ∪ 受影响下游。

    下游步骤 s 含入条件：依赖列传递可达 且（s 未登记 artifact_map（保守含入，宁多勿漏）
    或 s 的产物 glob 与受影响产物集合相交）。受影响产物集合随含入增长，迭代至不动点；
    返回值按主表行序。
    WARNING-01（批次 3 架构审查）：起点未登记/产物为空时种子为空，注册下游会被全部裁出
    （实证 closure(S0.5-H) 仅剩未登记步骤）——按「宁多勿漏」退化为依赖传递可达全下游
    （等价于跳过产物裁剪）；设计文档侧的语义登记由 architect 同步处理。
    """
    downstream = _dependency_downstream(table, start_step)
    affected_artifacts = set(artifact_map.get(start_step) or [])
    if not affected_artifacts:
        downstream_set = set(downstream)
        return [
            row["cells"][0]
            for row in table["rows"]
            if row["cells"][0] == start_step or row["cells"][0] in downstream_set
        ]
    affected = {start_step}
    changed = True
    while changed:
        changed = False
        for step_id in downstream:
            if step_id in affected:
                continue
            patterns = artifact_map.get(step_id)
            if patterns is None or any(
                _artifact_patterns_overlap(pattern, affected_pattern)
                for pattern in patterns
                for affected_pattern in affected_artifacts
            ):
                affected.add(step_id)
                affected_artifacts.update(patterns or [])
                changed = True
    return [row["cells"][0] for row in table["rows"] if row["cells"][0] in affected]


def _current_cycle_index(feature_dir: Path, step_id: str) -> int:
    """被归档 cycle 序号 = 该步骤开周期基位置（T5 移交口径：len(cycles)）。

    无旁车文件/schema 不识别/无周期条目（存量 feature 正常态）→ 1（免迁移双格式兜底，
    存量视为 cycle 1）；任何异常 fail-safe → 1，绝不阻断 rollback 主流程。
    """
    try:
        path = feature_dir / DASHBOARD_STEP_TIMINGS_FILE
        if not path.exists():
            return 1
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or loaded.get("schema") != STEP_TIMINGS_SCHEMA_VERSION:
            return 1
        steps = loaded.get("steps")
        entry = steps.get(step_id) if isinstance(steps, dict) else None
        cycles = entry.get("cycles") if isinstance(entry, dict) else None
        if not isinstance(cycles, list) or not cycles:
            return 1
        return len(cycles)
    except Exception:
        return 1


def archive_cycle_rows(status_path: Path, table: dict[str, Any], rows: list[dict[str, Any]], cycle_index: int, reason: str) -> None:
    """014 R14 一体封装（接口文档 §6）：当前 cycle 行归档 + 主表新一轮 pending 行。

    归档侧复用 parse_util.append_pipeline_history_rows（T4 原子 append，(步骤, cycle_index)
    幂等、惰性创建归档区），归档行保留置位前状态（主表 6 列 + cycle_index/归档时间/归档原因
    共 9 列）；随后重解析主表（归档 append 可能移动行号），把 rows 对应步骤置 pending 并
    追加回滚备注。两段写各自 atomic_write（设计 §4.3.2 WARNING-03：无跨文件事务，崩溃
    中间态由 record_step_timing 悬空开周期自愈与 flow verify cycle 对账兜底）。

    MAJOR-01（批次 3 架构审查）：归档 cycle_index 逐步骤取各自开周期基位置
    （_current_cycle_index(feature_dir, step_id)，与 record_step_timing heal 的 per-step
    口径及 D-5「两处 cycle_index 同源同值」对齐），按 cycle_index 分组调用 append；
    多 cycle 分叉场景（如 S2.1-R 先单独回滚过、再随 S2.1 回滚）下非目标步骤的当前
    cycle 归档行不再被幂等键跳过、heal 不再失配落入废弃兜底。入参 cycle_index 为
    缺省兜底（逐步骤查得值优先；当前实现逐步骤计算恒有返回值，参数保留作契约兼容）。
    T17 锚点：多 cycle 分叉回归用例覆盖本条。
    """
    archived_at = flow_now_iso()
    feature_dir = status_path.parent
    groups: dict[int, list[list[str]]] = {}
    for row in rows:
        step_cycle_index = _current_cycle_index(feature_dir, str(row["cells"][0]))
        groups.setdefault(step_cycle_index, []).append(list(row["cells"][:6]))
    for step_cycle_index in sorted(groups):
        append_pipeline_history_rows(
            status_path, groups[step_cycle_index], step_cycle_index,
            archived_at=archived_at, reason=reason,
        )
    fresh = parse_pipeline_table(status_path)
    by_id = {row["cells"][0]: row for row in fresh["rows"]}
    dirty: list[dict[str, Any]] = []
    for row in rows:
        target = by_id.get(row["cells"][0])
        if target is None:
            continue
        cells = target["cells"]
        cells[4] = "pending"
        if reason:
            while len(cells) < 6:
                cells.append("")
            append = f"回滚：{reason}"
            cells[5] = f"{cells[5]}；{append}" if cells[5] else append
        dirty.append(target)
    save_pipeline_rows(status_path, fresh, dirty)


def command_flow_rollback(args: argparse.Namespace, *, dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None, side_effects: WriteSideEffects | None = None) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    # 014 R7 guard（D-18）：批次 collecting 时 rollback 不限目标步骤全拒；
    # guard 必须位于归档/周期闭合等一切写操作之前（T7 cycle 化后为跨文件写）。
    collecting_batch = _collecting_clarification_batch(workspace, feature)
    if collecting_batch is not None and not getattr(args, "override_clarify_guard", False):
        return _emit_clarify_collecting_guard(args, collecting_batch, action="flow rollback")
    status_path = status_path_for_feature(workspace, feature)
    resolution = resolve_step(
        args.step,
        mode=feature_pipeline_mode(feature),
        extra_rows=_custom_step_rows(status_path),
    )
    args.step = resolution.step_id
    # 016 T-10（D-40）：归档 + 字段 + 进度概要 + step timing 全程持 STATUS 锁。
    with flow_status_write_lock(status_path):
        table = parse_pipeline_table(status_path)
        start = next(
            (
                index
                for index, row in enumerate(table["rows"])
                if row["cells"][0] == args.step
            ),
            None,
        )
        if start is None:
            raise DevkitFlowError(f"流水线中不存在步骤: {args.step}")

        # 014 R14（D-A5 有意语义变更）：默认 --affected-only 取受影响闭包；
        # --all-downstream 保留旧语义（目标步骤到表尾全部重置）。
        if getattr(args, "all_downstream", False):
            affected = table["rows"][start:]
        else:
            closure = set(compute_affected_closure(table, args.step, STEP_ARTIFACT_MAP))
            affected = [row for row in table["rows"] if row["cells"][0] in closure]
        affected_ids = [row["cells"][0] for row in affected]
        unaffected = [
            step_id
            for step_id in _dependency_downstream(table, args.step)
            if step_id not in affected_ids
        ]
        prior_states = {row["cells"][0]: row["cells"][4] for row in affected}

        # cycle 化（014 D-5）：当前 cycle 行移入 append-only 归档区（保留置位前状态），
        # 主表生成新一轮 pending 行；归档 cycle_index 逐步骤取各自开周期基位置（MAJOR-01，
        # 机器事实源 = .step-timings.json cycles[]，两处同源同值），存量无归档区/无旁车视为
        # cycle 1；JSON 输出的 cycle_index 以回滚起点步骤为锚（新 cycle 序号）。
        feature_dir = workspace / feature["feature_dir"]
        archived_cycle = _current_cycle_index(feature_dir, args.step)
        archive_cycle_rows(status_path, table, affected, archived_cycle, (args.reason or "").strip())
        set_status_field(status_path, "最后更新", flow_today())
        rewrite_progress_summary(status_path, progress_summary_values(parse_pipeline_table(status_path)))

        # 014 D-5 缺陷修复（原 flow.py:669-672 废弃删除）：归档完成后对每个回退步骤走
        # record_step_timing pending 闭合路径（T5 移交：开周期按归档时间闭合 ended_by=rollback，
        # 回退周期耗时可计量；无归档信息的悬空开周期沿用废弃兜底）。
        now = flow_now_iso()
        session_ref = flow_session_ref(workspace)
        timing_warnings: list[str] = []
        cycle_events: list[dict[str, Any]] = []
        for step_id in affected_ids:
            cycles_before = _step_cycle_entries(feature_dir, step_id)
            timing_warning = record_step_timing(
                feature_dir, step_id, "pending", prior_states.get(step_id) or "", now,
                session_ref=session_ref,
            )
            if timing_warning:
                timing_warnings.append(timing_warning)
            # R19（T7 预留点接线）：每个受影响步骤本次新闭合的周期发 step_cycle
            #（ended_by=rollback 取自 cycles[] 条目本身，与归档/闭合同源；逐步骤差分，
            # 崩溃自愈补齐闭合同样覆盖；废弃兜底删除的悬空开周期不发事件）。
            cycle_events.extend(
                _step_cycle_events_since(
                    workspace, feature, step_id, cycles_before, command="flow rollback"
                )
            )

    # 点位 #4：每个被回退步骤发一条 step_transition(trigger=rollback, to_status=pending)；
    # step_cycle 先于 step_transition（设计 §4.2.1 时序：闭合周期 → 发 step_cycle → 发 step_transition）。
    rollback_trace = _flow_feature_trace(workspace, feature)  # D-23：v1 事件 trace 注入
    events: list[dict[str, Any]] = cycle_events + [
        flow_emit_event(
            workspace, feature, "step_transition",
            {
                "step_id": step_id,
                "from_status": prior_states.get(step_id) or None,
                "to_status": "pending",
                "trigger": "rollback",
                "confirmed": False,
            },
            command="flow rollback",
            trace=rollback_trace,
        )
        for step_id in affected_ids
    ]
    design_task_ids = sorted(
        {ROLLBACK_DESIGN_TASK_HINTS[step] for step in affected_ids if step in ROLLBACK_DESIGN_TASK_HINTS}
    )
    message = f"已回滚 {len(affected_ids)} 个步骤: {', '.join(affected_ids)}。"
    if unaffected:
        message += f" 下游未受影响步骤保持不变: {', '.join(unaffected)}。"
    if design_task_ids:
        message += (
            f" 回滚范围覆盖设计/审查步骤，对应设计任务 {', '.join(design_task_ids)} "
            "需人工复核（未自动修改）。"
        )
    data: dict[str, Any] = {
        "status": "ok",
        "message": message,
        "feature": feature["feature"],
        "step": resolution.step_id,
        "affected_steps": affected_ids,
        "cycle_index": archived_cycle + 1,
        "archived_steps": affected_ids,
        "unaffected_downstream": unaffected,
        "design_tasks_to_review": design_task_ids,
    }
    data["_events"] = events
    attach_step_timing_warning(data, "；".join(timing_warnings) if timing_warnings else None)
    attach_step_resolution(data, resolution)
    if collecting_batch is not None:
        # override 放行锚点接线（R20，接口 §3.4）：override 动作记 intervention
        #（kind=clarify, source=human, target=batch_id；仅事件，不阻断）。
        data["clarify_guard_override"] = True
        data["_events"].append(
            _flow_intervention_event(
                workspace, feature,
                kind="clarify", source="human",
                target=str(collecting_batch.get("batchId") or "").strip() or None,
                text="override clarify guard: flow rollback",
                command="flow rollback",
            )
        )
    data = apply_write_side_effects(data, feature, side_effects)
    attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0


def validate_flow_task_state(state: str) -> None:
    """任务目标状态枚举守卫（设计/开发任务共用）。"""
    if state not in FLOW_TASK_STATES:
        raise FlowGuardError(f"非法任务状态: {state}（可选: {', '.join(FLOW_TASK_STATES)}）", guard="invalid")


@dataclass
class TaskMemberPlan:
    """``plan_task_member`` 的求值结果：守卫已通过、尚未写入表。``cells`` 为该行置位后的完整单元格。"""

    task_id: str
    is_design: bool
    row: dict[str, str]
    note_column: str
    add_note_column: bool
    prior_state: str
    state: str
    cells: dict[str, str]
    reopened: bool
    baseline: dict[str, str] | None = None


def task_note_column(table: dict[str, Any], *, is_design: bool) -> str:
    if is_design:
        return "备注"
    if "产出/备注" in table["headers"]:
        return "产出/备注"
    return "备注"


def plan_task_member(
    table: dict[str, Any],
    task_id: str,
    state: str,
    *,
    is_design: bool,
    note: str | None,
    confirm_gates: bool,
    now: str,
) -> TaskMemberPlan:
    """016 T-09（§4.7.2.3）：任务置位的纯守卫与单元格计算（不写盘、不改 ``table``）。

    守卫与单元格语义与 2.45.0 ``command_flow_task_set`` 一致：状态枚举 → 任务存在 → ``已完成`` 需
    ``--confirm-gates``；「执行中」首置记实际开始、自「已完成」重开记原完成并清空实际完成；
    「已完成」每次都改写实际完成（重复完成不拒绝，R-24）；``note`` 追加到备注列。
    """
    validate_flow_task_state(state)
    row = find_task_row(table, task_id)
    note_column = task_note_column(table, is_design=is_design)
    add_note_column = note_column not in table["headers"]

    cells = dict(row)
    if add_note_column:
        cells.setdefault(note_column, "")
    previous = cells.get("状态", "").strip()
    reopened = False
    if state == "执行中":
        if not cells.get("实际开始", "").strip():
            cells["实际开始"] = now
        if previous == "已完成":
            old_completed = cells.get("实际完成", "").strip()
            if old_completed:
                append_cell_note(cells, note_column, f"原完成：{old_completed}")
            cells["实际完成"] = ""
            reopened = True
    elif state == "已完成":
        if not confirm_gates:
            raise FlowGuardError(
                "置为已完成必须携带 --confirm-gates（声明 Gate 矩阵已关闭）。",
                guard="gate",
            )
        cells["实际完成"] = now
    cells["状态"] = state
    if note:
        append_cell_note(cells, note_column, note)
    return TaskMemberPlan(
        task_id=task_id,
        is_design=is_design,
        row=row,
        note_column=note_column,
        add_note_column=add_note_column,
        prior_state=previous,
        state=state,
        cells=cells,
        reopened=reopened,
        baseline=dict(row),
    )


def apply_task_member_plan(table: dict[str, Any], plan: TaskMemberPlan) -> dict[str, str]:
    """把 ``plan_task_member`` 结果写入内存表（必要时补备注列，不落盘），返回被改写的行。

    用法约束（T-09 审查 P2-B）：同一张表上 plan → apply 交替执行。``plan.cells`` 是 plan 时整行的快照，
    若同一行在 plan 之后已被改写（例如对同一任务先后 plan 两次再统一 apply），或 ``plan.row`` 不属于
    传入的 ``table``（套到重新 parse 的表上），直接抛错，杜绝静默丢更新。
    """
    if not any(candidate is plan.row for candidate in table["rows"]):
        raise DevkitFlowError(f"内部错误：{plan.task_id} 的置位计划不属于当前任务表（须同表 plan→apply 交替）。")
    if plan.baseline is not None and plan.row != plan.baseline:
        raise DevkitFlowError(f"内部错误：{plan.task_id} 在 plan 之后已被改写（须 plan→apply 交替，不得重复 plan 同一任务）。")
    if plan.add_note_column and plan.note_column not in table["headers"]:
        table["headers"].append(plan.note_column)
        for candidate in table["rows"]:
            candidate.setdefault(plan.note_column, "")
    plan.row.update(plan.cells)
    return plan.row


def flow_task_member_event(
    workspace: Path,
    feature: dict[str, Any],
    task_id: str,
    previous: str,
    state: str,
    *,
    is_design: bool,
    no_ones_sync: bool,
) -> dict[str, Any]:
    """016 T-10：任务置位的 task_transition 事件（单步 ``flow task set`` 与批量入口共用，逐位一致）。"""
    # 点位 #5：设计/开发任务状态迁移发 task_transition(trigger=set)。
    return flow_emit_event(
        workspace, feature, "task_transition",
        {
            "task_id": task_id,
            "task_kind": "design" if is_design else "dev",
            "from_status": previous or None,
            "to_status": state,
            "trigger": "set",
            "ones_task_id": _payload_ones_task_id(workspace, feature, task_id),
        },
        command="flow task set",
        ones_sync_active=not no_ones_sync,
        trace=_flow_feature_trace(workspace, feature),  # D-23：v1 事件 trace 注入
    )


def command_flow_task_set(args: argparse.Namespace, *, dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None, side_effects: WriteSideEffects | None = None) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    task_id = args.task_id
    state = args.status
    validate_flow_task_state(state)

    is_design = task_id.startswith("D-")
    if is_design:
        target_path = status_path_for_feature(workspace, feature)
        save_table = save_design_task_table
    else:
        target_path = task_path_for_feature(workspace, feature)
        if not target_path.exists():
            raise DevkitFlowError(f"Task file not found: {target_path}")
        save_table = save_task_table

    # 016 T-09（BC-05）：读-改-写全程持锁（D-xx 锁 STATUS.md，与 flow set / 批量互斥；T-xx 锁 task.md）。
    # 重复「已完成」行为保持 2.45.0 不变（重写实际完成并再发 task_transition；BC-16 只改 manager 文本）。
    with flow_status_write_lock(target_path):
        table = parse_design_task_table(target_path) if is_design else parse_task_table(target_path)
        plan = plan_task_member(
            table,
            task_id,
            state,
            is_design=is_design,
            note=args.note,
            confirm_gates=bool(args.confirm_gates),
            now=flow_now_iso(),
        )
        row = apply_task_member_plan(table, plan)
        save_table(target_path, table)
    previous = plan.prior_state
    reopened = plan.reopened

    events: list[dict[str, Any]] = [
        flow_task_member_event(
            workspace, feature, task_id, previous, state,
            is_design=is_design, no_ones_sync=_no_ones_sync(args),
        )
    ]

    data: dict[str, Any] = {
        "status": "ok",
        "message": f"任务 {task_id} 状态已更新为 {state}。",
        "feature": feature["feature"],
        "task": {
            "id": task_id,
            "kind": "design" if is_design else "development",
            "state": state,
            "actual_start": row.get("实际开始", ""),
            "actual_end": row.get("实际完成", ""),
            "reopened": reopened,
        },
    }
    ones_field, ones_intent = flow_ones_follow_up(
        workspace, feature, [task_id], skip=_no_ones_sync(args)
    )
    if ones_field is not None:
        data["ones_sync"] = ones_field
    if ones_intent is not None:
        events.append(ones_intent)
    data["_events"] = events
    data = apply_write_side_effects(data, feature, side_effects)
    attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0


def command_flow_task_ensure_small(args: argparse.Namespace, *, dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None, side_effects: WriteSideEffects | None = None) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    scale = feature["summary"].get("scale", "").strip().lower()
    if scale != "small":
        raise DevkitFlowError(
            f"flow task ensure-small 仅适用于 small feature；当前规模为 {scale or 'unknown'}。"
        )
    description = markdown_cell(args.description)
    if not description:
        raise DevkitFlowError("--description 不能为空。")

    task_path = task_path_for_feature(workspace, feature)
    # 016 T-10（D-40）：task.md 读-改-写持 task.md 锁（与 flow task set T-xx 互斥）。
    with flow_status_write_lock(task_path):
        created = False
        if task_path.exists():
            table = parse_task_table(task_path)
            existing = next(
                (row for row in table["rows"] if row.get("任务ID", "").strip() == "T-01"),
                None,
            )
            if existing is None and table["rows"]:
                ids = ", ".join(row.get("任务ID", "?") for row in table["rows"])
                raise DevkitFlowError(
                    f"small feature 已有开发任务 {ids}，无法安全推断 T-01；请先核对现有 task.md。"
                )
            if existing is None:
                existing = small_task_row(description, args.write_scope, args.acceptance)
                for header in SMALL_TASK_HEADERS:
                    if header not in table["headers"]:
                        output_index = (
                            table["headers"].index("产出/备注")
                            if "产出/备注" in table["headers"]
                            else len(table["headers"])
                        )
                        table["headers"].insert(output_index, header)
                        for row in table["rows"]:
                            row.setdefault(header, "")
                table["rows"].append(existing)
                save_task_table(task_path, table)
                created = True
        else:
            existing = small_task_row(description, args.write_scope, args.acceptance)
            write_new_small_task_file(task_path, feature["feature"], existing)
            created = True

    # 点位 #6：实际创建 T-01 时才发 task_transition(trigger=ensure-small)，已存在则幂等跳过。
    events: list[dict[str, Any]] = []
    if created:
        created_task_id = str(existing.get("任务ID") or "T-01")
        events.append(flow_emit_event(
            workspace, feature, "task_transition",
            {
                "task_id": created_task_id,
                "task_kind": "dev",
                "from_status": None,
                "to_status": str(existing.get("状态") or "待开始"),
                "trigger": "ensure-small",
                "ones_task_id": _payload_ones_task_id(workspace, feature, created_task_id),
            },
            command="flow task ensure-small",
            trace=_flow_feature_trace(workspace, feature),  # D-23：v1 事件 trace 注入
        ))

    data: dict[str, Any] = {
        "status": "ok",
        "message": (
            "Small Feature 开发任务 T-01 已创建。"
            if created
            else "Small Feature 开发任务 T-01 已存在，未重复创建或覆盖。"
        ),
        "feature": feature["feature"],
        "created": created,
        "task": {
            "id": existing.get("任务ID", "T-01"),
            "description": existing.get("描述", ""),
            "state": existing.get("状态", ""),
            "ones_task_id": existing.get("ONES任务ID", ""),
        },
        "task_file": task_path.relative_to(workspace).as_posix(),
    }
    ones_field, ones_intent = flow_ones_follow_up(
        workspace, feature, ["T-01"], skip=_no_ones_sync(args)
    )
    if ones_field is not None:
        data["ones_sync"] = ones_field
    if ones_intent is not None:
        events.append(ones_intent)
    data["_events"] = events
    data = apply_write_side_effects(data, feature, side_effects)
    attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0


def command_flow_complete(args: argparse.Namespace, *, dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None, side_effects: WriteSideEffects | None = None) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    status_path = status_path_for_feature(workspace, feature)
    # 016 T-10（D-40）：前置校验与全部写盘（含经漏斗的归档、设计任务补行、状态字段）同处一个临界区；漏斗内锁可重入。
    with flow_status_write_lock(status_path):
        table = parse_pipeline_table(status_path)
        rows = [row["cells"] for row in table["rows"]]
        # 完成 Gate 集合：S5-R 永远必需；S5.7 按 scale（small 去 S5.7，016 D-30）；S5-E2E 按配置快照。
        # 016 D-40：scale/mode 走唯一口径（非法 scale 结构化报错；mode 未确认按两种模式并集裁剪）。
        _scale = feature_scale(feature)
        _mode = feature_resolved_mode(table, feature)
        expected_gates = set(expected_completion_gate_steps(pipeline_config_snapshot(workspace), _scale))
        gate_cells = {
            step_id: next((cells for cells in rows if cells[0] == step_id), None)
            for step_id in COMPLETION_GATE_STEPS
        }
        s5r = gate_cells["S5-R"]
        if s5r is None:
            raise DevkitFlowError(
                "流水线中不存在 S5-R（受影响模块全量回归），无法完成；"
                f"{_MIGRATE_STEPS_HINT} 补行并执行不带测试选择器的模块全量测试。"
            )
        if s5r[4] not in DONE_STATES:
            raise DevkitFlowError(f"S5-R 当前状态为 {s5r[4]}，模块全量回归未通过，无法完成。")
        # 016 D-30/D-18：S5.7 只对含它的档（large/large-no-context）硬检查；small 完成门不含 S5.7，
        # 其存量迁移遗留的 S5.7 行交下方 D-32 自动归档 + open_steps 处理，不在此拦截。
        s57 = gate_cells["S5.7"]
        if "S5.7" in expected_gates:
            if s57 is None:
                raise DevkitFlowError(
                    f"流水线中不存在 S5.7（Feature 级综合审查），无法完成；{_MIGRATE_STEPS_HINT} 补行。"
                )
            if s57[4] not in DONE_STATES:
                raise DevkitFlowError(f"S5.7 当前状态为 {s57[4]}，综合审查未通过，无法完成。")
        else:
            # 016 D-39（S5.7 P0-1）：完成门不含 S5.7 的档（small）没有 dev-once 递延落点——任务级架构审查
            # 若为 skipped 即零架构审查（违反 FEATURE N8）。置位时刻已由 apply_flow_set 拒绝，此处对存量兜底。
            _deferred_arch = [
                cells[0]
                for cells in rows
                if cells[0] in task_arch_review_steps_for_mode(_mode) and cells[4] in SKIPPED_STATES
            ]
            if _deferred_arch:
                raise DevkitFlowError(_task_arch_deferred_without_s57_message(_deferred_arch, scale=_scale))
        invalid_waived_steps = [
            cells[0]
            for cells in rows
            if cells[4] in WAIVED_STATES and cells[0] not in WAIVABLE_STEPS
        ]
        if invalid_waived_steps:
            raise DevkitFlowError(
                "waived 仅允许用于 S2 设计审查步骤与任务级架构审查（S5.5/S5.3）；"
                "以下步骤必须恢复为真实状态后才能完成: "
                + ", ".join(invalid_waived_steps)
                + "。"
            )
        # S5-E2E 准出（设计 §4.2.5）：行缺失+配置关闭合法；行缺失+配置开启才催 migrate-steps；
        # 行存在时现行为不变（done，或 skipped 且备注列含非空书面理由，R6）。
        s5e2e = gate_cells[E2E_GATE_STEP]
        if s5e2e is None:
            if E2E_GATE_STEP in expected_gates:
                raise DevkitFlowError(
                    f"流水线中不存在 {E2E_GATE_STEP}（需求准出 e2e 门），无法完成；"
                    f"{_MIGRATE_STEPS_HINT} 补行。"
                )
        elif s5e2e[4] not in DONE_STATES:
            s5e2e_note = s5e2e[5].strip() if len(s5e2e) > 5 else ""
            if not (s5e2e[4] in SKIPPED_STATES and s5e2e_note):
                raise DevkitFlowError(
                    f"{E2E_GATE_STEP} 当前状态为 {s5e2e[4]}，e2e 准出门未关闭，无法完成；"
                    "须 done（gate verdict=pass），或 skipped 且备注列含非空书面理由。"
                )
        # 016 D-32：存量 small feature 经 migrate-steps（只插不删）会留下 scale 收窄后已不属当前档的
        # pending 遗留行（如 small 的 S5.7）。完成门已按 scale 排除它，但 open_steps 全表终结检查仍要求
        # 它关闭——两层口径不一致会误报「存在未关闭步骤」。校验阶段把「pending ∧ scale 收窄多余 ∧ 非配置门」
        # 的遗留行视为将归档；真正的归档写盘在**全部前置校验通过之后**、经 apply_flow_set 漏斗执行
        # （016 D-41：失败命令零写盘；落盘/最后更新/进度概要/step timing/step_transition 事件一并产出）。
        # 仅 pending 生效；done/in-progress/blocked 保留真实轨迹。判定边界经 scale_narrowed_step_ids 双保险排除配置门行。
        _narrowed_ids, _narrowed_pending = scale_narrowed_pending_ids(table, _scale, _mode)
        open_steps = [
            f"{cells[0]}({cells[4]})"
            for cells in rows
            if cells[4] not in CLOSED_STATES
            and not (cells[0] in _narrowed_ids and cells[4] == "pending")
        ]
        if open_steps:
            raise DevkitFlowError(f"存在未关闭步骤: {', '.join(open_steps)}。")
        if _scale == "small":
            task_path = task_path_for_feature(workspace, feature)
            if not task_path.exists() or not any(
                row.get("任务ID", "").strip() == "T-01"
                for row in parse_task_table(task_path)["rows"]
            ):
                raise DevkitFlowError(
                    "Small Feature 缺少开发任务，无法完成；先执行 flow task ensure-small 创建 T-01。"
                )
        # ---- 全部前置校验通过：以下才开始写盘 ----
        events: list[dict[str, Any]] = []
        archived_steps: list[dict[str, str]] = []
        for _step_id in _narrowed_pending:
            _row = next(candidate for candidate in table["rows"] if candidate["cells"][0] == _step_id)
            _reason = f"scale-narrowed: 遗留行按 {_scale} 档自动归档"
            _prior_note = _row["cells"][5] if len(_row["cells"]) > 5 else ""
            _satisfied, _ = dependency_report(table["rows"], _row["cells"][3])
            # 遗留行的依赖可能指向已被裁剪的行（如 canonical S5.7 → S5-E2E）：此时以 force+reason 走漏斗，
            # 备注由漏斗写「强制置位：{reason}」；依赖满足时直接把 reason 追加到原备注。
            # ONES follow-up 由本命令末尾统一发一次需求同步，此处不重复入队。
            _result = apply_flow_set(
                workspace,
                feature,
                _step_id,
                "skipped",
                note=(f"{_prior_note}；{_reason}" if _prior_note.strip() else _reason) if _satisfied else None,
                force=not _satisfied,
                reason=None if _satisfied else _reason,
                trigger="complete",
                no_ones_sync=True,
            )
            events.extend(_result.get("_events") or [])
            archived_steps.append(
                {"step": _step_id, "from_status": "pending", "to_status": "skipped", "reason": _reason}
            )
        if _narrowed_pending:
            table = parse_pipeline_table(status_path)
            rows = [row["cells"] for row in table["rows"]]
        if _scale == "small":
            ensure_design_tasks(
                status_path,
                "small",
                [{"id": cells[0], "state": cells[4]} for cells in rows],
            )
        set_status_field(status_path, "状态", "已完成")
    # 点位 #7：flow complete 发 feature_completed（统计口径对齐 snapshot.py），
    # 随后作为 flush 点发 complete-flush 快照（事件原位产出，_events 契约）。
    completed_event = _emit_feature_completed(workspace, feature, status_path, rows)
    if completed_event is not None:
        events.append(completed_event)
    events.append(flow_flush_snapshot(workspace, feature, trigger="complete-flush", command="flow complete"))
    # 报告生成/打开为 report/html_ui 适配层副作用：转为意图事件，由 cli 装配层执行（T-06），
    # 执行后由装配层补 data["report"] 与 message 的报告分句（保持既有 JSON 契约）。
    events.append(_flow_event("write_workflow_report", {}, command="flow complete"))
    if not args.no_open:
        events.append(_flow_event("open_local_html_report", {}, command="flow complete"))
    data: dict[str, Any] = {
        "status": "ok",
        "message": "所有流水线步骤已关闭，feature 状态已置为 已完成。",
        "feature": feature["feature"],
        # 016 D-32：如实呈现本次完成时自动归档的 scale 收窄遗留行（空数组表示无遗留行）。
        "archived_steps": archived_steps,
    }
    ones_field, ones_intent = flow_ones_follow_up(
        workspace, feature, skip=_no_ones_sync(args), sync_tasks=False
    )
    if ones_field is not None:
        data["ones_sync"] = ones_field
    if ones_intent is not None:
        events.append(ones_intent)
    data["_events"] = events
    data = apply_write_side_effects(data, feature, side_effects)
    attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0


def command_flow_archive(args: argparse.Namespace, *, dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None, side_effects: WriteSideEffects | None = None) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    status_path = status_path_for_feature(workspace, feature)
    # 016 T-10（D-40）：状态字段与归档备注两次写持 STATUS 锁。
    with flow_status_write_lock(status_path):
        set_status_field(status_path, "状态", "已归档")
        if args.reason:
            lines = status_path.read_text(encoding="utf-8").splitlines()
            start = next(
                (index for index, line in enumerate(lines) if line.strip() == "## 进度概要"),
                None,
            )
            note_line = f"- 归档备注: {args.reason.strip()}"
            if start is None:
                while lines and not lines[-1].strip():
                    lines.pop()
                lines.extend(["", "## 进度概要", "", note_line])
            else:
                end = next(
                    (
                        index
                        for index in range(start + 1, len(lines))
                        if lines[index].strip().startswith("## ")
                    ),
                    len(lines),
                )
                lines.insert(end, note_line)
            atomic_write_text(status_path, "\n".join(lines) + "\n")
    # 点位 #8（D-7）：flow archive 成功后发 feature_archived(trigger=flow-archive)。
    events: list[dict[str, Any]] = [
        flow_emit_event(
            workspace, feature, "feature_archived",
            {"trigger": "flow-archive"},
            command="flow archive",
            trace=_flow_feature_trace(workspace, feature),  # D-23：v1 事件 trace 注入
        )
    ]
    data: dict[str, Any] = {
        "status": "ok",
        "message": "feature 状态已置为 已归档，不再计入活跃 feature。",
        "feature": feature["feature"],
    }
    ones_field, ones_intent = flow_ones_follow_up(
        workspace, feature, skip=_no_ones_sync(args), sync_tasks=False
    )
    if ones_field is not None:
        data["ones_sync"] = ones_field
    if ones_intent is not None:
        events.append(ones_intent)
    data["_events"] = events
    data = apply_write_side_effects(data, feature, side_effects)
    attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0


# verify 完成 Gate 巡检文案表：(缺行 code, 缺行提示, waived code, waived 提示)；
# key 顺序与集合必须等于 COMPLETION_GATE_STEPS（循环按键查找，漂移即 KeyError 由测试钉死）。
# 缺行 code 名称零变更；fix 文案改 migrate-steps（接口 §2.5 / D-5）。
_VERIFY_COMPLETION_GATE_CHECKS: dict[str, tuple[str, str, str | None, str | None]] = {
    "S5-R": (
        "full_regression_gate_missing",
        f"流水线缺少 S5-R；完成前必须{_MIGRATE_STEPS_HINT} 补行并执行不带测试选择器的受影响模块全量回归。",
        "full_regression_gate_waived",
        "S5-R 不允许 waived；必须真实执行受影响模块全量回归并置为 done。",
    ),
    E2E_GATE_STEP: (
        "e2e_gate_missing",
        f"流水线缺少 {E2E_GATE_STEP}；完成前必须{_MIGRATE_STEPS_HINT} 补行并完成需求准出 e2e 门判定。",
        None,
        None,
    ),
    "S5.7": (
        "feature_review_gate_missing",
        f"流水线缺少 S5.7；完成前必须{_MIGRATE_STEPS_HINT} 补行并执行 Feature 级综合审查。",
        "feature_review_gate_waived",
        "S5.7 不允许 waived；必须真实执行 Feature 级综合审查并置为 done。",
    ),
}


# R11 doc-sync 交叉核对的保守判定口径（设计 §4.6.4 / 接口文档 §3.3）：
# 判定信号全部走确定性标识符（D-n 裁定号、P/Q 条目号、CB-*/CHG-* 批次号引用），不做语义推断。
# 严重度分级：裁定与状态列矛盾、设计文档裁定引用断链 = 默认级（需处理）；
# 「resolved 行缺回链引用」属提示性缺口 = info 级，不误报阻塞。
_DOC_SYNC_DECISION_ID = re.compile(r"\bD-\d+\b")
_DOC_SYNC_FEATURE_ITEM_ID = re.compile(r"\b[PQ]\d+\b")
_DOC_SYNC_BACK_LINK_ID = re.compile(r"\bD-\d+\b|\bCB-\d+\b|\bCHG-[\w-]+")
_DOC_SYNC_RESOLVED_MARKERS = ("resolved", "已解决")
_DOC_SYNC_DESIGN_DOC_GLOBS = ("*设计文档*.md", "*接口文档*.md", "*类图*.md")


class DocSyncChecker:
    """R11 doc-sync 检查组：FEATURE.md §3/§4 状态列 × DECISIONS.md 裁定行 × CHANGELOG.md 条目 × 设计三类文档交叉核对。"""

    def __init__(self, feature_dir: Path) -> None:
        self._feature_dir = feature_dir

    def check(self) -> list[dict[str, str]]:
        gaps: list[dict[str, str]] = []
        feature_path = self._feature_dir / "FEATURE.md"
        decisions_path = self._feature_dir / "DECISIONS.md"
        feature_text = feature_path.read_text(encoding="utf-8") if feature_path.exists() else ""
        decisions_rows = self._decision_rows(decisions_path)
        decision_ids = {decision_id for decision_id, _ in decisions_rows}

        # 1) DECISIONS.md → FEATURE.md：裁定行引用了 FEATURE §3/§4 条目（P/Q 编号）时，
        #    对应条目状态列必须已回写为 resolved；仍为 pending 即「DECISIONS 有裁定而 FEATURE 未回写」。
        item_states = self._feature_item_states(feature_text)
        for decision_id, row_text in decisions_rows:
            unresolved = [
                item_id
                for item_id in dict.fromkeys(_DOC_SYNC_FEATURE_ITEM_ID.findall(row_text))
                if item_id in item_states
                and not self._is_resolved(item_states[item_id][0])
            ]
            if unresolved:
                gaps.append({
                    "source_doc": "DECISIONS.md",
                    "missing_in": "FEATURE.md",
                    "item": decision_id,
                    "message": f"DECISIONS.md 裁定 {decision_id} 已裁定 FEATURE.md 条目 "
                    f"{', '.join(unresolved)}，但其 §3/§4 状态列未回写为 resolved。",
                })

        # 2) 设计三类文档 → DECISIONS.md：设计文档引用的 D-n 裁定号必须在 DECISIONS.md 存在对应行。
        #    DECISIONS.md 缺失时由既有 decisions_md_missing 收口，此处保守跳过避免重复误报。
        if decisions_path.exists():
            for doc_name, doc_text in self._design_docs():
                for ref in sorted(set(_DOC_SYNC_DECISION_ID.findall(doc_text)) - decision_ids):
                    gaps.append({
                        "source_doc": doc_name,
                        "missing_in": "DECISIONS.md",
                        "item": ref,
                        "message": f"{doc_name} 引用了裁定 {ref}，但 DECISIONS.md 中不存在该裁定行。",
                    })

        # 3) FEATURE.md §3/§4 → DECISIONS.md / CHANGELOG.md：状态列已 resolved 的条目
        #    行内应留有裁定号（D-n）或批次号（CB-*/CHG-*）回链；缺失只作 info 级提示。
        for item_id, (status_cell, row_text) in item_states.items():
            if self._is_resolved(status_cell) and not _DOC_SYNC_BACK_LINK_ID.search(row_text):
                gaps.append({
                    "source_doc": "FEATURE.md",
                    "missing_in": "DECISIONS.md 或 CHANGELOG.md",
                    "item": item_id,
                    "level": "info",
                    "message": f"FEATURE.md 条目 {item_id} 状态已 resolved，但行内缺少裁定/批次引用"
                    "（D-*/CB-*/CHG-*），无法回链 DECISIONS.md 或 CHANGELOG.md。",
                })
        return gaps

    @staticmethod
    def _decision_rows(path: Path) -> list[tuple[str, str]]:
        if not path.exists():
            return []
        rows: list[tuple[str, str]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\|\s*(D-\d+)\s*\|", line)
            if match:
                rows.append((match.group(1), line))
        return rows

    def _design_docs(self) -> list[tuple[str, str]]:
        docs: list[tuple[str, str]] = []
        for pattern in _DOC_SYNC_DESIGN_DOC_GLOBS:
            for path in sorted(self._feature_dir.glob(pattern)):
                if path.is_file():
                    docs.append((path.name, path.read_text(encoding="utf-8")))
        return docs

    @staticmethod
    def _feature_item_states(feature_text: str) -> dict[str, tuple[str, str]]:
        """FEATURE.md §3/§4 表格：编号 -> (状态单元格, 整行文本)。无「状态」列的小节保守跳过。"""
        items: dict[str, tuple[str, str]] = {}
        in_scope = False
        status_index: int | None = None
        for line in feature_text.splitlines():
            if line.startswith("## "):
                in_scope = re.match(r"^##\s*[34][\.、\s]", line) is not None
                status_index = None
                continue
            if not in_scope or not line.strip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not cells or all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells):
                continue
            if status_index is None:
                if "状态" in cells:
                    status_index = cells.index("状态")
                continue
            if len(cells) <= status_index or not cells[0]:
                continue
            items[cells[0]] = (cells[status_index], line)
        return items

    @staticmethod
    def _is_resolved(status_cell: str) -> bool:
        lowered = status_cell.casefold()
        return any(marker in lowered for marker in _DOC_SYNC_RESOLVED_MARKERS)


def _verify_cycle_reconciliation(
    feature_dir: Path,
    status_path: Path,
    state_by_id: dict[str, str],
) -> list[dict[str, str]]:
    """cycle 对账检查组（014 设计 §4.3.2/§6.3：verify 负责发现面，自愈通道负责修复）。

    发现两类中间态（均 info 级，自愈通道 record_step_timing 会在下次置位时修复）：
    ① 悬空开周期（cycle 无 ended_at 且步骤主表状态已非活跃）；
    ② 已归档未闭合（归档区存在 (步骤, cycle_index=开周期序号) 行但开周期未闭合）。
    口径与 record_step_timing 自愈通道一致：开周期序号 = 其 1 基位置（len(cycles)），
    活跃态集合 = STEP_TIMING_ACTIVE_STATES。只读、fail-safe：任何读取异常 → 无 issue。
    """
    found: list[dict[str, str]] = []
    try:
        timings_path = feature_dir / DASHBOARD_STEP_TIMINGS_FILE
        if not timings_path.exists():
            return found
        loaded: Any = json.loads(timings_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or loaded.get("schema") != STEP_TIMINGS_SCHEMA_VERSION:
            return found
        steps = loaded.get("steps")
        if not isinstance(steps, dict):
            return found
        archived_indexes: dict[str, set[str]] = {}
        for row in parse_pipeline_history(status_path).get("rows", []):
            cells = row.get("cells") if isinstance(row, dict) else None
            if isinstance(cells, list) and len(cells) >= 8:
                archived_indexes.setdefault(str(cells[0]).strip(), set()).add(
                    str(cells[6]).strip()
                )
        for step_id in sorted(steps):
            entry = steps.get(step_id)
            cycles = entry.get("cycles") if isinstance(entry, dict) else None
            if not isinstance(cycles, list) or not cycles:
                continue
            last = cycles[-1]
            if not isinstance(last, dict) or last.get("ended_at") is not None:
                continue
            if state_by_id.get(step_id) in STEP_TIMING_ACTIVE_STATES:
                continue  # 活跃态开周期 = 正常进行中，非中间态
            open_index = str(len(cycles))
            if open_index in archived_indexes.get(step_id, set()):
                found.append({
                    "code": "cycle_archived_unclosed",
                    "step_id": step_id,
                    "message": (
                        f"步骤 {step_id} 的周期 {open_index} 已归档但未闭合"
                        "（rollback 跨文件写中间态）；下次 flow 置位时自愈通道按归档时间补齐闭合。"
                    ),
                })
            else:
                found.append({
                    "code": "cycle_dangling_open",
                    "step_id": step_id,
                    "message": (
                        f"步骤 {step_id} 存在悬空开周期（周期 {open_index} 无 ended_at，"
                        f"主表状态为 {state_by_id.get(step_id) or '（无此行）'}）；"
                        "下次 flow 置位时自愈通道按兜底规则修复。"
                    ),
                })
    except Exception:
        return []
    return found


def command_flow_verify(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    status_path = status_path_for_feature(workspace, feature)
    feature_dir = workspace / feature["feature_dir"]
    table = parse_pipeline_table(status_path)
    rows = [row["cells"] for row in table["rows"]]
    state_by_id = {cells[0]: cells[4] for cells in rows}
    issues: list[dict[str, str]] = []

    def issue(
        code: str,
        message: str,
        *,
        level: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> None:
        item: dict[str, str] = {"code": code, "message": message}
        if extra:
            item.update(extra)
        if level is not None:
            item["level"] = level
        issues.append(item)

    header_state = feature["summary"].get("state", "")
    open_steps = [
        f"{cells[0]}({cells[4]})"
        for cells in rows
        if cells[4] not in CLOSED_STATES
    ]
    if not open_steps and header_state == "开发中":
        issue(
            "pipeline_complete_but_developing",
            "流水线全部 done/skipped/waived，但头部状态仍为 开发中（可执行 flow complete）。",
        )
    if header_state == "已完成" and open_steps:
        issue(
            "completed_with_open_steps",
            f"头部状态为 已完成，但存在未关闭步骤: {', '.join(open_steps)}。",
        )

    expected_summary = progress_summary_values(table)
    actual_summary = read_progress_summary(status_path)
    mismatched = {
        label: {"expected": expected_summary[label], "actual": actual_summary.get(label, "")}
        for label in PROGRESS_SUMMARY_LABELS
        if actual_summary.get(label) != expected_summary[label]
    }
    if mismatched:
        detail = "; ".join(
            f"{label} 期望「{pair['expected']}」实际「{pair['actual']}」"
            for label, pair in mismatched.items()
        )
        issue("progress_summary_outdated", f"进度概要与流水线不一致: {detail}。")

    git_result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    header_branch = feature["summary"].get("branch", "").strip()
    if git_result.returncode == 0:
        current_branch = git_result.stdout.strip()
        if current_branch and header_branch and current_branch != header_branch:
            issue(
                "branch_mismatch",
                f"分支字段为 {header_branch}，当前 git 分支为 {current_branch}。",
            )

    ones_sync_field = feature["summary"].get("ones_sync", "")
    if ones_sync_field.startswith("retry"):
        issue("ones_sync_retry", f"头部 ONES同步 处于 retry: {ones_sync_field}。")
    outbox_counts = flow_outbox_counts(workspace, feature)
    # 012 D-9 / S5.7 M-1：queued 是正常最终一致态（等待 ones sync / 显式节点补发），只作 info 提示；
    # 只有 retry / needs_input / comment 才是需要处理的「未清空条目」（与 snapshot blocker 口径一致）。
    if outbox_counts and any(value for key, value in outbox_counts.items() if key != "state_outbox_queued"):
        issue(
            "ones_outbox_pending",
            "ONES-SYNC.json 存在未清空条目: "
            f"stateOutbox retry={outbox_counts['state_outbox_retry']}, "
            f"needs-input={outbox_counts['state_outbox_needs_input']}, "
            f"comment pending={outbox_counts['comment_pending']}, "
            f"comment retry={outbox_counts['comment_retry']}。",
        )
    if outbox_counts and outbox_counts.get("state_outbox_queued"):
        issue(
            "ones_outbox_queued",
            f"ONES-SYNC.json 有 {outbox_counts['state_outbox_queued']} 条 queued 待补发（下次 ones sync / 显式同步节点自动发出）。",
            level="info",
        )

    if state_by_id.get("S1.1") in DONE_STATES and not (feature_dir / "FEATURE.md").exists():
        issue("feature_md_missing", "S1.1 已完成但 FEATURE.md 缺失。")
    if state_by_id.get("S2.1") in DONE_STATES:
        missing_docs = [
            label
            for label, pattern in (
                ("设计文档", "*设计文档*.md"),
                ("接口文档", "*接口文档*.md"),
                ("类图文档", "*类图*.md"),
            )
            if not any(feature_dir.glob(pattern))
        ]
        if missing_docs:
            issue(
                "design_docs_missing",
                f"S2.1 已完成但设计文档缺失: {', '.join(missing_docs)}。",
            )
    if not (feature_dir / "DECISIONS.md").exists():
        issue("decisions_md_missing", "DECISIONS.md 缺失。")

    # R11 doc-sync 检查组（设计 §4.6.4 / 接口文档 §3.3）：交叉核对产物以 DOC_SYNC_GAP
    # issue 追加（含 source_doc/missing_in/item 明细字段）；既有检查项与输出结构不变。
    for gap in DocSyncChecker(feature_dir).check():
        issue(
            "DOC_SYNC_GAP",
            gap["message"],
            level=gap.get("level"),
            extra={
                "source_doc": gap["source_doc"],
                "missing_in": gap["missing_in"],
                "item": gap["item"],
            },
        )

    # 014 §4.3.2 cycle 对账检查组：悬空开周期 / 已归档未闭合（info 级；自愈通道负责修复，
    # verify 只负责发现面；fail-safe，读取异常不产生 issue）。
    for cycle_issue in _verify_cycle_reconciliation(feature_dir, status_path, state_by_id):
        issue(
            cycle_issue["code"],
            cycle_issue["message"],
            level="info",
            extra={"step_id": cycle_issue["step_id"]},
        )

    # 完成 Gate 缺行巡检：按配置快照 expected_completion_gate_steps 判定（设计 §4.2.5）。
    # 行缺失+配置开启 → 既有 *_gate_missing code；行缺失+配置关闭 → 仅 e2e 记 info
    # e2e_gate_not_configured（F 行不在 COMPLETION_GATE_STEPS，不发任何项）。
    # S5-E2E 无 waived 专属 issue——其 waived 由下方 invalid_waived_steps 通用检查收口。
    _scale = feature_scale(feature)
    _verify_mode = feature_resolved_mode(table, feature)
    expected_gates = set(expected_completion_gate_steps(pipeline_config_snapshot(workspace), _scale))
    for step_id in COMPLETION_GATE_STEPS:
        missing_code, missing_message, waived_code, waived_message = _VERIFY_COMPLETION_GATE_CHECKS[step_id]
        if step_id not in state_by_id:
            if step_id in expected_gates:
                issue(missing_code, missing_message)
            elif step_id == E2E_GATE_STEP:
                issue(
                    "e2e_gate_not_configured",
                    f"{E2E_GATE_STEP} 未配置，流水线不生成该行（Q1=A）。",
                    level="info",
                )
        elif waived_code is not None and state_by_id[step_id] in WAIVED_STATES:
            issue(waived_code, waived_message)
    invalid_waived_steps = [
        step_id
        for step_id, state in state_by_id.items()
        if state in WAIVED_STATES and step_id not in WAIVABLE_STEPS
    ]
    if invalid_waived_steps:
        issue(
            "invalid_waived_steps",
            "waived 仅允许用于 S2 设计审查步骤与任务级架构审查（S5.5/S5.3）；非法步骤: "
            + ", ".join(invalid_waived_steps)
            + "。",
        )

    # 016 D-39（S5.7 P0-1）：完成门不含 S5.7 的档（small）上任务级架构审查为 skipped ⇒ 递延无落点 ⇒ 零审查。
    if "S5.7" not in expected_gates:
        _deferred_arch = [
            step_id
            for step_id in task_arch_review_steps_for_mode(_verify_mode)
            if state_by_id.get(step_id) in SKIPPED_STATES
        ]
        if _deferred_arch:
            issue(
                "task_arch_review_deferred_without_s57",
                _task_arch_deferred_without_s57_message(_deferred_arch, scale=_scale),
            )

    # 016 D-32：只读预警存量 scale 收窄遗留 pending 行（flow complete 会自动归档为 skipped）。
    # verify 只读、不写盘、不归档、fail-open（不阻塞）；实际归档只发生在 flow complete。
    _narrowed_ids, _pending_leftover = scale_narrowed_pending_ids(table, _scale, _verify_mode)
    if _pending_leftover:
        issue(
            "scale_narrowed_pending_will_archive",
            f"存量遗留行将在 flow complete 时自动归档为 skipped（scale-narrowed，{_scale} 档）: "
            + ", ".join(_pending_leftover)
            + "。",
            level="info",
        )

    task_path = task_path_for_feature(workspace, feature)
    if _scale == "small":
        try:
            small_tasks = parse_task_table(task_path)["rows"] if task_path.exists() else []
        except DevkitFlowError:
            small_tasks = []
        if not any(row.get("任务ID", "").strip() == "T-01" for row in small_tasks):
            issue(
                "small_development_task_missing",
                "Small Feature 缺少 T-01；执行 flow task ensure-small 持久化开发任务。",
            )
    if task_path.exists() and state_by_id.get("S5") == "pending":
        try:
            task_table = parse_task_table(task_path)
        except DevkitFlowError:
            task_table = None
        if task_table is not None:
            doing = [
                row.get("任务ID", "")
                for row in task_table["rows"]
                if row.get("状态", "").strip() == "执行中"
            ]
            if doing:
                issue(
                    "task_doing_before_s5",
                    f"task.md 中 {', '.join(doing)} 为执行中，但流水线 S5 仍为 pending。",
                )

    data = {
        "status": "ok",
        "message": f"巡检完成，发现 {len(issues)} 个问题。" if issues else "巡检完成，未发现问题。",
        "feature": feature["feature"],
        "issue_count": len(issues),
        "issues": issues,
    }
    emit(data, args.format)
    return 0


def command_template(args: argparse.Namespace) -> int:
    content = embedded_templates()[args.name]
    if args.format == "json":
        emit({"status": "ok", "name": args.name, "content": content}, "json")
    else:
        sys.stdout.write(content)
    return 0


def _feature_init_routing_mode(workspace: Path) -> tuple[Any, BindingReceipt | None] | None:
    """feature init 的路由判定（profile=None、不拉取、不调用识别器）。判定失败返回 None，按 legacy 原流程继续——
    与 ``refresh_specrules`` 的 fail-open 口径一致；唯一例外与 ``setup`` 相同：项目显式写了 ``ruleSources.routing=auto``
    时配置非法即失败关闭（explicit 永不 fail-open），异常原样抛出、不建 feature 目录。"""
    project_config: dict[str, Any] = {}
    try:
        global_config, _ = load_config_file(Path.home() / ".config" / "devkit" / "config.json")
        project_config, _ = load_config_file(workspace / CONFIG_PATH)
        catalog = RuleSourceCatalog.fromConfig(global_config, project_config)
        receipt = BindingReceipt.load(workspace)
        decision = RuleSourceSelector().routingMode(
            workspace, catalog, None, receipt, catalog.sourceDirs(), allowImplicitAdoption=False
        )
    except Exception:  # noqa: BLE001 - fail-open（explicit 除外）
        rules = project_config.get("ruleSources") if isinstance(project_config, dict) else None
        if isinstance(rules, dict) and rules.get("routing") == "auto":
            raise
        return None
    return decision, receipt


def feature_init_binding_check(workspace: Path) -> list[str]:
    """BC-17：仅 ``adopted`` 时在产物落盘前校验绑定回执与当前画像指纹（设计 §4.7.1.3，接口 §2.5）。

    - explicit：回执缺失 / 不可读（写了 routing=auto 尚未 setup）或指纹不一致 → ``RULES_BINDING_STALE``（调用方
      未建 feature 目录）；
    - implicit：不一致只返回 warning 与「重新执行 setup」建议；
    - 当前识别 ``budget-exhausted``：不做一致性判定，只告警；识别本身抛异常时 implicit 只告警、explicit 原样抛出；
    - legacy / disabled：不调用识别器、不写画像缓存，返回 []（输出与 2.45.0 相同）。
    """
    routed = _feature_init_routing_mode(workspace)
    if routed is None:
        return []
    decision, receipt = routed
    if decision.mode != "adopted":
        return []
    explicit = decision.adoptedBy == "explicit"
    if receipt is None or not receipt.readable:
        if explicit:
            problem = "绑定回执缺失" if receipt is None else f"绑定回执不可读（{receipt.problem}）"
            raise DevkitFlowError(
                f"{problem}：ruleSources.routing=auto 已写入但尚未完成 setup；先执行 devkit-flow setup 再 feature init",
                error_code=RULES_BINDING_STALE,
            )
        return [f"绑定回执不可读（{receipt.problem if receipt else 'missing'}），未校验绑定指纹；重新执行 setup 重写回执"]
    try:
        profile = ProjectDetector().detect(workspace)
    except Exception as error:  # noqa: BLE001 - implicit 只告警（arch P2-10）；explicit 失败关闭
        if explicit:
            raise
        return [f"项目识别失败（{compact_error(error)}），未校验绑定指纹；修复后重新执行 setup"]
    if profile.budget.exhausted or profile.status == "budget-exhausted":
        return ["识别预算耗尽，未校验绑定指纹"]
    try:
        ProfileCache().write(profile)
    except OSError:
        pass
    if profile.fingerprint == receipt.profileFingerprint:
        return []
    message = "当前项目画像指纹与绑定回执不一致（项目结构已变化）"
    if explicit:
        raise DevkitFlowError(
            f"{message}：重新执行 devkit-flow setup 校验并重挂规则后再 feature init",
            error_code=RULES_BINDING_STALE,
        )
    return [f"{message}；沿用现有规则树继续，重新执行 setup 更新绑定"]


def command_feature_init(args: argparse.Namespace, *, side_effects: WriteSideEffects | None = None) -> int:
    workspace = Path.cwd()
    name = args.name.strip()
    if not FEATURE_NAME_PATTERN.fullmatch(name):
        raise DevkitFlowError(f"非法 feature 名: {args.name}（仅限小写字母/数字/中划线）。")
    next_id = next_feature_id(workspace)
    feature_dir = workspace / "spec" / f"{next_id}-{name}"
    if feature_dir.exists():
        raise DevkitFlowError(f"目录已存在: {feature_dir.relative_to(workspace)}。")
    spec_root = workspace / "spec"
    if spec_root.is_dir():
        clash = next(
            (
                child
                for child in sorted(spec_root.iterdir())
                if child.is_dir() and feature_name_from_dir(child) == name
            ),
            None,
        )
        if clash is not None:
            raise DevkitFlowError(f"已存在同名 feature 目录: {clash.relative_to(workspace)}。")

    # 016 T-05（BC-17）：adopted 下绑定回执 / 指纹校验先于任何产物落盘；legacy / disabled 返回 [] 且无副作用
    rule_warnings = feature_init_binding_check(workspace)

    branch = ""
    git_result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if git_result.returncode == 0:
        branch = git_result.stdout.strip()

    # 快照 / 有效行集先于 mkdir：校验失败不得留下空目录。
    # init 走 canonical 并集压缩（scale ∪ config），避免先裁 scale 再压缩导致悬空依赖。
    snapshot = pipeline_config_snapshot(workspace)
    pipeline_rows = canonical_effective_rows(args.scale, None, snapshot)

    feature_dir.mkdir(parents=True)
    status_path = feature_dir / "STATUS.md"
    decisions_path = feature_dir / "DECISIONS.md"
    # D-10（014 设计 §4.3.1/§4.3.4）：feature init 生成 tx_<uuid>，落 STATUS.md 头部 TraceID。
    trace_id = f"tx_{uuid.uuid4()}"
    atomic_write_text(
        status_path,
        render_status_template(
            name,
            args.scale,
            branch,
            flow_today(),
            snapshot=snapshot,
            pipeline_rows=pipeline_rows,
            trace_id=trace_id,
        ),
    )
    decisions = embedded_templates()["DECISIONS-TEMPLATE"].replace("{Feature}", name).replace("{feature}", name)
    atomic_write_text(decisions_path, decisions)

    values = progress_summary_values(parse_pipeline_table(status_path))
    values["剩余步骤"] += " → S5.x（模式确认后由 flow mode 追加）"
    rewrite_progress_summary(status_path, values)

    try:
        from devkit_flow.setup import refresh_specrules

        specrules_refresh = refresh_specrules(workspace)
    except Exception as error:
        specrules_refresh = {"status": "error", "message": compact_error(error)}
    data = {
        "status": "ok",
        "message": f"已创建 {feature_dir.relative_to(workspace)}（STATUS.md + DECISIONS.md，规模 {args.scale}）。",
        "feature": name,
        "feature_id": next_id,
        "feature_dir": feature_dir.relative_to(workspace).as_posix(),
        "scale": args.scale,
        "branch": branch,
        "files": [
            status_path.relative_to(workspace).as_posix(),
            decisions_path.relative_to(workspace).as_posix(),
        ],
        "pipeline_steps": [row["cells"][0] for row in parse_pipeline_table(status_path)["rows"]],
        "config_snapshot": snapshot.as_json(),
        "specrules_refresh": specrules_refresh,
    }
    if rule_warnings:
        data["warnings"] = rule_warnings
        # arch P2-11：text 输出只打印 message，告警须同时进 message 才可见（仅 adopted 下出现，legacy 输出不变）
        data["message"] += "".join(f"\nwarning: {item}" for item in rule_warnings)
    # 点位 #9（D-7）：feature init 发 feature_started(trigger=feature-init)。
    started_feature = {
        "feature": name,
        "feature_dir": data["feature_dir"],
        "status_file": status_path.relative_to(workspace).as_posix(),
    }
    data["_events"] = [
        flow_emit_event(
            workspace,
            started_feature,
            "feature_started",
            {
                "trigger": "feature-init",
                "initial_mode": args.scale or None,
                "ones_requirement_id": _payload_ones_requirement_id(workspace, started_feature),
            },
            command="feature init",
            # D-23：v1 事件 trace 注入（STATUS.md 已在上方落盘，TraceID 可读）。
            trace=_flow_feature_trace(workspace, started_feature),
        )
    ]
    data = apply_write_side_effects(data, started_feature, side_effects)
    emit(data, args.format)
    return 0


def command_flow_mode(args: argparse.Namespace, *, dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None, side_effects: WriteSideEffects | None = None) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    status_path = status_path_for_feature(workspace, feature)
    # 016 T-10（D-40）：模式检查、S5.x 行插入、S0.5-H 置位与模式字段同处一个临界区；漏斗内锁可重入。
    with flow_status_write_lock(status_path):
        table = parse_pipeline_table(status_path)
        gate = next((row for row in table["rows"] if row["cells"][0] == "S0.5-H"), None)
        if gate is None:
            raise DevkitFlowError("流水线中不存在 S0.5-H（模式选择），无法确认模式。")
        current_mode = feature["summary"].get("mode", "").strip()
        if current_mode in FLOW_MODES:
            raise DevkitFlowError(f"模式已设置为 {current_mode}，禁止重复执行 flow mode。")
        if gate["cells"][4] != "waiting-confirmation":
            raise DevkitFlowError(
                f"S0.5-H 当前状态为 {gate['cells'][4]}，不是 waiting-confirmation，无法确认模式。"
            )
        if any(re.fullmatch(r"S5\..+", row["cells"][0]) for row in table["rows"]):
            raise DevkitFlowError("流水线中已存在 S5.x 模式行，禁止重复追加。")
        s5_row = next((row for row in table["rows"] if row["cells"][0] == "S5"), None)
        if s5_row is None:
            raise DevkitFlowError("流水线中不存在 S5 行，无法追加模式行。")

        snapshot = pipeline_config_snapshot(workspace)
        # 016 F1-a：追加模式行时也必须按 scale 裁剪（此前只按配置门裁剪是 bug，导致 small 照拿 S5.7）。
        # 用 canonical_effective_rows(scale, mode) 统一算 scale∪config 裁剪 + 依赖压缩，再取模式行区。
        scale = feature_scale(feature)
        mode_row_ids = {row[0] for row in PIPELINE_MODE_ROWS[args.mode]}
        canonical = canonical_effective_rows(scale, args.mode, snapshot)
        mode_rows = [row for row in canonical if row[0] in mode_row_ids]
        lines = list(table["lines"])
        insert_at = s5_row["line_index"] + 1
        lines[insert_at:insert_at] = ["| " + " | ".join(row) + " |" for row in mode_rows]
        atomic_write_text(status_path, "\n".join(lines) + "\n")

        data = apply_flow_set(
            workspace,
            feature,
            "S0.5-H",
            "done",
            confirmed=True,
            trigger="mode",
            dashboard_refresh=dashboard_refresh,
            no_ones_sync=_no_ones_sync(args),
        )
        set_status_field(status_path, "模式", args.mode)
    data["message"] = (
        f"模式已确认为 {args.mode}：S5.x 模式行已追加，S0.5-H 已置 done，进度概要已重算。"
    )
    data["mode"] = args.mode
    data["appended_steps"] = [row[0] for row in mode_rows]
    data["config_snapshot"] = snapshot.as_json()
    data = apply_write_side_effects(data, feature, side_effects)
    attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0


_HISTORICAL_S57_DEPS = frozenset({"S5.3", "S5.6"})


def _migrate_steps_mode(table: dict[str, Any], header_mode: str) -> str | None:
    """S0.5-H 未 done 或模式头未确认时只处理公共行（设计 §4.2.4 / 接口 §2.1）。"""
    gate = next((row for row in table["rows"] if row["cells"][0] == "S0.5-H"), None)
    if gate is None or gate["cells"][4] not in DONE_STATES:
        return None
    if header_mode not in FLOW_MODES:
        return None
    return header_mode


def _dep_token_ids(depends_on: str) -> set[str]:
    tokens: set[str] = set()
    for alternative in dependency_alternatives(depends_on):
        tokens.update(alternative)
    return tokens


def _should_rewrite_dependency(
    step_id: str, old_dep: str, new_dep: str, inserted_ids: set[str]
) -> bool:
    if old_dep == new_dep:
        return False
    if _dep_token_ids(new_dep) & inserted_ids:
        return True
    return step_id == "S5.7" and bool(_dep_token_ids(old_dep) & _HISTORICAL_S57_DEPS)


def _skipped_disabled_steps(
    mode: str | None,
    snapshot: PipelineConfigSnapshot,
    existing_ids: set[str],
) -> list[str]:
    """配置关闭且 STATUS 缺失的受控行；门开启的缺失行不得落入此列表。"""
    if mode not in PIPELINE_MODE_ROWS:
        return []
    mode_ids = {row[0] for row in PIPELINE_MODE_ROWS[mode]}
    skipped: list[str] = []
    for gate_name, step_ids in CONFIG_GATED_STEPS.items():
        if snapshot.gate(gate_name).enabled:
            continue
        for step_id in step_ids:
            if step_id in mode_ids and step_id not in existing_ids:
                skipped.append(step_id)
    return skipped


def _migrate_steps_message(
    inserted: list[str],
    rewritten: list[DependencyRewrite],
    *,
    mode_unconfirmed: bool,
) -> str:
    count_inserted = len(inserted)
    count_rewritten = len(rewritten)
    if count_inserted == 0 and count_rewritten == 0:
        message = "No missing rows to insert."
    elif count_inserted and count_rewritten:
        row_word = "row" if count_inserted == 1 else "rows"
        dep_word = "dependency" if count_rewritten == 1 else "dependencies"
        message = (
            f"Inserted {count_inserted} {row_word} (pending) "
            f"and rewrote {count_rewritten} {dep_word}."
        )
    elif count_inserted:
        row_word = "row" if count_inserted == 1 else "rows"
        message = f"Inserted {count_inserted} {row_word} (pending)."
    else:
        dep_word = "dependency" if count_rewritten == 1 else "dependencies"
        message = f"Rewrote {count_rewritten} {dep_word}."
    if mode_unconfirmed:
        message = (
            f"{message.rstrip('.')}."
            " Mode not confirmed; only common rows processed. Run flow mode first."
        )
    return message


def _pipeline_anchor_index(lines: list[str]) -> int:
    in_pipeline = False
    anchor: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_pipeline:
                break
            in_pipeline = stripped == "## 流水线"
            continue
        if not in_pipeline or not stripped.startswith("|"):
            continue
        cells = [part.strip() for part in stripped.strip("|").split("|")]
        if not cells:
            continue
        if cells[0] == "步骤" or set(cells[0]) <= {"-"}:
            anchor = index
    if anchor is None:
        raise DevkitFlowError("STATUS.md 无流水线表，无法执行 flow migrate-steps。")
    return anchor


def _render_pipeline_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def command_flow_migrate_steps(
    args: argparse.Namespace,
    *,
    dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None,
    side_effects: WriteSideEffects | None = None,
) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    status_path = status_path_for_feature(workspace, feature)
    # 016 T-10（D-40）：读-比对-写持 STATUS 锁（dry-run 同样在锁内读取一致快照）。
    with flow_status_write_lock(status_path):
        table = parse_pipeline_table(status_path)
        if not any(line.strip() == "## 流水线" for line in table["lines"]):
            raise DevkitFlowError("STATUS.md 无流水线表，无法执行 flow migrate-steps。")

        scale = feature_scale(feature, allow_empty=False)  # 016 D-40/D-42：写命令空值 fail-closed，不按 large 插行
        mode = feature_resolved_mode(table, feature)
        snapshot = pipeline_config_snapshot(workspace)
        dry_run = bool(getattr(args, "dry_run", False))

        existing_ids: list[str] = []
        id_to_line: dict[str, int] = {}
        row_by_id: dict[str, dict[str, Any]] = {}
        for row in table["rows"]:
            step_id = row["cells"][0]
            existing_ids.append(step_id)
            if step_id not in id_to_line:
                id_to_line[step_id] = row["line_index"]
                row_by_id[step_id] = row
        existing_set = set(existing_ids)

        canonical = canonical_effective_rows(scale, mode, snapshot)
        missing_rows = missing_required_rows(existing_ids, scale, mode, snapshot)
        pending_inserts = {row[0]: list(row) for row in missing_rows}
        inserted_ids_set = set(pending_inserts)

        lines = list(table["lines"])
        last_line = _pipeline_anchor_index(lines)
        inserted_ids: list[str] = []
        already_present: list[str] = []
        rewritten: list[DependencyRewrite] = []

        for canon_row in canonical:
            step_id = canon_row[0]
            if step_id in id_to_line:
                already_present.append(step_id)
                last_line = id_to_line[step_id]
                old_dep = row_by_id[step_id]["cells"][3]
                new_dep = canon_row[3]
                if _should_rewrite_dependency(step_id, old_dep, new_dep, inserted_ids_set):
                    cells = list(row_by_id[step_id]["cells"])
                    cells[3] = new_dep
                    lines[id_to_line[step_id]] = _render_pipeline_row(cells)
                    rewritten.append(
                        DependencyRewrite(step=step_id, from_dep=old_dep, to_dep=new_dep)
                    )
                continue
            insert_row = pending_inserts.get(step_id)
            if insert_row is None:
                continue
            cells = list(insert_row)
            if len(cells) < 6:
                cells.extend([""] * (6 - len(cells)))
            cells[4] = "pending"
            insert_at = last_line + 1
            lines.insert(insert_at, _render_pipeline_row(cells))
            for tracked_id, tracked_index in list(id_to_line.items()):
                if tracked_index >= insert_at:
                    id_to_line[tracked_id] = tracked_index + 1
            id_to_line[step_id] = insert_at
            last_line = insert_at
            inserted_ids.append(step_id)

        skipped_disabled = _skipped_disabled_steps(mode, snapshot, existing_set)
        wrote = False
        new_text = "\n".join(lines) + "\n"
        old_text = "\n".join(table["lines"]) + "\n"
        if not dry_run and new_text != old_text:
            atomic_write_text(status_path, new_text)
            wrote = True
            if inserted_ids:
                rewrite_progress_summary(
                    status_path, progress_summary_values(parse_pipeline_table(status_path))
                )

    data: dict[str, Any] = {
        "status": "ok",
        "message": _migrate_steps_message(
            inserted_ids, rewritten, mode_unconfirmed=mode is None
        ),
        "feature": feature["feature"],
        "mode": mode,
        "scale": scale,
        "dry_run": dry_run,
        "config_snapshot": snapshot.as_json(),
        "inserted_steps": inserted_ids,
        "rewritten_dependencies": [item.as_json() for item in rewritten],
        "already_present": already_present,
        "skipped_disabled": skipped_disabled,
    }
    data["_events"] = []
    data = apply_write_side_effects(data, feature, side_effects)
    if wrote:
        attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0
