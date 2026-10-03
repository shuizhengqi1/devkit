from __future__ import annotations

import argparse
import html
from datetime import datetime
from pathlib import Path
from typing import Any
from devkit_flow.embedded import embedded_templates
from devkit_flow.config import ones_config_summary
from devkit_flow.constants import DONE_STATES, PIPELINE_STEP_STAGES
from devkit_flow.errors import DevkitFlowError
from devkit_flow.flow import flow_now_iso, flow_select_feature
from devkit_flow.parse_util import find_features, parse_design_task_table, parse_pipeline_table, parse_task_table, status_path_for_feature, summarize_features, task_path_for_feature, visible_design_task_rows_for_report
from devkit_flow.snapshot import (
    annotate_config_closed_steps,
    collect_config_gates,
    collect_feature_title,
    collect_report_timeline_events,
    disabled_pipeline_items,
    format_report_duration,
    report_clarification_stats,
    report_cycle_text,
    report_elapsed_text,
    report_feature_subtitle,
    report_file_changes,
    report_human_intervention_count,
    report_process_brief,
    report_state_bucket,
)
from devkit_flow.textio import atomic_write_text, emit, ones_workitem_url


# --- flow report（workflow-visualization.html 渲染） ---------------------
# 渲染壳：数据采集一律经 devkit_flow.snapshot（§4.3.2 共享采集层），本模块只负责
# 把采集结果渲染进模板。注入点与 spec/template/workflow-visualization-template.html
# 中的 {{TOKEN}} 与 <!-- AGENT_INJECT: ... --> 标记一一对应，模板改动需同步此处。
def ones_link_html(space_id: str, kind: str, workitem_id: str) -> str:
    """ONES ID 渲染为跳转链接；无 spaceId 或无 ID 时保持纯文本（不出链接）。"""
    if not workitem_id:
        return "-"
    text = html.escape(workitem_id)
    if not space_id:
        return text
    url = html.escape(ones_workitem_url(space_id, kind, workitem_id), quote=True)
    return f'<a href="{url}" target="_blank">{text}</a>'


def report_tag_html(state: str) -> str:
    bucket = report_state_bucket(state)
    css = {
        "done": "done",
        "skipped": "skipped",
        "waived": "skipped",
        "blocked": "major",
    }.get(bucket, "info")
    return f'<span class="tag {css}">{html.escape(state or "-")}</span>'


def report_pipeline_step_html(step: dict[str, str]) -> str:
    """单个流水线节点渲染：名称在前、Sx 括号内（接口 §2.7 / 设计 §4.3.4）。"""
    note_parts = []
    if step.get("dependency") and step["dependency"] != "-":
        note_parts.append(f"依赖：{step['dependency']}")
    if step.get("note"):
        note_parts.append(step["note"])
    note = "；".join(note_parts) or "暂无补充备注"
    return (
        f'<details open data-phase="{html.escape(step["id"], quote=True)}">'
        "<summary>"
        f'<span class="sname">{html.escape(step.get("name", "") or "-")}</span>'
        f'<span class="sid">（{html.escape(step.get("id", "") or "-")}）</span>'
        f'{report_tag_html(step["state"])}'
        f'<span class="agent">{html.escape(step.get("agent", ""))}</span>'
        "</summary>"
        f'<div class="leaf">{html.escape(note)}</div>'
        "</details>"
    )


def report_pipeline_stage_group_html(stage: str, steps: list[dict[str, str]]) -> str:
    """阶段分组外壳（组标题 + 组内步骤树，§4.4.2）；空阶段显式占位兜底，不缺位。"""
    done = sum(1 for step in steps if step["state"] in DONE_STATES)
    body = "\n".join(report_pipeline_step_html(step) for step in steps) or (
        '<div class="empty">本阶段无流水线步骤</div>'
    )
    return (
        f'<details class="stage-group" open data-stage="{html.escape(stage, quote=True)}">'
        "<summary>"
        f'<span class="stage-name">{html.escape(stage)}</span>'
        f'<span class="cnt">{done}/{len(steps)}</span>'
        "</summary>"
        f"{body}"
        "</details>"
    )


def report_disabled_gate_step_html(item: dict[str, str]) -> str:
    """未启用环节节点：灰显 + 名称（Sx）+ 配置来源，不依赖流水线行。"""
    return (
        f'<details open data-phase="{html.escape(item["id"], quote=True)}">'
        "<summary>"
        f'<span class="sname">{html.escape(item["name"])}</span>'
        f'<span class="sid">（{html.escape(item["id"])}）</span>'
        '<span class="tag skipped">未启用</span>'
        "</summary>"
        f'<div class="leaf">{html.escape(item["caption"])}</div>'
        "</details>"
    )


