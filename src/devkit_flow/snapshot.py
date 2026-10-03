from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from devkit_flow.clarify import clarification_state_path, load_clarification_batch
from devkit_flow.config import ones_config_summary, quality_gate_summary
from devkit_flow.constants import (
    CLOSED_STATES,
    CONFIG_GATED_STEPS,
    DONE_STATES,
    E2E_GATE_STEP,
    PIPELINE_COMMON_ROWS,
    PIPELINE_MODE_ROWS,
    PIPELINE_SCALE_STEPS,
    SKIPPED_STATES,
    WAIVED_STATES,
)
from devkit_flow.e2e import evaluate_e2e_gate
from devkit_flow.errors import DevkitFlowError
from devkit_flow.flow import dependency_report, feature_resolved_mode, flow_outbox_counts, next_action_text, pipeline_breakpoint, priority_missing_gate_ids, progress_summary_values, scale_narrowed_pending_ids
from devkit_flow.models import E2EGateVerdict, FeatureSnapshot
from devkit_flow.parse_util import parse_design_task_table, parse_pipeline_table, parse_status_file, parse_task_table, report_clarification_counts, report_clarification_stats, report_human_intervention_count, report_markdown_rows, status_path_for_feature, task_path_for_feature, visible_design_task_rows_for_report
from devkit_flow.pipeline_gates import (
    GateState,
    PipelineConfigSnapshot,
    disabled_gated_step_ids,
    expected_completion_gate_steps,
    missing_required_rows,
    pipeline_config_snapshot,
)
from devkit_flow.textio import compact_error


# --- snapshot（feature 过程态共享采集层，设计文档 §4.3.2） ---------------------
# 中性采集层：只采集不渲染。report_* 纯采集函数族自 report.py 原样下沉
# （函数体逐字保留，复盘报告行为不变），dashboard.py 与 report.py 共同消费，
# render 层模块互不依赖。
def report_state_bucket(state: str) -> str:
    if state in DONE_STATES:
        return "done"
    if state in SKIPPED_STATES:
        return "skipped"
    if state in WAIVED_STATES:
        return "waived"
    if state == "blocked":
        return "blocked"
    return "pending"


def report_file_changes(
    workspace: Path, feature_dir: Path, task_rows: list[dict[str, str]]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(rel: str) -> None:
        if rel in seen:
            return
        seen.add(rel)
        entries.append(
            {
                "path": rel,
                "status": "added",
                "additions": 0,
                "deletions": 0,
                "diff": [],
            }
        )

    for path in sorted(feature_dir.rglob("*")):
        if path.is_file() and path.name != "workflow-visualization.html":
            add(path.relative_to(workspace).as_posix())
    for row in task_rows:
        evidence = " ".join(
            value for value in (row.get("预计写入范围", ""), row.get("产出/备注", "")) if value
        )
        for token in re.findall(r"[A-Za-z0-9_./\-\[\]一-鿿]+\.[A-Za-z0-9]+", evidence):
            if "/" in token:
                add(token)
    return entries


def report_feature_subtitle(feature_dir: Path) -> str:
    feature_path = feature_dir / "FEATURE.md"
    if not feature_path.exists():
        return "-"
    for line in feature_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith(("#", "|", "- ", "* ", ">"))
            or re.match(r"^\*\*.+?\*\*[:：]", stripped)
        ):
            continue
        stripped = re.sub(r"[`*_]", "", stripped)
        return stripped[:220]
    return "-"


