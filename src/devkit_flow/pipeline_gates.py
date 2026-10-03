from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping
from devkit_flow.config import quality_gate_summary
from devkit_flow.constants import (
    ALL_MODE_STEP_IDS,
    COMMON_STEP_IDS,
    COMPLETION_GATE_STEPS,
    CONFIG_GATED_STEPS,
    PIPELINE_COMMON_ROWS,
    PIPELINE_MODE_ROWS,
    PIPELINE_SCALE_STEPS,
)
from devkit_flow.e2e import e2e_config_summary, resolve_e2e_config
from devkit_flow.errors import DevkitFlowError


_OR_SPLIT = re.compile(r"\s*或\s*")
_PARENTHETICAL = re.compile(r"[（(][^)）]*[)）]")

_DEPENDS_ON_INDEX = 3


@dataclass(frozen=True)
class GateState:
    enabled: bool
    source: str


@dataclass(frozen=True)
class PipelineConfigSnapshot:
    """配置门快照。内部键为连字符形态（fund-security）；JSON 投影为 snake_case。"""

    gates: Mapping[str, GateState]

    def __post_init__(self) -> None:
        validate_snapshot_gate_keys(self.gates)

    def gate(self, name: str) -> GateState:
        validate_snapshot_gate_keys(self.gates)
        return self.gates[name]

    def as_json(self) -> dict[str, dict[str, object]]:
        return {
            json_gate_key(name): {"enabled": state.enabled, "source": state.source}
            for name, state in self.gates.items()
        }


@dataclass(frozen=True)
class DependencyRewrite:
    """migrate-steps 依赖重写记录。属性名避让 Python 保留字 from。"""

    step: str
    from_dep: str
    to_dep: str

    def as_json(self) -> dict[str, str]:
        return {"step": self.step, "from": self.from_dep, "to": self.to_dep}


def json_gate_key(internal_name: str) -> str:
    """内部/CLI 连字符门名 → JSON/错误码 snake_case。禁止反向统一。"""
    return internal_name.replace("-", "_")


def validate_snapshot_gate_keys(gates: Mapping[str, GateState]) -> None:
    """快照键必须与 CONFIG_GATED_STEPS 连字符键精确一致；未知键或 snake_case 内部键直接报错。"""
    expected = set(CONFIG_GATED_STEPS)
    actual = set(gates)
    for key in sorted(actual - expected):
        hyphen = key.replace("_", "-")
        if "_" in key and hyphen in expected:
            raise KeyError(
                f"snapshot gate key {key!r} is snake_case; use internal key {hyphen!r}"
            )
        raise KeyError(f"unknown config-gated quality gate: {key}")
    missing = expected - actual
    if missing:
        raise KeyError(
            "missing config-gated quality gate: " + ", ".join(sorted(missing))
        )


def pipeline_config_snapshot(workspace: Path) -> PipelineConfigSnapshot:
    """聚合 e2e / fund-security 配置为门快照（Q1=A / D-1）。"""
    gates: dict[str, GateState] = {}
    for gate_name in CONFIG_GATED_STEPS:
        gates[gate_name] = _read_gate_state(workspace, gate_name)
    return PipelineConfigSnapshot(gates=gates)


def effective_pipeline_rows(
    base_rows: list[list[str]], snapshot: PipelineConfigSnapshot
) -> list[list[str]]:
    """剔除关闭门受控行并压缩依赖（设计 §4.2.2 / §4.2.3）。"""
    return compress_dependencies(base_rows, disabled_gated_step_ids(snapshot))


def compress_dependencies(
    rows: list[list[str]], removed_ids: Iterable[str]
) -> list[list[str]]:
    """生成期依赖压缩（D-4）：缺失行的下游直接接其上游，传递闭包后去重保序。"""
    removed = {step_id for step_id in removed_ids if step_id}
    removed_dep = {
        row[0]: row[_DEPENDS_ON_INDEX]
        for row in rows
        if row and row[0] in removed and len(row) > _DEPENDS_ON_INDEX
    }
    compressed: list[list[str]] = []
    for row in rows:
        if not row or row[0] in removed:
            continue
        copied = list(row)
        if len(copied) > _DEPENDS_ON_INDEX:
            copied[_DEPENDS_ON_INDEX] = _rewrite_depends_on(
                copied[_DEPENDS_ON_INDEX], removed_dep
            )
        compressed.append(copied)
    return compressed


