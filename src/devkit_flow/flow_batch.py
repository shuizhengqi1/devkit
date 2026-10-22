"""016 T-10：`devkit-flow flow batch-set` 批量状态操作（R-07～R-09；设计 §4.7.2，接口 §2.1/§3.1/§4.1/§8.1）。

一次调用在 STATUS.md 排他锁内完成「恢复检查 → 修订号/幂等校验 → 整体预校验（内存逐成员求值，与单步共用同一份
守卫代码）→ 预写账本 → 单次原子写 → 后置记账」，事件携带确定性 ``event_id = uuid5``。成员限同一 STATUS.md 的
流水线步骤（统一 ``--status``）与 ``D-xx`` 设计任务；``T-xx`` 开发任务、``-H`` 人工 Gate、``waived`` 一律拒绝。

依赖方向：``flow_batch → flow``（复用 ``plan_step_member`` / ``plan_task_member`` 等纯守卫与事件构建函数）、
``flow_batch → ones``（只读 ONES-SYNC.json 核实失败项已持久化，D-62），``cli → flow_batch``；``flow.py`` 不 import 本模块。
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import socket
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from devkit_flow.constants import (
    DONE_STATES,
    FLOW_STEP_STATES,
    FLOW_TASK_STATES,
    SKIPPED_STATES,
    STATE_OUTBOX_PREFLIGHT_SCOPE,
    WAIVED_STATES,
)
from devkit_flow.errors import DevkitFlowError
from devkit_flow.flow import (
    STEP_TIMING_ACTIVE_STATES,
    FlowGuardError,
    WriteSideEffects,
    _collecting_clarification_batch,
    _custom_step_rows,
    _step_cycle_entries,
    apply_step_member_plan,
    apply_task_member_plan,
    apply_write_side_effects,
    attach_step_timing_warning,
    clarify_collecting_guard_message,
    flow_now_iso,
    flow_ones_follow_up,
    flow_select_feature,
    flow_session_ref,
    flow_step_member_events,
    flow_task_member_event,
    flow_today,
    plan_step_member,
    plan_task_member,
    predict_step_cycle_closures,
    progress_summary_values,
    record_step_timing,
    render_progress_summary_text,
    step_clarify_guard_applies,
)
def comment_outbox_path(workspace: Path, feature: dict[str, Any]) -> Path:
    del workspace, feature
    return Path("/nonexistent/devkit-public-no-outbox")


def load_comment_outbox(path: Path) -> dict[str, Any]:
    del path
    return {"stateOutbox": [], "commentEvents": []}
from devkit_flow.parse_util import (
    parse_design_task_table,
    parse_design_task_table_text,
    parse_pipeline_table_text,
    parse_task_table,
    render_design_task_table_text,
    render_pipeline_rows_text,
    render_status_field_text,
    status_path_for_feature,
    task_path_for_feature,
)
from devkit_flow.step_alias import feature_pipeline_mode, resolve_step
from devkit_flow.textio import (
    FlowStatusLockTimeout,
    FlowStatusLockUnavailable,
    atomic_write_text,
    attach_dashboard_warning,
    emit,
    flow_status_write_lock,
    process_alive,
)

FLOW_BATCH_SCHEMA_VERSION = 1
FLOW_OP_RECORD_SCHEMA_VERSION = 1
FLOW_OPS_LEDGER_FILE = ".flow-ops.jsonl"
FLOW_OPS_LEDGER_RETAIN = 500
FLOW_BATCH_LOCK_TIMEOUT_SECONDS = 10.0
# D-51③：committed = STATUS 已提交、事件/ONES 意图待派发；dispatched = 已交给装配层派发（闭合）。
FLOW_BATCH_PHASES = ("prepared", "committed", "dispatched", "aborted", "superseded", "abandoned")
FLOW_BATCH_OPEN_PHASES = ("prepared", "committed")
# 已提交过 STATUS 的阶段：显式 --op-id 认幂等、缺省 opId 命中即加盐（abandoned 也已提交、事件可能已部分发出）。
_FLOW_BATCH_SUBMITTED_PHASES = ("committed", "dispatched", "abandoned")
# D-57②：派发 sink 与回执状态；D-58①：派发租约时长（ONES 显式同步走网络，留足余量；过期后他人可接管补发）。
FLOW_BATCH_SINKS = ("telemetry", "ones")
FLOW_BATCH_SINK_STATES = ("pending", "ok", "not-applicable")
FLOW_BATCH_SINK_SETTLED = ("ok", "not-applicable")
FLOW_BATCH_DISPATCH_LEASE_SECONDS = 60
# D-62：同一 opId 待派发补发（租约失效后接管）上界；达到后仍有 pending sink → abandoned（告警，不再恢复与合并）。
FLOW_BATCH_MAX_REDISPATCHES = 5

FLOW_BATCH_INVALID_MEMBER = "FLOW_BATCH_INVALID_MEMBER"
FLOW_BATCH_DEPENDENCY_UNMET = "FLOW_BATCH_DEPENDENCY_UNMET"
FLOW_BATCH_GATE_REQUIRED = "FLOW_BATCH_GATE_REQUIRED"
FLOW_BATCH_OP_CONFLICT = "FLOW_BATCH_OP_CONFLICT"
FLOW_BATCH_REVISION_CONFLICT = "FLOW_BATCH_REVISION_CONFLICT"
FLOW_BATCH_LOCK_TIMEOUT = "FLOW_BATCH_LOCK_TIMEOUT"
FLOW_BATCH_LOCK_UNAVAILABLE = "FLOW_BATCH_LOCK_UNAVAILABLE"
FLOW_BATCH_RECOVERY_REQUIRED = "FLOW_BATCH_RECOVERY_REQUIRED"

# uuid5 命名空间（确定性，跨机器/跨进程一致）：opId 派生与事件 event_id 共用。
FLOW_BATCH_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://devkit-flow.local/flow/batch-set")
# 由 cli 装配层消费、不入遥测队列的意图事件类型（与 cli._INTENT_EVENT_TYPES 同集合）：不分配 event_id。
_FLOW_BATCH_INTENT_TYPES = frozenset(
    {"ones_follow_up", "write_workflow_report", "open_local_html_report", "quality_collect"}
)
_FLOW_BATCH_DESIGN_TASK_RE = re.compile(r"^D-\d+$")
_FLOW_BATCH_GUARD_CODES = {
    "invalid": FLOW_BATCH_INVALID_MEMBER,
    "dependency": FLOW_BATCH_DEPENDENCY_UNMET,
    "gate": FLOW_BATCH_GATE_REQUIRED,
}


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def flow_status_revision(content: str | bytes) -> str:
    """STATUS.md 修订号 = 文件**原始字节**的 sha256（接口 §3.1 ``baseRevision`` / ``revision``）。

    传 ``str`` 时按 UTF-8 编码（仅用于内存渲染出的新文本，与 ``atomic_write_text`` 落盘字节一致）。"""
    raw = content if isinstance(content, (bytes, bytearray)) else content.encode("utf-8")
    return hashlib.sha256(bytes(raw)).hexdigest()


def read_status_snapshot(path: Path) -> tuple[str, str]:
    """读 STATUS.md：返回 (按原始字节计算的 revision, 换行归一化后的文本)。

    revision 必须基于原始字节（D-51⑥ / Codex P2-12：CRLF 文件的 ``read_text`` 会被归一化成 LF，
    导致客户端按字节计算的合法 ``--expect-revision`` 被误判冲突）；解析与渲染沿用归一化文本（与单步一致）。"""
    raw = path.read_bytes()
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return flow_status_revision(raw), text


# --------------------------------------------------------------------------------------------
# 请求与成员
# --------------------------------------------------------------------------------------------


@dataclass
class BatchMember:
    kind: str
    id: str
    target_state: str
    from_state: Optional[str] = None
    noop: bool = False
    forced: bool = False
    raw: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "from": self.from_state,
            "to": self.target_state,
            "noop": self.noop,
            "forced": self.forced,
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "from": self.from_state,
            "to": self.target_state,
            "noop": self.noop,
        }


@dataclass
class BatchRequest:
    feature: Optional[str]
    members: list[BatchMember]
    confirm_gates: bool = False
    note: Optional[str] = None
    op_id: Optional[str] = None
    expect_revision: Optional[str] = None
    dry_run: bool = False
    no_ones_sync: bool = False

    def canonical_members(self) -> list[list[str]]:
        return [[member.kind, member.id, member.target_state] for member in self.members]

    def content_digest(self) -> str:
        """``contentDigest = sha256(规范化成员列表 + note)``（设计 §4.7.2.3 第 4 步）。"""
        payload = json.dumps(
            {"members": self.canonical_members(), "note": self.note},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def derived_op_id(self, feature_name: str, base_revision: str) -> str:
        """缺省 opId = uuid5(NS, feature + 规范化成员列表 + 基线 revision)。"""
        payload = json.dumps(
            [feature_name, self.canonical_members(), base_revision],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return str(uuid.uuid5(FLOW_BATCH_NAMESPACE, payload))


def _batch_reject(code: str, message: str) -> DevkitFlowError:
    return DevkitFlowError(message, error_code=code)


def build_batch_request(args: argparse.Namespace) -> BatchRequest:
    """命令行 → BatchRequest：只做与 STATUS.md 无关的成员级校验（种类、前缀、状态枚举、重复、--confirm-gates）。"""
    members: list[BatchMember] = []
    seen_tasks: set[str] = set()
    step_state = getattr(args, "status", None)
    for kind, raw in getattr(args, "batch_members", None) or []:
        if kind == "step":
            state = str(step_state or "")
            if state in WAIVED_STATES:
                raise _batch_reject(
                    FLOW_BATCH_INVALID_MEMBER,
                    f"[step:{raw}] waived 只能由 flow accept-risk 记录，不能批量置位。",
                )
            if state not in FLOW_STEP_STATES:
                raise _batch_reject(
                    FLOW_BATCH_INVALID_MEMBER,
                    f"[step:{raw}] 非法状态 {state}"
                    f"（可选: {', '.join(sorted(FLOW_STEP_STATES - WAIVED_STATES))}）。",
                )
            members.append(BatchMember(kind="step", id=str(raw).strip(), target_state=state, raw=str(raw)))
            continue
        text = str(raw).strip()
        task_id, sep, state = text.partition("=")
        task_id = task_id.strip()
        state = state.strip()
        if not sep or not task_id or not state:
            raise _batch_reject(
                FLOW_BATCH_INVALID_MEMBER,
                f"[task:{text}] 格式须为 D-xx=<状态>。",
            )
        if not _FLOW_BATCH_DESIGN_TASK_RE.fullmatch(task_id):
            raise _batch_reject(
                FLOW_BATCH_INVALID_MEMBER,
                f"[task:{task_id}] 批量入口只接受 STATUS.md 设计任务 D-xx；"
                "开发任务 T-xx 请逐条使用 flow task set。",
            )
        if state not in FLOW_TASK_STATES:
            raise _batch_reject(
                FLOW_BATCH_INVALID_MEMBER,
                f"[task:{task_id}] 非法任务状态 {state}（可选: {', '.join(FLOW_TASK_STATES)}）。",
            )
        if task_id in seen_tasks:
            # D-43 / T-09 审查 P2-B：同一任务重复出现会让后一个计划覆盖前一个，直接拒绝。
            raise _batch_reject(
                FLOW_BATCH_INVALID_MEMBER,
                f"[task:{task_id}] 同一设计任务在一次批量中重复出现。",
            )
        seen_tasks.add(task_id)
        if state == "已完成" and not bool(getattr(args, "confirm_gates", False)):
            raise _batch_reject(
                FLOW_BATCH_GATE_REQUIRED,
                f"[task:{task_id}] 置为已完成必须携带 --confirm-gates（声明 Gate 矩阵已关闭）。",
            )
        members.append(BatchMember(kind="task", id=task_id, target_state=state, raw=text))
    if not members:
        raise _batch_reject(FLOW_BATCH_INVALID_MEMBER, "flow batch-set 至少需要一个 --step 或 --task 成员。")
    return BatchRequest(
        feature=getattr(args, "feature", None),
        members=members,
        confirm_gates=bool(getattr(args, "confirm_gates", False)),
        note=getattr(args, "note", None),
        op_id=getattr(args, "op_id", None),
        expect_revision=getattr(args, "expect_revision", None),
        dry_run=bool(getattr(args, "dry_run", False)),
        no_ones_sync=bool(getattr(args, "no_ones_sync", False)),
    )


# --------------------------------------------------------------------------------------------
# 预校验与内存求值
# --------------------------------------------------------------------------------------------


@dataclass
class BatchPlan:
    members: list[BatchMember]
    base_revision: str
    target_revision: str
    new_status_text: str
    progress: dict[str, str]

    @property
    def changed(self) -> bool:
        return self.target_revision != self.base_revision

    def active_members(self) -> list[tuple[int, BatchMember]]:
        return [(index, member) for index, member in enumerate(self.members) if not member.noop]


class BatchPlanner:
    """整体预校验（零写入）：对内存文本副本逐成员 plan → apply 交替求值，前一成员结果可满足后续成员依赖。"""

    def __init__(
        self,
        workspace: Path,
        feature: dict[str, Any],
        *,
        gate_guard: Callable[[Path], Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.feature = feature
        self.gate_guard = gate_guard

    def resolve_steps(self, request: BatchRequest, status_path: Path) -> None:
        """步骤成员别名解析（与 flow set 同口径），并拒绝 -H 人工 Gate 与重复步骤。"""
        steps = [member for member in request.members if member.kind == "step"]
        if not steps:
            return
        mode = feature_pipeline_mode(self.feature)
        extra_rows = _custom_step_rows(status_path)
        seen: set[str] = set()
        for member in steps:
            try:
                member.id = resolve_step(member.raw, mode=mode, extra_rows=extra_rows).step_id
            except DevkitFlowError as error:
                raise _batch_reject(
                    FLOW_BATCH_INVALID_MEMBER, f"[step:{member.raw}] {error}"
                ) from None
            if member.id.endswith("-H"):
                raise _batch_reject(
                    FLOW_BATCH_INVALID_MEMBER,
                    f"[step:{member.id}] 人工 Gate（-H）只能走 flow confirm，不得批量置位。",
                )
            if member.id in seen:
                raise _batch_reject(
                    FLOW_BATCH_INVALID_MEMBER,
                    f"[step:{member.id}] 同一步骤在一次批量中重复出现。",
                )
            seen.add(member.id)

    def plan(self, request: BatchRequest, status_text: str, base_revision: str | None = None) -> BatchPlan:
        if base_revision is None:
            base_revision = flow_status_revision(status_text)
        text = status_text
        now = flow_now_iso()
        collecting = _collecting_clarification_batch(self.workspace, self.feature)
        step_changed = False
        members = [
            BatchMember(kind=member.kind, id=member.id, target_state=member.target_state, raw=member.raw)
            for member in request.members
        ]
        for member in members:
            if member.kind == "step":
                text, changed = self._plan_step(member, text, request, collecting)
                step_changed = step_changed or changed
            else:
                text = self._plan_task(member, text, request, now)
        if step_changed:
            # 与单步 flow set 同序：行 → 最后更新 → 进度概要（只在有步骤成员实际变化时改写，
            # 设计任务成员与单步 flow task set 一样不触碰这两处）。
            text = render_status_field_text(text, "最后更新", flow_today())
            text = render_progress_summary_text(text, progress_summary_values(parse_pipeline_table_text(text)))
        progress = progress_summary_values(parse_pipeline_table_text(text))
        # 文本未变（全部 noop）→ revision 不变：不得用归一化文本的摘要冒充原始字节摘要（CRLF 文件）。
        target_revision = base_revision if text == status_text else flow_status_revision(text)
        return BatchPlan(
            members=members,
            base_revision=base_revision,
            target_revision=target_revision,
            new_status_text=text,
            progress=progress,
        )

    def _plan_step(
        self,
        member: BatchMember,
        text: str,
        request: BatchRequest,
        collecting: dict[str, Any] | None,
    ) -> tuple[str, bool]:
        table = parse_pipeline_table_text(text)
        row = next((candidate for candidate in table["rows"] if candidate["cells"][0] == member.id), None)
        if row is None:
            raise _batch_reject(FLOW_BATCH_INVALID_MEMBER, f"[step:{member.id}] 流水线中不存在步骤 {member.id}。")
        member.from_state = row["cells"][4]
        if member.from_state == member.target_state:
            member.noop = True  # BC-16：已处于目标态 → 不改备注/时间戳、不发事件
            return text, False
        if step_clarify_guard_applies(collecting, member.target_state):
            raise _batch_reject(
                FLOW_BATCH_GATE_REQUIRED,
                f"[step:{member.id}] "
                + clarify_collecting_guard_message(
                    collecting or {}, action=f"flow set --status {member.target_state}"
                ),
            )
        try:
            plan = plan_step_member(
                self.workspace,
                self.feature,
                table,
                member.id,
                member.target_state,
                note=request.note,
                gate_guard=self.gate_guard,
            )
        except FlowGuardError as error:
            raise _batch_reject(
                _FLOW_BATCH_GUARD_CODES.get(error.guard, FLOW_BATCH_INVALID_MEMBER),
                f"[step:{member.id}] {error}",
            ) from None
        except DevkitFlowError as error:
            raise _batch_reject(FLOW_BATCH_INVALID_MEMBER, f"[step:{member.id}] {error}") from None
        member.forced = False  # 批量入口不提供 --force；字段保留与 flow set 同形
        row = apply_step_member_plan(plan, table)
        return render_pipeline_rows_text(table, [row]), True

    def _plan_task(self, member: BatchMember, text: str, request: BatchRequest, now: str) -> str:
        try:
            table = parse_design_task_table_text(text)
        except DevkitFlowError as error:
            raise _batch_reject(FLOW_BATCH_INVALID_MEMBER, f"[task:{member.id}] {error}") from None
        row = next((candidate for candidate in table["rows"] if candidate.get("任务ID") == member.id), None)
        if row is None:
            raise _batch_reject(
                FLOW_BATCH_INVALID_MEMBER, f"[task:{member.id}] 设计任务表中不存在 {member.id}。"
            )
        member.from_state = row.get("状态", "").strip()
        if member.from_state == member.target_state:
            member.noop = True  # BC-16：重复完成在批量入口是 noop（单步 flow task set 行为不变）
            return text
        try:
            plan = plan_task_member(
                table,
                member.id,
                member.target_state,
                is_design=True,
                note=request.note,
                confirm_gates=request.confirm_gates,
                now=now,
            )
            apply_task_member_plan(table, plan)
        except FlowGuardError as error:
            raise _batch_reject(
                _FLOW_BATCH_GUARD_CODES.get(error.guard, FLOW_BATCH_INVALID_MEMBER),
                f"[task:{member.id}] {error}",
            ) from None
        except DevkitFlowError as error:
            raise _batch_reject(FLOW_BATCH_INVALID_MEMBER, f"[task:{member.id}] {error}") from None
        return render_design_task_table_text(text, table)


# --------------------------------------------------------------------------------------------
# 账本 .flow-ops.jsonl
# --------------------------------------------------------------------------------------------


@dataclass
class FlowOpRecord:
    op_id: str
    content_digest: str
    phase: str
    base_revision: str
    target_revision: str
    members: list[dict[str, Any]]
    at: str
    no_ones_sync: bool = False
    # D-51③ / Codex P2-6：每个非 noop 步骤成员在 record_step_timing 之前的 cycles[] 快照（键 = 成员序号字符串）。
    cycles_before: dict[str, Any] = field(default_factory=dict)
    # D-57④：prepared 时对快照副本预演出的「本批次会闭合的周期序号」（键 = 成员序号字符串）。prepared → completed
    # 恢复时 step_cycle 只发这些序号，不把后续单步产生的周期计入旧批次。
    expected_cycles: dict[str, Any] = field(default_factory=dict)
    # D-57④：committed 起持久化原批次待发事件的确定性载荷（build_events 的输出，含 event_id）；补发只发这些。
    pending_events: Optional[list[dict[str, Any]]] = None
    # D-57②：按 sink 的派发回执——telemetry / ones ∈ pending | ok | not-applicable；全部非 pending 才 dispatched。
    dispatch_receipts: dict[str, str] = field(default_factory=dict)
    # D-58①：派发租约 {pid, host, expiresAt}；在途且未过期的他人批次不重发、不报 recovered。
    dispatch_lease: Optional[dict[str, Any]] = None
    # D-62：已发生的待派发补发次数（redispatch 接管时 +1），达到 FLOW_BATCH_MAX_REDISPATCHES 仍未闭合即 abandoned。
    redispatches: int = 0
    schema_version: int = FLOW_OP_RECORD_SCHEMA_VERSION

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "opId": self.op_id,
            "contentDigest": self.content_digest,
            "phase": self.phase,
            "baseRevision": self.base_revision,
            "targetRevision": self.target_revision,
            "members": self.members,
            "at": self.at,
            "noOnesSync": self.no_ones_sync,
            "cyclesBefore": self.cycles_before,
        }
        if self.expected_cycles:
            data["expectedCycles"] = self.expected_cycles
        if self.pending_events is not None:
            data["pendingEvents"] = self.pending_events
        if self.dispatch_receipts:
            data["dispatchReceipts"] = self.dispatch_receipts
        if self.redispatches:
            data["redispatches"] = self.redispatches
        if self.phase == "committed":
            data["dispatchLease"] = self.dispatch_lease
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "FlowOpRecord":
        """按当前版本完整校验必填字段与类型（D-51⑥ / Codex P2-7）；不合格抛 ValueError（调用方判账本损坏）。"""
        _validate_record_shape(data)
        cycles = data.get("cyclesBefore")
        expected = data.get("expectedCycles")
        events = data.get("pendingEvents")
        receipts = data.get("dispatchReceipts")
        lease = data.get("dispatchLease")
        return cls(
            op_id=data["opId"],
            content_digest=data["contentDigest"],
            phase=data["phase"],
            base_revision=data["baseRevision"],
            target_revision=data["targetRevision"],
            members=[dict(item) for item in data["members"]],
            at=data["at"],
            no_ones_sync=bool(data.get("noOnesSync", False)),
            cycles_before=dict(cycles) if isinstance(cycles, dict) else {},
            expected_cycles=dict(expected) if isinstance(expected, dict) else {},
            pending_events=[dict(item) for item in events] if isinstance(events, list) else None,
            dispatch_receipts=dict(receipts) if isinstance(receipts, dict) else {},
            dispatch_lease=dict(lease) if isinstance(lease, dict) else None,
            redispatches=int(data.get("redispatches") or 0),
            schema_version=data["schemaVersion"],
        )

    _KEEP = object()

    def closed(
        self,
        phase: str,
        *,
        pending_events: Any = _KEEP,
        receipts: Any = _KEEP,
        lease: Any = _KEEP,
        redispatches: Any = _KEEP,
    ) -> "FlowOpRecord":
        return FlowOpRecord(
            op_id=self.op_id,
            content_digest=self.content_digest,
            phase=phase,
            base_revision=self.base_revision,
            target_revision=self.target_revision,
            members=self.members,
            at=flow_now_iso(),
            no_ones_sync=self.no_ones_sync,
            cycles_before=self.cycles_before,
            expected_cycles=self.expected_cycles,
            pending_events=self.pending_events if pending_events is FlowOpRecord._KEEP else pending_events,
            dispatch_receipts=(
                dict(self.dispatch_receipts) if receipts is FlowOpRecord._KEEP else dict(receipts)
            ),
            dispatch_lease=self.dispatch_lease if lease is FlowOpRecord._KEEP else lease,
            redispatches=self.redispatches if redispatches is FlowOpRecord._KEEP else int(redispatches),
        )

    def batch_members(self) -> list[BatchMember]:
        return [
            BatchMember(
                kind=str(item["kind"]),
                id=str(item["id"]),
                target_state=str(item["to"]),
                from_state=item.get("from") if isinstance(item.get("from"), str) else None,
                noop=bool(item.get("noop", False)),
            )
            for item in self.members
        ]

    def cycles_by_index(self) -> dict[int, list[dict[str, Any]] | None]:
        return _int_keyed(self.cycles_before)

    def expected_cycles_by_index(self) -> Optional[dict[int, list[int] | None]]:
        """无 expectedCycles（27694b8 写的旧记录）→ None：调用方退回按快照差分（旧口径）。"""
        if not self.expected_cycles:
            return None
        return _int_keyed(self.expected_cycles)

    def sink_state(self, sink: str) -> str:
        return self.dispatch_receipts.get(sink, "pending")


def _int_keyed(mapping: dict[str, Any]) -> dict[int, Any]:
    result: dict[int, Any] = {}
    for key, value in mapping.items():
        try:
            result[int(key)] = value
        except (TypeError, ValueError):
            continue
    return result


def _is_str(value: Any, *, non_empty: bool = True) -> bool:
    return isinstance(value, str) and (bool(value) or not non_empty)


def _validate_record_shape(data: dict[str, Any]) -> None:
    """当前版本（schemaVersion=1）记录的必填字段与类型；与 flow-op-record.schema.json 同口径。"""
    if not _is_str(data.get("opId")):
        raise ValueError("opId 缺失或非字符串")
    if data.get("phase") not in FLOW_BATCH_PHASES:
        raise ValueError(f"phase 非法：{data.get('phase')!r}")
    for key in ("contentDigest", "baseRevision", "targetRevision"):
        value = data.get(key)
        if not isinstance(value, str) or not _SHA256_HEX_RE.fullmatch(value):
            raise ValueError(f"{key} 缺失或不是 sha256")
    if not _is_str(data.get("at")):
        raise ValueError("at 缺失或非字符串")
    members = data.get("members")
    if not isinstance(members, list) or not members:
        raise ValueError("members 缺失或为空")
    for position, item in enumerate(members):
        if not isinstance(item, dict):
            raise ValueError(f"members[{position}] 不是对象")
        if item.get("kind") not in ("step", "task"):
            raise ValueError(f"members[{position}].kind 非法")
        if not _is_str(item.get("id")) or not _is_str(item.get("to")):
            raise ValueError(f"members[{position}] 缺少 id/to")
        if "from" not in item or not (item["from"] is None or isinstance(item["from"], str)):
            raise ValueError(f"members[{position}].from 缺失或类型不符")
        if "noop" in item and not isinstance(item["noop"], bool):
            raise ValueError(f"members[{position}].noop 不是布尔")
    if "noOnesSync" in data and not isinstance(data["noOnesSync"], bool):
        raise ValueError("noOnesSync 不是布尔")
    cycles = data.get("cyclesBefore")
    if cycles is not None:
        if not isinstance(cycles, dict) or not all(
            value is None or isinstance(value, list) for value in cycles.values()
        ):
            raise ValueError("cyclesBefore 形状不符")
    expected = data.get("expectedCycles")
    if expected is not None:
        if not isinstance(expected, dict) or not all(
            value is None
            or (isinstance(value, list) and all(isinstance(item, int) and not isinstance(item, bool) for item in value))
            for value in expected.values()
        ):
            raise ValueError("expectedCycles 形状不符")
    events = data.get("pendingEvents")
    if events is not None and (not isinstance(events, list) or not all(isinstance(item, dict) for item in events)):
        raise ValueError("pendingEvents 形状不符")
    receipts = data.get("dispatchReceipts")
    if receipts is not None:
        if not isinstance(receipts, dict) or not all(
            key in FLOW_BATCH_SINKS and value in FLOW_BATCH_SINK_STATES for key, value in receipts.items()
        ):
            raise ValueError("dispatchReceipts 形状不符")
    lease = data.get("dispatchLease")
    if lease is not None:
        if not (
            isinstance(lease, dict)
            and isinstance(lease.get("pid"), int)
            and not isinstance(lease.get("pid"), bool)
            and lease.get("pid") > 0
            and _is_str(lease.get("host"), non_empty=False)
            and _is_str(lease.get("expiresAt"))
        ):
            raise ValueError("dispatchLease 形状不符")
    redispatches = data.get("redispatches")
    if redispatches is not None and (
        isinstance(redispatches, bool) or not isinstance(redispatches, int) or redispatches < 0
    ):
        raise ValueError("redispatches 不是非负整数")


def _error_reason(error: BaseException) -> str:
    """不含路径的简短原因（P3 / N3-3 口径）：OSError → errno 名 + strerror；其余 → 异常类型名。"""
    if isinstance(error, OSError):
        name = errno.errorcode.get(error.errno or 0, "") if error.errno else ""
        return " ".join(part for part in (name, error.strerror or "") if part) or type(error).__name__
    return type(error).__name__


def _pending_sinks(record: "FlowOpRecord") -> str:
    return "、".join(sink for sink in FLOW_BATCH_SINKS if record.sink_state(sink) == "pending") or "未知"


def _warn_stderr(message: str) -> None:
    """N4-1：abandoned 告警同时写 stderr（接口 §8.1「告警写入 warnings[] 与 stderr」）；消息不含本机路径。"""
    try:
        print(f"devkit-flow: 警告：{message}", file=sys.stderr)
    except Exception:
        pass


def _record_schema_version(data: Any) -> Optional[int]:
    if not isinstance(data, dict):
        return None
    version = data.get("schemaVersion")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    return version


class FlowOpLedger:
    """``{feature_dir}/.flow-ops.jsonl``：同一 opId 依次追加 prepared → committed → dispatched / abandoned
    （或 prepared → aborted / superseded）。

    写入只在 STATUS.md 写锁内；``status`` 的 ``flow_ops`` 只读读取、不加锁，损坏时报 ``unreadable``（不吞成 0）。
    任何一行无法解析或（当前版本）结构不合格即视为账本损坏（无法判定是否存在
    未闭合记录），抛 FLOW_BATCH_RECOVERY_REQUIRED 且不改 STATUS.md；``schemaVersion`` 高于本版本的记录
    **先于任何字段校验**按 §9.3 忽略（D-51⑥ / Codex P2-8）。
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _lines(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            return [line for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, UnicodeDecodeError) as error:
            raise self._corrupt(f"无法读取（{_error_reason(error)}）") from None

    def _corrupt(self, detail: str) -> DevkitFlowError:
        """P3：只带账本文件名与行号 / 原因，不带本机路径（OSError 的 str 含完整路径，用 ``_error_reason``）。"""
        return _batch_reject(
            FLOW_BATCH_RECOVERY_REQUIRED,
            f"批量操作账本损坏，无法判定未闭合记录（STATUS.md 未改动，请人工核对）：{self.path.name}（{detail}）",
        )

    def records(self) -> list[FlowOpRecord]:
        records: list[FlowOpRecord] = []
        for number, line in enumerate(self._lines(), start=1):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                raise self._corrupt(f"第 {number} 行不是合法 JSON") from None
            version = _record_schema_version(data)
            if version is None:
                raise self._corrupt(f"第 {number} 行缺少合法 schemaVersion")
            if version > FLOW_OP_RECORD_SCHEMA_VERSION:
                continue
            try:
                records.append(FlowOpRecord.from_json(data))
            except (KeyError, TypeError, ValueError) as error:
                raise self._corrupt(f"第 {number} 行字段形状不符：{error}") from None
        return records

    def latest_by_op(self) -> dict[str, FlowOpRecord]:
        latest: dict[str, FlowOpRecord] = {}
        for record in self.records():
            latest.pop(record.op_id, None)  # 保持「最近一次写入」的顺序
            latest[record.op_id] = record
        return latest

    def find(self, op_id: str) -> Optional[FlowOpRecord]:
        return self.latest_by_op().get(op_id)

    def open_records(self) -> list[FlowOpRecord]:
        return [record for record in self.latest_by_op().values() if record.phase in FLOW_BATCH_OPEN_PHASES]

    def reconcile(self, current_revision: str) -> list[tuple[FlowOpRecord, str]]:
        """未闭合记录判定（只判定、不写入）：
        - ``prepared``（设计 §4.7.2.4 四态表）：当前 revision == targetRevision → ``completed``；
          == baseRevision → ``aborted``；其他 → ``superseded``。
        - ``committed``（D-51③：已提交、待派发）：派发租约仍有效（持有者在途）→ ``in-flight``（D-58①：不重发、
          不报 recovered）；否则 → ``redispatch``：只补发仍为 pending 的 sink（D-57②）。"""
        resolutions: list[tuple[FlowOpRecord, str]] = []
        for record in self.open_records():
            if record.phase == "committed":
                resolutions.append((record, "in-flight" if dispatch_lease_active(record.dispatch_lease, record.op_id) else "redispatch"))
            elif current_revision == record.target_revision:
                resolutions.append((record, "completed"))
            elif current_revision == record.base_revision:
                resolutions.append((record, "aborted"))
            else:
                resolutions.append((record, "superseded"))
        return resolutions

    def append(self, record: FlowOpRecord) -> None:
        line = json.dumps(record.to_json(), ensure_ascii=False, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._trim()

    def _trim(self) -> None:
        """保留最近 FLOW_OPS_LEDGER_RETAIN 条：从最旧处只截已闭合 opId 的记录（接口 §8.1）；
        prepared / committed（待派发）均视为未闭合，永不截断。"""
        lines = self._lines()
        if len(lines) <= FLOW_OPS_LEDGER_RETAIN:
            return
        latest_phase: dict[str, str] = {}
        parsed: list[tuple[str, Optional[str]]] = []
        for line in lines:
            try:
                data = json.loads(line)
                op_id = str(data.get("opId")) if isinstance(data, dict) else None
                phase = data.get("phase") if isinstance(data, dict) else None
            except json.JSONDecodeError:
                op_id, phase = None, None
            parsed.append((line, op_id))
            if op_id and isinstance(phase, str):
                latest_phase[op_id] = phase
        excess = len(lines) - FLOW_OPS_LEDGER_RETAIN
        kept: list[str] = []
        for line, op_id in parsed:
            if excess > 0 and op_id and latest_phase.get(op_id) not in (None, *FLOW_BATCH_OPEN_PHASES):
                excess -= 1
                continue
            kept.append(line)
        atomic_write_text(self.path, "\n".join(kept) + "\n")


# --------------------------------------------------------------------------------------------
# 提交、恢复与结果
# --------------------------------------------------------------------------------------------


@dataclass
class FlowBatchResult:
    status: str
    message: str
    feature: str
    op_id: str
    idempotent: bool
    base_revision: str
    revision: Optional[str]
    recovered: list[dict[str, Any]] = field(default_factory=list)
    members: list[BatchMember] = field(default_factory=list)
    progress: dict[str, str] = field(default_factory=dict)
    ones_sync: Optional[dict[str, Any]] = None
    warnings: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    schema_version: int = FLOW_BATCH_SCHEMA_VERSION

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "status": self.status,
            "message": self.message,
            "feature": self.feature,
            "opId": self.op_id,
            "idempotent": self.idempotent,
            "baseRevision": self.base_revision,
        }
        if self.revision is not None:
            data["revision"] = self.revision
        data["recovered"] = list(self.recovered)
        data["members"] = [member.to_json() for member in self.members]
        data["progress"] = dict(self.progress)
        if self.ones_sync is not None:
            data["ones_sync"] = self.ones_sync
        data["warnings"] = list(self.warnings)
        return data


def flow_batch_event_id(op_id: str, member_index: int, event_type: str, ordinal: int = 0) -> str:
    """确定性 event_id = uuid5(NS, opId + 成员序号 + 事件类型)；同成员同类型多条时追加序号。"""
    key = f"{op_id}:{member_index}:{event_type}" + (f":{ordinal}" if ordinal else "")
    return str(uuid.uuid5(FLOW_BATCH_NAMESPACE, key))


def _batch_timing_recorded(feature_dir: Path, step_id: str, state: str, since: str) -> bool:
    """恢复补写前判定该步骤 timing 是否已在中断前写入（record_step_timing 对终态非幂等，避免重复记周期）。"""
    entries = _step_cycle_entries(feature_dir, step_id) or []
    if state in STEP_TIMING_ACTIVE_STATES:
        return bool(entries) and entries[-1].get("ended_at") is None
    if state in DONE_STATES or state in SKIPPED_STATES:
        if not entries or not entries[-1].get("ended_at"):
            return False
        try:
            return datetime.fromisoformat(str(entries[-1]["ended_at"])) >= datetime.fromisoformat(since)
        except (TypeError, ValueError):
            return False
    return False  # pending：无开周期时 record_step_timing 本身为 no-op，重放无害


# --------------------------------------------------------------------------------------------
# 派发租约（D-58①）
# --------------------------------------------------------------------------------------------

# 本进程正在派发的批次（键 = (opId, expiresAt)）：同 pid 的租约只有仍在此集合中才算在途（同进程先前调用中断后
# 留下的租约视为持有者已死，可接管）。不落盘——账本租约形状只含 pid / host / expiresAt（接口 §8.1）。
_ACTIVE_DISPATCH_LEASES: set[tuple[str, str]] = set()


def _lease_host() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_dispatch_lease(op_id: str) -> dict[str, Any]:
    """本进程派发租约：expiresAt = 写入时刻 + 60 s（ISO-8601 UTC，接口 §8.1）。"""
    expires = (_utc_now() + timedelta(seconds=FLOW_BATCH_DISPATCH_LEASE_SECONDS)).isoformat(timespec="seconds")
    _ACTIVE_DISPATCH_LEASES.add((op_id, expires))
    return {"pid": os.getpid(), "host": _lease_host(), "expiresAt": expires}


def release_dispatch_lease(op_id: str, lease: Optional[dict[str, Any]]) -> None:
    if isinstance(lease, dict):
        _ACTIVE_DISPATCH_LEASES.discard((op_id, str(lease.get("expiresAt"))))


def _batch_holder_pid_alive(pid: int) -> bool:
    # None（无法判定）保守视为存活，由到期时间兜底（DR-9）。
    return process_alive(pid) is not False


def _same_lease(left: Optional[dict[str, Any]], right: Optional[dict[str, Any]]) -> bool:
    return (
        isinstance(left, dict)
        and isinstance(right, dict)
        and all(left.get(key) == right.get(key) for key in ("pid", "host", "expiresAt"))
    )


def dispatch_lease_active(lease: Optional[dict[str, Any]], op_id: str) -> bool:
    """租约有效 = 未到期且持有者存活（同机按 pid 判定；同 pid 看是否仍在本进程派发中；异机只看到期）。"""
    if not isinstance(lease, dict):
        return False
    try:
        expires = datetime.fromisoformat(str(lease.get("expiresAt")))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if _utc_now() >= expires:
            return False
    except (TypeError, ValueError):
        return False
    if str(lease.get("host") or "") != _lease_host():
        return True
    pid = lease.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if pid == os.getpid():
        return (op_id, str(lease.get("expiresAt"))) in _ACTIVE_DISPATCH_LEASES
    return _batch_holder_pid_alive(pid)


@dataclass
class PendingDispatch:
    """已提交、待派发的一批（本次提交或恢复出的 committed 记录）：只派发仍为 pending 的 sink，派发后按回执结算。"""

    record: FlowOpRecord
    members: list[BatchMember]
    lease: Optional[dict[str, Any]] = None

    @property
    def send_telemetry(self) -> bool:
        return self.record.sink_state("telemetry") == "pending"

    @property
    def ones_state(self) -> str:
        return self.record.sink_state("ones")

    def telemetry_events(self) -> list[dict[str, Any]]:
        return [dict(event) for event in (self.record.pending_events or [])] if self.send_telemetry else []


def _json_normalized(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """事件落账本前按 JSON 往返归一（派发与补发使用同一份载荷，保证逐位一致）。"""
    return json.loads(json.dumps(events, ensure_ascii=False, default=str))


def _initial_receipts(no_ones_sync: bool) -> dict[str, str]:
    """committed 初始回执（设计 §4.7.2.3 第 8 步）：两个 sink 均 pending；带 --no-ones-sync 时 ones=not-applicable。"""
    return {"telemetry": "pending", "ones": "not-applicable" if no_ones_sync else "pending"}


class BatchCommitter:
    """预写账本（含 cycles 快照与预演的闭合周期序号）→ 单次原子写 STATUS.md → 逐成员 step timing → 构建确定性事件
    → 追加 committed（持久化事件载荷 + sink 回执 + 派发租约）；命令派发后按回执结算（D-51③ / D-57② / D-58①）。"""

    def __init__(self, workspace: Path, feature: dict[str, Any], status_path: Path, ledger: FlowOpLedger) -> None:
        self.workspace = workspace
        self.feature = feature
        self.status_path = status_path
        self.feature_dir = status_path.parent
        self.ledger = ledger

    def write_status(self, text: str) -> None:
        atomic_write_text(self.status_path, text)

    def snapshot_cycles(self, members: list[BatchMember]) -> dict[str, Any]:
        return {
            str(index): _step_cycle_entries(self.feature_dir, member.id)
            for index, member in enumerate(members)
            if member.kind == "step" and not member.noop
        }

    def predict_cycles(self, members: list[BatchMember], snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            key: predict_step_cycle_closures(
                self.feature_dir,
                members[int(key)].id,
                before,
                members[int(key)].target_state,
                members[int(key)].from_state or "",
            )
            for key, before in snapshot.items()
        }

    def commit(
        self, plan: BatchPlan, request: BatchRequest, op_id: str
    ) -> tuple[list[dict[str, Any]], list[str], PendingDispatch]:
        snapshot = self.snapshot_cycles(plan.members)
        prepared = FlowOpRecord(
            op_id=op_id,
            content_digest=request.content_digest(),
            phase="prepared",
            base_revision=plan.base_revision,
            target_revision=plan.target_revision,
            members=[member.to_record() for member in plan.members],
            at=flow_now_iso(),
            no_ones_sync=request.no_ones_sync,
            cycles_before=snapshot,
            expected_cycles=self.predict_cycles(plan.members, snapshot),
        )
        self.ledger.append(prepared)
        self.write_status(plan.new_status_text)
        warnings = self.record_timings(plan.members, since=None)
        events = self.build_events(
            op_id,
            plan.members,
            prepared.cycles_by_index(),
            prepared.expected_cycles_by_index(),
            no_ones_sync=request.no_ones_sync,
        )
        pending = self._commit_pending(prepared, plan.members, events)
        return list(pending.record.pending_events or []), warnings, pending

    def _commit_pending(
        self, record: FlowOpRecord, members: list[BatchMember], events: list[dict[str, Any]]
    ) -> PendingDispatch:
        normalized = _json_normalized(events)
        lease = new_dispatch_lease(record.op_id)
        committed = record.closed(
            "committed", pending_events=normalized, receipts=_initial_receipts(record.no_ones_sync), lease=lease
        )
        try:
            self.ledger.append(committed)
        except BaseException:
            release_dispatch_lease(record.op_id, lease)
            raise
        return PendingDispatch(record=committed, members=members, lease=lease)

    def record_timings(self, members: list[BatchMember], *, since: str | None) -> list[str]:
        now = flow_now_iso()
        session_ref = flow_session_ref(self.workspace)
        warnings: list[str] = []
        for member in members:
            if member.noop or member.kind != "step":
                continue
            if since is not None and _batch_timing_recorded(self.feature_dir, member.id, member.target_state, since):
                continue
            warning = record_step_timing(
                self.feature_dir,
                member.id,
                member.target_state,
                member.from_state or "",
                now,
                session_ref=session_ref,
            )
            if warning:
                warnings.append(warning)
        return warnings

    def build_events(
        self,
        op_id: str,
        members: list[BatchMember],
        cycles_before: dict[int, list[dict[str, Any]] | None],
        expected_cycles: Optional[dict[int, list[int] | None]],
        *,
        no_ones_sync: bool,
    ) -> list[dict[str, Any]]:
        """每个非 noop 成员产出与单步逐位一致的事件（step: flow set 漏斗同款；task: flow task set 同款），
        非意图事件携带确定性 event_id。``expected_cycles`` 非 None 时 step_cycle 只发预演出的周期序号（D-57④）。"""
        events: list[dict[str, Any]] = []
        for index, member in enumerate(members):
            if member.noop:
                continue
            if member.kind == "step":
                cycle_indices = None
                if expected_cycles is not None:
                    cycle_indices = expected_cycles.get(index) or []
                member_events = flow_step_member_events(
                    self.workspace,
                    self.feature,
                    member.id,
                    member.from_state or "",
                    member.target_state,
                    trigger="set",
                    confirmed=False,
                    no_ones_sync=no_ones_sync,
                    cycles_before=cycles_before.get(index),
                    cycle_indices=cycle_indices,
                )
            else:
                member_events = [
                    flow_task_member_event(
                        self.workspace,
                        self.feature,
                        member.id,
                        member.from_state or "",
                        member.target_state,
                        is_design=True,
                        no_ones_sync=no_ones_sync,
                    )
                ]
            ordinals: dict[str, int] = {}
            for event in member_events:
                event_type = str(event.get("type"))
                if event_type in _FLOW_BATCH_INTENT_TYPES:
                    continue
                ordinal = ordinals.get(event_type, 0)
                ordinals[event_type] = ordinal + 1
                event["event_id"] = flow_batch_event_id(op_id, index, event_type, ordinal)
            events.extend(member_events)
        return events

    def recover(
        self, record: FlowOpRecord, resolution: str
    ) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], list[str], Optional[PendingDispatch]]:
        """执行 ``FlowOpLedger.reconcile`` 的判定：
        - completed（prepared 且 STATUS 已是目标态）：补写尚未落盘的 step timing → 用快照 + 预演周期序号构建事件
          → 追加 committed（持久化载荷、回执、租约）；
        - redispatch（committed、租约失效）：追加带本进程租约的 committed 接管，只补发仍为 pending 的 sink——
          事件取记录里持久化的原载荷，不按当前 timing 重算（D-57④）；
        - in-flight（committed、他人租约有效）：不接管、不补发、不报 recovered（D-58①），返回 None；
        - redispatch 已达上界（D-62，FLOW_BATCH_MAX_REDISPATCHES 次）：追加 abandoned 并告警，不补发、不报 recovered、
          不参与 ONES 意图合并；redispatch（含 abandoned）只在本次请求通过预校验后由调用方触发（Codex r4 P1）；
        - aborted / superseded：只追加闭合记录（superseded 不覆盖较新状态）。
        对外 ``resolution`` 只用接口 §3.1 的三值：redispatch 报告为 completed。"""
        if resolution == "in-flight":
            return None, [], [], None
        if resolution == "completed":
            members = record.batch_members()
            warnings = self.record_timings(members, since=record.at)
            events = self.build_events(
                record.op_id,
                members,
                record.cycles_by_index(),
                record.expected_cycles_by_index(),
                no_ones_sync=record.no_ones_sync,
            )
            pending = self._commit_pending(record, members, events)
            return (
                {"opId": record.op_id, "resolution": "completed"},
                pending.telemetry_events(),
                warnings,
                pending,
            )
        if resolution == "redispatch":
            if record.redispatches >= FLOW_BATCH_MAX_REDISPATCHES:
                self.ledger.append(record.closed("abandoned", lease=None))
                return None, [], [
                    f"批量操作 {record.op_id} 的 {_pending_sinks(record)} 已补发 {record.redispatches} 次仍未成功，"
                    "已放弃自动补发（abandoned）；请人工核对后用对应单步命令（如 devkit-flow ones sync）处理。"
                ], None
            members = record.batch_members()
            events = record.pending_events
            if events is None:  # 27694b8 写的旧 committed 记录没有载荷：按快照差分重建一次并持久化
                events = _json_normalized(
                    self.build_events(
                        record.op_id,
                        members,
                        record.cycles_by_index(),
                        record.expected_cycles_by_index(),
                        no_ones_sync=record.no_ones_sync,
                    )
                )
            lease = new_dispatch_lease(record.op_id)
            receipts = record.dispatch_receipts or _initial_receipts(record.no_ones_sync)
            takeover = record.closed(
                "committed", pending_events=events, receipts=receipts, lease=lease,
                redispatches=record.redispatches + 1,
            )
            try:
                self.ledger.append(takeover)
            except BaseException:
                release_dispatch_lease(record.op_id, lease)
                raise
            pending = PendingDispatch(record=takeover, members=members, lease=lease)
            return (
                {"opId": record.op_id, "resolution": "completed"},
                pending.telemetry_events(),
                [],
                pending,
            )
        self.ledger.append(record.closed(resolution))
        return {"opId": record.op_id, "resolution": resolution}, [], [], None


