from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from devkit_flow.embedded import embedded_templates
from devkit_flow.constants import (
    CLOSED_STATES,
    COMPLETION_GATE_STEPS,
    DASHBOARD_DOC_GROUPS,
    DASHBOARD_FILE_PRIORITY,
    DASHBOARD_PREVIEW_EXTENSIONS,
    DASHBOARD_PREVIEW_FILE_LIMIT,
    DASHBOARD_PREVIEW_TOTAL_LIMIT,
    DASHBOARD_REFRESH_INTERVAL_MS,
    DASHBOARD_STEP_TIMINGS_FILE,
    DONE_STATES,
    PIPELINE_STEP_STAGES,
    STEP_TIMINGS_SCHEMA_VERSION,
)
from devkit_flow.e2e import E2E_NOT_CONFIGURED_REASON
from devkit_flow.errors import DevkitFlowError
from devkit_flow.flow import (
    STEP_TIMING_ACTIVE_STATES,
    dependency_alternatives,
    flow_now_iso,
    flow_select_feature,
)
from devkit_flow.html_ui import bundled_mermaid_path, open_local_html_report, render_markdown_subset
from devkit_flow.models import E2EGateVerdict, FeatureSnapshot
from devkit_flow.parse_util import find_features, summarize_features
from devkit_flow.pipeline_gates import PipelineConfigSnapshot, expected_completion_gate_steps
from devkit_flow.snapshot import (
    annotate_config_closed_steps,
    collect_feature_snapshot,
    disabled_pipeline_items,
    snapshot_config_gates,
    snapshot_scale_mode,
    step_display_label,
)
from devkit_flow.switches import dashboard_switch_summary
from devkit_flow.textio import atomic_write_text, compact_error, emit, ones_workitem_url


# --- flow dashboard（dashboard.html 过程态渲染，009 设计文档 §4.2–§4.7） ----------
# 注入点与 spec/template/dashboard-template.html 中的 {{TOKEN}} 与
# <!-- AGENT_INJECT: ... --> 标记一一对应（18 token + 16 inject，接口文档 §4 修订版），
# 模板改动需同步此处与三处守卫测试。
# 本模块只消费 snapshot.py；render 层模块互不依赖（dashboard 不 import report）。
DASHBOARD_STAGE_ORDER = ("需求", "开发", "测试", "审查")
DASHBOARD_STAGE_KEYS = {
    "需求": "requirement",
    "开发": "development",
    "测试": "testing",
    "审查": "review",
}
# 与 dashboard-template.html 的 .pill.st-* CSS 类契约一致（任务表/e2e 门沿用）。
DASHBOARD_STATE_PILLS = {
    "done",
    "in-progress",
    "waiting-confirmation",
    "blocked",
    "pending",
    "skipped",
    "waived",
}
# 步骤表状态圆点呈现类（模板 .st.* CSS 契约，设计 §4.2 决策 3）：键集与
# DASHBOARD_STATE_PILLS 一致（Q6 映射沿用键值），值为模板短类名。
DASHBOARD_STATE_CLASSES = {
    "done": "done",
    "in-progress": "run",
    "waiting-confirmation": "wait",
    "blocked": "blocked",
    "pending": "pend",
    "skipped": "skip",
    "waived": "waived",
}
# 呈现层 kind 枚举（009 设计 §4.3 / 接口 §2.7）：零变更、不新增 e2e 第四态。
DASHBOARD_OVERALL_STATUS_KINDS = frozenset(
    {"blocked", "waiting", "running", "gate-missing", "attention", "done"}
)


