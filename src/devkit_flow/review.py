from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from devkit_flow.config import _config_object_layer, _mapping_field, load_devkit_config, save_devkit_config
from devkit_flow.constants import ANSI_ESCAPE, ATX_HEADING_RE, BUNDLE_DESIGN_TEMPLATE_OUTLINE, TEMPLATE_PROBE_FETCH_TIMEOUT_SECONDS, TEMPLATE_PROBE_LIST_NAME_RE, TEMPLATE_PROBE_PROBE_TIMEOUT_SECONDS, TEMPLATE_PROBE_SKILL_RELATIVE_PATHS, CLEAR_ONLY_GLOBAL, DEFAULT_REVIEW_MAX_ROUNDS, DEFAULT_REVIEW_NEW_CLASS_BUDGET, DESIGN_TEMPLATE_INVALID_POINTER, DOCTOR_TEMPLATE_PROBE_FIX, INHERIT_ONLY_PROJECT, KM_TEMPLATE_POINTER_RE, PRESET_DESIGN_TEMPLATE_KM, Q9_SRE_TERMS, Q9_SRE_TOOL_TERMS, REVIEW_ROUNDS_INVALID_LAYER, REVIEW_ROUNDS_INVALID_MAX, REVIEW_ROUNDS_INVALID_STEP, SRE_FULL_FEATURE_MARKERS, SRE_SCHEMA_MARKERS, SRE_WRITE_DESIGN_MARKERS, TASK_ARCH_REVIEW_STEP_RE, known_review_steps_for_mode
from devkit_flow.errors import DevkitFlowError
from devkit_flow.models import TemplateProbeResult, CuratorSkipDecision, DesignTemplateConfig, DesignTemplatePointer, DesignTemplateResolution, ReviewRoundsConfig, SreIntent, SreRouteDecision
from devkit_flow.parse_util import find_features, select_feature
from devkit_flow.step_alias import StepResolution, attach_step_resolution, feature_pipeline_mode, resolve_step
from devkit_flow.switches import docs_curator_summary
from devkit_flow.textio import atomic_write_text, compact_error, emit


def format_review_rounds_status_text(summary: dict[str, Any]) -> str:
    return f"review-rounds: {summary.get('max_rounds')} (source={summary.get('source')})"


def format_design_template_status_text(body: dict[str, Any]) -> str:
    mode = body.get("effective_mode") or "preset"
    source = body.get("source")
    reason = body.get("fallback_reason")
    kind = body.get("kind")
    if mode == "custom":
        return f"design-template: custom {kind} (source={source})"
    if reason:
        return f"design-template: {mode} (source={source}, reason={reason})"
    return f"design-template: {mode} (source={source})"


