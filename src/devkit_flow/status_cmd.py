from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from devkit_flow.config import context_source_summary, format_review_gate_timing_status_text, load_config_file, load_review_gate_timing_summary, ones_config_summary, quality_gate_summary
from devkit_flow.constants import CONFIG_PATH
from devkit_flow.errors import DevkitFlowError
from devkit_flow.constants import TASK_CLOSED_STATES, TASK_DESC_MAX_CHARS, TASK_ITEMS_LIMIT
from devkit_flow.flow_batch import FLOW_OPS_LEDGER_FILE, FlowOpLedger
from devkit_flow.parse_util import find_features, parse_task_table, select_feature, task_path_for_feature
from devkit_flow.project_detect import ProfileCache, ProjectDetector
from devkit_flow.review import design_template_summary, format_design_template_status_text, format_review_rounds_status_text, review_rounds_summary
from devkit_flow.rule_sources import RULES_OK_STATUSES, BindingReceipt, compute_rule_context
from devkit_flow.switches import dashboard_switch_summary, docs_curator_summary, format_dashboard_status_text, format_docs_curator_status_text, format_html_ui_status_text, format_progress_bar_status_text, format_spec_artifacts_status_text, format_specrules_status_text, html_ui_summary, progress_bar_summary, spec_artifacts_summary, specrules_routing_preview, specrules_summary
from devkit_flow.telemetry import telemetry_config_summary
from devkit_flow.textio import emit_update_notice, load_support_file_readonly, maybe_check_latest_update, runtime_support_root, version_tuple
from devkit_flow.workflow_auto import format_workflow_status_text, workflow_auto_takeover_summary

# 纯数字点分（2.45.0）。带预发布后缀的字符串在 version_tuple 之前被拒绝。
_DOTTED_VERSION = re.compile(r"^\d+(?:\.\d+)+$")
_JOURNAL_READER: Any = None


def lifecycle_state_root() -> Path:
    """与 runtime.runtime_lifecycle_controller 相同的默认根，只拼路径不创建目录。"""
    return Path.home() / ".config" / "devkit" / "runtime-lifecycle"


_SCAN_MISSING_ERRORS = (FileNotFoundError, NotADirectoryError)


def _scan_child_names(directory: Path, *, want_dir: bool) -> tuple[list[str], bool]:
    """子项名与扫描是否失败：目录不存在 / 不是目录 → ``([], False)``；存在但扫描失败（权限等 OSError）→ ``([], True)``。

    与 ``journal_query._scan`` 同口径（missing 与 scan failure 区分，016 D-98 ⑤）；单个条目类型判定失败仍跳过。
    """
    names: list[str] = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                try:
                    matched = entry.is_dir(follow_symlinks=False) if want_dir else entry.is_file(follow_symlinks=False)
                except OSError:
                    continue
                if matched:
                    names.append(entry.name)
    except _SCAN_MISSING_ERRORS:
        return [], False
    except OSError:
        return [], True
    return names, False


def _journal_transaction_scan(state_root: Path | None = None) -> tuple[bool, bool]:
    """默认根与 experimental 根下 ``(是否已有事务 *.json, 是否有已存在目录扫描失败)``。缺失目录按没有处理，不 mkdir。

    扫描失败时继续扫描其余目录（不提前返回），使「目录不存在 → 正常零」与「目录存在但读不了」可区分。
    """
    root = lifecycle_state_root() if state_root is None else Path(state_root)
    found = False
    failed = False
    for base in (root, root / "experimental"):
        runtimes, base_failed = _scan_child_names(base, want_dir=True)
        failed = failed or base_failed
        for runtime in runtimes:
            if runtime == "experimental":
                continue
            transactions = base / runtime / "journal" / "transactions"
            names, tx_failed = _scan_child_names(transactions, want_dir=False)
            failed = failed or tx_failed
            if any(name.endswith(".json") for name in names):
                found = True
    return found, failed


def journal_has_transaction_json(state_root: Path | None = None) -> bool:
    """默认根与 experimental 根下是否已有事务 ``*.json``。缺失目录按没有处理，不 mkdir。"""
    found, _failed = _journal_transaction_scan(state_root)
    return found