def scale_step_ids(scale: str) -> set[str]:
    """PIPELINE_SCALE_STEPS[scale] 的成员校验入口（016 D-40）：非法 scale 抛结构化 DevkitFlowError，
    与 flow migrate-steps 同文案，禁止裸 KeyError 回溯穿透到 flow next/verify/complete。
    调用方应先经 flow.feature_scale() 归一（strip/lower/空值默认 large）；此处只做最终成员校验。"""
    if scale not in PIPELINE_SCALE_STEPS:
        raise DevkitFlowError(f"STATUS.md 规模无效: {scale or '(空)'}。")
    return set(PIPELINE_SCALE_STEPS[scale])


def expected_completion_gate_steps(
    snapshot: PipelineConfigSnapshot, scale: str
) -> tuple[str, ...]:
    """COMPLETION_GATE_STEPS 过滤关闭门受控行 + 规模裁剪掉的行，保持原顺序。常量本身不变。

    016 D-18：scale 为必填位置参（漏传即 TypeError，fail-closed）。small 档去 S5.7（D-30），
    故完成门 = {S5-R}(+启用 S5-E2E)；large/large-no-context = {S5-R, S5.7}(+S5-E2E)。
    直接以 scale 表判定该完成门步骤是否属当前档，无需 mode（scale 表已枚举保留的模式行 id）。
    016 D-40：非法 scale 取值走 scale_step_ids 结构化报错（运行期 fail-closed，不裸 KeyError）。
    """
    scale_selected = scale_step_ids(scale)
    removed = disabled_gated_step_ids(snapshot)
    return tuple(
        step_id
        for step_id in COMPLETION_GATE_STEPS
        if step_id not in removed and step_id in scale_selected
    )


def missing_required_rows(
    existing_ids: Iterable[str],
    scale: str,
    mode: str | None,
    snapshot: PipelineConfigSnapshot,
) -> list[list[str]]:
    """canonical 有效行集相对现有行集的差集（migrate-steps / verify 共用）。"""
    existing = {step_id for step_id in existing_ids if step_id}
    return [
        row
        for row in canonical_effective_rows(scale, mode, snapshot)
        if row[0] not in existing
    ]


def disabled_gated_step_ids(snapshot: PipelineConfigSnapshot) -> frozenset[str]:
    validate_snapshot_gate_keys(snapshot.gates)
    removed: list[str] = []
    for gate_name, step_ids in CONFIG_GATED_STEPS.items():
        if not snapshot.gates[gate_name].enabled:
            removed.extend(step_ids)
    return frozenset(removed)


def _read_gate_state(workspace: Path, gate_name: str) -> GateState:
    if gate_name == "e2e":
        configured = resolve_e2e_config(workspace) is not None
        summary = e2e_config_summary(workspace)
        return GateState(enabled=configured, source=str(summary.get("source") or "none"))
    if gate_name == "fund-security":
        summary = quality_gate_summary(workspace)
        return GateState(
            enabled=bool(summary.get("enabled", True)),
            source=str(summary.get("source") or "default"),
        )
    raise KeyError(f"unknown config-gated quality gate: {gate_name}")


def _scale_removed_step_ids(scale: str, mode: str | None) -> frozenset[str]:
    """按规模裁剪掉的 step id 集：全集 = COMMON ∪ MODE[mode]（016 F1-a：覆盖模式行）。

    mode 必填。mode 已确认时全集取 COMMON ∪ MODE[mode]；mode 未确认（None / 不可解析）时取
    COMMON ∪ ALL_MODE_STEP_IDS（两种模式并集，016 D-40）——否则存量 feature 的 `模式` 头为
    `pending`（feature init 自写字面量）时 S5.7 不在全集内、D-32 归档静默失效。多余的 removed id
    对 compress_dependencies 无副作用（只对 rows 中实际存在的行建 removed_dep），canonical 行集不变。
    补集 = 全集 − PIPELINE_SCALE_STEPS[scale]；scale 表已枚举保留的模式行 id，故 large 不丢 S5.7、
    small 去 S5.7 而留 S5-R 与配置门。非法 scale 走 scale_step_ids 结构化报错。
    """
    selected = scale_step_ids(scale)
    present: list[str] = list(COMMON_STEP_IDS)
    if mode in PIPELINE_MODE_ROWS:
        present.extend(row[0] for row in PIPELINE_MODE_ROWS[mode])
    else:
        present.extend(ALL_MODE_STEP_IDS)
    return frozenset(step_id for step_id in present if step_id not in selected)