def report_cycle_text(
    summary: dict[str, str],
    feature_dir: Path,
    design_rows: list[dict[str, str]],
    task_rows: list[dict[str, str]],
) -> str:
    def date_value(value: str) -> str:
        match = re.search(r"\d{4}-\d{2}-\d{2}", value)
        return match.group(0) if match else ""

    created = date_value(summary.get("created_at", ""))
    candidates = [date_value(summary.get("last_updated", ""))]
    for row in [*design_rows, *task_rows]:
        candidates.extend(
            [date_value(row.get("实际开始", "")), date_value(row.get("实际完成", ""))]
        )
    changelog_path = feature_dir / "CHANGELOG.md"
    if changelog_path.exists():
        candidates.extend(
            date_value(line)
            for line in changelog_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        )
    dated = [value for value in candidates if value]
    updated = max(dated) if dated else ""
    if not created and dated:
        created = min(dated)
    if not created and not updated:
        return "-"
    if not created or not updated:
        return created or updated
    duration = ""
    try:
        start = datetime.fromisoformat(created[:10]).date()
        end = datetime.fromisoformat(updated[:10]).date()
        if end >= start:
            duration = f"（{(end - start).days + 1} 天）"
    except ValueError:
        pass
    return f"{created} → {updated}{duration}"


def parse_report_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    match = re.search(
        r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?)([+-]\d{2}:\d{2})?)?",
        text,
    )
    if not match:
        return None
    date, time, tz = match.group(1), match.group(2), match.group(3)
    if time and time.count(":") == 1:
        time = f"{time}:00"
    iso = f"{date}T{time or '00:00:00'}{tz or '+08:00'}"
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed


def format_report_duration(seconds: float) -> str:
    total = int(seconds)
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    if not parts:
        return f"{secs} 秒" if secs else "不足 1 分钟"
    return " ".join(parts)


def report_changelog_timeline_events(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    events: list[tuple[str, str]] = []
    heading = "变更记录"
    pending_heading: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            heading = line[3:].strip() or "变更记录"
            heading_time = parse_report_datetime(heading)
            pending_heading = heading
            if heading_time is not None:
                events.append((heading, heading))
                pending_heading = None
            continue
        match = re.search(
            r"时间[：:]\s*(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)",
            line,
        )
        if match:
            label = pending_heading or heading
            events.append((match.group(1), label))
            pending_heading = None
    if pending_heading and parse_report_datetime(pending_heading) is None:
        # 标题无法解析为时间时不进入时间线，避免把 CHG-id 当成时刻。
        pass
    return events


def report_clarification_timeline_events(feature_dir: Path) -> list[tuple[str, str, str]]:
    path = feature_dir / "clarification-state.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    batch_id = str(payload.get("batchId") or "澄清批次").strip() or "澄清批次"
    events: list[tuple[str, str, str]] = []
    created = str(payload.get("createdAt") or "").strip()
    updated = str(payload.get("updatedAt") or "").strip()
    status = str(payload.get("status") or "").strip()
    if created:
        events.append((created, f"澄清批次 {batch_id} 开启", "change"))
    if updated and updated != created:
        label = "关闭" if status == "closed" else ("密封" if status == "ready-to-apply" else "更新")
        events.append((updated, f"澄清批次 {batch_id} {label}", "change"))
    return events


def report_feature_gate_timeline_events(feature_dir: Path) -> list[tuple[str, str, str]]:
    path = feature_dir / "FEATURE.md"
    if not path.exists():
        return []
    rows = report_markdown_rows(path)
    events: list[tuple[str, str, str]] = []
    for row in rows:
        gate = (row.get("Gate") or row.get("门禁") or "").strip()
        when = (row.get("时间") or "").strip()
        state = (row.get("状态") or "").strip().lower()
        if not gate or not when or parse_report_datetime(when) is None:
            continue
        if state in {"pending", "-", ""}:
            continue
        events.append((when, f"人工确认：{gate}", "review"))
    return events


def collect_report_timeline_events(
    feature_dir: Path,
    summary: dict[str, str],
    design_rows: list[dict[str, str]],
    task_rows: list[dict[str, str]],
) -> list[tuple[datetime, str, str, str]]:
    raw: list[tuple[str, str, str]] = []
    created = summary.get("created_at", "").strip()
    if created:
        raw.append((created, "Feature 创建", "milestone"))
    for row in [*design_rows, *task_rows]:
        task_id = row.get("任务ID", "")
        label = row.get("描述", "")
        started = row.get("实际开始", "").strip()
        finished = row.get("实际完成", "").strip()
        if started:
            raw.append((started, f"{task_id} {label} 开始", ""))
        if finished:
            raw.append((finished, f"{task_id} {label} 完成", "milestone"))
    raw.extend(
        (when, label, "change")
        for when, label in report_changelog_timeline_events(feature_dir / "CHANGELOG.md")
    )
    raw.extend(report_clarification_timeline_events(feature_dir))
    raw.extend(report_feature_gate_timeline_events(feature_dir))
    last_updated = summary.get("last_updated", "").strip()
    if last_updated and parse_report_datetime(last_updated) is not None:
        last_dt = parse_report_datetime(last_updated)
        has_precise = any(
            parse_report_datetime(when) is not None and "T" in when
            for when, _label, _css in raw
        )
        if last_dt is not None and ("T" in last_updated or not has_precise):
            raw.append((last_updated, "Feature 最后更新", "review"))

    parsed: list[tuple[datetime, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for when, label, css in raw:
        moment = parse_report_datetime(when)
        if moment is None:
            continue
        key = (when, label)
        if key in seen:
            continue
        seen.add(key)
        parsed.append((moment, when, label, css))
    parsed.sort(key=lambda item: (item[0], item[2]))
    has_clock = any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when.strip()) for _dt, when, _label, _css in parsed)
    if has_clock:
        parsed = [
            item
            for item in parsed
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item[1].strip())
        ]
    return parsed