def _require_review_max_rounds(layer: dict[str, Any], *, default: int) -> int:
    if "maxRounds" not in layer:
        return default
    value = layer["maxRounds"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise DevkitFlowError(REVIEW_ROUNDS_INVALID_MAX)
    if value < 1 or value > 100:
        raise DevkitFlowError(REVIEW_ROUNDS_INVALID_MAX)
    return value


def merge_review_rounds(
    global_config: dict[str, Any], project_config: dict[str, Any]
) -> ReviewRoundsConfig:
    global_layer = _config_object_layer(
        global_config, "reviewRounds", REVIEW_ROUNDS_INVALID_LAYER
    )
    project_layer = _config_object_layer(
        project_config, "reviewRounds", REVIEW_ROUNDS_INVALID_LAYER
    )
    if global_layer is not None:
        _require_review_max_rounds(global_layer, default=DEFAULT_REVIEW_MAX_ROUNDS)
    if project_layer is not None:
        return ReviewRoundsConfig(
            max_rounds=_require_review_max_rounds(
                project_layer, default=DEFAULT_REVIEW_MAX_ROUNDS
            ),
            source="project",
            default_max_rounds=DEFAULT_REVIEW_MAX_ROUNDS,
        )
    if global_layer is not None:
        return ReviewRoundsConfig(
            max_rounds=_require_review_max_rounds(
                global_layer, default=DEFAULT_REVIEW_MAX_ROUNDS
            ),
            source="global",
            default_max_rounds=DEFAULT_REVIEW_MAX_ROUNDS,
        )
    return ReviewRoundsConfig(
        max_rounds=DEFAULT_REVIEW_MAX_ROUNDS,
        source="default",
        default_max_rounds=DEFAULT_REVIEW_MAX_ROUNDS,
    )


def review_rounds_summary(workspace: Path) -> dict[str, Any]:
    resolution = load_devkit_config(workspace)
    return asdict(merge_review_rounds(resolution.global_config, resolution.project_config))


def remove_review_rounds(config: dict[str, Any]) -> None:
    config.pop("reviewRounds", None)


def command_review_rounds_configure(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )
    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError(INHERIT_ONLY_PROJECT)
        remove_review_rounds(target_config)
        message = "Project review-rounds override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError(CLEAR_ONLY_GLOBAL)
        remove_review_rounds(target_config)
        message = "Global review-rounds configuration cleared."
    else:
        max_rounds = int(args.max_rounds)
        if max_rounds < 1 or max_rounds > 100:
            raise DevkitFlowError(REVIEW_ROUNDS_INVALID_MAX)
        target_config["reviewRounds"] = {"maxRounds": max_rounds}
        message = f"Review rounds max set to {max_rounds} for {args.scope} scope."
    save_devkit_config(workspace, target_config, args.scope)
    summary = review_rounds_summary(workspace)
    emit(
        {"status": "configured", "message": message, "review_rounds": summary},
        args.format,
    )
    return 0


def command_review_rounds_status(args: argparse.Namespace) -> int:
    summary = review_rounds_summary(Path.cwd())
    emit(
        {
            "status": "ok",
            "message": format_review_rounds_status_text(summary),
            "review_rounds": summary,
        },
        args.format,
    )
    return 0


def is_known_review_step(step: str, mode: str | None = None) -> bool:
    if TASK_ARCH_REVIEW_STEP_RE.fullmatch(step):
        return True
    return step in known_review_steps_for_mode(mode)


def resolve_cli_review_step(
    raw: str, feature: dict[str, Any]
) -> tuple[str, StepResolution | None]:
    """T-*-arch 直通；其余按模式语义解析后再做 review 步骤校验。"""
    stripped = (raw or "").strip()
    if TASK_ARCH_REVIEW_STEP_RE.fullmatch(stripped):
        return stripped, None
    mode = feature_pipeline_mode(feature)
    resolution = resolve_step(stripped, mode=mode)
    return resolution.step_id, resolution


def review_loop_state_path(workspace: Path, feature: dict[str, Any]) -> Path:
    return workspace / feature["feature_dir"] / "review-loop-state.json"


def load_review_loop_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schemaVersion": 1, "steps": {}}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DevkitFlowError(f"Invalid {path}: {error}") from error
    if not isinstance(parsed, dict):
        raise DevkitFlowError(f"Invalid {path}")
    steps = parsed.get("steps")
    if steps is None:
        parsed["steps"] = {}
    elif not isinstance(steps, dict):
        raise DevkitFlowError(f"Invalid {path}")
    parsed.setdefault("schemaVersion", 1)
    return parsed


def save_review_loop_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def default_review_step_state() -> dict[str, Any]:
    return {
        "attempt": 0,
        "loop_status": "open",
        "new_class_count": 0,
        "last_finding_ids": [],
    }


def parse_finding_ids(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _record_finding_ids(record: dict[str, Any]) -> list[str]:
    finding_ids = record.get("last_finding_ids") or []
    if not isinstance(finding_ids, list):
        return []
    return [str(item) for item in finding_ids]


def review_step_payload(
    *,
    feature: dict[str, Any],
    step: str,
    record: dict[str, Any],
    max_rounds: int,
    status: str,
) -> dict[str, Any]:
    attempt = int(record.get("attempt") or 0)
    loop_status = str(record.get("loop_status") or "open")
    new_class_count = int(record.get("new_class_count") or 0)
    new_class_budget = DEFAULT_REVIEW_NEW_CLASS_BUDGET
    review_mode = "full" if attempt == 0 else "verify-only"
    allowed = (
        loop_status == "open"
        and attempt < max_rounds
        and new_class_count < new_class_budget
    )
    return {
        "status": status,
        "feature": feature["feature"],
        "step": step,
        "allowed": allowed,
        "attempt": attempt,
        "max_rounds": max_rounds,
        "loop_status": loop_status,
        "review_mode": review_mode,
        "new_class_count": new_class_count,
        "new_class_budget": new_class_budget,
        "last_finding_ids": _record_finding_ids(record),
    }


def _select_review_feature(args: argparse.Namespace) -> dict[str, Any]:
    selected = select_feature(find_features(Path.cwd()), args.feature)
    if selected.get("status") != "ok":
        raise DevkitFlowError(selected.get("message") or "feature not found")
    return selected


def command_review_rounds_check(args: argparse.Namespace) -> int:
    feature = _select_review_feature(args)
    step, resolution = resolve_cli_review_step(args.step, feature)
    if not is_known_review_step(step, feature_pipeline_mode(feature)):
        raise DevkitFlowError(REVIEW_ROUNDS_INVALID_STEP)
    workspace = Path.cwd()
    max_rounds = review_rounds_summary(workspace)["max_rounds"]
    state = load_review_loop_state(review_loop_state_path(workspace, feature))
    record = state.get("steps", {}).get(step) or default_review_step_state()
    payload = review_step_payload(
        feature=feature, step=step, record=record, max_rounds=max_rounds, status="ok"
    )
    if resolution is not None:
        attach_step_resolution(payload, resolution)
    emit(payload, args.format)
    return 0


def command_review_rounds_tick(args: argparse.Namespace) -> int:
    feature = _select_review_feature(args)
    step, resolution = resolve_cli_review_step(args.step, feature)
    if not is_known_review_step(step, feature_pipeline_mode(feature)):
        raise DevkitFlowError(REVIEW_ROUNDS_INVALID_STEP)
    workspace = Path.cwd()
    max_rounds = review_rounds_summary(workspace)["max_rounds"]
    path = review_loop_state_path(workspace, feature)
    state = load_review_loop_state(path)
    steps = state.setdefault("steps", {})
    record = dict(steps.get(step) or default_review_step_state())
    record.setdefault("new_class_count", 0)
    record.setdefault("last_finding_ids", [])
    finding_ids = parse_finding_ids(getattr(args, "finding_ids", None))
    if finding_ids is not None:
        record["last_finding_ids"] = finding_ids
    if args.event == "failed":
        record["attempt"] = int(record.get("attempt") or 0) + 1
        if getattr(args, "new_class", False):
            record["new_class_count"] = int(record.get("new_class_count") or 0) + 1
        attempt = int(record["attempt"])
        new_class_count = int(record.get("new_class_count") or 0)
        if attempt >= max_rounds or new_class_count >= DEFAULT_REVIEW_NEW_CLASS_BUDGET:
            record["loop_status"] = "human_required"
        else:
            record["loop_status"] = "open"
    else:
        record["loop_status"] = "passed"
    steps[step] = record
    save_review_loop_state(path, state)
    payload = review_step_payload(
        feature=feature,
        step=step,
        record=record,
        max_rounds=max_rounds,
        status="recorded",
    )
    if resolution is not None:
        attach_step_resolution(payload, resolution)
    emit(payload, args.format)
    return 0


def command_review_rounds_reset(args: argparse.Namespace) -> int:
    feature = _select_review_feature(args)
    step, resolution = resolve_cli_review_step(args.step, feature)
    if not is_known_review_step(step, feature_pipeline_mode(feature)):
        raise DevkitFlowError(REVIEW_ROUNDS_INVALID_STEP)
    workspace = Path.cwd()
    max_rounds = review_rounds_summary(workspace)["max_rounds"]
    path = review_loop_state_path(workspace, feature)
    state = load_review_loop_state(path)
    steps = state.setdefault("steps", {})
    record = default_review_step_state()
    steps[step] = record
    save_review_loop_state(path, state)
    payload = review_step_payload(
        feature=feature, step=step, record=record, max_rounds=max_rounds, status="ok"
    )
    if resolution is not None:
        attach_step_resolution(payload, resolution)
    emit(payload, args.format)
    return 0


def review_rounds_doctor(workspace: Path) -> dict[str, Any]:
    try:
        summary = review_rounds_summary(workspace)
    except DevkitFlowError as error:
        return {
            "status": "warn",
            "message": compact_error(error),
            "fix": "check .devkit/config.json JSON syntax",
        }
    return {"status": "ok", "message": f"{summary['max_rounds']}/{summary['source']}"}


def _design_template_pointer_value(layer: dict[str, Any]) -> str:
    value = layer.get("pointer")
    if not isinstance(value, str) or not value.strip():
        raise DevkitFlowError(DESIGN_TEMPLATE_INVALID_POINTER)
    return value.strip()


def classify_template_pointer(pointer: str) -> str:
    text = str(pointer).strip()
    if KM_TEMPLATE_POINTER_RE.fullmatch(text):
        return "km"
    if text.lower().endswith(".md"):
        return "markdown"
    raise DevkitFlowError(DESIGN_TEMPLATE_INVALID_POINTER)


def merge_design_template(
    global_config: dict[str, Any],
    project_config: dict[str, Any],
) -> DesignTemplatePointer:
    global_layer = _config_object_layer(global_config, "designTemplate", "Invalid designTemplate")
    project_layer = _config_object_layer(project_config, "designTemplate", "Invalid designTemplate")
    if project_layer is not None:
        pointer = _design_template_pointer_value(project_layer)
        return DesignTemplatePointer(
            pointer=pointer,
            kind=classify_template_pointer(pointer),
            source="project",
        )
    if global_layer is not None:
        pointer = _design_template_pointer_value(global_layer)
        return DesignTemplatePointer(
            pointer=pointer,
            kind=classify_template_pointer(pointer),
            source="global",
        )
    return DesignTemplatePointer(pointer=None, kind="none", source="default")


def resolve_markdown_pointer(workspace: Path, pointer: str) -> Path:
    text = str(pointer).strip()
    if not text.lower().endswith(".md"):
        raise DevkitFlowError(DESIGN_TEMPLATE_INVALID_POINTER)
    expanded = Path(text).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    workspace_root = workspace.expanduser().resolve()
    resolved = (workspace_root / expanded).resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as error:
        raise DevkitFlowError(DESIGN_TEMPLATE_INVALID_POINTER) from error
    return resolved


def resolve_design_template_config(workspace: Path) -> DesignTemplateConfig:
    resolution = load_devkit_config(workspace)
    merged = merge_design_template(resolution.global_config, resolution.project_config)
    if merged.kind == "none" or not merged.pointer:
        return DesignTemplateConfig(
            configured=False,
            pointer=PRESET_DESIGN_TEMPLATE_KM,
            kind="none",
            source=merged.source,
            resolved_path=None,
        )
    if merged.kind == "markdown":
        path = resolve_markdown_pointer(workspace, merged.pointer)
        return DesignTemplateConfig(
            configured=True,
            pointer=merged.pointer,
            kind="markdown",
            source=merged.source,
            resolved_path=str(path),
        )
    return DesignTemplateConfig(
        configured=True,
        pointer=merged.pointer,
        kind="km",
        source=merged.source,
        resolved_path=None,
    )


def _session_exposes_template_probe() -> bool:
    flag = os.environ.get("TEMPLATE_PROBE_AVAILABLE", "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    for key in ("DEVKIT_SESSION_SKILLS", "MCP_SERVERS", "CLAUDE_CODE_MCP_SERVERS", "TEMPLATE_PROBE_SKILL"):
        raw = os.environ.get(key, "")
        if not raw:
            continue
        if TEMPLATE_PROBE_LIST_NAME_RE.search(raw.replace(",", "\n")):
            return True
        if re.search(r"(?i)(?:^|[\s,/])template_probe(?:$|[\s,/])", raw):
            return True
    return False


def _template_probe_list_hit(command_name: str) -> bool | None:
    executable = shutil.which(command_name)
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "list"],
            capture_output=True,
            text=True,
            timeout=TEMPLATE_PROBE_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = ANSI_ESCAPE.sub("", f"{result.stdout}\n{result.stderr}")
    return TEMPLATE_PROBE_LIST_NAME_RE.search(output) is not None


def _template_probe_skill_dir_hit() -> bool:
    home = Path.home()
    return any((home / relative).is_file() for relative in TEMPLATE_PROBE_SKILL_RELATIVE_PATHS)


def probe_template_probe_skill() -> TemplateProbeResult:
    return TemplateProbeResult(available=False, method="none", detail="local outline")
    if _session_exposes_template_probe():
        return TemplateProbeResult(available=True, method="session", detail="session exposes template_probe")
    oa_hit = _template_probe_list_hit("oa-skills")
    if oa_hit:
        return TemplateProbeResult(available=True, method="oa-skills", detail="oa-skills list")
    mt_hit = _template_probe_list_hit("mtskills")
    if mt_hit:
        return TemplateProbeResult(available=True, method="mtskills", detail="mtskills list")
    if _template_probe_skill_dir_hit():
        return TemplateProbeResult(available=True, method="skill-dir", detail="template_probe/SKILL.md")
    return TemplateProbeResult(available=False, method="none", detail="")


def template_content_complete(text: str) -> bool:
    if text is None:
        return False
    payload = str(text)
    if not payload.strip():
        return False
    return ATX_HEADING_RE.search(payload) is not None


def _km_content_id(pointer: str) -> str | None:
    match = re.search(r"/docs/(\d+)", str(pointer).strip())
    return match.group(1) if match else None


def _fetch_km_markdown(pointer: str) -> str | None:
    content_id = _km_content_id(pointer)
    if not content_id:
        return None
    executable = shutil.which("oa-skills")
    if executable is None:
        return None
    for args in (
        ["template_probe", "getSimpleMarkdown", "--contentId", content_id],
        ["template_probe", "getMarkdown", "--contentId", content_id],
    ):
        try:
            result = subprocess.run(
                [executable, *args],
                capture_output=True,
                text=True,
                timeout=TEMPLATE_PROBE_FETCH_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        output = (result.stdout or "").strip()
        if output:
            return output
    return None


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _preset_template_resolution(
    *,
    configured: bool,
    pointer: str | None,
    kind: str,
    source: str,
    resolved_path: str | None,
    template_probe: TemplateProbeResult,
    fallback_reason: str,
    content_status: str,
) -> DesignTemplateResolution:
    return DesignTemplateResolution(
        configured=configured,
        pointer=pointer,
        kind=kind,
        source=source,
        resolved_path=resolved_path,
        template_probe=template_probe,
        effective_mode="preset",
        effective_pointer=PRESET_DESIGN_TEMPLATE_KM,
        bundle_outline=BUNDLE_DESIGN_TEMPLATE_OUTLINE,
        fallback_reason=fallback_reason,
        content_status=content_status,
    )


def resolve_design_template(workspace: Path) -> DesignTemplateResolution:
    resolution = load_devkit_config(workspace)
    merged = merge_design_template(resolution.global_config, resolution.project_config)
    template_probe = probe_template_probe_skill()
    if merged.kind == "none" or not merged.pointer:
        if template_probe.available:
            text = _fetch_km_markdown(PRESET_DESIGN_TEMPLATE_KM)
            if text is not None and template_content_complete(text):
                return DesignTemplateResolution(
                    configured=False,
                    pointer=None,
                    kind="none",
                    source=merged.source,
                    resolved_path=None,
                    template_probe=template_probe,
                    effective_mode="preset",
                    effective_pointer=PRESET_DESIGN_TEMPLATE_KM,
                    bundle_outline=BUNDLE_DESIGN_TEMPLATE_OUTLINE,
                    fallback_reason=None,
                    content_status="ready",
                )
            return _preset_template_resolution(
                configured=False,
                pointer=None,
                kind="none",
                source=merged.source,
                resolved_path=None,
                template_probe=template_probe,
                fallback_reason="unreadable" if text is None else "incomplete",
                content_status="fallback",
            )
        return _preset_template_resolution(
            configured=False,
            pointer=None,
            kind="none",
            source=merged.source,
            resolved_path=None,
            template_probe=template_probe,
            fallback_reason="unconfigured",
            content_status="fallback",
        )

    pointer = merged.pointer
    if merged.kind == "markdown":
        try:
            path = resolve_markdown_pointer(workspace, pointer)
        except DevkitFlowError:
            return _preset_template_resolution(
                configured=True,
                pointer=pointer,
                kind="markdown",
                source=merged.source,
                resolved_path=None,
                template_probe=template_probe,
                fallback_reason="path_escape",
                content_status="fallback",
            )
        resolved_path = str(path)
        text = _read_text_file(path)
        if text is None:
            return _preset_template_resolution(
                configured=True,
                pointer=pointer,
                kind="markdown",
                source=merged.source,
                resolved_path=resolved_path,
                template_probe=template_probe,
                fallback_reason="unreadable",
                content_status="fallback",
            )
        if not template_content_complete(text):
            return _preset_template_resolution(
                configured=True,
                pointer=pointer,
                kind="markdown",
                source=merged.source,
                resolved_path=resolved_path,
                template_probe=template_probe,
                fallback_reason="incomplete",
                content_status="fallback",
            )
        return DesignTemplateResolution(
            configured=True,
            pointer=pointer,
            kind="markdown",
            source=merged.source,
            resolved_path=resolved_path,
            template_probe=template_probe,
            effective_mode="custom",
            effective_pointer=pointer,
            bundle_outline=BUNDLE_DESIGN_TEMPLATE_OUTLINE,
            fallback_reason=None,
            content_status="ready",
        )

    if not template_probe.available:
        return _preset_template_resolution(
            configured=True,
            pointer=pointer,
            kind="km",
            source=merged.source,
            resolved_path=None,
            template_probe=template_probe,
            fallback_reason="template_probe_unavailable",
            content_status="fallback",
        )
    text = _fetch_km_markdown(pointer)
    if text is None:
        return _preset_template_resolution(
            configured=True,
            pointer=pointer,
            kind="km",
            source=merged.source,
            resolved_path=None,
            template_probe=template_probe,
            fallback_reason="unreadable",
            content_status="fallback",
        )
    if not template_content_complete(text):
        return _preset_template_resolution(
            configured=True,
            pointer=pointer,
            kind="km",
            source=merged.source,
            resolved_path=None,
            template_probe=template_probe,
            fallback_reason="incomplete",
            content_status="fallback",
        )
    return DesignTemplateResolution(
        configured=True,
        pointer=pointer,
        kind="km",
        source=merged.source,
        resolved_path=None,
        template_probe=template_probe,
        effective_mode="custom",
        effective_pointer=pointer,
        bundle_outline=BUNDLE_DESIGN_TEMPLATE_OUTLINE,
        fallback_reason=None,
        content_status="ready",
    )


DESIGN_TEMPLATE_CHECK_CACHE_RELATIVE_PATH = Path(".config") / "devkit" / "design-template-check.json"
DESIGN_TEMPLATE_CHECK_TTL_HOURS = 4


def design_template_check_cache_path() -> Path:
    return Path.home() / DESIGN_TEMPLATE_CHECK_CACHE_RELATIVE_PATH


def _design_template_cache_key(pointer: str) -> str:
    return hashlib.sha1(str(pointer).encode("utf-8")).hexdigest()


def _read_design_template_check_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {}
    return data["entries"]


def _write_design_template_check_cache(path: Path, pointer: str, summary: dict[str, Any]) -> None:
    """写入 pointer 对应的验证 verdict；任何失败静默，绝不影响主命令退出码。"""
    try:
        entries = _read_design_template_check_cache(path)
        entries[_design_template_cache_key(pointer)] = {
            "pointer": pointer,
            "kind": summary.get("kind"),
            "checkedAt": summary.get("content_checked_at"),
            "probe": summary.get("template_probe"),
            "content_status": summary.get("content_status"),
            "fallback_reason": summary.get("fallback_reason"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False) + "\n",
        )
    except (OSError, ValueError):
        pass  # 缓存写不进去不影响主命令


def _fresh_cache_entry(entry: Any, now: datetime) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    checked_at_raw = entry.get("checkedAt")
    probe = entry.get("probe")
    fallback_reason = entry.get("fallback_reason")
    if not isinstance(checked_at_raw, str) or not isinstance(probe, dict):
        return None
    if not isinstance(probe.get("available"), bool):
        return None
    if entry.get("content_status") not in {"ready", "fallback"}:
        return None
    if fallback_reason is not None and not isinstance(fallback_reason, str):
        return None
    try:
        checked_at = datetime.fromisoformat(checked_at_raw)
    except ValueError:
        return None
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    if now - checked_at >= timedelta(hours=DESIGN_TEMPLATE_CHECK_TTL_HOURS):
        return None
    return entry


def _fast_probe_template_probe() -> TemplateProbeResult:
    """零子进程探测（仅 env 标记 + skill 目录），供缓存路径在缓存未命中时填充 template_probe 字段。"""
    if _session_exposes_template_probe():
        return TemplateProbeResult(available=True, method="session", detail="session exposes template_probe")
    if _template_probe_skill_dir_hit():
        return TemplateProbeResult(available=True, method="skill-dir", detail="template_probe/SKILL.md")
    return TemplateProbeResult(available=False, method="none", detail="")


def _km_layer_summary(
    config: DesignTemplateConfig,
    *,
    template_probe: dict[str, Any],
    content_status: str,
    fallback_reason: str | None,
    content_checked_at: str | None,
    content_source: str,
) -> dict[str, Any]:
    ready = content_status == "ready"
    custom = ready and config.kind == "km"
    return {
        "configured": config.configured,
        "pointer": config.pointer if config.kind == "km" else None,
        "kind": config.kind,
        "source": config.source,
        "resolved_path": None,
        "template_probe": template_probe,
        "effective_mode": "custom" if custom else "preset",
        "effective_pointer": config.pointer if custom else PRESET_DESIGN_TEMPLATE_KM,
        "bundle_outline": BUNDLE_DESIGN_TEMPLATE_OUTLINE,
        "fallback_reason": fallback_reason,
        "content_status": content_status,
        "content_checked_at": content_checked_at,
        "content_source": content_source,
    }


def _cached_km_summary(config: DesignTemplateConfig, entry: dict[str, Any]) -> dict[str, Any]:
    probe = entry["probe"]
    template_probe = {
        "available": bool(probe.get("available")),
        "method": str(probe.get("method") or "none"),
        "detail": str(probe.get("detail") or ""),
    }
    return _km_layer_summary(
        config,
        template_probe=template_probe,
        content_status=entry["content_status"],
        fallback_reason=entry["fallback_reason"],
        content_checked_at=entry["checkedAt"],
        content_source="cache",
    )


def _live_markdown_summary(
    config: DesignTemplateConfig,
    *,
    template_probe: TemplateProbeResult,
    checked_at: str,
) -> dict[str, Any]:
    resolved_path = config.resolved_path
    text = _read_text_file(Path(resolved_path)) if resolved_path else None
    if text is None:
        fallback_reason = "unreadable"
    elif not template_content_complete(text):
        fallback_reason = "incomplete"
    else:
        fallback_reason = None
    ready = fallback_reason is None
    return {
        "configured": True,
        "pointer": config.pointer,
        "kind": "markdown",
        "source": config.source,
        "resolved_path": resolved_path,
        "template_probe": asdict(template_probe),
        "effective_mode": "custom" if ready else "preset",
        "effective_pointer": config.pointer if ready else PRESET_DESIGN_TEMPLATE_KM,
        "bundle_outline": BUNDLE_DESIGN_TEMPLATE_OUTLINE,
        "fallback_reason": fallback_reason,
        "content_status": "ready" if ready else "fallback",
        "content_checked_at": checked_at,
        "content_source": "live",
    }


def _path_escape_summary(
    merged: DesignTemplatePointer,
    *,
    template_probe: TemplateProbeResult,
    checked_at: str,
) -> dict[str, Any]:
    return {
        "configured": True,
        "pointer": merged.pointer,
        "kind": "markdown",
        "source": merged.source,
        "resolved_path": None,
        "template_probe": asdict(template_probe),
        "effective_mode": "preset",
        "effective_pointer": PRESET_DESIGN_TEMPLATE_KM,
        "bundle_outline": BUNDLE_DESIGN_TEMPLATE_OUTLINE,
        "fallback_reason": "path_escape",
        "content_status": "fallback",
        "content_checked_at": checked_at,
        "content_source": "live",
    }


def design_template_summary(workspace: Path) -> dict[str, Any]:
    """配置层 + 缓存 verdict，全程零网络 I/O、零子进程。

    markdown 指针本地实时验证（content_source=live）；km/preset 命中未过期缓存
    → content_source=cache；未命中/过期/损坏 → content_source=unknown +
    fallback_reason=unverified。
    """
    try:
        config = resolve_design_template_config(workspace)
    except DevkitFlowError as error:
        # 仅 markdown 路径逃逸降级为 path_escape；配置/指针错误维持原样上抛
        resolution = load_devkit_config(workspace)
        merged = merge_design_template(resolution.global_config, resolution.project_config)
        if merged.kind != "markdown" or not merged.pointer:
            raise error
        return _path_escape_summary(
            merged,
            template_probe=_fast_probe_template_probe(),
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
    if config.kind == "markdown":
        return _live_markdown_summary(
            config,
            template_probe=_fast_probe_template_probe(),
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
    pointer = config.pointer or PRESET_DESIGN_TEMPLATE_KM
    entries = _read_design_template_check_cache(design_template_check_cache_path())
    entry = _fresh_cache_entry(
        entries.get(_design_template_cache_key(pointer)),
        datetime.now(timezone.utc),
    )
    if entry is not None:
        return _cached_km_summary(config, entry)
    return _km_layer_summary(
        config,
        template_probe=asdict(_fast_probe_template_probe()),
        content_status="fallback",
        fallback_reason="unverified",
        content_checked_at=None,
        content_source="unknown",
    )


def design_template_live_summary(workspace: Path) -> dict[str, Any]:
    """实时验证（probe + fetch）并回写 TTL 缓存；供 design-template status/configure 使用。"""
    summary = asdict(resolve_design_template(workspace))
    summary["content_checked_at"] = datetime.now(timezone.utc).isoformat()
    summary["content_source"] = "live"
    if summary["kind"] in ("none", "km"):
        pointer = summary["pointer"] if summary["kind"] == "km" else PRESET_DESIGN_TEMPLATE_KM
        _write_design_template_check_cache(
            design_template_check_cache_path(),
            pointer,
            summary,
        )
    return summary


def remove_design_template(config: dict[str, Any]) -> None:
    config.pop("designTemplate", None)


def command_design_template_configure(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )

    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError(INHERIT_ONLY_PROJECT)
        remove_design_template(target_config)
        message = "Project design-template override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError(CLEAR_ONLY_GLOBAL)
        remove_design_template(target_config)
        message = "Global design-template configuration cleared."
    else:
        pointer = str(args.pointer or "").strip()
        kind = classify_template_pointer(pointer)
        if kind == "markdown":
            resolve_markdown_pointer(workspace, pointer)
        target_config["designTemplate"] = {"pointer": pointer}
        message = f"Design template pointer set for {args.scope} scope."

    save_devkit_config(workspace, target_config, args.scope)
    summary = design_template_live_summary(workspace)
    emit(
        {
            "status": "configured",
            "message": message,
            "design_template": summary,
        },
        args.format,
    )
    return 0


def command_design_template_status(args: argparse.Namespace) -> int:
    summary = design_template_live_summary(Path.cwd())
    emit(
        {
            "status": "ok",
            "message": format_design_template_status_text(summary),
            "design_template": summary,
        },
        args.format,
    )
    return 0


def decide_curator_skip(switch: Any, operation: str) -> CuratorSkipDecision:
    enabled = bool(_mapping_field(switch, "enabled"))
    source = str(_mapping_field(switch, "source") or "default")
    skipped = not enabled
    return CuratorSkipDecision(
        skipped=skipped,
        reason="skipped-disabled" if skipped else "enabled",
        source=source,
        operation=operation,
    )


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def match_sre_intent(raw_text: str) -> SreIntent:
    text = raw_text or ""
    lowered = text.lower()
    matched_term = None
    for term in (*Q9_SRE_TERMS, *Q9_SRE_TOOL_TERMS):
        if term.lower() in lowered:
            matched_term = term
            break
    exclude_reason = None
    if _contains_marker(text, SRE_WRITE_DESIGN_MARKERS):
        exclude_reason = "write-design"
    elif _contains_marker(text, SRE_FULL_FEATURE_MARKERS):
        exclude_reason = "full-feature"
    elif _contains_marker(text, SRE_SCHEMA_MARKERS):
        exclude_reason = "schema"
    return SreIntent(
        raw_text=text,
        matched=matched_term is not None,
        matched_term=matched_term,
        exclude_reason=exclude_reason,
    )


def decide_sre_route(intent: Any, entry_kind: str) -> SreRouteDecision:
    if entry_kind in {"EXPLICIT_COMMAND", "EXPLICIT_AGENT"}:
        return SreRouteDecision(outcome="HANDOFF_SRE", entry_kind=entry_kind, reason="explicit")
    exclude_reason = _mapping_field(intent, "exclude_reason")
    if exclude_reason:
        return SreRouteDecision(
            outcome="SKIP_TO_OTHER",
            entry_kind=entry_kind,
            reason=str(exclude_reason),
        )
    if _mapping_field(intent, "matched"):
        return SreRouteDecision(outcome="HANDOFF_SRE", entry_kind=entry_kind, reason="intent")
    return SreRouteDecision(outcome="SKIP_TO_OTHER", entry_kind=entry_kind, reason="unmatched")


def docs_curator_doctor(workspace: Path) -> dict[str, Any]:
    try:
        summary = docs_curator_summary(workspace)
    except DevkitFlowError as error:
        return {
            "status": "warn",
            "message": compact_error(error),
            "fix": "check .devkit/config.json JSON syntax",
        }
    state = "on" if summary["enabled"] else "off"
    return {"status": "ok", "message": f"{state}/{summary['source']}"}


def design_template_doctor(workspace: Path, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        resolved = summary if summary is not None else design_template_live_summary(workspace)
    except DevkitFlowError as error:
        return {
            "status": "warn",
            "message": compact_error(error),
            "fix": "check .devkit/config.json JSON syntax",
        }
    template_probe = resolved.get("template_probe") or {}
    if resolved.get("kind") == "km" and not template_probe.get("available"):
        return {
            "status": "warn",
            "message": (
                f"km pointer source={resolved.get('source')}; "
                "template_probe unavailable; fallback=preset"
            ),
            "fix": DOCTOR_TEMPLATE_PROBE_FIX,
        }
    return {
        "status": "ok",
        "message": format_design_template_status_text(resolved),
    }