def scale_narrowed_step_ids(scale: str, mode: str | None) -> frozenset[str]:
    """016 D-32：因 scale 收窄而多余的 step id 集（COMMON∪MODE − PIPELINE_SCALE_STEPS[scale]），
    显式排除 CONFIG_GATED_STEPS。

    用于 flow complete/verify/next 判定「存量 feature 经 migrate-steps（只插不删）留下、且已不属当前档」
    的遗留物理行是否应自动归档（complete）、预警（verify）或不作为断点（next/dashboard，D-40）。
    **安全边界**：配置门受控行（S5-E2E/S5.1-F/S5.2-F）由 config 轴决定去留，D-16 不变式保证其恒在
    scale 表内、不落入 _scale_removed_step_ids——因此下方减法在 D-16 成立时是 no-op（S5.7 P2-8 实测
    6 组 removed≡narrowed）；保留它作纵深防御：若 D-16 被破坏，自动归档也永不静默跳过资金安全/e2e 门。
    """
    removed = _scale_removed_step_ids(scale, mode)
    config_ids = {step_id for ids in CONFIG_GATED_STEPS.values() for step_id in ids}
    return frozenset(removed - config_ids)


def canonical_effective_rows(
    scale: str, mode: str | None, snapshot: PipelineConfigSnapshot
) -> list[list[str]]:
    """全量 canonical 行集上合并 scale/config 剔除 ID，压缩后再过滤输出。

    必须带着被剔除行参与压缩：否则 large-no-context 下 S2.1 仍会依赖已删的 S2.0。
    """
    rows = [list(row) for row in PIPELINE_COMMON_ROWS]
    if mode in PIPELINE_MODE_ROWS:
        rows.extend(list(row) for row in PIPELINE_MODE_ROWS[mode])
    removed = _scale_removed_step_ids(scale, mode) | set(disabled_gated_step_ids(snapshot))
    return compress_dependencies(rows, removed)


def _token_step_id(token: str) -> str:
    return _PARENTHETICAL.sub("", token).strip()


def _parse_alternatives(cell: str) -> list[list[str]]:
    text = cell.strip()
    if not text or text == "-":
        return []
    alternatives: list[list[str]] = []
    for part in _OR_SPLIT.split(text):
        tokens = [token.strip() for token in part.split(",") if token.strip()]
        if tokens:
            alternatives.append(tokens)
    return alternatives


def _rewrite_depends_on(cell: str, removed_dep: Mapping[str, str]) -> str:
    if not removed_dep:
        return cell
    alternatives = _parse_alternatives(cell)
    if not alternatives:
        return cell
    if not any(
        _token_step_id(token) in removed_dep
        for alternative in alternatives
        for token in alternative
    ):
        return cell
    expanded = _expand_alternatives(alternatives, removed_dep, frozenset())
    if not expanded:
        return "-"
    rendered = [",".join(tokens) for tokens in expanded]
    if len(rendered) == 1:
        return rendered[0]
    return " 或 ".join(rendered)


def _expand_alternatives(
    alternatives: list[list[str]],
    removed_dep: Mapping[str, str],
    stack: frozenset[str],
) -> list[list[str]]:
    expanded: list[list[str]] = []
    for tokens in alternatives:
        working: list[list[str]] = [[]]
        for token in tokens:
            working = [
                head + tail
                for head in working
                for tail in _expand_token(token, removed_dep, stack)
            ]
        expanded.extend(working)
    return _dedupe_alternatives(expanded)


def _expand_token(
    token: str, removed_dep: Mapping[str, str], stack: frozenset[str]
) -> list[list[str]]:
    step_id = _token_step_id(token)
    if step_id not in removed_dep or step_id in stack:
        return [[token]] if step_id not in removed_dep else [[]]
    nested = _parse_alternatives(removed_dep[step_id])
    if not nested:
        return [[]]
    return _expand_alternatives(nested, removed_dep, stack | {step_id})


def _dedupe_alternatives(alternatives: list[list[str]]) -> list[list[str]]:
    unique: list[list[str]] = []
    seen_alts: set[tuple[str, ...]] = set()
    for tokens in alternatives:
        deduped: list[str] = []
        seen_tokens: set[str] = set()
        for token in tokens:
            key = _token_step_id(token) or token
            if not key or key in seen_tokens:
                continue
            seen_tokens.add(key)
            deduped.append(token)
        alt_key = tuple(_token_step_id(token) or token for token in deduped)
        if not alt_key or alt_key in seen_alts:
            continue
        seen_alts.add(alt_key)
        unique.append(deduped)
    return unique