def report_process_brief(
    workspace: Path,
    feature: dict[str, Any],
    steps: list[dict[str, str]],
    design_rows: list[dict[str, str]],
    task_rows: list[dict[str, str]],
) -> dict[str, Any]:
    feature_dir = workspace / feature["feature_dir"]
    _, unresolved = report_clarification_counts(feature_dir / "question-clarification.md")
    blocked_steps = [step["id"] for step in steps if step["state"] == "blocked"]
    failed_tasks = [
        row.get("任务ID", "?")
        for row in [*design_rows, *task_rows]
        if row.get("状态", "").strip() in {"失败", "failed", "blocked"}
    ]
    rework_pattern = re.compile(r"返工|重开|reopen|原完成：", re.I)
    rework_tasks = [
        row.get("任务ID", "?")
        for row in task_rows
        if rework_pattern.search(" ".join(str(value) for value in row.values()))
    ]
    summary = feature["summary"]
    ones_retry = summary.get("ones_sync", "").strip().startswith("retry") or any(
        row.get("ONES同步", "").strip().startswith("retry")
        for row in [*design_rows, *task_rows]
    )
    outbox = flow_outbox_counts(workspace, feature)
    # 012 D-9：queued 是正常的最终一致态（等待补发），不算 ONES 重试 blocker；retry / needs_input / comment 才算。
    if outbox and any(value for key, value in outbox.items() if key != "state_outbox_queued"):
        ones_retry = True

    step_ids = {step["id"] for step in steps}
    gate_issues: list[str] = []
    if summary.get("state", "") in {"已完成", "completed", "done"} or "S5.7" in step_ids:
        for gate in ("S5-R", "S5.7"):
            step = next((item for item in steps if item["id"] == gate), None)
            if step is None:
                gate_issues.append(f"{gate} 缺失")
            elif step["state"] not in DONE_STATES:
                gate_issues.append(f"{gate}={step['state']}")

    issues: list[str] = []
    recommendations: list[str] = []
    if blocked_steps:
        issues.append(f"阻塞步骤 {len(blocked_steps)} 个：{', '.join(blocked_steps)}。")
        recommendations.append("先关闭阻塞步骤并重新执行 flow next，避免跨 Gate 推进。")
    if failed_tasks:
        issues.append(f"失败任务 {len(failed_tasks)} 个：{', '.join(failed_tasks)}。")
        recommendations.append("按任务归属完成修复、目标测试、资金安全和任务级复审闭环。")
    if ones_retry:
        issues.append("ONES 同步存在 retry，需要处理本地 outbox 或平台错误。")
        recommendations.append("先用 ONES doctor/sync 处理 retry，保留本地状态作为事实源。")
    if unresolved:
        issues.append(f"未关闭澄清 {unresolved} 个，结论尚未完全落盘。")
        recommendations.append("将剩余问题一次性归入当前澄清批次，统一影响分析后再推进。")
    if rework_tasks:
        issues.append(f"返工任务 {len(rework_tasks)} 个：{', '.join(rework_tasks)}。")
        recommendations.append("复盘返工触发点，把可前置发现的问题补到需求、设计或任务级 Gate。")
    if gate_issues:
        issues.append("完成门禁异常：" + "、".join(gate_issues) + "。")
        recommendations.append("补齐 S5-R 模块全量回归和 S5.7 综合审查证据后再完成 Feature。")

    if blocked_steps or failed_tasks or ones_retry:
        health, level, tag_class = "有阻塞", "blocked", "major"
    elif unresolved or rework_tasks or gate_issues:
        health, level, tag_class = "需关注", "attention", "rework"
    else:
        health, level, tag_class = "流程顺畅", "smooth", "done"
        issues.append("未发现阻塞、未关闭澄清、ONES retry 或返工证据。")
        recommendations.append("保留当前的模块全量回归与综合审查纪律，并持续维护可追溯证据。")

    open_steps = sum(
        1 for step in steps if step["state"] not in CLOSED_STATES
    )
    brief_summary = (
        f"共 {len(steps)} 个流程步骤、{len(design_rows) + len(task_rows)} 个任务；"
        f"当前仍有 {open_steps} 个未关闭步骤。"
    )
    return {
        "health": health,
        "level": level,
        "tag_class": tag_class,
        "summary": brief_summary,
        "issues": issues,
        "recommendations": recommendations,
        "blocked": len(blocked_steps),
        "unresolved": unresolved,
        "reworks": len(rework_tasks),
    }


