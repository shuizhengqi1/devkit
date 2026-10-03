from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from devkit_flow.config import load_devkit_config, save_devkit_config
from devkit_flow.constants import CLEAR_ONLY_GLOBAL, INHERIT_ONLY_PROJECT
from devkit_flow.errors import DevkitFlowError
from devkit_flow.models import E2EGateVerdict, E2ESourceConfig, QualityGateReport
from devkit_flow.parse_util import find_features, select_feature
from devkit_flow.textio import compact_error, emit


# --- e2e 准出门（W3，设计文档 §4.5；接口文档 §4.3/§4.4/§5/§6） ---------------------
# devkit 只管门：配置 resolve/层覆盖合并、quality-gate.json 只读加载、三值判门、
# e2e configure/status 命令与 doctor 只读检查。S5-E2E 置位守卫不直接挂在本模块：
# flow.py 不 import 本模块（单向依赖），由 cli.py 把 evaluate_e2e_gate 作为
# gate_guard 回调注入 apply_flow_set（D-13）。
E2E_CONFIG_SCHEMA_VERSION = 1

QUALITY_GATE_SCHEMA_VERSION = 1

# 一期唯一合法 sourceType；二期 external-repo 等新增类型各自挂同名子对象，根形状不变（§5）。
E2E_SOURCE_TYPES = ("local",)

E2E_NOT_CONFIGURED_REASON = "not-configured"

QUALITY_GATE_SUMMARY_FIELDS = ("total", "passed", "failed", "skipped")

# 失败明细必填子字段（接口文档 §6：result=fail 时 failures[].caseId/title/message 必填 string）。
QUALITY_GATE_FAILURE_FIELDS = ("caseId", "title", "message")


def _e2e_layer(config: dict[str, Any], scope: str) -> dict[str, Any] | None:
    if "e2e" not in config:
        return None
    layer = config["e2e"]
    if not isinstance(layer, dict):
        raise DevkitFlowError(f"Invalid e2e configuration ({scope} scope): expected an object")
    return layer


def _require_non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DevkitFlowError(f"invalid-summary: {name} 必须为非负 int，实际={value!r}")
    return value


def _require_iso8601(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DevkitFlowError(f"missing-field: {name}")
    try:
        datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise DevkitFlowError(f"invalid-timestamp: {name} 不是合法 ISO8601（{value!r}）") from error
    return value.strip()


def resolve_e2e_config(workspace: Path) -> E2ESourceConfig | None:
    """层覆盖合并：项目层 e2e 根键整体覆盖全局层（与 qualityGates 口径一致，D-10）。

    无 e2e 键 = 未配置（None）；配置存在但非法（形状/schemaVersion/sourceType）
    一律 DevkitFlowError fail-closed（接口文档 §9），调用方不得静默降级。
    """
    resolution = load_devkit_config(workspace)
    layer = _e2e_layer(resolution.project_config, "project")
    source = "project"
    if layer is None:
        layer = _e2e_layer(resolution.global_config, "global")
        source = "global"
    if layer is None:
        return None

    schema_version = layer.get("schemaVersion")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != E2E_CONFIG_SCHEMA_VERSION
    ):
        raise DevkitFlowError(
            f"Invalid e2e configuration ({source} scope): schemaVersion 必须为 "
            f"{E2E_CONFIG_SCHEMA_VERSION}，实际={schema_version!r}"
        )
    source_type = layer.get("sourceType")
    if source_type not in E2E_SOURCE_TYPES:
        raise DevkitFlowError(
            f"Invalid e2e configuration ({source} scope): 未知 sourceType={source_type!r}"
            f"（一期仅支持 {', '.join(E2E_SOURCE_TYPES)}）"
        )
    local = layer.get("local")
    if not isinstance(local, dict):
        raise DevkitFlowError(
            f"Invalid e2e configuration ({source} scope): local 必须为对象"
        )
    path = local.get("path")
    gate_file = local.get("gateFile")
    if not isinstance(path, str) or not path.strip():
        raise DevkitFlowError(
            f"Invalid e2e configuration ({source} scope): local.path 缺失或为空"
        )
    if not isinstance(gate_file, str) or not gate_file.strip():
        raise DevkitFlowError(
            f"Invalid e2e configuration ({source} scope): local.gateFile 缺失或为空"
        )
    return E2ESourceConfig(
        schema_version=schema_version,
        source_type=source_type,
        path=path.strip(),
        gate_file=gate_file.strip(),
        source=source,
    )