def journal_reader() -> Any:
    """按文件路径加载 LifecycleJournalReader。支撑树缺失或加载失败时返回 None，不写 bytecode。"""
    global _JOURNAL_READER
    if _JOURNAL_READER is not None:
        return _JOURNAL_READER
    support = runtime_support_root("runtime_lifecycle")
    if support is None:
        return None
    try:
        module = load_support_file_readonly(
            support,
            "runtime_lifecycle/journal_query.py",
            "devkit_runtime_journal_query",
        )
    except (ImportError, OSError, SyntaxError):
        return None
    _JOURNAL_READER = module.LifecycleJournalReader()
    return _JOURNAL_READER


def status_project_profile(workspace: Path) -> dict[str, Any]:
    """缓存快速口径。不识别、不写缓存。无缓存时 classification / fingerprint 为 null。"""
    cache = ProfileCache()
    state = cache.quickState(workspace)
    empty = {"state": "not-detected", "classification": None, "fingerprint": None}
    if state == "not-detected":
        return empty
    profile = cache.read(workspace)
    if profile is None:
        return empty
    return {
        "state": state,
        "classification": profile.classification,
        "fingerprint": profile.fingerprint,
    }


def status_rule_routing(workspace: Path) -> dict[str, Any]:
    """路由预览只公开 mode。画像取自缓存，allowImplicitAdoption=false。"""
    try:
        routing, _binding = specrules_routing_preview(workspace)
    except Exception:
        return {"mode": None}
    if not isinstance(routing, dict):
        return {"mode": None}
    return {"mode": routing.get("mode")}


LIFECYCLE_UNREADABLE = "unreadable"
LIFECYCLE_REASON_READER_UNAVAILABLE = "reader-unavailable"
LIFECYCLE_REASON_READ_ERROR = "read-error"
LIFECYCLE_REASON_UNREADABLE_ENTRIES = "unreadable-entries"


def _lifecycle_unreadable(body: dict[str, Any], reason: str, unreadable_entries: int | None) -> dict[str, Any]:
    """不可读状态只加键（与 flow_ops 同用 ``"unreadable"`` 口径，016 D-96 ④ / P2-5）。

    ``read_state="unreadable"`` 使「账本不可读」与「确无未完成事务」可区分；``unreadable_entries`` 为不可读条目数，
    整体读取失败（reader 不可用 / 读取异常）时无法计数 → ``None``；``unreadable_reason`` 与 doctor
    ``runtime-lifecycle-transactions`` 的 warn 原因一一对应。``unfinished_total`` 仅统计可读的未完成事务。
    """
    body["read_state"] = LIFECYCLE_UNREADABLE
    body["unreadable_reason"] = reason
    body["unreadable_entries"] = unreadable_entries
    return body


def status_runtime_lifecycle(state_root: Path | None = None) -> dict[str, Any]:
    """未完成事务计数。没有任何事务 json 时不加载 journal_query。

    可读且无不可读条目时输出保持 ``{"unfinished_total", "runtimes"}`` 两键不变；事务目录存在但前置扫描失败、
    reader 不可用、读取异常或存在 ``unreadable`` 条目时追加 ``read_state`` / ``unreadable_reason`` / ``unreadable_entries``（不折算为“零未完成”）。
    """
    root = lifecycle_state_root() if state_root is None else Path(state_root)
    has_json, scan_failed = _journal_transaction_scan(root)
    if scan_failed:
        # 事务目录存在但扫描失败（PermissionError 等）：不得折算为“零未完成”（016 D-98 ⑤），与目录不存在区分。
        return _lifecycle_scan_failed(root, has_json)
    if not has_json:
        return {"unfinished_total": 0, "runtimes": []}
    reader = journal_reader()
    if reader is None:
        return _lifecycle_unreadable({"unfinished_total": 0, "runtimes": []}, LIFECYCLE_REASON_READER_UNAVAILABLE, None)
    try:
        entries = reader.unfinished(root)
    except Exception:
        return _lifecycle_unreadable({"unfinished_total": 0, "runtimes": []}, LIFECYCLE_REASON_READ_ERROR, None)
    return _lifecycle_body_from_entries(entries)


