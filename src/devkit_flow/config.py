from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any
from devkit_flow.constants import ANSI_ESCAPE, CONFIG_PATH, KBA_INSTALL_COMMAND, KBA_SKILL_PACKAGE_NAME
from devkit_flow.errors import DevkitFlowError
from devkit_flow.models import ConfigResolution, ReviewGateTiming
from devkit_flow.textio import atomic_write_text, emit, run_external


ONES_FOLLOW_UP_SYNC_AUTO = "auto"
ONES_FOLLOW_UP_SYNC_MANUAL = "manual"
ONES_FOLLOW_UP_SYNC_VALUES = (ONES_FOLLOW_UP_SYNC_AUTO, ONES_FOLLOW_UP_SYNC_MANUAL)
CREATION_CONFIRM_GROUPS = ("requirement", "designTask", "task")


def creation_confirm_flags(ones: dict[str, Any] | None) -> dict[str, bool]:
    raw = ones.get("creationConfirm") if isinstance(ones, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    return {group: raw.get(group) is True for group in CREATION_CONFIRM_GROUPS}


def normalize_follow_up_sync(value: object) -> str:
    raw = str(value or ONES_FOLLOW_UP_SYNC_AUTO).strip().lower()
    if raw not in ONES_FOLLOW_UP_SYNC_VALUES:
        raise DevkitFlowError(
            "ones.followUpSync must be auto or manual; "
            f"got {value!r}."
        )
    return raw


def ones_follow_up_sync(ones: dict[str, Any]) -> str:
    if not ones:
        return ONES_FOLLOW_UP_SYNC_AUTO
    try:
        return normalize_follow_up_sync(ones.get("followUpSync", ONES_FOLLOW_UP_SYNC_AUTO))
    except DevkitFlowError:
        return ONES_FOLLOW_UP_SYNC_AUTO


def load_config_file(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return {}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DevkitFlowError(f"Invalid {path}: {error}") from error
    if not isinstance(data, dict):
        raise DevkitFlowError(f"Invalid {path}: root must be an object")
    return data, True


def deep_merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_config(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def nested_dict(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return {}
        value = value.get(key)
    return value if isinstance(value, dict) else {}


def load_devkit_config(workspace: Path) -> ConfigResolution:
    global_config, global_exists = load_config_file(Path.home() / ".config" / "devkit" / "config.json")
    project_config, project_exists = load_config_file(workspace / CONFIG_PATH)
    effective = deep_merge_config(global_config, project_config)
    global_has_ones = "ones" in global_config
    project_has_ones = "ones" in project_config
    if global_has_ones and project_has_ones:
        source = "merged"
    elif project_has_ones:
        source = "project"
    elif global_has_ones:
        source = "global"
    else:
        source = "none"
    return ConfigResolution(
        effective=effective,
        global_config=global_config,
        project_config=project_config,
        global_exists=global_exists,
        project_exists=project_exists,
        source=source,
    )


def ones_config_summary(workspace: Path) -> dict[str, Any]:
    """This build has no issue tracker. Leftover config must not surface one."""
    del workspace
    return {
        "configured": False,
        "enabled": False,
        "auto_create": False,
        "space_id": "",
        "parent_requirement_id": "",
        "source": "public",
        "follow_up_sync": "off",
        "creation_confirm": {group: False for group in CREATION_CONFIRM_GROUPS},
    }


def quality_gate_summary(workspace: Path) -> dict[str, Any]:
    resolution = load_devkit_config(workspace)
    global_gate = nested_dict(resolution.global_config, "qualityGates", "fundSecurity")
    project_gate = nested_dict(resolution.project_config, "qualityGates", "fundSecurity")
    if project_gate:
        source = "project"
        enabled = bool(project_gate.get("enabled", True))
    elif global_gate:
        source = "global"
        enabled = bool(global_gate.get("enabled", True))
    else:
        source = "default"
        enabled = True
    return {"enabled": enabled, "source": source}


def context_source_summary(workspace: Path) -> dict[str, Any]:
    resolution = load_devkit_config(workspace)
    global_source = nested_dict(resolution.global_config, "contextSources", "kba")
    project_source = nested_dict(resolution.project_config, "contextSources", "kba")
    if project_source:
        source = "project"
        enabled = bool(project_source.get("enabled", False))
    elif global_source:
        source = "global"
        enabled = bool(global_source.get("enabled", False))
    else:
        source = "default"
        enabled = False
    return {
        "enabled": enabled,
        "source": source,
        "skill": KBA_SKILL_PACKAGE_NAME,
    }


def remove_kba_context_source(config: dict[str, Any]) -> None:
    context_sources = config.get("contextSources")
    if not isinstance(context_sources, dict):
        return
    context_sources.pop("kba", None)
    if not context_sources:
        config.pop("contextSources", None)


def command_context_source_status(args: argparse.Namespace) -> int:
    summary = context_source_summary(Path.cwd())
    emit(
        {
            "status": "ok",
            "context_source": summary,
        },
        args.format,
    )
    return 0


def command_context_source_configure(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )

    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError("--inherit is only valid with --scope project")
        remove_kba_context_source(target_config)
        message = "Project KBA override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError("--clear is only valid with --scope global")
        remove_kba_context_source(target_config)
        message = "Global KBA context source configuration cleared."
    else:
        context_sources = target_config.setdefault("contextSources", {})
        if not isinstance(context_sources, dict):
            raise DevkitFlowError("Invalid contextSources configuration: expected an object")
        context_sources["kba"] = {"enabled": bool(args.enable)}
        message = f"KBA context source {'enabled' if args.enable else 'disabled'} for {args.scope} scope."

    save_devkit_config(workspace, target_config, args.scope)
    emit(
        {
            "status": "configured",
            "message": message,
            "context_source": context_source_summary(workspace),
        },
        args.format,
    )
    return 0


def kba_context_source_doctor(workspace: Path) -> dict[str, Any]:
    summary = context_source_summary(workspace)
    if not summary["enabled"]:
        return {
            "status": "ok",
            "message": f"disabled (source: {summary['source']})",
        }

    mtskills = shutil.which("mtskills")
    if mtskills is None:
        return {
            "status": "warn",
            "message": "enabled but mtskills is not available on PATH",
            "fix": KBA_INSTALL_COMMAND,
        }

    result = run_external([mtskills, "list"], check=False)
    output = ANSI_ESCAPE.sub("", f"{result.stdout}\n{result.stderr}").strip()
    if result.returncode != 0:
        return {
            "status": "warn",
            "message": f"enabled but mtskills list failed: {output or f'exit {result.returncode}'}",
            "fix": KBA_INSTALL_COMMAND,
        }

    installed_pattern = rf"(?m)^\s*{re.escape(KBA_SKILL_PACKAGE_NAME)}(?:\s|$)"
    if re.search(installed_pattern, output) is None:
        return {
            "status": "warn",
            "message": f"enabled but {KBA_SKILL_PACKAGE_NAME} is not installed",
            "fix": KBA_INSTALL_COMMAND,
        }

    return {
        "status": "ok",
        "message": (
            f"enabled; {KBA_SKILL_PACKAGE_NAME} installed "
            f"(source: {summary['source']})"
        ),
    }


def remove_fund_security_gate(config: dict[str, Any]) -> None:
    quality_gates = config.get("qualityGates")
    if not isinstance(quality_gates, dict):
        return
    quality_gates.pop("fundSecurity", None)
    if not quality_gates:
        config.pop("qualityGates", None)


def command_quality_gate_configure(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )

    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError("--inherit is only valid with --scope project")
        remove_fund_security_gate(target_config)
        message = "Project fund-security override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError("--clear is only valid with --scope global")
        remove_fund_security_gate(target_config)
        message = "Global fund-security configuration cleared."
    else:
        quality_gates = target_config.setdefault("qualityGates", {})
        if not isinstance(quality_gates, dict):
            raise DevkitFlowError("Invalid qualityGates configuration: expected an object")
        quality_gates["fundSecurity"] = {"enabled": bool(args.enable)}
        message = f"Fund-security check {'enabled' if args.enable else 'disabled'} for {args.scope} scope."

    save_devkit_config(workspace, target_config, args.scope)
    emit(
        {
            "status": "configured",
            "message": message,
            "fund_security": quality_gate_summary(workspace),
        },
        args.format,
    )
    return 0


def _mapping_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def save_devkit_config(workspace: Path, config: dict[str, Any], scope: str = "project") -> None:
    target = Path.home() / ".config" / "devkit" / "config.json" if scope == "global" else workspace / CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def _config_object_layer(config: dict[str, Any], key: str, invalid_message: str) -> dict[str, Any] | None:
    if not isinstance(config, dict):
        raise DevkitFlowError(invalid_message)
    if key not in config:
        return None
    layer = config[key]
    if not isinstance(layer, dict):
        raise DevkitFlowError(invalid_message)
    return layer


def _require_boolean_enabled(layer: dict[str, Any], *, default: bool) -> bool:
    if "enabled" not in layer:
        return default
    value = layer["enabled"]
    if not isinstance(value, bool):
        raise DevkitFlowError("expected a boolean")
    return value


REVIEW_GATE_TIMING_INVALID_LAYER = (
    "Invalid reviewGateTiming in {scope} config: expected an object with a string timing; "
    "fix with: devkit-flow review-gate configure --scope {scope} --timing dev-once"
)
REVIEW_GATE_TIMING_INVALID_VALUE = (
    "Invalid reviewGateTiming.timing in {scope} config: expected dev-once or per-task; "
    "fix with: devkit-flow review-gate configure --scope {scope} --timing dev-once"
)


def merge_review_gate_timing(
    project_cfg: dict[str, Any], global_cfg: dict[str, Any]
) -> ReviewGateTiming:
    """Resolve whole layers, retaining warnings even below a valid override."""
    timing = "dev-once"
    source = "default"
    warnings: list[str] = []
    for scope, config in (("project", project_cfg), ("global", global_cfg)):
        if "reviewGateTiming" not in config:
            continue
        layer = config["reviewGateTiming"]
        if not isinstance(layer, dict):
            warnings.append(REVIEW_GATE_TIMING_INVALID_LAYER.format(scope=scope))
            continue
        if not layer:
            continue
        value = layer.get("timing")
        if not isinstance(value, str) or value not in ("dev-once", "per-task"):
            warnings.append(REVIEW_GATE_TIMING_INVALID_VALUE.format(scope=scope))
            continue
        if source == "default":
            timing, source = value, scope
    if source == "default" and warnings:
        source = "fallback"
    return ReviewGateTiming(timing=timing, source=source, warnings=tuple(warnings))


def review_gate_timing_summary(
    project_cfg: dict[str, Any], global_cfg: dict[str, Any]
) -> dict[str, Any]:
    timing = merge_review_gate_timing(project_cfg, global_cfg)
    return {
        "timing": timing.timing,
        "source": timing.source,
        "default_timing": timing.default_timing,
        "warnings": list(timing.warnings),
    }


def load_review_gate_timing_summary(workspace: Path) -> dict[str, Any]:
    global_config, _ = load_config_file(Path.home() / ".config" / "devkit" / "config.json")
    project_config, _ = load_config_file(workspace / CONFIG_PATH)
    return review_gate_timing_summary(project_config, global_config)


def format_review_gate_timing_status_text(summary: dict[str, Any]) -> str:
    warnings = summary["warnings"]
    warning_text = "; warnings: " + " | ".join(warnings) if warnings else ""
    return f"review-gate-timing: {summary['timing']} (source={summary['source']}{warning_text})"


def remove_review_gate_timing(config: dict[str, Any], scope: str) -> None:
    if scope not in ("project", "global"):
        raise DevkitFlowError("review-gate scope must be project or global")
    config.pop("reviewGateTiming", None)


def command_review_gate_configure(args: argparse.Namespace) -> int:
    if args.inherit and args.scope != "project":
        raise DevkitFlowError("--inherit is only valid with --scope project")
    if args.clear and args.scope != "global":
        raise DevkitFlowError("--clear is only valid with --scope global")
    workspace = Path.cwd()
    global_config, _ = load_config_file(Path.home() / ".config" / "devkit" / "config.json")
    project_config, _ = load_config_file(workspace / CONFIG_PATH)
    target_config = copy.deepcopy(global_config if args.scope == "global" else project_config)
    if args.inherit:
        remove_review_gate_timing(target_config, args.scope)
        message = "Project review-gate-timing override removed; global inheritance restored."
    elif args.clear:
        remove_review_gate_timing(target_config, args.scope)
        message = "Global review-gate-timing configuration cleared."
    else:
        target_config["reviewGateTiming"] = {"timing": args.timing}
        message = f"Review gate timing set to {args.timing} for {args.scope} scope."
    save_devkit_config(workspace, target_config, args.scope)
    summary = load_review_gate_timing_summary(workspace)
    emit({"status": "configured", "message": message, "review_gate_timing": summary}, args.format)
    return 0


def command_review_gate_status(args: argparse.Namespace) -> int:
    summary = load_review_gate_timing_summary(Path.cwd())
    emit(
        {
            "status": "ok",
            "message": format_review_gate_timing_status_text(summary),
            "review_gate_timing": summary,
        },
        args.format,
    )
    return 0