def e2e_gate_file_path(workspace: Path, config: E2ESourceConfig) -> Path:
    """gateFile 相对 local.path 解析（也接受绝对路径）；local.path 相对 workspace 解析（§5）。"""
    gate = Path(config.gate_file).expanduser()
    if gate.is_absolute():
        return gate
    base = Path(config.path).expanduser()
    if not base.is_absolute():
        base = workspace / base
    return base / gate


def load_quality_gate(path: Path) -> QualityGateReport:
    """只读加载并校验 quality-gate.json（§6 字段集）；任何契约违例 DevkitFlowError，
    由 evaluate_e2e_gate 收敛为 blocked（宁红勿假绿，D-9/D-12）。"""
    if not path.is_file():
        raise DevkitFlowError(f"gate-file-missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DevkitFlowError(f"invalid-json: {path}: {compact_error(error)}") from error
    if not isinstance(payload, dict):
        raise DevkitFlowError(f"invalid-json: {path}: root must be an object")

    schema_version = payload.get("schemaVersion")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise DevkitFlowError(f"missing-field: schemaVersion（实际={schema_version!r}）")
    if schema_version != QUALITY_GATE_SCHEMA_VERSION:
        raise DevkitFlowError(
            f"unknown-schemaVersion: {schema_version}（仅支持 {QUALITY_GATE_SCHEMA_VERSION}，不猜测语义）"
        )
    run_id = payload.get("runId")
    if not isinstance(run_id, str) or not run_id.strip():
        raise DevkitFlowError(f"missing-field: runId（实际={run_id!r}）")
    result = payload.get("result")
    if result not in ("pass", "fail"):
        raise DevkitFlowError(f"invalid-result: {result!r}（仅允许 pass/fail）")
    started_at = _require_iso8601(payload.get("startedAt"), "startedAt")
    finished_at = _require_iso8601(payload.get("finishedAt"), "finishedAt")

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise DevkitFlowError(f"missing-field: summary（实际={summary!r}）")
    counts = {
        field: _require_non_negative_int(summary.get(field), f"summary.{field}")
        for field in QUALITY_GATE_SUMMARY_FIELDS
    }
    # 统计口径归执行方：不做 total=passed+failed+skipped 交叉校验（§6）。

    feature = payload.get("feature")
    if feature is not None and not isinstance(feature, str):
        raise DevkitFlowError(f"invalid-field: feature 必须为 string（实际={feature!r}）")
    failures_raw = payload.get("failures")
    report_url = payload.get("reportUrl")
    failures: tuple[dict[str, Any], ...] = ()
    if result == "fail":
        if not isinstance(failures_raw, list):
            raise DevkitFlowError("missing-field: failures（result=fail 时必填，§6）")
        for item in failures_raw:
            if not isinstance(item, dict):
                raise DevkitFlowError(f"invalid-field: failures 元素必须为对象（实际={item!r}）")
            for field_name in QUALITY_GATE_FAILURE_FIELDS:
                value = item.get(field_name)
                if not isinstance(value, str) or not value.strip():
                    raise DevkitFlowError(
                        f"missing-field: failures[].{field_name}（result=fail 时必填非空 string，§6；实际={value!r}）"
                    )
        failures = tuple(failures_raw)
        if not isinstance(report_url, str) or not report_url.strip():
            raise DevkitFlowError("missing-field: reportUrl（result=fail 时必填，§6）")
    return QualityGateReport(
        schema_version=schema_version,
        run_id=run_id.strip(),
        feature=(feature or "").strip(),
        result=result,
        started_at=started_at,
        finished_at=finished_at,
        summary=counts,
        failures=failures,
        report_url=(report_url or "").strip() if isinstance(report_url, str) else "",
    )


def evaluate_e2e_gate(workspace: Path) -> E2EGateVerdict:
    """门判定真值表（§6）：verdict 仅 pass | fail | blocked。

    - 未配置 → blocked + reason=not-configured
    - gate 文件缺失/非法 JSON/必填缺失/未知 schemaVersion/result 非法 → blocked
    - result=fail，或 result=pass 但 summary.failed>0（宁红勿假绿）→ fail
    - result=pass 且 failed=0 → pass
    配置本身非法（JSON/schemaVersion/sourceType）fail-closed 抛出 DevkitFlowError（§9）。
    """
    config = resolve_e2e_config(workspace)
    if config is None:
        return E2EGateVerdict(verdict="blocked", reason=E2E_NOT_CONFIGURED_REASON)
    gate_path = e2e_gate_file_path(workspace, config)
    try:
        report = load_quality_gate(gate_path)
    except DevkitFlowError as error:
        return E2EGateVerdict(verdict="blocked", reason=compact_error(error))

    failed = report.summary["failed"]
    if report.result == "fail":
        return E2EGateVerdict(
            verdict="fail",
            reason=f"result=fail（failed={failed}/{report.summary['total']}）",
            failures=report.failures,
            report_url=report.report_url,
            report=report,
        )
    if failed > 0:
        # 不一致按 fail 处理（§6/D-9）：宁红勿假绿。
        return E2EGateVerdict(
            verdict="fail",
            reason=f"result=pass 但 summary.failed={failed}（不一致，按 fail 处理）",
            failures=report.failures,
            report_url=report.report_url,
            report=report,
        )
    return E2EGateVerdict(verdict="pass", report_url=report.report_url, report=report)


def e2e_config_summary(workspace: Path) -> dict[str, Any]:
    """e2e 有效配置摘要（configure/status 输出共用形状，接口文档 §4.3/§4.4）。"""
    config = resolve_e2e_config(workspace)
    if config is None:
        return {
            "configured": False,
            "source": "none",
            "schemaVersion": None,
            "sourceType": None,
            "local": None,
        }
    return {
        "configured": True,
        "source": config.source,
        "schemaVersion": config.schema_version,
        "sourceType": config.source_type,
        "local": {"path": config.path, "gateFile": config.gate_file},
    }


def _remove_e2e_layer(config: dict[str, Any]) -> None:
    config.pop("e2e", None)


def command_e2e_configure(args: argparse.Namespace) -> int:
    """套 configure 骨架（设计 §4.5.3）：load → deepcopy 目标层 → 分支 → save → emit 回读摘要。

    写入时不校验 --path/--gate-file 存在性（exit 0；text warning 并入 message），
    存在性校验归 doctor（warn）与判门（blocked），避免「先配置后落盘」被误拒。
    """
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )

    actions = sum(
        1 for active in (bool(args.source_type), bool(args.inherit), bool(args.clear)) if active
    )
    if actions > 1:
        raise DevkitFlowError(
            "--source-type 设置分支与 --inherit / --clear 互斥，一次只能指定一个动作。"
        )
    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError(INHERIT_ONLY_PROJECT)
        _remove_e2e_layer(target_config)
        message = "Project e2e override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError(CLEAR_ONLY_GLOBAL)
        _remove_e2e_layer(target_config)
        message = "Global e2e configuration cleared."
    else:
        if not args.source_type:
            raise DevkitFlowError(
                "e2e configure 必须指定 --source-type local --path <路径> --gate-file <路径>，"
                "或 --inherit（project）/ --clear（global）之一。"
            )
        if args.source_type != "local":
            raise DevkitFlowError(
                f"未知 e2e source-type: {args.source_type}（一期仅支持 local）"
            )
        if not args.path or not args.gate_file:
            raise DevkitFlowError("--source-type local 必须同时提供 --path 与 --gate-file。")
        target_config["e2e"] = {
            "schemaVersion": E2E_CONFIG_SCHEMA_VERSION,
            "sourceType": "local",
            "local": {"path": args.path, "gateFile": args.gate_file},
        }
        message = (
            f"e2e 源已配置（scope={args.scope}）：sourceType=local，"
            f"path={args.path}，gateFile={args.gate_file}。"
        )
        candidate = E2ESourceConfig(
            schema_version=E2E_CONFIG_SCHEMA_VERSION,
            source_type="local",
            path=args.path,
            gate_file=args.gate_file,
            source=args.scope,
        )
        base = Path(args.path).expanduser()
        if not base.is_absolute():
            base = workspace / base
        missing = [str(p) for p in (base, e2e_gate_file_path(workspace, candidate)) if not p.exists()]
        if missing:
            message += (
                f" 警告：以下路径当前不存在（写入不校验存在性，归 doctor/判门时校验）："
                f"{'; '.join(missing)}"
            )

    save_devkit_config(workspace, target_config, args.scope)
    emit(
        {
            "status": "configured",
            "message": message,
            "e2e": e2e_config_summary(workspace),
        },
        args.format,
    )
    return 0