def _lifecycle_scan_failed(root: Path, has_json: bool) -> dict[str, Any]:
    """前置扫描失败 → ``read_state=unreadable``、``unreadable_reason=read-error``。

    其余可扫描目录已有事务 json 时仍经 reader 统计可读的未完成事务（不可读部分不得掩盖可读部分）；
    reader 不可用 / 异常或无可读事务 json 时 ``unreadable_entries`` 无法计数 → ``None``。
    """
    zero: dict[str, Any] = {"unfinished_total": 0, "runtimes": []}
    if not has_json:
        return _lifecycle_unreadable(zero, LIFECYCLE_REASON_READ_ERROR, None)
    reader = journal_reader()
    if reader is None:
        return _lifecycle_unreadable(zero, LIFECYCLE_REASON_READ_ERROR, None)
    try:
        entries = reader.unfinished(root)
    except Exception:
        return _lifecycle_unreadable(zero, LIFECYCLE_REASON_READ_ERROR, None)
    body = _lifecycle_body_from_entries(entries)
    count = body.get("unreadable_entries") if body.get("read_state") == LIFECYCLE_UNREADABLE else None
    return _lifecycle_unreadable(
        {"unfinished_total": body["unfinished_total"], "runtimes": body["runtimes"]},
        LIFECYCLE_REASON_READ_ERROR,
        count,
    )


def _lifecycle_body_from_entries(entries: Any) -> dict[str, Any]:
    """reader 条目 → 计数主体；存在 ``unreadable`` 条目时追加不可读三键（reason=unreadable-entries）。"""
    names: list[str] = []
    total = 0
    unreadable = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("state") == LIFECYCLE_UNREADABLE:
            unreadable += 1
            continue
        if entry.get("state") != "unfinished":
            continue
        total += 1
        runtime = entry.get("runtime")
        if isinstance(runtime, str) and runtime not in names:
            names.append(runtime)
    body: dict[str, Any] = {"unfinished_total": total, "runtimes": names}
    if unreadable:
        return _lifecycle_unreadable(body, LIFECYCLE_REASON_UNREADABLE_ENTRIES, unreadable)
    return body


def format_runtime_lifecycle_status_text(lifecycle: Any) -> str | None:
    """文本模式只在不可读时追加一行；可读时返回 None（R-24：无异常输出逐字节不变）。"""
    if not isinstance(lifecycle, dict) or lifecycle.get("read_state") != LIFECYCLE_UNREADABLE:
        return None
    entries = lifecycle.get("unreadable_entries")
    count = "unknown" if entries is None else str(entries)
    return (
        f"lifecycle journal: unreadable (reason={lifecycle.get('unreadable_reason')}, "
        f"unreadable_entries={count}, unfinished_readable={lifecycle.get('unfinished_total')}); "
        "see devkit-flow doctor"
    )


FLOW_OPS_UNREADABLE = "unreadable"


def status_flow_ops(workspace: Path, selection: dict[str, Any]) -> int | str:
    """当前 feature 未闭合批量记录数，不加 STATUS 写锁、只读。

    无单一 feature 或账本不存在 → 0；账本存在但无法判定（损坏 / 半写 / 不可读 / 被占成目录）→
    ``"unreadable"``（016 T-26，D-75 / F-008：不得吞成 0，否则与“无未闭合记录”不可区分）。
    """
    if not isinstance(selection, dict) or selection.get("status") != "ok":
        return 0
    feature_dir = selection.get("feature_dir")
    if not isinstance(feature_dir, str) or not feature_dir:
        return 0
    ledger_path = Path(workspace) / feature_dir / FLOW_OPS_LEDGER_FILE
    try:
        if not ledger_path.exists() and not ledger_path.is_symlink():
            return 0
        if not ledger_path.is_file():
            return FLOW_OPS_UNREADABLE
        return len(FlowOpLedger(ledger_path).open_records())
    except (DevkitFlowError, OSError):
        return FLOW_OPS_UNREADABLE


def doctor_detect_project(workspace: Path) -> tuple[Any, Exception | None]:
    """doctor 内唯一一次项目识别（D-73 ①）：返回 ``(profile, None)`` 或 ``(None, error)``，不写 ProfileCache。

    ``project-detection`` 与 ``rule-binding`` 两项检查共用这份内存画像，避免两次独立扫描各自重置预算。
    """
    try:
        return ProjectDetector().detect(workspace), None
    except Exception as error:
        return None, error