def report_elapsed_text(events: list[tuple[datetime, str, str, str]]) -> str:
    if not events:
        return "-"
    start = events[0][0]
    end = events[-1][0]
    return format_report_duration((end - start).total_seconds())


def collect_feature_title(workspace: Path, feature: dict[str, Any]) -> str:
    """STATUS.md 首个一级标题（去「 状态」后缀）作为展示标题；读不到时回退 feature 名。"""
    title = feature["feature"]
    status_path = workspace / feature["status_file"]
    try:
        first_heading = next(
            line[2:].strip() for line in status_path.read_text(encoding="utf-8").splitlines() if line.startswith("# ")
        )
        title = first_heading.removesuffix(" 状态").strip() or title
    except (OSError, StopIteration):
        pass
    return title


def snapshot_waiting_items(
    breakpoint: dict[str, Any] | None,
    dependencies_satisfied: bool,
    dependencies: list[dict[str, Any]],
    e2e_gate: E2EGateVerdict | None = None,
) -> list[str]:
    """hero「在等谁」（设计 §4.3.2）：依赖未满足步骤 / waiting-confirmation / blocked /
    外部 e2e（断点停在 S5-E2E 且门非 pass 时列出当前 verdict 与原因）。"""
    items: list[str] = []
    if breakpoint is None:
        return items
    state = breakpoint["state"]
    if state == "waiting-confirmation":
        items.append(f"人工确认 {breakpoint['id']}（{breakpoint['name']}）：flow confirm")
    elif state == "blocked":
        items.append(f"阻塞 {breakpoint['id']}（{breakpoint['name']}）：{breakpoint['note'] or '（无备注）'}")
    if (
        breakpoint["id"] == E2E_GATE_STEP
        and state in {"pending", "in-progress"}
        and e2e_gate is not None
        and e2e_gate.verdict != "pass"
    ):
        detail = e2e_gate.reason or "等待执行方回写 quality-gate.json"
        items.append(f"外部 e2e 门（verdict={e2e_gate.verdict}）：{detail}")
    if not dependencies_satisfied:
        seen: set[str] = set()
        for alternative in dependencies:
            for step in alternative["steps"]:
                if step["satisfied"] or step["id"] in seen:
                    continue
                seen.add(step["id"])
                items.append(f"依赖 {step['id']}（{step['state']}）")
    return items