def _selected_feature_name(workspace: Path) -> str:
    """e2e status 的防误配提示（§4.4）：当前选中 feature 名；无进行中 feature 时返回空。"""
    features = find_features(workspace)
    if not features:
        return ""
    selected = select_feature(features, None)
    if selected.get("status") != "ok":
        return ""
    return str(selected.get("feature", ""))


def command_e2e_status(args: argparse.Namespace) -> int:
    """有效合并配置 + 门判定摘要（§4.4）；不接收 --feature。gate.feature 与当前
    选中 feature 不一致时输出 warn 字段（不阻断，防误配提示）。"""
    workspace = Path.cwd()
    verdict = evaluate_e2e_gate(workspace)
    gate: dict[str, Any] = {"verdict": verdict.verdict}
    if verdict.reason:
        gate["reason"] = verdict.reason
    report = verdict.report
    if report is not None:
        gate["runId"] = report.run_id
        gate["result"] = report.result
        gate["finishedAt"] = report.finished_at
        gate["reportUrl"] = report.report_url
    data: dict[str, Any] = {
        "status": "ok",
        "e2e": e2e_config_summary(workspace),
        "gate": gate,
    }
    if report is not None and report.feature:
        current = _selected_feature_name(workspace)
        if current and report.feature != current:
            data["warn"] = (
                f"gate.feature={report.feature} 与当前选中 feature {current} 不一致"
                "（防误配提示，不阻断）。"
            )
    summary = data["e2e"]
    if not summary["configured"]:
        # Q1=A：未配置不生成 S5-E2E 行。verdict=blocked / reason=not-configured
        # 语义保留给「行存在 + 未配置」的置 done 守卫，不新增第四态。
        data["message"] = (
            f"e2e 未配置；流水线不生成 S5-E2E 行（Q1=A）；"
            f"gate verdict=blocked（{verdict.reason}）。"
        )
    else:
        local = summary["local"]
        data["message"] = (
            f"e2e 已配置（source={summary['source']}，sourceType={summary['sourceType']}，"
            f"path={local['path']}，gateFile={local['gateFile']}）；"
            f"gate verdict={verdict.verdict}"
            + (f"（{verdict.reason}）" if verdict.reason else "")
            + "。"
        )
    emit(data, args.format)
    return 0


def e2e_doctor(workspace: Path) -> dict[str, Any]:
    """doctor 只读检查项（§4.7）：配置可读且 sourceType 合法 → ok（附 sourceType 与
    gateFile）；gateFile 当前不存在 → warn；配置非法经 DevkitFlowError 由调用方
    按既有配置项同口径 fail-closed 处理。不自动修复。"""
    config = resolve_e2e_config(workspace)
    if config is None:
        return {"status": "ok", "message": "not configured (optional)"}
    gate_path = e2e_gate_file_path(workspace, config)
    detail = f"sourceType={config.source_type}, gateFile={gate_path}"
    if not gate_path.is_file():
        return {
            "status": "warn",
            "message": f"configured (source: {config.source}, {detail}) but gateFile 当前不存在",
        }
    return {
        "status": "ok",
        "message": f"configured (source: {config.source}, {detail})",
    }