def doctor_project_detection(workspace: Path, detection: tuple[Any, Exception | None] | None = None) -> dict[str, Any]:
    """内存识别，不写 ProfileCache。只有检测抛错或预算耗尽才 warn。``detection`` 缺省时自行识别一次。"""
    profile, error = doctor_detect_project(workspace) if detection is None else detection
    if profile is None:
        return {
            "status": "warn",
            "message": f"project detection failed: {type(error).__name__}",
            "fix": "devkit-flow project detect",
            "detail": {"classification": None, "status": None, "fingerprint": None, "modules": []},
        }
    detail = {
        "classification": profile.classification,
        "status": profile.status,
        "fingerprint": profile.fingerprint,
        "modules": [
            {"id": module.id, "classification": module.classification}
            for module in profile.modules
        ],
    }
    exhausted = bool(profile.budget.exhausted or profile.status == "budget-exhausted")
    if exhausted:
        return {
            "status": "warn",
            "message": "project detection budget exhausted; profile is partial",
            "fix": "devkit-flow project detect",
            "detail": detail,
        }
    return {
        "status": "ok",
        "message": f"project detection {profile.classification} ({profile.status})",
        "fix": "",
        "detail": detail,
    }


def doctor_rule_binding(workspace: Path, detection: tuple[Any, Exception | None] | None = None) -> dict[str, Any]:
    """只读规则预览。legacy-diagnostic 不告警；explicit 且绑定状态越出 bound / unbound-no-kind 才 warn。

    ``detection`` 为 :func:`doctor_detect_project` 的结果时复用其画像、不再识别；识别已失败则跳过预览（不重试）。
    """
    empty = {"routingMode": None, "routingSource": None, "adoptedBy": None, "modules": []}
    profile = None
    if detection is not None:
        profile = detection[0]
        if profile is None:
            return {
                "status": "warn",
                "message": "rule binding preview skipped: project detection failed",
                "fix": "devkit-flow project detect",
                "detail": empty,
            }
    try:
        context, decision = compute_rule_context(workspace, profile=profile)
    except Exception as error:
        return {
            "status": "warn",
            "message": f"rule binding preview failed: {type(error).__name__}",
            "fix": "devkit-flow project rules",
            "detail": empty,
        }
    modules = [
        {
            "module": item.binding.module,
            "kind": item.binding.kind,
            "bindingStatus": item.binding.bindingStatus,
        }
        for item in context.modules
    ]
    detail = {
        "routingMode": context.routingMode,
        "routingSource": context.routingSource,
        "adoptedBy": context.adoptedBy,
        "modules": modules,
    }
    explicit_bad = (
        decision.mode == "adopted"
        and decision.adoptedBy == "explicit"
        and any(item["bindingStatus"] not in RULES_OK_STATUSES for item in modules)
    )
    if explicit_bad:
        return {
            "status": "warn",
            "message": "explicit rule binding has modules outside bound/unbound-no-kind",
            "fix": "devkit-flow project rules",
            "detail": detail,
        }
    return {
        "status": "ok",
        "message": f"rule binding {context.routingMode} ({context.routingSource})",
        "fix": "",
        "detail": detail,
    }


def doctor_runtime_transactions(state_root: Path | None = None) -> dict[str, Any]:
    """两根未完成事务。unreadable 条目单独 warn，不把损坏文件当成未完成事务。"""
    root = lifecycle_state_root() if state_root is None else Path(state_root)
    reader = journal_reader()
    if reader is None:
        return {
            "status": "warn",
            "message": "lifecycle journal reader is unavailable",
            "fix": "",
            "detail": {"entries": []},
        }
    try:
        raw_entries = reader.unfinished(root)
    except Exception as error:
        return {
            "status": "warn",
            "message": f"lifecycle journal is unreadable: {type(error).__name__}",
            "fix": "",
            "detail": {"entries": []},
        }
    entries: list[dict[str, Any]] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        entries.append({
            "runtime": entry.get("runtime"),
            "root": entry.get("root"),
            "state": entry.get("state"),
            "transactionId": entry.get("transactionId"),
            "operation": entry.get("operation"),
            "status": entry.get("status"),
            "createdAt": entry.get("createdAt"),
        })
    lines: list[str] = []
    for entry in entries:
        if entry["state"] != "unfinished":
            continue
        runtime = entry["runtime"] if isinstance(entry["runtime"], str) else "-"
        force = " --force" if entry["root"] == "experimental" else ""
        lines.append(
            f"{entry['root']} {runtime} {entry['transactionId']} {entry['operation']} "
            f"{entry['status']} {entry['createdAt']}; "
            f"devkit-flow runtime recover --runtime {runtime}{force}"
        )
    if any(entry["state"] == "unreadable" for entry in entries):
        lines.append("transaction journal has unreadable entries")
    if lines:
        return {
            "status": "warn",
            "message": "; ".join(lines),
            "fix": "",
            "detail": {"entries": entries},
        }
    return {
        "status": "ok",
        "message": "no unfinished lifecycle transactions",
        "fix": "",
        "detail": {"entries": entries},
    }