# 资金风险卡点口径（T9 收窄）：只有真实待推进状态（pending/in-progress/blocked）计入；
# skipped（含 config: 书面跳过）与 waived 属已关闭，不再作为资金风险噪音入列。
FUNDING_RISK_OPEN_STATES = frozenset({"pending", "in-progress", "blocked"})


def collect_config_gates(workspace: Path) -> PipelineConfigSnapshot:
    """采集配置门快照（设计 §4.2.5）：供 dashboard/report 渲染未启用环节。

    e2e 配置非法时 fail-soft 为 enabled=True（与 evaluate_e2e_gate 的 config-error
    「已配置」口径一致），不阻断过程态快照。
    """
    try:
        return pipeline_config_snapshot(workspace)
    except DevkitFlowError:
        fund = quality_gate_summary(workspace)
        return PipelineConfigSnapshot(
            gates={
                "e2e": GateState(enabled=True, source="project"),
                "fund-security": GateState(
                    enabled=bool(fund.get("enabled", True)),
                    source=str(fund.get("source") or "default"),
                ),
            }
        )


def attach_config_gates(
    snapshot: FeatureSnapshot, config: PipelineConfigSnapshot
) -> FeatureSnapshot:
    """把配置快照挂到 FeatureSnapshot.config_gates，并把 JSON 投影写入 feature dict。"""
    if snapshot.feature is not None:
        snapshot.feature["config_gates"] = config.as_json()
    return replace(snapshot, config_gates=config)


def snapshot_config_gates(snapshot: FeatureSnapshot) -> PipelineConfigSnapshot | None:
    """读取采集层挂上的配置快照；离线构造且未挂载时返回 None。"""
    value = getattr(snapshot, "config_gates", None)
    if isinstance(value, PipelineConfigSnapshot):
        return value
    raw = (snapshot.feature or {}).get("config_gates")
    if isinstance(raw, PipelineConfigSnapshot):
        return raw
    return None


def step_display_label(name: str, step_id: str) -> str:
    """展示层步骤标签：名称在前、Sx 括号内（接口 §2.7 / 设计 §4.3.4）。"""
    return f"{(name or '-').strip() or '-'}（{(step_id or '-').strip() or '-'}）"


def pipeline_step_name(step_id: str, mode: str | None = None) -> str:
    """从行常量取步骤中文名；优先当前模式，再公共行，再跨模式回退。"""
    if mode in PIPELINE_MODE_ROWS:
        for row in PIPELINE_MODE_ROWS[mode]:
            if row[0] == step_id:
                return row[1]
    for row in PIPELINE_COMMON_ROWS:
        if row[0] == step_id:
            return row[1]
    for rows in PIPELINE_MODE_ROWS.values():
        for row in rows:
            if row[0] == step_id:
                return row[1]
    return step_id


def disabled_gate_caption(gate_name: str, source: str) -> str:
    """未启用环节来源标注（接口 §2.7）。e2e 未配置用固定文案；其余 config: source。"""
    if gate_name == "e2e":
        return "未启用：e2e 未配置"
    return f"已关闭（config: {source}）"


def gate_name_for_step(step_id: str) -> str | None:
    for gate_name, step_ids in CONFIG_GATED_STEPS.items():
        if step_id in step_ids:
            return gate_name
    return None


def _all_enabled_config() -> PipelineConfigSnapshot:
    return PipelineConfigSnapshot(
        gates={
            name: GateState(enabled=True, source="default")
            for name in CONFIG_GATED_STEPS
        }
    )


def _normalize_scale(scale: str) -> str:
    """采集层 fail-soft 归一：strip + lower（与 flow.feature_scale 同一大小写口径，016 D-42），
    空值/未知 → large（辅助产物不因 STATUS 头异常拒绝渲染，D-5）。"""
    text = (scale or "").strip().lower() or "large"
    return text if text in PIPELINE_SCALE_STEPS else "large"


