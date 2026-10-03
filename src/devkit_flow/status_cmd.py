from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from devkit_flow.config import context_source_summary, format_review_gate_timing_status_text, load_review_gate_timing_summary, ones_config_summary, quality_gate_summary
from devkit_flow.telemetry import telemetry_config_summary
from devkit_flow.parse_util import find_features, select_feature
from devkit_flow.review import design_template_summary, format_design_template_status_text, format_review_rounds_status_text, review_rounds_summary
from devkit_flow.switches import dashboard_switch_summary, docs_curator_summary, format_dashboard_status_text, format_docs_curator_status_text, format_html_ui_status_text, format_spec_artifacts_status_text, format_specrules_status_text, html_ui_summary, spec_artifacts_summary, specrules_summary
from devkit_flow.textio import emit_update_notice, maybe_check_latest_update
from devkit_flow.workflow_auto import format_workflow_status_text, workflow_auto_takeover_summary


def _append_flow_config_status_lines(lines: list[str], data: dict[str, Any]) -> None:
    specrules = data.get("specrules")
    if isinstance(specrules, dict) and "effective_state" in specrules:
        lines.append(format_specrules_status_text(specrules))
    docs_curator = data.get("docs_curator")
    if isinstance(docs_curator, dict) and "enabled" in docs_curator:
        lines.append(format_docs_curator_status_text(docs_curator))
    dashboard = data.get("dashboard")
    if isinstance(dashboard, dict) and "enabled" in dashboard:
        lines.append(format_dashboard_status_text(dashboard))
    html_ui = data.get("html_ui")
    if isinstance(html_ui, dict) and "enabled" in html_ui:
        lines.append(format_html_ui_status_text(html_ui))
    spec_artifacts = data.get("spec_artifacts")
    if isinstance(spec_artifacts, dict) and "enabled" in spec_artifacts:
        lines.append(format_spec_artifacts_status_text(spec_artifacts))
    review_rounds = data.get("review_rounds")
    if isinstance(review_rounds, dict) and "max_rounds" in review_rounds:
        lines.append(format_review_rounds_status_text(review_rounds))
    review_gate_timing = data.get("review.gate_timing")
    if isinstance(review_gate_timing, dict) and "timing" in review_gate_timing:
        lines.append(format_review_gate_timing_status_text(review_gate_timing))
    design_template = data.get("design_template")
    if isinstance(design_template, dict) and design_template.get("effective_mode"):
        lines.append(format_design_template_status_text(design_template))

def format_text(data: dict[str, Any]) -> str:
    status = data["status"]
    if status != "ok":
        lines = [data.get("message", status)]
        for feature in data.get("features", []):
            current = feature.get("current_step") or {}
            step = current.get("id", "-")
            name = current.get("name", "-")
            lines.append(f"- {feature['feature']} ({feature['feature_dir']}): {step} {name}")
        ones = data.get("integrations", {}).get("ones")
        if ones:
            lines.append(
                "ones: "
                + ("enabled" if ones["enabled"] else "disabled")
                + (f" (space {ones['space_id']})" if ones["space_id"] else "")
            )
        workflow = (data.get("workflow") or {}).get("auto_takeover")
        if isinstance(workflow, dict):
            lines.append(format_workflow_status_text(workflow).split("\n")[0])
        _append_flow_config_status_lines(lines, data)
        return "\n".join(lines) + "\n"

    current = data.get("current_step") or {}
    summary = data["summary"]
    lines = [
        f"feature: {data['feature']}",
        f"directory: {data['feature_dir']}",
        f"state: {summary.get('state') or '-'}",
        f"scale: {summary.get('scale') or '-'}",
        f"mode: {summary.get('mode') or '-'}",
        f"branch: {summary.get('branch') or '-'}",
        f"current_step: {current.get('id', '-')} {current.get('name', '-')}",
        f"current_state: {current.get('state', '-')}",
        "completed_steps: " + (", ".join(data["completed_steps"]) or "-"),
        "waived_steps: " + (", ".join(data.get("waived_steps", [])) or "-"),
        "remaining_steps: " + (", ".join(data["remaining_steps"]) or "-"),
    ]
    if summary.get("ones_requirement_id"):
        lines.append(f"ones_requirement_id: {summary['ones_requirement_id']}")
    if summary.get("ones_sync"):
        lines.append(f"ones_sync: {summary['ones_sync']}")
    ones = data.get("integrations", {}).get("ones")
    if ones:
        lines.append(
            "ones: "
            + ("enabled" if ones["enabled"] else "disabled")
            + (f" (space {ones['space_id']})" if ones["space_id"] else "")
        )
    workflow = (data.get("workflow") or {}).get("auto_takeover")
    if isinstance(workflow, dict):
        lines.append(format_workflow_status_text(workflow).split("\n")[0])
    _append_flow_config_status_lines(lines, data)
    if current.get("note"):
        lines.append(f"note: {current['note']}")
    return "\n".join(lines) + "\n"


def command_status(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    data = select_feature(find_features(workspace), args.feature)
    data["integrations"] = {
        "ones": ones_config_summary(workspace),
        "telemetry": telemetry_config_summary(workspace),
    }
    data["quality_gates"] = {"fund_security": quality_gate_summary(workspace)}
    data["context_sources"] = {"kba": context_source_summary(workspace)}
    data["workflow"] = {"auto_takeover": workflow_auto_takeover_summary(workspace)}
    data["docs_curator"] = docs_curator_summary(workspace)
    data["dashboard"] = dashboard_switch_summary(workspace)
    data["specrules"] = specrules_summary(workspace)
    data["html_ui"] = html_ui_summary(workspace)
    data["spec_artifacts"] = spec_artifacts_summary(workspace)
    data["review_rounds"] = review_rounds_summary(workspace)
    data["review.gate_timing"] = load_review_gate_timing_summary(workspace)
    data["design_template"] = design_template_summary(workspace)
    update = maybe_check_latest_update()
    if update is not None:
        data["update"] = update
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(format_text(data))
        emit_update_notice(update, args.format)
    return 0