# --------------------------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------------------------


def _batch_message(members: list[BatchMember], *, dry_run: bool = False) -> str:
    noop = sum(1 for member in members if member.noop)
    if dry_run:
        return f"预校验通过：将批量更新 {len(members)} 个成员（{noop} 个 noop），未写入。"
    if noop == len(members):
        return f"批量更新 {len(members)} 个成员（{noop} 个 noop）：全部已处于目标态，未写入。"
    return f"批量更新 {len(members)} 个成员（{noop} 个 noop）。"


def _ones_follow_up_for_batch(
    workspace: Path, feature: dict[str, Any], members: list[BatchMember], *, no_ones_sync: bool
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """ONES follow-up 意图每批 1 条：含设计任务成员时同 flow task set（显式同步所选任务），否则同 flow set。"""
    task_ids: list[str] = []
    for member in members:
        if member.kind == "task" and not member.noop and member.id not in task_ids:
            task_ids.append(member.id)
    if task_ids:
        return flow_ones_follow_up(workspace, feature, task_ids, skip=no_ones_sync)
    return flow_ones_follow_up(workspace, feature, skip=no_ones_sync, sync_tasks=False)


def _ones_follow_up_for_pending(
    workspace: Path, feature: dict[str, Any], pending: list[PendingDispatch]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """本次提交与恢复出的待派发批次合并为 1 条 ONES 意图（D-51③ / Codex P2-5）：只合并 ones sink 仍为 pending 且
    未带 --no-ones-sync 的批次（D-57②：已成功的 sink 不重发）；剩余批次全部带 flag 时按 flag 跳过（与单批语义一致）。"""
    candidates = [item for item in pending if item.ones_state != "ok"]
    if not candidates:
        return None, None
    active = [item for item in candidates if item.ones_state == "pending" and not item.record.no_ones_sync]
    if not active:
        return _ones_follow_up_for_batch(workspace, feature, candidates[0].members, no_ones_sync=True)
    members = [member for item in active for member in item.members]
    return _ones_follow_up_for_batch(workspace, feature, members, no_ones_sync=False)


def _resolve_op_id(request: BatchRequest, ledger: FlowOpLedger, feature_name: str, base_revision: str) -> str:
    """缺省 opId = uuid5(feature + 成员 + 基线 revision)。D-51②：缺省 opId 不认「已提交即幂等」——
    命中已提交（committed/dispatched）记录说明那次提交已被撤销（STATUS 回到了它的基线），必须按新请求重新执行；
    为免与那次已派发事件的确定性 event_id 相撞，按序加盐派生下一个未用过的 opId（同一账本状态下确定）。"""
    derived = request.derived_op_id(feature_name, base_revision)
    candidate = derived
    salt = 0
    while True:
        existing = ledger.find(candidate)
        if existing is None or existing.phase not in _FLOW_BATCH_SUBMITTED_PHASES:
            return candidate
        salt += 1
        candidate = str(uuid.uuid5(FLOW_BATCH_NAMESPACE, f"{derived}:{salt}"))


# ONES 子系统的持久化位置（D-62）：stateOutbox 中仍待处理的条目状态、commentEvents 中仍待发送的事件状态、
# 任务行「ONES同步」列中由 ONES 自身后续 sync / bootstrap / recover 处理的前缀。
_ONES_STATE_OUTBOX_PENDING = frozenset({"retry", "queued", "needs_input"})
_ONES_COMMENT_PENDING = frozenset({"pending", "retry"})
_ONES_ROW_PERSISTED_PREFIXES = ("retry", "creating")


def _ones_task_row_cells(workspace: Path, feature: dict[str, Any]) -> dict[str, str]:
    """任务ID → 最新「ONES同步」列（设计任务表 + task.md），``requirement`` → STATUS.md 摘要「ONES同步」；读不到跳过。"""
    cells: dict[str, str] = {}
    try:
        for line in status_path_for_feature(workspace, feature).read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("**ONES同步**:"):
                cells["requirement"] = line.strip()[len("**ONES同步**:"):].strip()
                break
    except Exception:
        pass
    for loader, path in (
        (parse_design_task_table, status_path_for_feature(workspace, feature)),
        (parse_task_table, task_path_for_feature(workspace, feature)),
    ):
        try:
            rows = loader(path)["rows"] if path.exists() else []
        except Exception:
            rows = []
        for row in rows:
            cells.setdefault(str(row.get("任务ID", "")), str(row.get("ONES同步", "")).strip())
    return cells


def _ones_errors_persisted(workspace: Path, feature: dict[str, Any], errors: list[Any]) -> bool:
    """D-62：逐条核实 ONES 结果中的失败项已被 ONES 子系统持久化（后续由 ONES 自身补发 / 人工处理）：
    - ``preflight`` → stateOutbox 中 scope=STATE_OUTBOX_PREFLIGHT_SCOPE 的待处理条目；
    - ``comment`` → commentEvents 中 eventId 对应事件仍为 pending / retry；
    - 其余（requirement / 任务 ID）→ stateOutbox 中同 scope 的待处理条目，或该任务行（需求为 STATUS 摘要）「ONES同步」
      列为 retry / creating（creating 检查点：建单中断后由人工 ``ones link`` / ``ones recover-create`` 处理，自动重发
      无意义）。
    任何一项找不到（含 outbox 读不出）→ False（该 sink 保持 pending）。"""
    try:
        path = comment_outbox_path(workspace, feature)
        outbox = load_comment_outbox(path) if path.exists() else {"stateOutbox": [], "commentEvents": []}
    except Exception:
        return False
    state_scopes = {
        str(entry.get("scope", "")): True
        for entry in outbox.get("stateOutbox") or []
        if isinstance(entry, dict) and entry.get("status") in _ONES_STATE_OUTBOX_PENDING
    }
    comment_ids = {
        str(event.get("eventId", ""))
        for event in outbox.get("commentEvents") or []
        if isinstance(event, dict) and event.get("status") in _ONES_COMMENT_PENDING
    }
    row_cells: Optional[dict[str, str]] = None
    for item in errors:
        if not isinstance(item, dict):
            return False
        scope = str(item.get("scope", ""))
        if scope == "comment":
            if str(item.get("id", "")) not in comment_ids:
                return False
            continue
        if scope == "preflight":
            scope = STATE_OUTBOX_PREFLIGHT_SCOPE
        if scope in state_scopes:
            continue
        if row_cells is None:
            row_cells = _ones_task_row_cells(workspace, feature)
        if not row_cells.get(scope, "").startswith(_ONES_ROW_PERSISTED_PREFIXES):
            return False
    return True


def _ones_sink_settled(workspace: Path, feature: dict[str, Any], ones_field: Any) -> bool:
    """D-62 ONES sink 结算口径「持久化即成功」：无需同步 / synced / queued 且无失败项 → ok；``failed``（异常、入队或写
    outbox 失败、配置无效等，均未持久化）→ pending；其余（partial / clarification_needed 等）只要 ``errors`` 中每一项都
    已落 ONES 的 stateOutbox / commentEvents / retry·creating 列 → ok，否则 pending。字段澄清
    （OnesTransitionFieldsRequired）已由 ONES 记为 needs_input 并另行上报；建单确认类澄清不落 outbox、errors 为空，
    同样记 ok（人工配置门，下一次 sync 会再次出现，与单步命令一致）。"""
    if not isinstance(ones_field, dict):
        return True
    if ones_field.get("status") == "failed":
        return False
    errors = ones_field.get("errors") or []
    if not isinstance(errors, list):
        return False
    return _ones_errors_persisted(workspace, feature, errors) if errors else True


def _dispatch_outcomes(
    workspace: Path,
    feature: dict[str, Any],
    pending: list[PendingDispatch],
    data: dict[str, Any],
    receipts: Any,
    ones_dispatched: bool,
) -> list[tuple[PendingDispatch, bool, bool]]:
    """按 sink 解读派发回执（D-57② / D-61② / D-62）：
    - telemetry：装配层回执里列出的入队失败 event_id（含 outbox append 返回 False）属于哪个批次，该批次 telemetry
      保持 pending；无回执（未注入 side_effects 的测试缝，D-2）视为成功；
    - ones：按 ``_ones_sink_settled`` 的「持久化即成功」口径。"""
    failed_ids: set[str] = set()
    if isinstance(receipts, dict):
        failed_ids = {str(item) for item in receipts.get("telemetry_failed_event_ids") or []}
    ones_ok = _ones_sink_settled(workspace, feature, data.get("ones_sync")) if ones_dispatched else True
    outcomes: list[tuple[PendingDispatch, bool, bool]] = []
    for item in pending:
        telemetry_ok = not any(
            str(event.get("event_id")) in failed_ids
            for event in (item.record.pending_events or [])
            if event.get("event_id")
        )
        outcomes.append((item, telemetry_ok, ones_ok))
    return outcomes


def _settle_dispatch(
    status_path: Path, ledger: FlowOpLedger, outcomes: list[tuple[PendingDispatch, bool, bool]]
) -> list[str]:
    """派发后在锁内按回执结算（重新取锁，D-51③ / D-57②）：成功的 sink 记 ok；全部 ok/not-applicable 才追加
    dispatched，否则追加 committed（失败 sink 保持 pending、释放本进程租约），下次调用只补发失败部分。
    记账失败只告警：记录保持 committed，下次调用补发（至少一次，event_id 确定）。"""
    warnings: list[str] = []
    if not outcomes:
        return warnings
    try:
        with flow_status_write_lock(status_path, timeout=FLOW_BATCH_LOCK_TIMEOUT_SECONDS, required=True):
            latest = ledger.latest_by_op()
            for item, telemetry_ok, ones_ok in outcomes:
                current = latest.get(item.record.op_id)
                if current is None or current.phase != "committed":
                    continue
                receipts = {sink: current.sink_state(sink) for sink in FLOW_BATCH_SINKS}
                if item.send_telemetry and telemetry_ok:
                    receipts["telemetry"] = "ok"
                if item.ones_state == "pending" and ones_ok:
                    receipts["ones"] = "ok"
                held = current.dispatch_lease
                own_lease = _same_lease(held, item.lease)
                lease = None if own_lease or not dispatch_lease_active(held, current.op_id) else held
                if all(state in FLOW_BATCH_SINK_SETTLED for state in receipts.values()):
                    ledger.append(current.closed("dispatched", receipts=receipts, lease=None))
                else:
                    ledger.append(current.closed("committed", receipts=receipts, lease=lease))
                    failed = "、".join(sink for sink, state in receipts.items() if state == "pending")
                    warnings.append(
                        f"批量操作 {current.op_id} 的 {failed} 派发未成功，已保持待派发，下次调用将只补发这部分。"
                    )
    except Exception as error:  # 派发已完成；记账失败不改变结果，仅提示会在下次调用重复补发
        warnings.append(f"批量操作账本未能记录派发回执（下次调用将重复补发事件）：{error}")
    return warnings


def command_flow_batch_set(
    args: argparse.Namespace,
    *,
    dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None,
    gate_guard: Callable[[Path], Any] | None = None,
    side_effects: WriteSideEffects | None = None,
) -> int:
    request = build_batch_request(args)
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    status_path = status_path_for_feature(workspace, feature)
    planner = BatchPlanner(workspace, feature, gate_guard=gate_guard)
    planner.resolve_steps(request, status_path)

    if request.dry_run:
        base_revision, status_text = read_status_snapshot(status_path)
        plan = planner.plan(request, status_text, base_revision)
        result = FlowBatchResult(
            status="dry-run",
            message=_batch_message(plan.members, dry_run=True),
            feature=feature["feature"],
            op_id=request.op_id or request.derived_op_id(feature["feature"], plan.base_revision),
            idempotent=False,
            base_revision=plan.base_revision,
            revision=None,
            members=plan.members,
            progress=plan.progress,
        )
        emit(result.to_json(), args.format)
        return 0

    ledger = FlowOpLedger(status_path.parent / FLOW_OPS_LEDGER_FILE)
    committer = BatchCommitter(workspace, feature, status_path, ledger)
    warnings: list[str] = []  # step timing 告警（并入 step_timings.warning）
    alerts: list[str] = []  # abandoned 告警（N4-1 / N4-3：只进 warnings[] 与 stderr，不进 step_timings）
    recovered: list[dict[str, Any]] = []
    pending: list[PendingDispatch] = []
    redispatch_candidates: list[FlowOpRecord] = []
    wrote = False

    def take_over_redispatches() -> None:
        """Codex r4 P1：待派发补发的接管（redispatches +1）与达上界 abandoned 推迟到本次请求通过预校验、确实进入
        派发时才执行——预校验失败的请求不写接管记录、不消耗补发额度、不触发 abandoned。"""
        for record in redispatch_candidates:
            resolution, _events, abandoned, pending_item = committer.recover(record, "redispatch")
            if resolution is not None:
                recovered.append(resolution)
            alerts.extend(abandoned)
            if pending_item is not None:
                pending.append(pending_item)

    try:
        try:
            # D-57①：事务命令要求可保证的互斥（spec 目录 fd 锁）——不可用时失败关闭（不降级为后备锁/无锁写）。
            with flow_status_write_lock(status_path, timeout=FLOW_BATCH_LOCK_TIMEOUT_SECONDS, required=True):
                base_revision, status_text = read_status_snapshot(status_path)
                # 1) 乐观并发条件（D-51③：先校验，再收口账本；恢复不改 STATUS.md，判定口径不变）。
                if request.expect_revision and request.expect_revision != base_revision:
                    raise _batch_reject(
                        FLOW_BATCH_REVISION_CONFLICT,
                        f"STATUS.md 当前 revision {base_revision} 与 --expect-revision 不符（已被修改），请重新读取后重试。",
                    )
                # 2) 恢复检查：收口账本中未闭合记录（§4.7.2.4 + 待派发补发）。prepared 的收口照旧先做（幂等判定依赖
                #    它）；committed 待派发补发（redispatch）只记下候选，待本次请求通过预校验后再接管（Codex r4 P1）——
                #    请求被拒绝时旧批次原样保留，下次调用仍会补发；他人在途批次跳过。
                for record, verdict in ledger.reconcile(base_revision):
                    if verdict == "redispatch":
                        redispatch_candidates.append(record)
                        continue
                    resolution, _events, recovered_warnings, pending_item = committer.recover(record, verdict)
                    if resolution is not None:
                        recovered.append(resolution)
                    warnings.extend(recovered_warnings)
                    if pending_item is not None:
                        pending.append(pending_item)
                # 3) 幂等键（D-51②：只有显式 --op-id 认「已提交即幂等」）。
                existing = ledger.find(request.op_id) if request.op_id else None
                if existing is not None and existing.phase in _FLOW_BATCH_SUBMITTED_PHASES:
                    if existing.content_digest != request.content_digest():
                        raise _batch_reject(
                            FLOW_BATCH_OP_CONFLICT,
                            f"opId {request.op_id} 已于 {existing.at} 提交过内容不同的批量操作；请换用新的 --op-id。",
                        )
                    if existing.phase == "abandoned":  # N4-3：幂等命中已放弃派发的批次时显式提示
                        alerts.append(
                            f"批量操作 {request.op_id} 的派发已放弃（abandoned），{_pending_sinks(existing)} 未送达且不再"
                            "自动补发；请人工核对后用对应单步命令（如 devkit-flow ones sync）处理。"
                        )
                    take_over_redispatches()
                    result = FlowBatchResult(
                        status="ok",
                        message=f"幂等命中：批量操作 {request.op_id} 已于 {existing.at} 提交，本次未做任何写入。",
                        feature=feature["feature"],
                        op_id=str(request.op_id),
                        idempotent=True,
                        base_revision=existing.base_revision,
                        revision=existing.target_revision,
                        recovered=recovered,
                        members=existing.batch_members(),
                        progress=progress_summary_values(parse_pipeline_table_text(status_text)),
                        warnings=[*warnings, *alerts],
                    )
                else:
                    op_id = request.op_id or _resolve_op_id(request, ledger, feature["feature"], base_revision)
                    # 4) 整体预校验（零写入）；任一成员失败 → 整批拒绝，磁盘不变。
                    plan = planner.plan(request, status_text, base_revision)
                    take_over_redispatches()
                    if plan.changed:
                        _member_events, timing_warnings, pending_item = committer.commit(plan, request, op_id)
                        warnings.extend(timing_warnings)
                        pending.append(pending_item)
                        wrote = True
                    result = FlowBatchResult(
                        status="ok",
                        message=_batch_message(plan.members),
                        feature=feature["feature"],
                        op_id=op_id,
                        idempotent=False,
                        base_revision=plan.base_revision,
                        revision=plan.target_revision,
                        recovered=recovered,
                        members=plan.members,
                        progress=plan.progress,
                        warnings=[*warnings, *alerts],
                    )
        except FlowStatusLockTimeout as error:
            raise _batch_reject(FLOW_BATCH_LOCK_TIMEOUT, str(error)) from None
        except FlowStatusLockUnavailable as error:
            raise _batch_reject(FLOW_BATCH_LOCK_UNAVAILABLE, str(error)) from None
        for alert in alerts:
            _warn_stderr(alert)

        # 5) 派发（锁外）：只派发各批次仍为 pending 的 sink；事件取持久化载荷（D-57②④）。
        events: list[dict[str, Any]] = [event for item in pending for event in item.telemetry_events()]
        ones_dispatched = False
        if pending:
            ones_field, ones_intent = _ones_follow_up_for_pending(workspace, feature, pending)
            result.ones_sync = ones_field
            if ones_intent is not None:
                events.append(ones_intent)
                ones_dispatched = True
        result.events = events
        data = result.to_json()
        data["_events"] = list(events)
        timing_warning = "；".join(warning for warning in warnings if warning) or None
        attach_step_timing_warning(data, timing_warning)
        data = apply_write_side_effects(data, feature, side_effects)
        receipts = data.pop("_dispatch_receipts", None)
        settle_warnings = _settle_dispatch(
            status_path, ledger, _dispatch_outcomes(workspace, feature, pending, data, receipts, ones_dispatched)
        )
    finally:
        for item in pending:
            release_dispatch_lease(item.record.op_id, item.lease)
    if settle_warnings:
        data.setdefault("warnings", []).extend(settle_warnings)
    if wrote or recovered:
        attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0