def report_pipeline_tree_html(
    steps: list[dict[str, str]],
    disabled_items: list[dict[str, str]] | None = None,
) -> str:
    """按 PIPELINE_STEP_STAGES 四阶段分组输出（组标题 + 组内步骤树，设计 §4.4.2 / D-6）。

    阶段顺序固定取映射值出现序（test_pipeline_step_stages_cover_all_pipeline_steps
    钉死为 需求→开发→测试→审查）；存量 STATUS.md 中映射未覆盖的步骤行追加显式
    「未归组」组兜底，绝不静默丢弃。配置关闭且无行的门追加「未启用环节」组（P2）。
    """
    disabled = disabled_items or []
    grouped: dict[str, list[dict[str, str]]] = {}
    ungrouped: list[dict[str, str]] = []
    for step in steps:
        stage = PIPELINE_STEP_STAGES.get(step["id"], "")
        if stage:
            grouped.setdefault(stage, []).append(step)
        else:
            ungrouped.append(step)
    nodes = [
        report_pipeline_stage_group_html(stage, grouped.get(stage, []))
        for stage in dict.fromkeys(PIPELINE_STEP_STAGES.values())
    ]
    if ungrouped:
        nodes.append(
            report_pipeline_stage_group_html("未归组（PIPELINE_STEP_STAGES 未覆盖）", ungrouped)
        )
    if disabled:
        body = "\n".join(report_disabled_gate_step_html(item) for item in disabled)
        nodes.append(
            '<details class="stage-group" open data-stage="未启用环节">'
            "<summary>"
            '<span class="stage-name">未启用环节</span>'
            f'<span class="cnt">0/{len(disabled)}</span>'
            "</summary>"
            f"{body}"
            "</details>"
        )
    return "\n".join(nodes)


def report_design_task_rows_html(rows: list[dict[str, str]], space_id: str) -> str:
    rendered: list[str] = []
    for row in rows:
        note = row.get("备注", "").strip() or row.get("ONES同步", "").strip() or "-"
        rendered.append(
            "<tr>"
            f'<td class="id">{html.escape(row.get("任务ID", "-"))}</td>'
            f'<td>{html.escape(row.get("描述", "-"))}</td>'
            f'<td>{report_tag_html(row.get("状态", "-"))}</td>'
            f'<td class="time">{html.escape(row.get("实际开始", "").strip() or "-")}</td>'
            f'<td class="time">{html.escape(row.get("实际完成", "").strip() or "-")}</td>'
            f'<td>{ones_link_html(space_id, "task", row.get("ONES任务ID", "").strip())}</td>'
            f'<td class="note">{html.escape(note)}</td>'
            "</tr>"
        )
    return "\n".join(rendered) or '<tr><td colspan="7">-</td></tr>'


def report_development_task_rows_html(rows: list[dict[str, str]], space_id: str) -> str:
    rendered: list[str] = []
    for row in rows:
        note = row.get("门禁进度", "").strip() or row.get("产出/备注", "").strip() or "-"
        rendered.append(
            "<tr>"
            f'<td class="id">{html.escape(row.get("任务ID", "-"))}</td>'
            f'<td>{html.escape(row.get("描述", "-"))}</td>'
            f'<td>{report_tag_html(row.get("状态", "-"))}</td>'
            f'<td class="note">{html.escape(note)}</td>'
            f'<td class="time">{html.escape(row.get("实际开始", "").strip() or "-")}</td>'
            f'<td class="time">{html.escape(row.get("实际完成", "").strip() or "-")}</td>'
            f'<td>{ones_link_html(space_id, "task", row.get("ONES任务ID", "").strip())}</td>'
            "</tr>"
        )
    return "\n".join(rendered) or '<tr><td colspan="7">-</td></tr>'


def report_list_items(items: list[str]) -> str:
    return "\n".join(f"<li>{html.escape(item)}</li>" for item in items)


def report_file_groups_html(file_changes: list[dict[str, Any]]) -> str:
    groups: dict[str, list[str]] = {}
    for entry in file_changes:
        path = str(entry["path"])
        group = path.split("/", 1)[0] if "/" in path else "根目录"
        groups.setdefault(group, []).append(path)
    rendered: list[str] = []
    for group, paths in sorted(groups.items()):
        items = "".join(
            f'<li><span class="p">{html.escape(path)}</span></li>' for path in sorted(paths)
        )
        rendered.append(
            f'<details class="fgroup" open><summary>{html.escape(group)}'
            f'<span class="cnt">{len(paths)} 个文件</span></summary>'
            f'<div class="body"><ul>{items}</ul></div></details>'
        )
    return "\n".join(rendered) or '<div class="empty">暂无可核验的文件证据</div>'