def relate_source_checkout(source_version: str | None, cli_version: str) -> dict[str, Any]:
    """文件已经确认存在之后的版本关系。任一端不是纯数字点分则 unreadable，不抛 ValueError。"""
    readable_source = source_version if isinstance(source_version, str) else None
    if (
        not isinstance(source_version, str)
        or _DOTTED_VERSION.fullmatch(source_version) is None
        or not isinstance(cli_version, str)
        or _DOTTED_VERSION.fullmatch(cli_version) is None
    ):
        return {"state": "unreadable", "source_version": readable_source, "relation": None}
    try:
        source_tuple = version_tuple(source_version)
        cli_tuple = version_tuple(cli_version)
    except ValueError:
        return {"state": "unreadable", "source_version": source_version, "relation": None}
    if source_tuple > cli_tuple:
        relation = "ahead"
    elif source_tuple < cli_tuple:
        relation = "behind"
    else:
        relation = "equal"
    return {"state": "detected", "source_version": source_version, "relation": relation}


def _package_spec_tail(facts: dict[str, Any] | None) -> str | None:
    if not isinstance(facts, dict):
        return None
    spec = facts.get("packageSpec")
    if not isinstance(spec, str) or "@" not in spec:
        return None
    tail = spec.rsplit("@", 1)[-1]
    return tail or None