def _normalize_mode(mode: str | None) -> str | None:
    text = (mode or "").strip()
    return text if text in PIPELINE_MODE_ROWS else None


def _ids_if_gates_enabled(scale: str, mode: str | None) -> set[str]:
    """配置全开时 canonical 行 id；模式未定时取两套模式并集，避免漏项。"""
    snapshot = _all_enabled_config()
    scale = _normalize_scale(scale)
    if mode in PIPELINE_MODE_ROWS:
        return {row[0] for row in missing_required_rows((), scale, mode, snapshot)}
    ids = {row[0] for row in missing_required_rows((), scale, None, snapshot)}
    for candidate in PIPELINE_MODE_ROWS:
        ids.update(row[0] for row in missing_required_rows((), scale, candidate, snapshot))
    return ids


def disabled_pipeline_items(
    config: PipelineConfigSnapshot,
    *,
    existing_ids: Iterable[str],
    scale: str = "large",
    mode: str | None = None,
) -> list[dict[str, str]]:
    """因配置关闭而未生成的门（P2）：基于配置快照，不依赖流水线行存在。

    已有存量行走「正常渲染 + 备注来源」，不进入本列表，避免与表格重复。
    """
    existing = {step_id for step_id in existing_ids if step_id}
    disabled_ids = disabled_gated_step_ids(config)
    if not disabled_ids:
        return []
    generated = _ids_if_gates_enabled(scale, mode)
    normalized_mode = _normalize_mode(mode)
    items: list[dict[str, str]] = []
    for gate_name, step_ids in CONFIG_GATED_STEPS.items():
        state = config.gate(gate_name)
        if state.enabled:
            continue
        for step_id in step_ids:
            if step_id not in disabled_ids or step_id not in generated or step_id in existing:
                continue
            name = pipeline_step_name(step_id, normalized_mode)
            caption = disabled_gate_caption(gate_name, state.source)
            items.append(
                {
                    "id": step_id,
                    "name": name,
                    "label": step_display_label(name, step_id),
                    "gate": gate_name,
                    "source": state.source,
                    "caption": caption,
                }
            )
    return items


def annotate_config_closed_steps(
    steps: list[dict[str, str]],
    config: PipelineConfigSnapshot | None,
) -> list[dict[str, str]]:
    """行存在 + 配置关闭：正常渲染行，并在备注补上来源（设计 §4.2.5 矩阵第 2 行）。"""
    if config is None:
        return list(steps)
    disabled = disabled_gated_step_ids(config)
    if not disabled:
        return list(steps)
    annotated: list[dict[str, str]] = []
    for step in steps:
        step_id = step.get("id", "")
        if step_id not in disabled:
            annotated.append(step)
            continue
        gate_name = gate_name_for_step(step_id)
        source = config.gate(gate_name).source if gate_name else ""
        caption = disabled_gate_caption(gate_name or "", source)
        note = (step.get("note") or "").strip()
        if caption in note or (source and f"config: {source}" in note):
            annotated.append(step)
            continue
        copied = dict(step)
        copied["note"] = f"{note}；{caption}" if note else caption
        annotated.append(copied)
    return annotated


def snapshot_scale_mode(snapshot: FeatureSnapshot) -> tuple[str, str | None]:
    summary = (snapshot.feature or {}).get("summary") or {}
    return _normalize_scale(str(summary.get("scale") or "")), _normalize_mode(
        str(summary.get("mode") or "")
    )