def report_retrospective_timeline_html(
    feature_dir: Path,
    summary: dict[str, str],
    design_rows: list[dict[str, str]],
    task_rows: list[dict[str, str]],
) -> str:
    events = collect_report_timeline_events(feature_dir, summary, design_rows, task_rows)
    if not events:
        return '<div class="empty">暂无可核验的时间证据</div>'
    rendered: list[str] = []
    previous: datetime | None = None
    for moment, when, label, css in events:
        gap = ""
        if previous is not None:
            delta = (moment - previous).total_seconds()
            if delta >= 1:
                gap = f'<div class="g">间隔 {html.escape(format_report_duration(delta))}</div>'
            elif delta == 0:
                gap = '<div class="g">同时</div>'
        rendered.append(
            f'<div class="tl-item {html.escape(css)}">{gap}'
            f'<div class="t">{html.escape(when)}</div>'
            f'<div class="h">{html.escape(label)}</div></div>'
        )
        previous = moment
    return "\n".join(rendered)


def report_retrospective_metric_card(
    value: object, label: str, *, hint: str = "", wide: bool = False
) -> str:
    css = "card wide" if wide else "card"
    extra = f'<div class="s">{html.escape(hint)}</div>' if hint else ""
    return (
        f'<div class="{css}">'
        f'<div class="v">{html.escape(str(value))}</div>'
        f'<div class="k">{html.escape(label)}</div>{extra}</div>'
    )


def render_workflow_report(
    workspace: Path,
    feature: dict[str, Any],
    steps: list[dict[str, str]],
    design_rows: list[dict[str, str]],
    task_rows: list[dict[str, str]],
) -> tuple[str, dict[str, int]]:
    """Render the single supported light retrospective report."""
    feature_dir = workspace / feature["feature_dir"]
    summary = feature["summary"]
    try:
        ones_space_id = ones_config_summary(workspace).get("space_id", "").strip()
    except DevkitFlowError:
        ones_space_id = ""

    clarification = report_clarification_stats(feature_dir)
    clarification_count = clarification["items"]
    changelog_path = feature_dir / "CHANGELOG.md"
    changelog_count = (
        sum(1 for line in changelog_path.read_text(encoding="utf-8").splitlines() if line.startswith("## "))
        if changelog_path.exists()
        else 0
    )
    steps_done = sum(1 for step in steps if report_state_bucket(step["state"]) == "done")
    all_tasks = [*design_rows, *task_rows]
    tasks_done = sum(1 for row in all_tasks if row.get("状态", "").strip() in DONE_STATES)
    file_changes = report_file_changes(workspace, feature_dir, task_rows)
    brief = report_process_brief(workspace, feature, steps, design_rows, task_rows)
    timeline_events = collect_report_timeline_events(
        feature_dir, summary, design_rows, task_rows
    )
    elapsed = report_elapsed_text(timeline_events)
    human_interventions = report_human_intervention_count(steps, feature_dir)
    clarification_hint = (
        f"已答 {clarification['answered']} / {clarification['items']}"
        + (f" · {clarification['batches']} 批" if clarification["batches"] else "")
        + (f" · 约 {clarification['chars']} 字" if clarification["chars"] else "")
    )

    title = collect_feature_title(workspace, feature)
    config = collect_config_gates(workspace)
    scale = str(summary.get("scale") or "large")
    mode = str(summary.get("mode") or "") or None
    display_steps = annotate_config_closed_steps(steps, config)
    disabled_items = disabled_pipeline_items(
        config,
        existing_ids=(step["id"] for step in steps),
        scale=scale,
        mode=mode,
    )

    tokens = {
        "FEATURE_NAME": Path(feature["feature_dir"]).name,
        "FEATURE_TITLE": title,
        "FEATURE_SUBTITLE": report_feature_subtitle(feature_dir),
        "FEATURE_STATE": summary.get("state", "-") or "-",
        "FEATURE_SCALE": summary.get("scale", "-") or "-",
        "FEATURE_MODE": summary.get("mode", "-") or "-",
        "FEATURE_BRANCH": summary.get("branch", "-") or "-",
        "FEATURE_CYCLE": report_cycle_text(summary, feature_dir, design_rows, task_rows),
        "GENERATED_AT": flow_now_iso(),
        "BRIEF_LEVEL": brief["level"],
        "BRIEF_TAG_CLASS": brief["tag_class"],
        "BRIEF_HEALTH": brief["health"],
        "BRIEF_SUMMARY": brief["summary"],
    }
    injects = {
        "process brief issues": report_list_items(brief["issues"]),
        "process brief recommendations": report_list_items(brief["recommendations"]),
        "pipeline tree": report_pipeline_tree_html(display_steps, disabled_items),
        "design task rows": report_design_task_rows_html(design_rows, ones_space_id),
        "development task rows": report_development_task_rows_html(task_rows, ones_space_id),
        "file groups": report_file_groups_html(file_changes),
        "timeline items": report_retrospective_timeline_html(feature_dir, summary, design_rows, task_rows),
        "metric cards": "\n".join(
            [
                report_retrospective_metric_card(
                    human_interventions,
                    "用户介入",
                    hint="人工 Gate（-H）次数 + 澄清批次数",
                ),
                report_retrospective_metric_card(
                    clarification_count,
                    "澄清内容",
                    hint=clarification_hint,
                    wide=True,
                ),
                report_retrospective_metric_card(
                    elapsed,
                    "总执行时间",
                    hint="最早到最晚可核验时间戳之差",
                    wide=True,
                ),
                report_retrospective_metric_card(len(steps), "流程步骤"),
                report_retrospective_metric_card(steps_done, "已完成步骤"),
                report_retrospective_metric_card(len(all_tasks), "任务总数"),
                report_retrospective_metric_card(tasks_done, "已完成任务"),
                report_retrospective_metric_card(brief["reworks"], "返工任务"),
                report_retrospective_metric_card(changelog_count, "变更记录"),
                report_retrospective_metric_card(len(file_changes), "文件证据"),
            ]
        ),
    }

    rendered = embedded_templates()["workflow-visualization-template"]
    for key, value in tokens.items():
        rendered = rendered.replace("{{" + key + "}}", html.escape(str(value)))
    requirement_id = summary.get("ones_requirement_id", "").strip()
    requirement = ones_link_html(ones_space_id, "requirement", requirement_id)
    if requirement_id:
        requirement = f"<strong>{requirement}</strong>"
    rendered = rendered.replace("{{ONES_REQUIREMENT}}", requirement)
    for marker, content in injects.items():
        full_marker = f"<!-- AGENT_INJECT: {marker} -->"
        if full_marker not in rendered:
            raise DevkitFlowError(f"可视化模板缺少注入点: {full_marker}")
        rendered = rendered.replace(full_marker, content)
    if "{{" in rendered:
        raise DevkitFlowError("可视化模板仍包含未替换变量。")

    stats = {
        "steps_total": len(steps),
        "steps_done": steps_done,
        "tasks_total": len(all_tasks),
        "tasks_done": tasks_done,
        "clarifications": clarification_count,
        "clarification_answered": clarification["answered"],
        "clarification_batches": clarification["batches"],
        "human_interventions": human_interventions,
        "elapsed": elapsed,
        "changelog_entries": changelog_count,
        "files_listed": len(file_changes),
        "blocked_steps": brief["blocked"],
        "unresolved_clarifications": brief["unresolved"],
        "rework_tasks": brief["reworks"],
    }
    return rendered, stats