def _provenance_row(runtime: str, root_label: str, document: dict[str, Any], cli_version: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    row: dict[str, Any] = {
        "runtime": runtime,
        "root": root_label,
        "installed_version": None,
        "expected_version": cli_version,
        "committed_state": None,
        "source": "absent",
    }
    state = document.get("state")
    if state == "absent":
        return row, None
    if state != "ok":
        row["source"] = "unknown"
        return row, None
    facts = document.get("committedFacts")
    if not isinstance(facts, dict):
        row["source"] = "unknown"
        return row, None
    artifact_state = facts.get("committedArtifactState")
    if artifact_state == "NONE":
        row["committed_state"] = "NONE"
        row["source"] = "lifecycle-state"
        return row, None
    version = document.get("version")
    row["installed_version"] = version if isinstance(version, str) else None
    row["committed_state"] = artifact_state if isinstance(artifact_state, str) else None
    row["source"] = "lifecycle-state"
    tail = _package_spec_tail(facts)
    if tail is None or row["installed_version"] is None or tail == row["installed_version"]:
        return row, None
    return row, {
        "runtime": runtime,
        "root": root_label,
        "package_spec_version": tail,
        "installed_version": row["installed_version"],
    }


def _rule_packages(workspace: Path) -> list[dict[str, Any]]:
    """绑定回执里已经写下的规则包身份。没有回执就是空数组，不因此改变 classification。"""
    try:
        receipt = BindingReceipt.load(workspace)
    except Exception:
        return []
    if receipt is None or not receipt.readable:
        return []
    return [
        {
            "module": item.module,
            "kind": item.kind,
            "repoUrl": item.repoUrl,
            "packageVersion": item.packageVersion,
        }
        for item in receipt.modules
    ]


def assemble_version_provenance(
    workspace: Path,
    cli_version: str,
    source_checkout: dict[str, Any],
    state_root: Path | None = None,
) -> dict[str, Any]:
    """由默认根行与 source_checkout 决定 classification。experimental 只列出。

    ``config_layers`` 列出实际存在的配置层（D-73 ③，见 :func:`config_layers`）。016 T-26（D-77）：任一层
    ``state=invalid`` → ``classification=config``、检查项 ``warn``；优先级 install-transaction > outdated-install >
    config > ok，高优先级成立时 invalid 层仍列出并在 message 说明。``rule-defect`` 本 feature 无触发条件。
    """
    root = lifecycle_state_root() if state_root is None else Path(state_root)
    rows: list[dict[str, Any]] = []
    default_mismatches: list[dict[str, Any]] = []
    unfinished = False
    reader = journal_reader()
    reader_failed = reader is None
    if reader is not None:
        try:
            for label, base in (("default", root), ("experimental", root / "experimental")):
                for runtime in reader.runtimes(base):
                    try:
                        document = reader.committedState(base, runtime)
                    except Exception:
                        document = {"state": "unreadable"}
                    if not isinstance(document, dict):
                        document = {"state": "unreadable"}
                    row, mismatch = _provenance_row(runtime, label, document, cli_version)
                    rows.append(row)
                    if mismatch is not None and label == "default":
                        default_mismatches.append(mismatch)
            unfinished = any(
                isinstance(entry, dict) and entry.get("state") == "unfinished"
                for entry in reader.unfinished(root)
            )
        except Exception:
            reader_failed = True
    layers = config_layers(workspace)
    invalid_layers = [layer["layer"] for layer in layers if layer.get("state") == "invalid"]
    classification = "ok"
    default_rows = [row for row in rows if row["root"] == "default"]
    version_differs = any(
        row["installed_version"] is not None and row["installed_version"] != cli_version
        for row in default_rows
    )
    if unfinished:
        classification = "install-transaction"
    elif version_differs or source_checkout.get("relation") == "ahead":
        classification = "outdated-install"
    elif invalid_layers:
        classification = "config"
    warn = classification in {"outdated-install", "install-transaction"} or reader_failed or bool(invalid_layers)
    if any(row["source"] == "unknown" for row in default_rows):
        warn = True
    if default_mismatches:
        warn = True
    if source_checkout.get("state") == "unreadable" or source_checkout.get("relation") == "behind":
        warn = True
    message = _provenance_message(
        classification,
        source_checkout,
        rows,
        cli_version,
        default_mismatches,
        unfinished=unfinished,
        reader_failed=reader_failed,
        invalid_layers=invalid_layers,
    )
    return {
        "status": "warn" if warn else "ok",
        "message": message,
        "fix": "",
        "detail": {
            "cli_version": cli_version,
            "source_checkout": source_checkout,
            "runtimes": rows,
            "rule_packages": _rule_packages(workspace),
            "config_layers": layers,
            "classification": classification,
        },
    }


def config_layers(workspace: Path) -> list[dict[str, Any]]:
    """实际存在的配置层（只读，既有 ``load_config_file`` 口径），按合并顺序 global → project（后者覆盖前者）。

    元素 ``{layer, path, state, keys}``：``state=loaded`` 时 ``keys`` 为顶层键（排序）；文件存在但不是合法 JSON 对象或不可读
    → ``state=invalid``、``keys=[]``。两层都不存在时为 ``[]``。
    """
    layers: list[dict[str, Any]] = []
    for layer, path in (("global", Path.home() / ".config" / "devkit" / "config.json"), ("project", Path(workspace) / CONFIG_PATH)):
        try:
            data, exists = load_config_file(path)
        except (DevkitFlowError, OSError, UnicodeDecodeError):
            layers.append({"layer": layer, "path": str(path), "state": "invalid", "keys": []})
            continue
        if exists:
            layers.append({"layer": layer, "path": str(path), "state": "loaded", "keys": sorted(str(key) for key in data)})
    return layers


def _provenance_message(
    classification: str,
    source_checkout: dict[str, Any],
    rows: list[dict[str, Any]],
    cli_version: str,
    mismatches: list[dict[str, Any]],
    *,
    unfinished: bool,
    reader_failed: bool,
    invalid_layers: list[str] | None = None,
) -> str:
    parts = [f"version provenance {classification}"]
    if reader_failed:
        parts.append("lifecycle journal reader is unavailable")
    relation = source_checkout.get("relation")
    source_version = source_checkout.get("source_version")
    if source_checkout.get("state") == "detected" and relation == "ahead":
        parts.append(
            f"source {source_version} is newer than CLI {cli_version}; "
            "upgrade with devkit-flow upgrade or run python -m devkit_flow"
        )
    elif source_checkout.get("state") == "detected" and relation == "behind":
        parts.append(
            f"source {source_version} is behind CLI {cli_version}; 诊断以运行 CLI 为准"
        )
    elif source_checkout.get("state") == "unreadable":
        parts.append("source checkout version is unreadable")
    differed = [
        row for row in rows
        if row["root"] == "default" and row["installed_version"] not in (None, cli_version)
    ]
    if differed:
        shown = ", ".join(
            f"{row['runtime']} {row['installed_version']}!={cli_version}" for row in differed
        )
        parts.append(f"installed versions differ from CLI {cli_version}: {shown}")
    for mismatch in mismatches:
        parts.append(
            f"packageSpec {mismatch['package_spec_version']} != installed {mismatch['installed_version']}"
        )
    lifecycle_runtimes = [
        row["runtime"] for row in rows
        if row["root"] == "default" and row["source"] == "lifecycle-state" and isinstance(row["runtime"], str)
    ]
    if lifecycle_runtimes:
        parts.append(f"宿主实况请执行 devkit-flow runtime check --runtime {lifecycle_runtimes[0]}")
    if unfinished:
        parts.append("unfinished lifecycle transaction; devkit-flow runtime recover")
    if invalid_layers:
        # 只给层名（global / project），不拼本机路径；路径见 detail.config_layers
        parts.append(f"config layer invalid: {', '.join(invalid_layers)} (.devkit/config.json is not a readable JSON object)")
    return "; ".join(parts)


def _empty_task_summary(source: str) -> dict[str, Any]:
    return {"source": source, "total": 0, "closed": 0, "by_state": {}, "items": [], "items_truncated": False}


def _truncate_desc(text: str) -> str:
    if len(text) <= TASK_DESC_MAX_CHARS:
        return text
    return text[: TASK_DESC_MAX_CHARS - 1] + "…"


def _task_item(row: dict[str, str]) -> dict[str, str]:
    return {
        "id": (row.get("任务ID") or "").strip(),
        "desc": _truncate_desc((row.get("描述") or "").strip()),
        "state": (row.get("状态") or "").strip(),
    }


def task_progress_summary(workspace: Path, feature: dict[str, Any]) -> dict[str, Any]:
    """status JSON `tasks` 字段：只统计 task.md 中 `T-` 开头的任务行；缺失/异常 fail-open（设计 §4.3.4）。"""
    path = task_path_for_feature(workspace, feature)
    if not path.exists():
        return _empty_task_summary("missing")
    try:
        table = parse_task_table(path)
    except (DevkitFlowError, OSError, ValueError):
        return _empty_task_summary("invalid")
    rows = [row for row in table["rows"] if (row.get("任务ID") or "").strip().startswith("T-")]
    by_state: dict[str, int] = {}
    closed = 0
    for row in rows:
        state = (row.get("状态") or "").strip()
        by_state[state] = by_state.get(state, 0) + 1
        if state in TASK_CLOSED_STATES:
            closed += 1
    return {
        "source": "task.md",
        "total": len(rows),
        "closed": closed,
        "by_state": by_state,
        "items": [_task_item(row) for row in rows[:TASK_ITEMS_LIMIT]],
        "items_truncated": len(rows) > TASK_ITEMS_LIMIT,
    }


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
    progress_bar = data.get("progress_bar")
    if isinstance(progress_bar, dict) and "enabled" in progress_bar:
        lines.append(format_progress_bar_status_text(progress_bar))
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
    lifecycle_line = format_runtime_lifecycle_status_text(data.get("runtime_lifecycle"))
    if lifecycle_line is not None:
        lines.append(lifecycle_line)

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
    data["progress_bar"] = progress_bar_summary(workspace)
    if data["status"] == "ok":
        data["tasks"] = task_progress_summary(workspace, data)
    data["spec_artifacts"] = spec_artifacts_summary(workspace)
    data["review_rounds"] = review_rounds_summary(workspace)
    data["review.gate_timing"] = load_review_gate_timing_summary(workspace)
    data["design_template"] = design_template_summary(workspace)
    data["project_profile"] = status_project_profile(workspace)
    data["rule_routing"] = status_rule_routing(workspace)
    data["runtime_lifecycle"] = status_runtime_lifecycle()
    data["flow_ops"] = status_flow_ops(workspace, data)
    update = maybe_check_latest_update()
    if update is not None:
        data["update"] = update
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(format_text(data))
        emit_update_notice(update, args.format)
    return 0