def snapshot_blockers(
    workspace: Path,
    feature: dict[str, Any],
    steps: list[dict[str, str]],
    design_rows: list[dict[str, str]],
    task_rows: list[dict[str, str]],
    outbox: dict[str, int] | None,
) -> dict[str, Any]:
    """卡点区采集（设计 §4.3.2 第 3 节）：blocked 步骤 / 未决澄清 / ONES 重试 /
    资金风险（S5.1-F / S5.2-F 处于 FUNDING_RISK_OPEN_STATES 才计入，§6.6 展示口径）。"""
    summary = feature["summary"]
    feature_dir = workspace / feature["feature_dir"]
    blocked_steps = [
        {"id": step["id"], "name": step["name"], "note": step["note"]}
        for step in steps
        if step["state"] == "blocked"
    ]

    clarifications: list[str] = []
    batch = None
    try:
        batch = load_clarification_batch(clarification_state_path(workspace, feature))
    except DevkitFlowError:
        batch = None
    if batch:
        for question in batch.get("questions", []):
            if not isinstance(question, dict):
                continue
            if str(question.get("answer", "")).strip():
                continue
            question_id = str(question.get("id", "?")).strip() or "?"
            text = str(question.get("question", "")).strip()
            clarifications.append(f"{question_id}：{text}" if text else question_id)
    if not clarifications:
        _, unanswered = report_clarification_counts(feature_dir / "question-clarification.md")
        if unanswered:
            clarifications.append(f"question-clarification.md 未解决 {unanswered} 个")

    ones_retries: list[str] = []
    header_sync = str(summary.get("ones_sync", "")).strip()
    if header_sync.startswith("retry"):
        ones_retries.append(f"STATUS 头部 ONES同步={header_sync}")
    for row in [*design_rows, *task_rows]:
        row_sync = str(row.get("ONES同步", "")).strip()
        if row_sync.startswith("retry"):
            ones_retries.append(f"任务 {row.get('任务ID', '?')} ONES同步={row_sync}")
    if outbox:
        outbox_labels = (
            ("stateOutbox queued", "state_outbox_queued"),
            ("stateOutbox retry", "state_outbox_retry"),
            ("stateOutbox needs_input", "state_outbox_needs_input"),
            ("comment pending", "comment_pending"),
            ("comment retry", "comment_retry"),
        )
        detail = "、".join(
            f"{label}={outbox[key]}" for label, key in outbox_labels if outbox.get(key)
        )
        if detail:
            ones_retries.append(f"ONES-SYNC outbox 未清空（{detail}）")

    funding_risks = [
        {"id": step["id"], "name": step["name"], "state": step["state"], "note": step["note"]}
        for step in steps
        if step["id"] in {"S5.1-F", "S5.2-F"} and step["state"] in FUNDING_RISK_OPEN_STATES
    ]
    return {
        "blocked_steps": blocked_steps,
        "clarifications": clarifications,
        "ones_retries": ones_retries,
        "funding_risks": funding_risks,
    }