def write_workflow_report(
    workspace: Path,
    feature: dict[str, Any],
    output: str | None = None,
) -> dict[str, Any]:
    status_path = status_path_for_feature(workspace, feature)
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
    design_rows = visible_design_task_rows_for_report(
        str(feature.get("summary", {}).get("scale", "")),
        parse_design_task_table(status_path)["rows"],
    )
    task_path = task_path_for_feature(workspace, feature)
    task_rows = parse_task_table(task_path)["rows"] if task_path.exists() else []

    rendered, stats = render_workflow_report(workspace, feature, steps, design_rows, task_rows)

    if output:
        output_path = Path(output)
        if not output_path.is_absolute():
            output_path = workspace / output_path
    else:
        output_path = workspace / feature["feature_dir"] / "workflow-visualization.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output_path, rendered)
    try:
        relative_output = output_path.relative_to(workspace).as_posix()
    except ValueError:
        relative_output = str(output_path)
    return {
        "output": relative_output,
        "absolute_path": str(output_path.resolve()),
        "stats": stats,
    }


def command_flow_report(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    feature = None
    if args.feature:
        matches = [
            candidate
            for candidate in find_features(workspace)
            if args.feature in {candidate["feature"], Path(candidate["feature_dir"]).name}
        ]
        if len(matches) == 1:
            feature = matches[0]
        else:
            emit(
                {
                    "status": "feature_not_found" if not matches else "multiple_features",
                    "message": (
                        f"Feature not found: {args.feature}"
                        if not matches
                        else f"Multiple feature directories match: {args.feature}"
                    ),
                    "features": summarize_features(matches),
                },
                args.format,
            )
            return 1
    else:
        feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    report = write_workflow_report(workspace, feature, args.output)

    data = {
        "status": "ok",
        "message": (
            f"可视化报告已生成: {report['output']}"
            f"（步骤 {report['stats']['steps_done']}/{report['stats']['steps_total']}，"
            f"任务 {report['stats']['tasks_done']}/{report['stats']['tasks_total']}）。"
        ),
        "feature": feature["feature"],
        **report,
    }
    emit(data, args.format)
    return 0