def dashboard_group_steps_by_stage(
    steps: list[dict[str, str]], stage_map: dict[str, str]
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    """按 PIPELINE_STEP_STAGES 分四阶段（顺序固定 需求→开发→测试→审查，§4.4.2）；
    未归组节点（存量/自定义步骤行）单列返回，绝不静默丢弃。"""
    grouped: dict[str, list[dict[str, str]]] = {stage: [] for stage in DASHBOARD_STAGE_ORDER}
    ungrouped: list[dict[str, str]] = []
    for step in steps:
        stage = stage_map.get(step["id"], "")
        if stage in grouped:
            grouped[stage].append(step)
        else:
            ungrouped.append(step)
    return grouped, ungrouped


def dashboard_stage_progress(steps: list[dict[str, str]]) -> str:
    """阶段 n/m 计数（轨道 stepper 段 + 阶段 section header 共用）。

    S5.7 综合审查 W-2 口径统一：分子=CLOSED_STATES（done/skipped/waived），
    与 PROGRESS_SUMMARY / PROGRESS_PERCENT 同族——同一 railbox 内总进度与
    四段合计必须可对账（设计 §4.3：轨道所有分子=CLOSED_STATES）。"""
    total = len(steps)
    closed = sum(1 for step in steps if step["state"] in CLOSED_STATES)
    return f"{closed}/{total}"


def dashboard_step_timings(feature_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """读 R7 旁车 .step-timings.json（接口文档 §7，dashboard 私有只读）。

    文件缺失（存量 feature 正常态）/ JSON 损坏 / schema 不识别 / 结构不符一律返回
    空映射，渲染不报错（向后兼容）；读取端绝不修改旁车文件——悬空开周期的清理由
    写入侧 record_step_timing 的先行自愈规则完成（设计 §4.7.2）。"""
    path = feature_dir / DASHBOARD_STEP_TIMINGS_FILE
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict) or loaded.get("schema") != STEP_TIMINGS_SCHEMA_VERSION:
        return {}
    steps = loaded.get("steps")
    if not isinstance(steps, dict):
        return {}
    timings: dict[str, list[dict[str, Any]]] = {}
    for step_id, entry in steps.items():
        cycles = entry.get("cycles") if isinstance(entry, dict) else None
        if isinstance(cycles, list):
            timings[str(step_id)] = [cycle for cycle in cycles if isinstance(cycle, dict)]
    return timings


def dashboard_parse_iso(value: Any) -> datetime | None:
    """旁车时间值解析（防御：非字符串/非法格式返回 None）；naive 值按本地时区补齐，
    保证与 flow_now_iso() 的 aware 值可安全相减。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.astimezone()
    except (ValueError, OSError, OverflowError):  # 非法格式/平台极端日期：按缺失处理
        return None


def dashboard_format_duration(seconds: float) -> str:
    """耗时短格式（设计 §4.7.2）：<1m / Xm / Xh Ym。"""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def dashboard_step_timing_cells(
    step_id: str,
    state: str,
    timings: dict[str, list[dict[str, Any]]],
    generated_at: str,
) -> tuple[str, str]:
    """步骤「开始 / 耗时」两列展示派生（设计 §4.7.2 周期模型）。

    开始 = 首周期 started_at（MM-dd HH:mm 短格式）；耗时 = 全部闭合周期时长累计
    （多批次返工 = 各批次活跃时长之和）；步骤当前 in-progress 且有开周期 →
    「进行 Xm」（GENERATED_AT − 当前开周期起点，静态快照随 5s reload 自然推进）；
    其余活跃态开周期（STEP_TIMING_ACTIVE_STATES 内的 waiting/blocked，集合从
    flow.py 导入不复写）与悬空开周期（state 非活跃，rollback 直写所致）均只显示
    已闭合累计、不计进行中时长；无记录/无闭合周期 = "-"。"""
    cycles = timings.get(step_id) or []
    first_started = dashboard_parse_iso(cycles[0].get("started_at")) if cycles else None
    started_text = first_started.strftime("%m-%d %H:%M") if first_started else "-"
    closed_seconds = 0.0
    has_closed = False
    open_started: datetime | None = None
    for cycle in cycles:
        started = dashboard_parse_iso(cycle.get("started_at"))
        if cycle.get("ended_at") is None:
            open_started = started
            continue
        ended = dashboard_parse_iso(cycle.get("ended_at"))
        if started is not None and ended is not None:
            closed_seconds += max((ended - started).total_seconds(), 0.0)
            has_closed = True
    if open_started is not None and state in STEP_TIMING_ACTIVE_STATES and state == "in-progress":
        generated = dashboard_parse_iso(generated_at)
        if generated is not None:
            elapsed = max((generated - open_started).total_seconds(), 0.0)
            return started_text, f"进行 {dashboard_format_duration(elapsed)}"
    if has_closed:
        return started_text, dashboard_format_duration(closed_seconds)
    return started_text, "-"


def dashboard_step_row_html(
    step: dict[str, str],
    *,
    ungrouped: bool = False,
    timings: dict[str, list[dict[str, Any]]] | None = None,
    generated_at: str = "",
) -> str:
    """步骤表行（设计 §4.3 区块⑤ 六列：步骤/名称/状态/开始/耗时/备注；
    执行角色/依赖列随第二版信息结构收敛移除——依赖关系由依赖图承载）。"""
    note = step.get("note", "") or ""
    if ungrouped:
        marker = "未归组（PIPELINE_STEP_STAGES 未覆盖）"
        note = f"{marker}；{note}" if note else marker
    state = step.get("state", "") or "-"
    css = DASHBOARD_STATE_CLASSES.get(state, "pend")
    step_id = step.get("id", "-")
    started, duration = dashboard_step_timing_cells(
        step_id, state, timings or {}, generated_at
    )
    return (
        "<tr>"
        f'<td class="id">{html.escape(step_id)}</td>'
        f"<td>{html.escape(step_display_label(step.get('name', '-') or '-', step_id))}</td>"
        f'<td><span class="st {css}"><i></i>{html.escape(state)}</span></td>'
        f'<td class="id">{html.escape(started)}</td>'
        f'<td class="id">{html.escape(duration)}</td>'
        f'<td class="note">{dashboard_clamp_cell(note or "-", f"{step_id}:note")}</td>'
        "</tr>"
    )


def dashboard_step_rows_html(
    steps: list[dict[str, str]],
    ungrouped: list[dict[str, str]] | None = None,
    *,
    timings: dict[str, list[dict[str, Any]]] | None = None,
    generated_at: str = "",
) -> str:
    rows = [
        dashboard_step_row_html(step, timings=timings, generated_at=generated_at)
        for step in steps
    ]
    rows.extend(
        dashboard_step_row_html(step, ungrouped=True, timings=timings, generated_at=generated_at)
        for step in (ungrouped or [])
    )
    return "\n".join(rows) or '<tr><td colspan="6">暂无节点</td></tr>'


def dashboard_ones_link_html(space_id: str, kind: str, workitem_id: str) -> str:
    """ONES ID 渲染为跳转链接（口径同 report.ones_link_html；render 层模块互不依赖，故本层私有）。"""
    if not workitem_id:
        return "-"
    text = html.escape(workitem_id)
    if not space_id:
        return text
    url = html.escape(ones_workitem_url(space_id, kind, workitem_id), quote=True)
    return f'<a href="{url}" target="_blank">{text}</a>'


def dashboard_task_row_html(row: dict[str, str], space_id: str, *, design: bool) -> str:
    """任务表 6 列：任务ID/描述/状态/门禁进度/ONES 任务/产出备注；
    设计任务表无「门禁进度」列，以「-」占位，备注取自「备注」列。"""
    state = row.get("状态", "-").strip() or "-"
    css = {
        "已完成": "done",
        "执行中": "in-progress",
        "待执行": "pending",
        "失败": "blocked",
    }.get(state, "pending")
    if design:
        gate = "-"
        note = row.get("备注", "").strip() or row.get("ONES同步", "").strip() or "-"
    else:
        gate = row.get("门禁进度", "").strip() or "-"
        note = row.get("产出/备注", "").strip() or "-"
    task_id = row.get("任务ID", "-")
    return (
        "<tr>"
        f'<td class="id">{html.escape(task_id)}</td>'
        f'<td>{dashboard_clamp_cell(row.get("描述", "-"), f"{task_id}:desc")}</td>'
        f'<td><span class="pill st-{css}">{html.escape(state)}</span></td>'
        f"<td>{html.escape(gate)}</td>"
        f'<td>{dashboard_ones_link_html(space_id, "task", row.get("ONES任务ID", "").strip())}</td>'
        f'<td class="note">{dashboard_clamp_cell(note, f"{task_id}:note")}</td>'
        "</tr>"
    )


def dashboard_task_block_html(rows: list[dict[str, str]], space_id: str, *, design: bool) -> str:
    """阶段任务表整块注入（接口文档 §4.2 #3/#5/#7/#10）：注入内容自带区块外壳，
    空任务列表返回空串实现整段隐藏——测试/审查阶段现状恒空表从此不再渲染。"""
    if not rows:
        return ""
    rendered = "\n".join(dashboard_task_row_html(row, space_id, design=design) for row in rows)
    return (
        "<h3>阶段任务</h3>\n"
        '<div class="tbl-wrap">\n'
        "<table>\n"
        "<thead><tr><th>任务ID</th><th>描述</th><th>状态</th><th>门禁进度</th>"
        "<th>ONES 任务</th><th>产出 / 备注</th></tr></thead>\n"
        f"<tbody>\n{rendered}\n</tbody>\n"
        "</table>\n"
        "</div>"
    )


def dashboard_list_items(items: list[str]) -> str:
    return "\n".join(f"<li>{html.escape(item)}</li>" for item in items) or "<li>暂无</li>"


# e2e 门 verdict → 既有 pill CSS 类（与 .pill.st-* 契约一致，不新增样式）。
DASHBOARD_E2E_VERDICT_PILLS = {"pass": "done", "fail": "blocked", "blocked": "pending"}


def dashboard_e2e_gate_html(verdict: E2EGateVerdict | None) -> str:
    """测试阶段分区的 e2e 准出门摘要：verdict pill + result/runId/
    finishedAt/reportUrl 链接 + blocked/fail 原因与失败明细。

    009 呈现降噪（接口文档 §4.2 #8）：not-configured 渲染中性灰「未配置」文案而非
    红色 blocked pill——数据层 verdict 三值不变，判定消费 e2e.py 的
    E2E_NOT_CONFIGURED_REASON 常量（review-arch-batch1 W1，不字符串硬编码）。"""
    if verdict is None:
        return '<p class="desc">未采集 e2e 门状态。</p>'
    if verdict.reason == E2E_NOT_CONFIGURED_REASON:
        return (
            '<p class="desc">e2e 门未配置：本 feature 不设 e2e 准出要求；'
            "如需启用，执行 <code>devkit-flow e2e configure</code>。</p>"
        )
    css = DASHBOARD_E2E_VERDICT_PILLS.get(verdict.verdict, "pending")
    parts = [
        f'<p><span class="pill st-{css}">{html.escape(verdict.verdict)}</span>'
    ]
    report = verdict.report
    if report is not None:
        parts.append(
            f" result={html.escape(report.result)} · runId={html.escape(report.run_id)}"
            f" · finishedAt={html.escape(report.finished_at)}"
        )
    report_url = verdict.report_url or (report.report_url if report is not None else "")
    if report_url:
        parts.append(
            f' · <a href="{html.escape(report_url, quote=True)}" target="_blank">报告</a>'
        )
    parts.append("</p>")
    if verdict.reason:
        parts.append(f'<p class="desc">{html.escape(verdict.reason)}</p>')
    if verdict.failures:
        parts.append(
            "<ul>"
            + "".join(
                "<li>"
                + html.escape(
                    f"{item.get('caseId', '?')} {item.get('title', '')}: {item.get('message', '')}".strip()
                )
                + "</li>"
                for item in verdict.failures
                if isinstance(item, dict)
            )
            + "</ul>"
        )
    return "\n".join(parts)


def dashboard_overall_status(snapshot: FeatureSnapshot) -> tuple[str, str]:
    """dashboard 侧状态仲裁（009 设计 §4.3 真值表 8 分支，自上而下首条命中即返回）。

    Q5 裁定：不改 next_action_text / evaluate_e2e_gate / CLOSED_STATES 共享层口径，
    仅在呈现层把三条独立状态口径收敛为单一事实源；返回 (text, kind)，
    kind ∈ {blocked, waiting, running, gate-missing, attention, done} 仅为呈现层枚举，
    不写回任何数据层、不新增 e2e 第四态（008 定稿约束）。
    """
    breakpoint_info = snapshot.breakpoint
    if breakpoint_info is not None:
        state = breakpoint_info.get("state", "")
        label = step_display_label(
            breakpoint_info.get("name", "") or "-",
            breakpoint_info.get("id", "") or "-",
        )
        if state == "blocked":  # 分支 1
            return f"受阻：{label}", "blocked"
        if state == "waiting-confirmation":  # 分支 2
            return f"等待人工确认：{breakpoint_info['id']}", "waiting"
        # 分支 3：断点存在的其余状态（in-progress / pending 等）
        return f"进行中：{label}", "running"
    if not snapshot.pipeline:
        # 分支 4（表注③）：复刻 next_action_text 的 table["rows"] 非空守卫口径（flow.py），
        # 与「流水线已全部关闭，可执行 flow complete 收尾」同屏语义一致，消除同页矛盾。
        return "已完成（流水线为空，待 flow complete 收尾）", "done"
    # 分支 5：按配置快照过滤后的 expected_completion_gate_steps 复算缺失 Gate 行
    #（Q1=A 下行缺失+配置关闭不再 gate-missing；无快照时回退 COMPLETION_GATE_STEPS）。
    step_ids = {step["id"] for step in snapshot.pipeline}
    config = snapshot_config_gates(snapshot)
    _scale = snapshot_scale_mode(snapshot)[0]
    expected_gates = (
        expected_completion_gate_steps(config, _scale)
        if config is not None
        else COMPLETION_GATE_STEPS
    )
    missing_gates = [step_id for step_id in expected_gates if step_id not in step_ids]
    if missing_gates:
        return f"流程未闭合：缺 {', '.join(missing_gates)} Gate", "gate-missing"
    # 分支 7 改读配置快照（设计 §4.2.5）：e2e.enabled=false 即未配置。
    # 无快照时回退 e2e_gate.reason（表注①/②，常量单点消费 e2e.py）。
    # 采集降级 verdict=blocked + reason=config-error:* 属「已配置」，归分支 6。
    gate = snapshot.e2e_gate
    configured = _dashboard_e2e_configured(snapshot, config)
    if configured and gate is not None and gate.verdict in ("fail", "blocked"):  # 分支 6
        return f"待收尾：e2e 门 {gate.verdict}", "attention"
    if not configured:
        return "已完成（e2e 门未配置）", "done"
    return "已完成", "done"  # 分支 8：Gate 行齐全且 e2e 已配置 pass


def _dashboard_e2e_configured(
    snapshot: FeatureSnapshot, config: PipelineConfigSnapshot | None
) -> bool:
    if config is not None:
        return config.gate("e2e").enabled
    gate = snapshot.e2e_gate
    return gate is not None and gate.reason != E2E_NOT_CONFIGURED_REASON


def dashboard_disabled_gates_html(items: list[dict[str, str]]) -> str:
    """「未启用环节」整块注入：基于配置快照，灰显 + 来源标注；空则空串。

    挂在 status waiting block 注入点之后（模板无新 AGENT_INJECT），不占用流水线行、
    不计入阶段 n/m。
    """
    if not items:
        return ""
    rows = "\n".join(
        f'<li>{html.escape(item["label"])} — {html.escape(item["caption"])}</li>'
        for item in items
    )
    return (
        '<section class="waiting" id="disabled-gates">\n'
        "<h2>未启用环节</h2>\n"
        f'<ul style="color:var(--ink3)">\n{rows}\n</ul>\n'
        "</section>"
    )


def dashboard_waiting_block_html(waiting_on: list[str]) -> str:
    """「在等谁」整块注入（接口文档 §4.2 #1）：注入内容自带区块外壳，
    等待项为空返回空串实现整段隐藏（空占位噪音清除，设计 §4.3 区块②）。"""
    if not waiting_on:
        return ""
    items = "\n".join(f"<li>{html.escape(item)}</li>" for item in waiting_on)
    return (
        '<section class="waiting">\n'
        "<h2>在等谁</h2>\n"
        f"<ul>\n{items}\n</ul>\n"
        "</section>"
    )


def dashboard_blockers_panel_html(blockers: dict[str, Any]) -> str:
    """卡点面板四合一整块注入（接口文档 §4.2 #11：原 blocker blocked steps /
    clarifications / ones retries / funding risks 四注入点合并）。

    只渲染非空分类；全空返回空串整段隐藏（状态条已含无卡点语义，设计 §4.3 区块④）。
    摘要文本由注入内容自带（原 BLOCKER_SUMMARY token 并入本块，接口文档 §4.1）。"""
    categories = (
        (
            "Blocked 步骤",
            [
                f"{step_display_label(step['name'], step['id'])}：{step['note'] or '（无备注）'}"
                for step in blockers["blocked_steps"]
            ],
        ),
        ("未决澄清", list(blockers["clarifications"])),
        ("ONES 待重试", list(blockers["ones_retries"])),
        (
            "资金风险",
            [
                f"{step_display_label(step['name'], step['id'])} 状态 {step['state']}：{step['note'] or '（无备注）'}"
                for step in blockers["funding_risks"]
            ],
        ),
    )
    counts = [len(items) for _, items in categories]
    if not any(counts):
        return ""
    summary = (
        f"blocked 步骤 {counts[0]} 个 · 未决澄清 {counts[1]} 项 · "
        f"ONES 待重试 {counts[2]} 项 · 资金风险待跟进 {counts[3]} 项"
    )
    boxes = "\n".join(
        '<div class="brief-box">\n'
        f"<h3>{html.escape(title)}</h3>\n"
        f"<ul>{dashboard_list_items(items)}</ul>\n"
        "</div>"
        for title, items in categories
        if items
    )
    return (
        '<section class="blockers">\n'
        "<h2>当前卡点</h2>\n"
        f'<p class="desc">{html.escape(summary)}</p>\n'
        f'<div class="brief-grid">\n{boxes}\n</div>\n'
        "</section>"
    )


# --- 过程文件枚举（009 设计 §4.6 / 类图 §2，R5）---------------------------------
# render 层私有（不进 snapshot.py，避免波及共享采集层与复盘链路，§3.1 非目标）；
# 双限额与扩展白名单从 constants.py 单点消费（接口文档 §6）；由
# write_feature_dashboard 枚举后经 render_dashboard(process_files=...) 接线到
# `sidebar doc nav` 与 `doc templates` 两个注入块（T11 第二版），
# 本区函数保持可独立单测的纯函数形态。


@dataclass(frozen=True)
class ProcessFileEntry:
    """feature 目录顶层过程文件条目（ER 图 PROCESS_FILE 六字段）。

    preview_text 三态：None = 不内联（扩展不在 DASHBOARD_PREVIEW_EXTENSIONS 白名单、
    读取失败或总量限额已到后停止内联）；字符串（含空文件的空串）= 内联文本；
    truncated 标记单文件超 DASHBOARD_PREVIEW_FILE_LIMIT 截断。
    """

    name: str
    rel_href: str
    size_bytes: int
    mtime: str
    preview_text: str | None = None
    truncated: bool = False


def dashboard_file_priority_key(name: str) -> tuple[int, int, str]:
    """清单排序键（§4.6）：DASHBOARD_FILE_PRIORITY 命中项按元组序置顶——
    以 "[" 开头的三件套条目（[设计文档]/[接口文档]/[类图]）按文件名前缀匹配，
    其余条目精确匹配；未命中项整体排后并按文件名字典序（新类型产物零维护自动入列）。"""
    for index, pattern in enumerate(DASHBOARD_FILE_PRIORITY):
        matched = name.startswith(pattern) if pattern.startswith("[") else name == pattern
        if matched:
            return (0, index, name)
    return (1, 0, name)


def dashboard_process_files(workspace: Path, feature_dir: str) -> list[ProcessFileEntry]:
    """枚举 feature 目录顶层过程文件（§4.6，P4 排除规则而非白名单）。

    只遍历顶层不递归；排除名字以 "." 开头的隐藏项、目录项、全部 *.html（渲染产物：
    dashboard.html 自身 / workflow-visualization.html / design-preview.html）。
    rel_href 经 urllib.parse.quote 编码（含 "[ ]" 等字符的文件名可点击）；预览读取套
    双限额：单文件超 64KB 截断，页面总量达 512KB 后剩余文件停止内联（只留清单行+链接）。
    """
    directory = workspace / feature_dir
    if not directory.is_dir():
        return []
    names = sorted(
        (
            item.name
            for item in directory.iterdir()
            if not item.name.startswith(".")
            and not item.is_dir()
            and item.suffix.lower() != ".html"
        ),
        key=dashboard_file_priority_key,
    )
    entries: list[ProcessFileEntry] = []
    budget = DASHBOARD_PREVIEW_TOTAL_LIMIT
    inline_stopped = False
    for name in names:
        path = directory / name
        try:
            stat = path.stat()
        except OSError:  # 枚举与 stat 之间文件消失/不可读：跳过该项，不阻断渲染
            continue
        preview: str | None = None
        truncated = False
        previewable = Path(name).suffix.lower() in DASHBOARD_PREVIEW_EXTENSIONS
        if previewable and not inline_stopped:
            if min(stat.st_size, DASHBOARD_PREVIEW_FILE_LIMIT) > budget:
                # 总量限额到顶（§4.6）：本文件及其后全部文件只渲染清单行+链接
                inline_stopped = True
            else:
                try:
                    with path.open("rb") as handle:
                        data = handle.read(DASHBOARD_PREVIEW_FILE_LIMIT + 1)
                except OSError:
                    data = None
                if data is not None:
                    truncated = len(data) > DASHBOARD_PREVIEW_FILE_LIMIT
                    if truncated:
                        data = data[:DASHBOARD_PREVIEW_FILE_LIMIT]
                    preview = data.decode("utf-8", errors="replace")
                    budget -= len(data)
        entries.append(
            ProcessFileEntry(
                name=name,
                rel_href=quote(name),
                size_bytes=stat.st_size,
                mtime=datetime.fromtimestamp(stat.st_mtime)
                .astimezone()
                .isoformat(timespec="seconds"),
                preview_text=preview,
                truncated=truncated,
            )
        )
    return entries


def dashboard_format_size(size_bytes: int) -> str:
    """清单行文件大小的人类可读格式（呈现辅助，非契约常量）。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


# --- R5 第二版呈现：侧边栏分组导航 + 页内阅读视图文档注入（009 设计 §4.6.2/§4.6.3） ---
# 原 details+pre 折叠内联预览区（process files 注入与 dashboard_process_files_html）
# 弃用，能力由 sidebar doc nav + doc templates + 模板阅读视图承接（接口文档 §4.2）。


# 侧边栏文档类型徽标（按分组派生，设计 §4.6.2）：审查报告 RV / 决策与变更 DC / 其余 MD。
DASHBOARD_DOC_GROUP_BADGES = {"审查报告": "RV", "决策与变更": "DC"}


def dashboard_doc_group_name(name: str) -> str:
    """文件名 → 分组名（DASHBOARD_DOC_GROUPS 前缀匹配，首个命中组生效）；
    末组「其他」匹配元组为空，作为未命中项兜底（接口文档 §6）。"""
    for group_name, prefixes in DASHBOARD_DOC_GROUPS:
        if any(name.startswith(prefix) for prefix in prefixes):
            return group_name
    return DASHBOARD_DOC_GROUPS[-1][0]


def dashboard_sidebar_doc_nav_html(entries: list[ProcessFileEntry]) -> str:
    """侧边栏文档分组导航注入（接口文档 §4.2 #1）。

    首项恒为「总览」（data-doc 空串，与 doc 键「总览写空串」单一口径对应）；其后按
    DASHBOARD_DOC_GROUPS 顺序分组，组内保持 entries 传入顺序（DASHBOARD_FILE_PRIORITY
    优先序），组空不渲染组标题；无文档时仅渲染「总览」项。文件名两个注入面
    （data-doc 属性与展示文本）全量 html.escape。"""
    parts = [
        '<a class="active" data-doc=""><span class="fico">◫</span><span class="fn">总览</span></a>'
    ]
    grouped: dict[str, list[ProcessFileEntry]] = {name: [] for name, _ in DASHBOARD_DOC_GROUPS}
    for entry in entries:
        grouped[dashboard_doc_group_name(entry.name)].append(entry)
    for group_name, _ in DASHBOARD_DOC_GROUPS:
        members = grouped[group_name]
        if not members:
            continue
        badge = DASHBOARD_DOC_GROUP_BADGES.get(group_name, "MD")
        parts.append(f'<div class="nav-title">{html.escape(group_name)}</div>')
        for entry in members:
            parts.append(
                f'<a data-doc="{html.escape(entry.name, quote=True)}">'
                f'<span class="fico">{badge}</span>'
                f'<span class="fn">{html.escape(entry.name)}</span></a>'
            )
    return "\n".join(parts)


def dashboard_doc_templates_html(entries: list[ProcessFileEntry]) -> str:
    """全部内嵌文档的 template 集合注入（接口文档 §4.2 #16）。

    <template id="t-doc-{index}" data-doc="{文件名}">：id 仅保证 DOM 唯一性，打开与
    恢复一律按 data-doc 文件名寻址（与侧边栏导航项同键，reload 间文档增删不因顺序
    索引漂移错位）。限额内正文经 render_markdown_subset(wide_table_cards=True) 渲染
    （文本节点全量 html.escape 由渲染器保证，mermaid 围栏输出 <pre class='mermaid'>）；
    超限/白名单外为占位 + 原文件链接；无文档注入空串。"""
    parts: list[str] = []
    for index, entry in enumerate(entries):
        href = html.escape(entry.rel_href, quote=True)
        open_link = f'<a href="{href}">打开原文件</a>'
        if entry.preview_text is None:
            body = (
                '<p class="desc">文档未内嵌'
                f"（{html.escape(dashboard_format_size(entry.size_bytes))}，体积限额/类型不支持）。</p>"
                f"<p>{open_link}</p>"
            )
        else:
            body = render_markdown_subset(entry.preview_text, wide_table_cards=True)
            if entry.truncated:
                body += f'<p class="desc">已截断（单文档预览上限 64KB），{open_link}查看全文。</p>'
        parts.append(
            f'<template id="t-doc-{index}" data-doc="{html.escape(entry.name, quote=True)}">'
            f'<div class="md">{body}</div></template>'
        )
    return "\n".join(parts)


def dashboard_progress_percent(closed: int, total: int) -> int:
    """总进度百分比（接口文档 §4.1 #8）：closed/total 向下取整（floor），
    total=0 为 0；floor 语义保证仅 closed == total 时为 100（未全闭合永不显示
    100，与轨道「n/m 步已关闭」并列不产生状态矛盾）。"""
    if total <= 0:
        return 0
    return closed * 100 // total


def dashboard_pipeline_rail_stages_html(displayed: dict[str, list[dict[str, str]]]) -> str:
    """流水线轨道四阶段 stepper 段注入（接口文档 §4.2 #2，恒四段）。

    段状态 class：done = 全部关闭（CLOSED_STATES：done/skipped/waived，total>0）/
    run = 存在活跃步骤（集合复用 flow.py STEP_TIMING_ACTIVE_STATES 单点定义）/
    pend = 其余；n/m 计数与阶段 section header 同口径（dashboard_stage_progress，
    CLOSED_STATES，与总进度同族——S5.7 W-2）；data-stage 供模板 JS 绑定点击
    跳转对应阶段 section（R6）。"""
    parts: list[str] = []
    for stage in DASHBOARD_STAGE_ORDER:
        steps = displayed.get(stage, [])
        if steps and all(step.get("state", "") in CLOSED_STATES for step in steps):
            css = "done"
        elif any(step.get("state", "") in STEP_TIMING_ACTIVE_STATES for step in steps):
            css = "run"
        else:
            css = "pend"
        parts.append(
            f'<div class="stage-seg {css}" data-stage="stg-{DASHBOARD_STAGE_KEYS[stage]}">'
            '<div class="node"></div>'
            f'<div class="s-name">{html.escape(stage)}</div>'
            f'<div class="s-cnt">{dashboard_stage_progress(steps)}</div>'
            "</div>"
        )
    return "\n".join(parts)


def dashboard_breakpoint_card_html(
    breakpoint_info: dict[str, str] | None, next_action: str
) -> str:
    """断点卡整块注入（接口文档 §4.2 #3）：无断点（流水线全关闭）注入空串整卡隐藏
    ——完成语义由轨道仲裁状态承担；「下一步」建议数据仍取 snapshot.next_action
    （共享层不改，原 STATUS_NEXT_ACTION token 并入本块）。"""
    if breakpoint_info is None:
        return ""
    label = step_display_label(
        breakpoint_info.get("name", "-") or "-",
        breakpoint_info.get("id", "-") or "-",
    )
    body = (
        f"当前断点 <b>{html.escape(label)}</b>"
        f"· 状态 {html.escape(breakpoint_info.get('state', '-') or '-')}"
    )
    next_html = (
        f'\n<div class="next">下一步：{html.escape(next_action)}</div>' if next_action else ""
    )
    return (
        '<div class="bp">\n<div class="bp-dot"></div>\n'
        f"<div>{body}</div>{next_html}\n</div>"
    )


def dashboard_clamp_html(text: str, limit: int = 120) -> str:
    """长文折叠（§4.3）：超过 limit 字符渲染 details.clamp——summary 为前 80 字符 + …，
    展开体为完整全文；未超限原样返回。两个分支输出均 html.escape（设计 §6 安全行；
    本函数返回 HTML 片段，调用方不得二次转义）。"""
    if len(text) <= limit:
        return html.escape(text)
    return (
        '<details class="clamp">'
        f"<summary>{html.escape(text[:80])}…</summary>"
        f"{html.escape(text)}"
        "</details>"
    )


def dashboard_clamp_cell(text: str, key: str, *, limit: int = 120) -> str:
    """clamp 注入接线（DECISIONS D-4）：dashboard_clamp_html 签名按 plan 钉死无 ID 通道，
    data-key 在接线处以字符串后处理补挂——折叠分支的 details 带语义派生
    data-key="clamp:{ID}:{字段}"（接口文档 §5），展开态跨 reload 保持；
    未折叠分支与 dashboard_clamp_html 行为一致（已 html.escape）。"""
    rendered = dashboard_clamp_html(text, limit)
    if rendered.startswith('<details class="clamp">'):
        rendered = (
            f'<details class="clamp" data-key="clamp:{html.escape(key, quote=True)}">'
            + rendered[len('<details class="clamp">'):]
        )
    return rendered


# 依赖图节点状态 classDef（§4.2 修订：主题中性取值——节点透明填充 + 亮/暗色板中间调
# 描边，文字色不指定继承 mermaid default/dark 主题文字色，双主题均可读；
# pending 不挂 class 走 mermaid 主题默认样式）。
DASHBOARD_GRAPH_CLASS_DEFS = (
    "classDef stDone fill:transparent,stroke:#26A87A",
    "classDef stActive fill:transparent,stroke:#3E96EA",
    "classDef stBlocked fill:transparent,stroke:#E05A5A",
    "classDef stWaiting fill:transparent,stroke:#D89A2B",
)


def dashboard_graph_state_class(state: str) -> str:
    """节点状态 → classDef 名；空串表示不挂 class（pending 及未知状态）。"""
    if state in CLOSED_STATES:
        return "stDone"
    if state == "in-progress":
        return "stActive"
    if state == "blocked":
        return "stBlocked"
    if state == "waiting-confirmation":
        return "stWaiting"
    return ""


def dashboard_dependency_graph(steps: list[dict[str, str]], stage_map: dict[str, str]) -> str:
    """由流水线行依赖列生成 mermaid graph TD + 四大阶段 subgraph 分组 + 状态 classDef
    （009 设计 §4.2 依赖图修复：TD + 分组后 23 节点在 1280px 内可读）。

    未归组节点入「需求」子图（与表格区兜底口径一致）；跨子图连线由 mermaid 自动布线。"""
    if not steps:
        return 'graph TD\n  empty["暂无流水线数据"]'
    node_ids: dict[str, str] = {}
    for step in steps:
        node_ids[step["id"]] = "n_" + re.sub(r"[^0-9A-Za-z_]", "_", step["id"])
    grouped, ungrouped = dashboard_group_steps_by_stage(steps, stage_map)
    lines = ["graph TD"]
    for stage in DASHBOARD_STAGE_ORDER:
        rows = [*grouped[stage], *(ungrouped if stage == "需求" else [])]
        if not rows:
            continue
        lines.append(f'  subgraph stage_{DASHBOARD_STAGE_KEYS[stage]}["{stage}"]')
        for step in rows:
            label = step_display_label(step.get("name", "-") or "-", step["id"]).replace(
                '"', "'"
            )
            lines.append(f'    {node_ids[step["id"]]}["{label}"]')
        lines.append("  end")
    for step in steps:
        target = node_ids[step["id"]]
        for alternative in dependency_alternatives(step.get("dependency", "")):
            for dep_id in alternative:
                source = node_ids.get(dep_id)
                if source:
                    lines.append(f"  {source} --> {target}")
    lines.extend(f"  {class_def}" for class_def in DASHBOARD_GRAPH_CLASS_DEFS)
    by_class: dict[str, list[str]] = {}
    for step in steps:
        css = dashboard_graph_state_class(step.get("state", ""))
        if css:
            by_class.setdefault(css, []).append(node_ids[step["id"]])
    for css in ("stDone", "stActive", "stBlocked", "stWaiting"):
        nodes = by_class.get(css)
        if nodes:
            lines.append(f"  class {','.join(nodes)} {css}")
    return "\n".join(lines)


def dashboard_mermaid_src() -> str:
    """页面内 mermaid 渲染沿用 html-ui 随包资产（bundled_mermaid_path，§4.3.2 第 4 节）；
    资产缺失时返回空串，模板内 JS 守卫自行降级为源码展示，不阻断渲染。"""
    path = bundled_mermaid_path()
    if not path.is_file():
        return ""
    try:
        return path.resolve().as_uri()
    except ValueError:
        return ""


def render_dashboard(
    snapshot: FeatureSnapshot,
    process_files: list[ProcessFileEntry] | None = None,
    step_timings: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """按 dashboard-template 契约渲染（18 TOKEN + 16 INJECT，接口文档 §4 修订版）：
    替换全部 {{TOKEN}} 与 AGENT_INJECT 注入点，残留 {{ 或缺注入点即
    DevkitFlowError（与 render_workflow_report 同口径）。

    process_files 与 step_timings 由调用方（write_feature_dashboard）采集后传入
    ——render 层保持只消费入参的纯函数形态，缺省 None 按空数据渲染（离线单测路径）。"""
    summary = snapshot.feature["summary"]
    config = snapshot_config_gates(snapshot)
    scale, mode = snapshot_scale_mode(snapshot)
    display_steps = annotate_config_closed_steps(snapshot.pipeline, config)
    # D-6 单一事实源顶层 import（T9 收敛：T5/T8 并行窗口的双通道兜底访问器已退役）。
    grouped, ungrouped = dashboard_group_steps_by_stage(display_steps, PIPELINE_STEP_STAGES)
    # 未归组节点兜底进「需求」分区并显式标记：仅当存量 STATUS.md 含
    # PIPELINE_STEP_STAGES 未覆盖的步骤行（历史/自定义行）时出现。
    displayed = {
        stage: [*rows, *(ungrouped if stage == "需求" else [])]
        for stage, rows in grouped.items()
    }

    closed = sum(1 for step in snapshot.pipeline if step["state"] in CLOSED_STATES)
    total = len(snapshot.pipeline)
    overall_status, overall_kind = dashboard_overall_status(snapshot)
    generated_at = flow_now_iso()
    entries = process_files or []
    timings = step_timings or {}

    tokens = {
        "FEATURE_NAME": Path(snapshot.feature["feature_dir"]).name,
        "FEATURE_TITLE": snapshot.title,
        "FEATURE_STATE": summary.get("state", "-") or "-",
        "FEATURE_SCALE": summary.get("scale", "-") or "-",
        "FEATURE_MODE": summary.get("mode", "-") or "-",
        "FEATURE_BRANCH": summary.get("branch", "-") or "-",
        "PROGRESS_SUMMARY": f"{closed}/{total} 步已关闭 · 剩余 {total - closed} 步",
        "PROGRESS_PERCENT": dashboard_progress_percent(closed, total),
        "OVERALL_STATUS": overall_status,
        "OVERALL_STATUS_KIND": overall_kind,
        "STAGE_REQUIREMENT_PROGRESS": dashboard_stage_progress(displayed["需求"]),
        "STAGE_DEVELOPMENT_PROGRESS": dashboard_stage_progress(displayed["开发"]),
        "STAGE_TESTING_PROGRESS": dashboard_stage_progress(displayed["测试"]),
        "STAGE_REVIEW_PROGRESS": dashboard_stage_progress(displayed["审查"]),
        "REFRESH_INTERVAL_MS": DASHBOARD_REFRESH_INTERVAL_MS,
        "GENERATED_AT": generated_at,
        "MERMAID_SRC": dashboard_mermaid_src(),
    }
    injects = {
        "sidebar doc nav": dashboard_sidebar_doc_nav_html(entries),
        "pipeline rail stages": dashboard_pipeline_rail_stages_html(displayed),
        "breakpoint card": dashboard_breakpoint_card_html(
            snapshot.breakpoint, snapshot.next_action
        ),
        "status waiting block": dashboard_waiting_block_html(snapshot.waiting_on)
        + dashboard_disabled_gates_html(
            disabled_pipeline_items(
                config,
                existing_ids=(step["id"] for step in snapshot.pipeline),
                scale=scale,
                mode=mode,
            )
            if config is not None
            else []
        ),
        "stage requirement step rows": dashboard_step_rows_html(
            grouped["需求"], ungrouped, timings=timings, generated_at=generated_at
        ),
        "stage requirement task block": dashboard_task_block_html(
            snapshot.design_tasks, snapshot.ones_space_id, design=True
        ),
        "stage development step rows": dashboard_step_rows_html(
            grouped["开发"], timings=timings, generated_at=generated_at
        ),
        "stage development task block": dashboard_task_block_html(
            snapshot.dev_tasks, snapshot.ones_space_id, design=False
        ),
        "stage testing step rows": dashboard_step_rows_html(
            grouped["测试"], timings=timings, generated_at=generated_at
        ),
        "stage testing task block": dashboard_task_block_html(
            [], snapshot.ones_space_id, design=False
        ),
        "stage testing e2e gate": dashboard_e2e_gate_html(snapshot.e2e_gate),
        "stage review step rows": dashboard_step_rows_html(
            grouped["审查"], timings=timings, generated_at=generated_at
        ),
        "stage review task block": dashboard_task_block_html(
            [], snapshot.ones_space_id, design=False
        ),
        "blockers panel": dashboard_blockers_panel_html(snapshot.blockers),
        "dependency graph": html.escape(
            dashboard_dependency_graph(display_steps, PIPELINE_STEP_STAGES)
        ),
        "doc templates": dashboard_doc_templates_html(entries),
    }

    rendered = embedded_templates()["dashboard-template"]
    for key, value in tokens.items():
        rendered = rendered.replace("{{" + key + "}}", html.escape(str(value)))
    requirement_id = summary.get("ones_requirement_id", "").strip()
    requirement = dashboard_ones_link_html(snapshot.ones_space_id, "requirement", requirement_id)
    if requirement_id:
        requirement = f"<strong>{requirement}</strong>"
    rendered = rendered.replace("{{ONES_REQUIREMENT}}", requirement)
    # 残留 {{ 检查必须在内容注入之前（模板级失败判定，语义不变）：注入内容可合法
    # 含双花括号原文——过程文件内联预览/长文 note 里出现 TOKEN 字样属正常数据，
    # 不得误判为模板未替换（009 R5 内联接线后的新暴露面）。
    if "{{" in rendered:
        raise DevkitFlowError("dashboard 模板仍包含未替换变量。")
    for marker, content in injects.items():
        full_marker = f"<!-- AGENT_INJECT: {marker} -->"
        if full_marker not in rendered:
            raise DevkitFlowError(f"dashboard 模板缺少注入点: {full_marker}")
        rendered = rendered.replace(full_marker, content)
    return rendered


def write_feature_dashboard(workspace: Path, feature: dict[str, Any]) -> dict[str, Any]:
    """重建 snapshot + 枚举过程文件 + 读时间旁车 → 渲染 → 原子写
    spec/00x-{feature}/dashboard.html。"""
    snapshot = collect_feature_snapshot(workspace, feature)
    entries = dashboard_process_files(workspace, feature["feature_dir"])
    timings = dashboard_step_timings(workspace / feature["feature_dir"])
    rendered = render_dashboard(snapshot, process_files=entries, step_timings=timings)
    output_path = workspace / feature["feature_dir"] / "dashboard.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output_path, rendered)
    try:
        relative_output = output_path.relative_to(workspace).as_posix()
    except ValueError:
        relative_output = str(output_path)
    all_tasks = [*snapshot.design_tasks, *snapshot.dev_tasks]
    stats = {
        "steps_total": len(snapshot.pipeline),
        "steps_done": sum(1 for step in snapshot.pipeline if step["state"] in DONE_STATES),
        "tasks_total": len(all_tasks),
        "tasks_done": sum(1 for row in all_tasks if row.get("状态", "").strip() in DONE_STATES),
        "blocked_steps": len(snapshot.blockers["blocked_steps"]),
        "pending_clarifications": len(snapshot.blockers["clarifications"]),
        "ones_retries": len(snapshot.blockers["ones_retries"]),
        "funding_risks": len(snapshot.blockers["funding_risks"]),
        "ungrouped_steps": len(snapshot.pipeline)
        - sum(len(rows) for rows in dashboard_group_steps_by_stage(snapshot.pipeline, PIPELINE_STEP_STAGES)[0].values()),
        # 009 可观测增补（接口文档 §2.3）：过程文件清单总数与实际内联数。
        "process_files_total": len(entries),
        "process_files_inlined": sum(
            1 for entry in entries if entry.preview_text is not None
        ),
    }
    return {
        "output": relative_output,
        "absolute_path": str(output_path.resolve()),
        "stats": stats,
    }


def refresh_dashboard_after_write(workspace: Path, feature: dict[str, Any]) -> str | None:
    """状态写入后的 dashboard 自动重渲染回调（设计 §4.3.3 / §5 签名；D-5 fail-safe）。

    由 cli.py 组装命令分发时注入各写入口命令尾部（D-13，域模块不新增 bundle 裸全局引用）。
    任何异常降级为 warning 字符串返回，绝不抛出——dashboard 是辅助产物，不得阻断状态写入。

    开关 gate 单点收口（009 设计 §4.4，T6）：gate 判定（含 load_devkit_config 配置
    读取）位于既有 fail-safe try 内部的首段——disabled 时从 try 内静默 return None
    （无 warning）；config 读取异常同样被本 try 捕获降级为 warning（§6 兼容性②），
    任何情况下不穿透、不阻断状态写入。17 处调用点与函数签名零改动。
    """
    try:
        if not dashboard_switch_summary(workspace)["enabled"]:
            return None  # 开关关闭：静默跳过，不生成不刷新、无 warning（§4.4）
        write_feature_dashboard(workspace, feature)
    except Exception as error:  # fail-safe 兜底（D-5）：config 读取/渲染失败仅 warning
        return f"dashboard 自动重渲染失败（状态写入已完成，不受影响）: {compact_error(error)}"
    return None


def command_flow_dashboard(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    # 开关 disabled 分支（接口文档 §2.3，T6）：不渲染，输出 status="disabled" +
    # 开启指引，exit 1。显式命令不走 fail-safe：config 读取/校验异常按 DevkitFlowError
    # 报错退出（用户主动调用应可见原因，与 §2.1 exit code 契约一致）。
    switch = dashboard_switch_summary(workspace)
    if not switch["enabled"]:
        emit(
            {
                "status": "disabled",
                "message": (
                    f"dashboard 开关已关闭（source={switch['source']}），未渲染。"
                    "如需开启，执行 devkit-flow dashboard configure --scope project --enable。"
                ),
            },
            args.format,
        )
        return 1
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
    # 显式渲染不走 fail-safe（与挂钩路径相反）：失败要报错退出，但须是干净的
    # DevkitFlowError 而非裸 traceback（T7 观察项，与 main 的 DevkitFlowError 收口对齐）。
    try:
        dashboard = write_feature_dashboard(workspace, feature)
    except DevkitFlowError:
        raise
    except Exception as error:
        raise DevkitFlowError(f"dashboard 渲染失败: {compact_error(error)}") from error
    opened = False
    open_error = None
    if args.open:
        opened, open_error = open_local_html_report(Path(dashboard["absolute_path"]))
    data = {
        "status": "ok",
        "message": (
            f"过程看板已生成: {dashboard['output']}"
            f"（步骤 {dashboard['stats']['steps_done']}/{dashboard['stats']['steps_total']}，"
            f"任务 {dashboard['stats']['tasks_done']}/{dashboard['stats']['tasks_total']}）。"
            + (
                " 已在默认浏览器打开。"
                if opened
                else (f" 未能自动打开（{open_error}），请手动打开该文件。" if open_error else "")
            )
        ),
        "feature": feature["feature"],
        "dashboard": dashboard,
        "opened": opened,
        "open_error": open_error,
    }
    emit(data, args.format)
    return 0