def collect_feature_snapshot(workspace: Path, feature: dict[str, Any]) -> FeatureSnapshot:
    """聚合 feature 目录既有结构化文件为中性快照（设计 §4.3.2；只采集不渲染）。

    解析函数零新写：流水线/任务表走 parse_util，断点/进度/依赖/ONES outbox 走 flow，
    澄清批次走 clarify，澄清统计与变更时间线族为本模块下沉的 report_* 纯采集函数；
    e2e 门状态 W3 起接入（T11）。
    """
    status_path = status_path_for_feature(workspace, feature)
    if not status_path.is_file():
        raise DevkitFlowError(f"Feature 缺少 STATUS.md，无法采集过程态快照: {status_path}")
    feature_dir = workspace / feature["feature_dir"]
    # RDE-W1-01：挂钩链路传入的 feature dict 取自命令开始时的选取结果，命令随后改写的
    # STATUS.md 头部字段（状态/ONES需求ID/ONES同步等）在旧 summary 中不可见；
    # 头部与流水线/任务表同口径从盘重读，保证快照与 STATUS.md 一致（一处修复覆盖全部挂钩）。
    feature = {**feature, "summary": parse_status_file(status_path, workspace)["summary"]}
    summary = feature["summary"]

    table = parse_pipeline_table(status_path)
    steps = [
        {
            "id": cells[0],
            "name": cells[1],
            "agent": cells[2],
            "dependency": cells[3],
            "state": cells[4],
            "note": cells[5] if len(cells) > 5 else "",
        }
        for cells in (row["cells"] for row in table["rows"])
    ]
    try:
        design_table_rows = parse_design_task_table(status_path)["rows"]
    except DevkitFlowError:
        # 历史/精简 STATUS.md 可能无设计任务表：与 task.md 缺失同口径按空表采集，
        # dashboard 是辅助产物，缺 section 不阻断渲染（D-5 fail-soft 同精神）。
        design_table_rows = []
    design_rows = visible_design_task_rows_for_report(
        str(summary.get("scale", "")),
        design_table_rows,
    )
    task_path = task_path_for_feature(workspace, feature)
    task_rows = parse_task_table(task_path)["rows"] if task_path.exists() else []

    # 016 D-40：与 flow next 同一断点谓词——scale 收窄遗留的 pending 行不是断点（CF-1/CF-2 同源守卫）。
    # 采集层 scale 走 _normalize_scale fail-soft（未知→large，D-5 辅助产物不因头异常拒绝渲染）。
    _scale = _normalize_scale(str(summary.get("scale") or ""))
    _narrowed_ids, _narrowed_pending = scale_narrowed_pending_ids(
        table, _scale, feature_resolved_mode(table, feature)
    )
    breakpoint_row = pipeline_breakpoint(table, skip_pending_ids=_narrowed_ids)
    progress = progress_summary_values(table)
    breakpoint_info: dict[str, Any] | None = None
    dependencies: list[dict[str, Any]] = []
    dependencies_satisfied = True
    if breakpoint_row is not None:
        cells = breakpoint_row["cells"]
        breakpoint_info = {
            "id": cells[0],
            "name": cells[1],
            "agent": cells[2],
            "state": cells[4],
            "note": cells[5] if len(cells) > 5 else "",
        }
        dependencies_satisfied, dependencies = dependency_report(table["rows"], cells[3])

    outbox = flow_outbox_counts(workspace, feature)
    try:
        ones_space_id = ones_config_summary(workspace).get("space_id", "").strip()
    except DevkitFlowError:
        ones_space_id = ""
    # e2e 门状态（W3/T11，设计 §4.3.2）：配置非法等 fail-closed 异常降级为
    # blocked + reason 入快照（dashboard 是辅助产物，采集失败不阻断渲染，D-5 同精神）。
    try:
        e2e_gate = evaluate_e2e_gate(workspace)
    except DevkitFlowError as error:
        e2e_gate = E2EGateVerdict(verdict="blocked", reason=f"config-error: {compact_error(error)}")
    # next_action 与 flow next 同源（设计 §4.3.2 唯一事实源）：必须喂同一份配置门集
    # 与缺门优先级，否则 e2e 关时 dashboard 侧误催 S5-E2E migrate-steps（S5.7 CF-1），
    # 断点未闭且缺启用门时不透出补行信号（CF-2）。config_gates 复用同一快照。
    config_gates = collect_config_gates(workspace)
    expected_gates = expected_completion_gate_steps(config_gates, _scale)
    missing_priority = priority_missing_gate_ids(table, feature, config_gates, expected_gates, scale=_scale)
    snapshot = FeatureSnapshot(
        feature=feature,
        title=collect_feature_title(workspace, feature),
        pipeline=steps,
        design_tasks=design_rows,
        dev_tasks=task_rows,
        progress=progress,
        breakpoint=breakpoint_info,
        next_action=next_action_text(
            table,
            breakpoint_row,
            dependencies_satisfied,
            expected_gates,
            missing_priority,
            _narrowed_pending,
        ),
        dependencies_satisfied=dependencies_satisfied,
        dependencies=dependencies,
        waiting_on=snapshot_waiting_items(breakpoint_info, dependencies_satisfied, dependencies, e2e_gate),
        blockers=snapshot_blockers(workspace, feature, steps, design_rows, task_rows, outbox),
        clarification=report_clarification_stats(feature_dir),
        ones_outbox=outbox,
        ones_space_id=ones_space_id,
        e2e_gate=e2e_gate,
    )
    return attach_config_gates(snapshot, config_gates)
