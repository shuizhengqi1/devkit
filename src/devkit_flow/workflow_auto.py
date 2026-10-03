from __future__ import annotations

import argparse
import copy
from pathlib import Path
from typing import Any
from devkit_flow.config import _mapping_field, load_devkit_config, save_devkit_config
from devkit_flow.constants import WORKFLOW_AUTO_TAKEOVER_REFUSE_MESSAGE, WORKFLOW_RUNTIME_KEYS
from devkit_flow.errors import DevkitFlowError
from devkit_flow.models import TakeoverDecision, WorkflowRuntimeSwitch
from devkit_flow.textio import emit


def workflow_runtime_keys() -> tuple[str, ...]:
    return WORKFLOW_RUNTIME_KEYS


def require_workflow_runtime(runtime: str) -> str:
    keys = workflow_runtime_keys()
    if runtime not in keys:
        raise DevkitFlowError(
            f"unknown runtime {runtime!r}; expected one of: {', '.join(keys)}"
        )
    return runtime


def auto_takeover_layer(config: dict[str, Any], runtime: str) -> dict[str, Any] | None:
    if not isinstance(config, dict):
        raise DevkitFlowError("Invalid workflow.autoTakeover")
    if "workflow" not in config:
        return None
    workflow = config["workflow"]
    if not isinstance(workflow, dict):
        raise DevkitFlowError("Invalid workflow.autoTakeover")
    if "autoTakeover" not in workflow:
        return None
    auto_takeover = workflow["autoTakeover"]
    if not isinstance(auto_takeover, dict):
        raise DevkitFlowError("Invalid workflow.autoTakeover")
    if runtime not in auto_takeover:
        return None
    layer = auto_takeover[runtime]
    if not isinstance(layer, dict):
        raise DevkitFlowError("Invalid workflow.autoTakeover")
    return layer


def auto_takeover_enabled(layer: dict[str, Any]) -> bool:
    if "enabled" not in layer:
        return True
    value = layer["enabled"]
    if not isinstance(value, bool):
        raise DevkitFlowError("expected a boolean")
    return value


def merge_auto_takeover(
    global_config: dict[str, Any],
    project_config: dict[str, Any],
    runtime: str,
) -> WorkflowRuntimeSwitch:
    require_workflow_runtime(runtime)
    global_layer = auto_takeover_layer(global_config, runtime)
    project_layer = auto_takeover_layer(project_config, runtime)
    global_enabled = auto_takeover_enabled(global_layer) if global_layer is not None else None
    project_enabled = auto_takeover_enabled(project_layer) if project_layer is not None else None
    if project_layer is not None:
        return WorkflowRuntimeSwitch(
            runtime=runtime,
            enabled=bool(project_enabled),
            source="project",
            default_enabled=True,
        )
    if global_layer is not None:
        return WorkflowRuntimeSwitch(
            runtime=runtime,
            enabled=bool(global_enabled),
            source="global",
            default_enabled=True,
        )
    return WorkflowRuntimeSwitch(
        runtime=runtime,
        enabled=True,
        source="default",
        default_enabled=True,
    )


def remove_workflow_auto_takeover(config: dict[str, Any], runtime: str) -> None:
    workflow = config.get("workflow")
    if not isinstance(workflow, dict):
        return
    auto_takeover = workflow.get("autoTakeover")
    if not isinstance(auto_takeover, dict):
        return
    auto_takeover.pop(runtime, None)
    if not auto_takeover:
        workflow.pop("autoTakeover", None)
    if not workflow:
        config.pop("workflow", None)


def _ensure_auto_takeover_object(config: dict[str, Any]) -> dict[str, Any]:
    workflow = config.get("workflow")
    if workflow is None:
        workflow = {}
        config["workflow"] = workflow
    elif not isinstance(workflow, dict):
        raise DevkitFlowError("Invalid workflow.autoTakeover")
    auto_takeover = workflow.get("autoTakeover")
    if auto_takeover is None:
        auto_takeover = {}
        workflow["autoTakeover"] = auto_takeover
    elif not isinstance(auto_takeover, dict):
        raise DevkitFlowError("Invalid workflow.autoTakeover")
    return auto_takeover


def _single_auto_takeover_payload(switch: WorkflowRuntimeSwitch) -> dict[str, Any]:
    return {
        "default_enabled": True,
        "runtime": switch.runtime,
        "enabled": switch.enabled,
        "source": switch.source,
    }


def _matrix_auto_takeover_payload(
    global_config: dict[str, Any],
    project_config: dict[str, Any],
) -> dict[str, Any]:
    runtimes: dict[str, Any] = {}
    for key in workflow_runtime_keys():
        switch = merge_auto_takeover(global_config, project_config, key)
        runtimes[key] = {
            "runtime": switch.runtime,
            "enabled": switch.enabled,
            "source": switch.source,
        }
    return {"default_enabled": True, "runtimes": runtimes}


