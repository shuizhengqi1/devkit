from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from devkit_flow.embedded import embedded_templates
from devkit_flow.constants import CANONICAL_DESIGN_TASKS, CLOSED_STATES, DESIGN_TASK_COLUMNS, DESIGN_TASK_SEPARATOR, DONE_STATES, INACTIVE_FEATURE_STATES, PIPELINE_TABLE_HEADER, PIPELINE_TABLE_SEPARATOR, SKIPPED_STATES, SMALL_TASK_HEADERS, TASK_SYNC_COLUMNS, WAIVED_STATES
from devkit_flow.errors import DevkitFlowError
from devkit_flow.textio import atomic_write_text


def feature_name_from_dir(feature_dir: Path) -> str:
    name = feature_dir.name
    match = re.match(r"^(?:\d{3,}|00x)-(.+)$", name)
    if match:
        return match.group(1)
    return name


def next_feature_id(workspace: Path) -> str:
    spec_root = workspace / "spec"
    highest = 0
    if spec_root.is_dir():
        for child in spec_root.iterdir():
            if not child.is_dir():
                continue
            match = re.match(r"^(\d{3,})-", child.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"{highest + 1:03d}"


def split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def parse_status_file(path: Path, workspace: Path) -> dict[str, Any]:
    summary: dict[str, str] = {}
    pipeline: list[dict[str, str]] = []
    in_pipeline = False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        header_match = re.match(r"^\*\*(.+?)\*\*:\s*(.*)$", line)
        if header_match:
            summary[header_match.group(1)] = header_match.group(2).strip()
            continue

        if line.startswith("## "):
            in_pipeline = line == "## 流水线"
            continue

        if not in_pipeline:
            continue

        cells = split_markdown_row(line)
        if len(cells) < 6:
            continue
        if cells[0] == "步骤" or set(cells[0]) <= {"-"}:
            continue

        pipeline.append(
            {
                "id": cells[0],
                "name": cells[1],
                "agent": cells[2],
                "depends_on": cells[3],
                "state": cells[4],
                "note": cells[5],
            }
        )

    feature_dir = path.parent
    feature = feature_name_from_dir(feature_dir)
    completed_steps = [step["id"] for step in pipeline if step["state"] in DONE_STATES]
    waived_steps = [step["id"] for step in pipeline if step["state"] in WAIVED_STATES]
    remaining_steps = [
        step["id"]
        for step in pipeline
        if step["state"] not in CLOSED_STATES
    ]
    current_step = next(
        (
            step
            for step in pipeline
            if step["state"] not in CLOSED_STATES
        ),
        None,
    )

    return {
        "feature": feature,
        "feature_dir": path.parent.relative_to(workspace).as_posix(),
        "status_file": path.relative_to(workspace).as_posix(),
        "summary": {
            "state": summary.get("状态", ""),
            "scale": summary.get("规模", ""),
            "mode": summary.get("模式", ""),
            "branch": summary.get("分支", ""),
            "created_at": summary.get("创建时间", ""),
            "last_updated": summary.get("最后更新", ""),
            "ones_requirement_id": summary.get("ONES需求ID", ""),
            "ones_sync": summary.get("ONES同步", ""),
            "trace_id": summary.get("TraceID", ""),
        },
        "current_step": current_step,
        "waived_steps": waived_steps,
        "completed_steps": completed_steps,
        "remaining_steps": remaining_steps,
        "pipeline": pipeline,
    }


def find_features(workspace: Path) -> list[dict[str, Any]]:
    spec_root = workspace / "spec"
    if not spec_root.is_dir():
        return []

    features: list[dict[str, Any]] = []
    for status_file in sorted(spec_root.glob("*/STATUS.md")):
        features.append(parse_status_file(status_file, workspace))
    return features


def is_active(feature: dict[str, Any]) -> bool:
    state = feature.get("summary", {}).get("state", "")
    return state not in INACTIVE_FEATURE_STATES


def select_feature(features: list[dict[str, Any]], requested: str | None) -> dict[str, Any]:
    active_features = [feature for feature in features if is_active(feature)]

    if requested:
        matches = [
            feature
            for feature in active_features
            if requested in {feature["feature"], Path(feature["feature_dir"]).name}
        ]
        if len(matches) == 1:
            return {"status": "ok", **matches[0]}
        if not matches:
            return {
                "status": "feature_not_found",
                "message": f"Feature not found: {requested}",
                "features": summarize_features(active_features),
            }
        return {
            "status": "multiple_active_features",
            "message": "Found multiple matching active features; choose one explicitly.",
            "features": summarize_features(matches),
        }

    if not active_features:
        return {
            "status": "no_active_feature",
            "message": "No active feature found.",
            "features": [],
        }
    if len(active_features) > 1:
        return {
            "status": "multiple_active_features",
            "message": "Found multiple active features; choose one explicitly.",
            "features": summarize_features(active_features),
        }
    return {"status": "ok", **active_features[0]}


def select_feature_for_ones(
    features: list[dict[str, Any]],
    requested: str | None,
    *,
    implicit_active_only: bool = False,
) -> dict[str, Any]:
    if requested:
        matches = [
            feature
            for feature in features
            if requested in {feature["feature"], Path(feature["feature_dir"]).name}
        ]
        if len(matches) == 1:
            return {"status": "ok", **matches[0]}
        if not matches:
            raise DevkitFlowError(f"Feature not found: {requested}")
        raise DevkitFlowError(f"Multiple features match: {requested}")

    selected = select_feature(features, None)
    if selected["status"] == "ok":
        return selected
    if implicit_active_only:
        if selected["status"] == "no_active_feature":
            raise DevkitFlowError(
                "No active feature found; pass --feature explicitly to select a completed feature."
            )
        raise DevkitFlowError("Found multiple active features; pass --feature explicitly.")
    if not features:
        raise DevkitFlowError("No feature with STATUS.md found.")
    if len(features) == 1:
        return {"status": "ok", **features[0]}
    raise DevkitFlowError("Found multiple features; pass --feature explicitly.")


def summarize_features(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "feature": feature["feature"],
            "feature_dir": feature["feature_dir"],
            "state": feature["summary"]["state"],
            "current_step": feature["current_step"],
        }
        for feature in features
    ]


def status_path_for_feature(workspace: Path, feature: dict[str, Any]) -> Path:
    return workspace / feature["status_file"]


def task_path_for_feature(workspace: Path, feature: dict[str, Any]) -> Path:
    return workspace / feature["feature_dir"] / "task.md"


def set_status_field(path: Path, field_name: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    prefix = f"**{field_name}**:"
    replacement = f"{prefix} {value}"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = replacement
            break
    else:
        insert_at = next(
            (
                index + 1
                for index, line in enumerate(lines)
                if line.startswith("**ONES需求ID**:")
            ),
            next(
                (index + 1 for index, line in enumerate(lines) if line.startswith("**状态**:")),
                1,
            ),
        )
        lines.insert(insert_at, replacement)
    atomic_write_text(path, "\n".join(lines) + "\n")


def set_status_requirement_id(path: Path, requirement_id: str) -> None:
    set_status_field(path, "ONES需求ID", requirement_id)


def set_status_ones_sync(path: Path, sync_state: str) -> None:
    set_status_field(path, "ONES同步", sync_state)


def parse_task_table(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise DevkitFlowError(f"Task file not found: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    header_index = -1
    headers: list[str] = []
    for index, line in enumerate(lines):
        cells = split_markdown_row(line)
        if "任务ID" in cells and "状态" in cells:
            header_index = index
            headers = cells
            break
    if header_index < 0:
        raise DevkitFlowError(f"No task table found in {path}")

    row_start = header_index + 2
    row_end = row_start
    rows: list[dict[str, str]] = []
    while row_end < len(lines):
        cells = split_markdown_row(lines[row_end])
        if not cells:
            break
        padded = cells + [""] * max(0, len(headers) - len(cells))
        rows.append(dict(zip(headers, padded[: len(headers)])))
        row_end += 1

    output_index = headers.index("产出/备注") if "产出/备注" in headers else len(headers)
    for column in TASK_SYNC_COLUMNS:
        if column not in headers:
            headers.insert(output_index, column)
            output_index += 1
    for row in rows:
        for header in headers:
            row.setdefault(header, "")

    return {
        "lines": lines,
        "header_index": header_index,
        "row_start": row_start,
        "row_end": row_end,
        "headers": headers,
        "rows": rows,
    }


def save_task_table(path: Path, table: dict[str, Any]) -> None:
    headers: list[str] = table["headers"]
    rows: list[dict[str, str]] = table["rows"]
    rendered = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("-" * max(6, len(header) * 2) for header in headers) + "|",
    ]
    for row in rows:
        rendered.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")

    lines: list[str] = table["lines"]
    start = table["header_index"]
    end = table["row_end"]
    updated = lines[:start] + rendered + lines[end:]
    atomic_write_text(path, "\n".join(updated) + "\n")


def markdown_cell(value: str) -> str:
    """Keep generated Markdown tables parseable by the CLI's simple pipe splitter."""
    return re.sub(r"\s+", " ", value.strip()).replace("|", "｜")


def small_task_row(description: str, write_scope: str, acceptance: str) -> dict[str, str]:
    return {
        "任务ID": "T-01",
        "描述": markdown_cell(description),
        "前置依赖": "D-01",
        "验收标准": markdown_cell(acceptance),
        "预计写入范围": markdown_cell(write_scope),
        "状态": "待执行",
        "门禁进度": "",
        "计划开始": "",
        "计划完成": "",
        "预计工作量": "",
        "实际开始": "",
        "实际完成": "",
        "ONES任务ID": "",
        "ONES同步": "pending",
        "产出/备注": "Small 单任务由 devkit-flow 创建；不执行多任务智能拆解",
    }


def write_new_small_task_file(path: Path, feature_name: str, row: dict[str, str]) -> None:
    separator = "|" + "|".join("-" * max(6, len(header) * 2) for header in SMALL_TASK_HEADERS) + "|"
    lines = [
        f"# {feature_name} 任务执行记录",
        "",
        "| " + " | ".join(SMALL_TASK_HEADERS) + " |",
        separator,
        "| " + " | ".join(row[header] for header in SMALL_TASK_HEADERS) + " |",
        "",
    ]
    atomic_write_text(path, "\n".join(lines))


def parse_design_task_table(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise DevkitFlowError(f"Status file not found: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    header_index = -1
    headers: list[str] = []
    for index, line in enumerate(lines):
        cells = split_markdown_row(line)
        if cells[:3] == ["任务ID", "类型", "描述"]:
            header_index = index
            headers = cells
            break
    if header_index < 0:
        raise DevkitFlowError(f"No design task table found in {path}")

    row_start = header_index + 2
    row_end = row_start
    rows: list[dict[str, str]] = []
    while row_end < len(lines):
        cells = split_markdown_row(lines[row_end])
        if not cells:
            break
        padded = cells + [""] * max(0, len(headers) - len(cells))
        rows.append(dict(zip(headers, padded[: len(headers)])))
        row_end += 1

    for column in DESIGN_TASK_COLUMNS:
        if column not in headers:
            headers.append(column)
    for row in rows:
        for header in headers:
            row.setdefault(header, "")

    return {
        "lines": lines,
        "header_index": header_index,
        "row_start": row_start,
        "row_end": row_end,
        "headers": headers,
        "rows": rows,
    }


def save_design_task_table(path: Path, table: dict[str, Any]) -> None:
    headers: list[str] = table["headers"]
    rows: list[dict[str, str]] = table["rows"]
    rendered = [
        "| " + " | ".join(headers) + " |",
        (
            DESIGN_TASK_SEPARATOR
            if headers == DESIGN_TASK_COLUMNS
            else "|" + "|".join("-" * max(6, len(header) * 2) for header in headers) + "|"
        ),
    ]
    for row in rows:
        rendered.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")

    current = parse_design_task_table(path)
    lines: list[str] = current["lines"]
    updated = lines[: current["header_index"]] + rendered + lines[current["row_end"] :]
    atomic_write_text(path, "\n".join(updated) + "\n")


def design_task_row(task_id: str, description: str) -> dict[str, str]:
    return {
        "任务ID": task_id,
        "类型": "设计",
        "描述": description,
        "状态": "待执行",
        "实际开始": "",
        "实际完成": "",
        "ONES任务ID": "",
        "ONES同步": "pending",
    }


def canonical_design_tasks_for_scale(scale: str) -> list[tuple[str, str]]:
    return CANONICAL_DESIGN_TASKS[:1] if str(scale).strip().lower() == "small" else CANONICAL_DESIGN_TASKS


def canonical_design_task_table_lines(scale: str) -> list[str]:
    lines = [
        "| " + " | ".join(DESIGN_TASK_COLUMNS) + " |",
        DESIGN_TASK_SEPARATOR,
    ]
    for task_id, description in canonical_design_tasks_for_scale(scale):
        row = design_task_row(task_id, description)
        lines.append("| " + " | ".join(str(row.get(header, "")) for header in DESIGN_TASK_COLUMNS) + " |")
    return lines


def infer_migrated_design_states(pipeline: list[dict[str, str]]) -> dict[str, str]:
    step_states = {step.get("id", ""): step.get("state", "").strip() for step in pipeline}
    completion_rules = [
        (
            "D-01",
            step_states.get("S1.1", "") in DONE_STATES
            and step_states.get("S2.0", "") in DONE_STATES | SKIPPED_STATES,
            ("S1.1", "S2.0"),
        ),
        (
            "D-02",
            all(step_states.get(step_id, "") in DONE_STATES for step_id in ("S2.1", "S2.2", "S2.3")),
            ("S2.1", "S2.2", "S2.3"),
        ),
        (
            "D-03",
            all(
                step_states.get(step_id, "") in DONE_STATES | WAIVED_STATES
                for step_id in ("S2.1-R", "S2.2-R")
            )
            and step_states.get("S2-H", "") in DONE_STATES,
            ("S2.1-R", "S2.2-R", "S2-H"),
        ),
    ]
    inferred: dict[str, str] = {}
    prior_group_done = False
    current_group_assigned = False
    inactive_states = {"", "pending", "todo", "待执行"}
    for task_id, is_done, step_ids in completion_rules:
        if is_done:
            inferred[task_id] = "已完成"
            prior_group_done = True
            continue
        has_evidence = any(step_states.get(step_id, "") not in inactive_states for step_id in step_ids)
        if not current_group_assigned and (prior_group_done or has_evidence):
            inferred[task_id] = "执行中"
            current_group_assigned = True
        else:
            inferred[task_id] = "待执行"
    return inferred


def design_task_has_progress(row: dict[str, str]) -> bool:
    state = row.get("状态", "").strip()
    checkpoint = row.get("ONES同步", "").strip()
    return bool(
        row.get("ONES任务ID", "").strip()
        or row.get("实际开始", "").strip()
        or row.get("实际完成", "").strip()
        or checkpoint not in {"", "pending"}
        or state not in {"", "待执行", "pending", "todo"}
    )


def visible_design_task_rows_for_report(
    scale: str,
    rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """small 报告不计、不渲染无进度的 D-02/D-03；large 全量保留。"""
    if str(scale).strip().lower() != "small":
        return list(rows)
    return [
        row
        for row in rows
        if row.get("任务ID", "").strip() not in {"D-02", "D-03"} or design_task_has_progress(row)
    ]


def ensure_design_tasks(
    path: Path,
    scale: str,
    pipeline: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    has_design_table = any(
        split_markdown_row(line)[:3] == ["任务ID", "类型", "描述"] for line in lines
    )
    if not has_design_table:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(["", "## 设计任务", ""])
        headers = DESIGN_TASK_COLUMNS
        lines.extend(
            [
                "| " + " | ".join(headers) + " |",
                DESIGN_TASK_SEPARATOR,
            ]
        )
        tasks = canonical_design_tasks_for_scale(scale)
        inferred_states = infer_migrated_design_states(pipeline or [])
        for task_id, description in tasks:
            row = design_task_row(task_id, description)
            row["状态"] = inferred_states[task_id]
            lines.append("| " + " | ".join(row[header] for header in headers) + " |")
        atomic_write_text(path, "\n".join(lines) + "\n")
        return parse_design_task_table(path)

    table = parse_design_task_table(path)
    existing = {row.get("任务ID", ""): row for row in table["rows"]}
    expected = canonical_design_tasks_for_scale(scale)
    expected_ids = {task_id for task_id, _ in expected}
    canonical_rows = [
        existing.get(task_id, design_task_row(task_id, description))
        for task_id, description in expected
    ]
    preserved_rows = [
        row
        for row in table["rows"]
        if row.get("任务ID", "") not in expected_ids
        and (
            row.get("任务ID", "") not in {"D-02", "D-03"}
            or design_task_has_progress(row)
        )
    ]
    table["rows"] = canonical_rows + preserved_rows
    save_design_task_table(path, table)
    return table


def find_task_row(table: dict[str, Any], task_id: str) -> dict[str, str]:
    for row in table["rows"]:
        if row.get("任务ID") == task_id:
            return row
    raise DevkitFlowError(f"Task not found: {task_id}")


def parse_pipeline_table(path: Path) -> dict[str, Any]:
    """Parse the ## 流水线 table keeping raw line indices for in-place rewrites."""
    lines = path.read_text(encoding="utf-8").splitlines()
    in_pipeline = False
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## "):
            in_pipeline = stripped == "## 流水线"
            continue
        if not in_pipeline:
            continue
        cells = split_markdown_row(line)
        if len(cells) < 6:
            continue
        if cells[0] == "步骤" or set(cells[0]) <= {"-"}:
            continue
        rows.append({"line_index": index, "cells": cells})
    return {"lines": lines, "rows": rows}


def save_pipeline_rows(path: Path, table: dict[str, Any], dirty_rows: list[dict[str, Any]]) -> None:
    """Rewrite only the given rows; every other line stays byte-identical."""
    lines = list(table["lines"])
    for row in dirty_rows:
        lines[row["line_index"]] = "| " + " | ".join(row["cells"]) + " |"
    atomic_write_text(path, "\n".join(lines) + "\n")


# --- ## 流水线历史 归档区（append-only，9 列；免迁移双格式：无归档区=cycle 1）---
# 列名自含于本模块（设计 §4.3.1 / 接口 §5），不依赖 constants.py；主表 6 列契约不动。

PIPELINE_HISTORY_HEADING = "## 流水线历史"
PIPELINE_HISTORY_COLUMNS = ["步骤", "名称", "Agent/Skill", "依赖", "状态", "备注", "cycle_index", "归档时间", "归档原因"]
PIPELINE_HISTORY_HEADER = "| " + " | ".join(PIPELINE_HISTORY_COLUMNS) + " |"
PIPELINE_HISTORY_SEPARATOR = (
    "|" + "|".join("-" * max(6, len(column) * 2) for column in PIPELINE_HISTORY_COLUMNS) + "|"
)


def parse_pipeline_history(path: Path) -> dict[str, Any]:
    """只读解析 ## 流水线历史 归档区；无归档区时 has_section=False、rows=[]。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    in_history = False
    has_section = False
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## "):
            in_history = stripped == PIPELINE_HISTORY_HEADING
            has_section = has_section or in_history
            continue
        if not in_history:
            continue
        cells = split_markdown_row(line)
        if len(cells) < 9:
            continue
        if cells[0] == "步骤" or set(cells[0]) <= {"-"}:
            continue
        rows.append({"line_index": index, "cells": cells[:9]})
    return {"lines": lines, "has_section": has_section, "rows": rows}


def append_pipeline_history_rows(
    path: Path,
    rows: list[list[str]],
    cycle_index: int,
    *,
    archived_at: str = "",
    reason: str = "",
) -> int:
    """归档 append helper：append-only、按 (步骤, cycle_index) 幂等、惰性创建归档区。

    rows 为主表 6 列 cells；每行追加 cycle_index/归档时间/归档原因三列落成 9 列归档行。
    返回本次实际追加的行数（全部幂等命中时为 0）。
    """
    if not rows:
        return 0
    history = parse_pipeline_history(path)
    lines = list(history["lines"])
    cycle_text = str(cycle_index)
    existing = {(row["cells"][0], row["cells"][6]) for row in history["rows"]}
    pending = [row for row in rows if row and (str(row[0]), cycle_text) not in existing]
    if not pending:
        return 0
    rendered = [
        "| "
        + " | ".join(
            [markdown_cell(str(cell)) for cell in row[:6]]
            + [cycle_text, markdown_cell(archived_at), markdown_cell(reason)]
        )
        + " |"
        for row in pending
    ]
    if not history["has_section"]:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(
            ["", PIPELINE_HISTORY_HEADING, "", PIPELINE_HISTORY_HEADER, PIPELINE_HISTORY_SEPARATOR]
        )
        lines.extend(rendered)
    elif history["rows"]:
        insert_at = history["rows"][-1]["line_index"] + 1
        lines[insert_at:insert_at] = rendered
    else:
        heading_index = next(
            index for index, line in enumerate(lines) if line.strip() == PIPELINE_HISTORY_HEADING
        )
        insert_at = heading_index + 1
        while insert_at < len(lines) and not lines[insert_at].strip():
            insert_at += 1
        if insert_at < len(lines) and split_markdown_row(lines[insert_at]):
            while insert_at < len(lines) and split_markdown_row(lines[insert_at]):
                insert_at += 1
            lines[insert_at:insert_at] = rendered
        else:
            scaffold = [PIPELINE_HISTORY_HEADER, PIPELINE_HISTORY_SEPARATOR]
            lines[insert_at:insert_at] = scaffold + rendered
    atomic_write_text(path, "\n".join(lines) + "\n")
    return len(pending)


def render_status_template(
    name: str,
    scale: str,
    branch: str,
    today: str,
    *,
    pipeline_rows: list[list[str]],
    snapshot: Any | None = None,
    trace_id: str = "",
) -> str:
    """从内嵌 STATUS-TEMPLATE 生成初始 STATUS.md：填头部、按规模裁剪流水线与设计任务表（不含模式行）。

    snapshot 由 flow 注入（本函数无 workspace 依赖，不读配置）。公共行不含
    CONFIG_GATED_STEPS 受控步骤；调用方用 canonical_effective_rows(scale, None, snapshot)
    算好（scale∪config 裁剪 + 依赖压缩）后经 pipeline_rows 传入。snapshot 仅作拼接依据契约参数，不在此再读盘。
    016 D-14：已删除 scale_common_pipeline_rows 回退（COMMON-only 退化实现）；pipeline_rows
    改为 keyword-only 必填参数——漏传即 TypeError（与 D-18 expected_completion_gate_steps 的 scale 必填同构，fail-closed）。
    trace_id 为 feature 级 `tx_<uuid>`（缺省为空不破坏解析）；生成与传参在 feature init。
    """
    del snapshot  # 由调用方记录到 JSON；此处不读 workspace。
    rows = [list(row) for row in pipeline_rows]
    header_values = {
        "状态": "开发中",
        "ONES需求ID": "",
        "ONES同步": "pending",
        "规模": scale,
        "模式": "pending",
        "分支": branch,
        "创建时间": today,
        "最后更新": today,
        "TraceID": trace_id,
    }
    out: list[str] = []
    skipping_until_heading = False
    skipping_table_rows = False
    for line in embedded_templates()["STATUS-TEMPLATE"].splitlines():
        stripped = line.strip()
        if skipping_until_heading:
            if stripped.startswith("## "):
                skipping_until_heading = False
            else:
                continue
        if skipping_table_rows:
            if not stripped or stripped.startswith("|"):
                continue
            skipping_table_rows = False
            if out and out[-1] != "":
                out.append("")
        if stripped == "## 流水线":
            out.extend([line, "", PIPELINE_TABLE_HEADER, PIPELINE_TABLE_SEPARATOR])
            out.extend("| " + " | ".join(row) + " |" for row in rows)
            skipping_until_heading = True
            continue
        if stripped == "## 设计任务":
            if out and out[-1] != "":
                out.append("")
            out.extend([line, ""])
            out.extend(canonical_design_task_table_lines(scale))
            skipping_table_rows = True
            continue
        if stripped.startswith("# "):
            out.append(f"# {name} 状态")
            continue
        header_match = re.match(r"^\*\*(.+?)\*\*:", stripped)
        if header_match and header_match.group(1) in header_values:
            out.append(f"**{header_match.group(1)}**: {header_values[header_match.group(1)]}".rstrip())
            continue
        out.append(line)
    return "\n".join(out) + "\n"


# --- feature 文件只读统计（question-clarification.md / clarification-state.json / STATUS 步骤）---
# 自 snapshot.py 下沉：telemetry / flow 亦消费，落在 1-10 基础层以免 flow→snapshot→flow 环
# （bundle 单文件形态不支持函数内延迟 import）。snapshot.py 顶层 re-export 保持既有 import 路径。


def report_markdown_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        headers = split_markdown_row(line)
        if not headers or index + 1 >= len(lines):
            continue
        separator = split_markdown_row(lines[index + 1])
        if not separator or not all(set(cell) <= {"-", ":"} for cell in separator):
            continue
        rows: list[dict[str, str]] = []
        for candidate in lines[index + 2 :]:
            cells = split_markdown_row(candidate)
            if not cells:
                break
            padded = cells + [""] * max(0, len(headers) - len(cells))
            rows.append(dict(zip(headers, padded[: len(headers)])))
        return rows
    return []


def report_clarification_counts(path: Path) -> tuple[int, int]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    checklist = re.findall(r"^- \[([ xX])\]\s+Q\d+", text, re.M)
    if checklist:
        return len(checklist), sum(1 for mark in checklist if mark == " ")
    rows = report_markdown_rows(path)
    resolved = {"answered", "resolved", "done", "closed", "已回答", "已解决", "已关闭", "完成"}
    unresolved = 0
    for row in rows:
        status = next(
            (value.strip().lower() for key, value in row.items() if key in {"状态", "status", "Status"}),
            "",
        )
        if status not in resolved:
            unresolved += 1
    return len(rows), unresolved


def report_clarification_stats(feature_dir: Path) -> dict[str, Any]:
    path = feature_dir / "question-clarification.md"
    items, unanswered = report_clarification_counts(path)
    answered = max(0, items - unanswered)
    batches: set[str] = set()
    chars = 0
    state_path = feature_dir / "clarification-state.json"
    json_questions: list[dict[str, Any]] = []
    if state_path.is_file():
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        batch_id = str(payload.get("batchId") or "").strip()
        if batch_id:
            batches.add(batch_id)
        raw_questions = payload.get("questions") or []
        if isinstance(raw_questions, list):
            json_questions = [item for item in raw_questions if isinstance(item, dict)]
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        batches.update(re.findall(r"\[batch:\s*(CB-[0-9]+)\]", text))
        if not json_questions:
            for line in text.splitlines():
                match = re.match(r"^- \[[ xX]\]\s+(.+)$", line)
                if match:
                    chars += len(match.group(1).strip())
    for question in json_questions:
        chars += len(str(question.get("question") or ""))
        chars += len(str(question.get("answer") or ""))
    return {
        "items": items,
        "answered": answered,
        "unanswered": unanswered,
        "batches": len(batches),
        "chars": chars,
    }


def report_human_intervention_count(steps: list[dict[str, str]], feature_dir: Path) -> int:
    gates = sum(
        1
        for step in steps
        if str(step.get("id", "")).endswith("-H")
        and step.get("state", "") not in {"", "pending"}
    )
    return gates + report_clarification_stats(feature_dir)["batches"]