def workflow_auto_takeover_summary(
    workspace: Path,
    runtime: str | None = None,
) -> dict[str, Any]:
    resolution = load_devkit_config(workspace)
    if runtime:
        require_workflow_runtime(runtime)
        switch = merge_auto_takeover(
            resolution.global_config,
            resolution.project_config,
            runtime,
        )
        return _single_auto_takeover_payload(switch)
    return _matrix_auto_takeover_payload(resolution.global_config, resolution.project_config)


def format_workflow_status_text(summary: dict[str, Any]) -> str:
    runtimes = summary.get("runtimes")
    if isinstance(runtimes, dict):
        lines = ["workflow auto-takeover: default on"]
        for key, item in runtimes.items():
            state = "on" if item.get("enabled") else "off"
            lines.append(f"- {key}: {state} (source={item.get('source')})")
        return "\n".join(lines)
    state = "on" if summary.get("enabled") else "off"
    return (
        f"workflow auto-takeover: {state} "
        f"({summary.get('runtime')}, source={summary.get('source')})"
    )


def workflow_auto_takeover_doctor_message(summary: dict[str, Any]) -> str:
    runtimes = summary.get("runtimes") or {}
    parts: list[str] = []
    for key in workflow_runtime_keys():
        item = runtimes.get(key) or {}
        state = "on" if item.get("enabled") else "off"
        parts.append(f"{key}={state}/{item.get('source') or 'default'}")
    return ", ".join(parts)


def workflow_session_notice_line(switch: Any) -> str:
    enabled = bool(_mapping_field(switch, "enabled"))
    runtime = str(_mapping_field(switch, "runtime") or "")
    source = str(_mapping_field(switch, "source") or "default")
    flag = "ON" if enabled else "OFF"
    return f"[devkit] workflow auto-takeover: {flag} ({runtime}, source={source})"


def decide_auto_takeover(switch: Any, trigger: Any) -> TakeoverDecision:
    trigger_kind = str(_mapping_field(trigger, "trigger_kind") or "")
    enabled = bool(_mapping_field(switch, "enabled"))
    source = str(_mapping_field(switch, "source") or "default")
    runtime = str(_mapping_field(switch, "runtime") or "")
    if trigger_kind == "EXPLICIT_OPT_OUT":
        return TakeoverDecision(
            outcome="SKIP_OPT_OUT",
            trigger_kind=trigger_kind,
            enabled=enabled,
            source=source,
            reason="explicit-opt-out",
        )
    if trigger_kind in {"EXPLICIT_SLASH", "EXPLICIT_SKILL"}:
        return TakeoverDecision(
            outcome="TAKEOVER",
            trigger_kind=trigger_kind,
            enabled=enabled,
            source=source,
            reason="explicit-entry",
        )
    if trigger_kind == "EXPLICIT_DEVKIT_FLOW":
        return TakeoverDecision(
            outcome="TAKEOVER",
            trigger_kind=trigger_kind,
            enabled=enabled,
            source=source,
            reason="explicit-devkit-flow",
        )
    if enabled:
        return TakeoverDecision(
            outcome="TAKEOVER",
            trigger_kind=trigger_kind,
            enabled=enabled,
            source=source,
            reason="auto-enabled",
        )
    return TakeoverDecision(
        outcome="REFUSE_OBSERVABLE",
        trigger_kind=trigger_kind,
        enabled=enabled,
        source=source,
        reason="auto-disabled",
        user_message=WORKFLOW_AUTO_TAKEOVER_REFUSE_MESSAGE.format(
            runtime=runtime,
            source=source,
        ),
    )


def command_workflow_status(args: argparse.Namespace) -> int:
    runtime = args.runtime
    if runtime:
        require_workflow_runtime(runtime)
    summary = workflow_auto_takeover_summary(Path.cwd(), runtime)
    emit(
        {
            "status": "ok",
            "message": format_workflow_status_text(summary),
            "workflow": {"auto_takeover": summary},
        },
        args.format,
    )
    return 0


def command_workflow_configure(args: argparse.Namespace) -> int:
    runtime = require_workflow_runtime(args.runtime)
    if args.clear and args.scope != "global":
        raise DevkitFlowError("--clear is only valid with --scope global")

    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )

    if args.inherit or args.clear:
        remove_workflow_auto_takeover(target_config, runtime)
        if args.scope == "global":
            message = f"Workflow auto-takeover cleared for {runtime} (global scope)."
        else:
            message = (
                f"Project auto-takeover override removed for {runtime}; "
                "global inheritance restored."
            )
    else:
        auto_takeover = _ensure_auto_takeover_object(target_config)
        auto_takeover[runtime] = {"enabled": bool(args.enable)}
        state = "enabled" if args.enable else "disabled"
        message = f"Workflow auto-takeover {state} for {runtime} ({args.scope} scope)."

    save_devkit_config(workspace, target_config, args.scope)
    summary = workflow_auto_takeover_summary(workspace, runtime)
    emit(
        {
            "status": "configured",
            "message": message,
            "workflow": {"auto_takeover": summary},
        },
        args.format,
    )
    return 0
