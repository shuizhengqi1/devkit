from __future__ import annotations

import argparse
import copy
import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from devkit_flow.config import _config_object_layer, _require_boolean_enabled, load_devkit_config, save_devkit_config
from devkit_flow.constants import CLEAR_ONLY_GLOBAL, HTML_UI_INVALID_LAYER, INHERIT_ONLY_PROJECT, SPEC_ARTIFACTS_INVALID_LAYER, SPEC_ARTIFACTS_SCOPE_GLOB, SPECRULES_CACHE_RELATIVE_PATH, SPECRULES_CUSTOM_CACHE_RELATIVE_PATH, SPECRULES_FOLLOW_GLOBAL_SCOPE, SPECRULES_ILLEGAL_REASON, SPECRULES_INVALID_LAYER, SPECRULES_INVALID_URL_MESSAGE, SPECRULES_REPO_URL, SPECRULES_REPO_URL_SCOPE, _SPECRULES_GITMODULES_PATH, _SPECRULES_SCP_URL
from devkit_flow.errors import DevkitFlowError
from devkit_flow.models import DashboardSwitch, DocsCuratorSwitch, GitUrl, HtmlUiSwitch, SpecArtifactsSwitch, SpecrulesResolution, SpecrulesSwitch
from devkit_flow.textio import compact_error, emit


def format_specrules_status_text(summary: dict[str, Any]) -> str:
    state = summary.get("effective_state")
    source = summary.get("source")
    leftover = "; leftover" if summary.get("cleanup_needed") else ""
    if state == "invalid":
        return f"specrules: illegal ({SPECRULES_ILLEGAL_REASON})"
    if state == "disabled":
        return f"specrules: off (source={source}){leftover}"
    if state == "custom-repo":
        return f"specrules: on custom-repo (source={source})"
    return f"specrules: on default-repo (source={source})"


def format_docs_curator_status_text(summary: dict[str, Any]) -> str:
    state = "on" if summary.get("enabled") else "off"
    return f"docs-curator: {state} (source={summary.get('source')})"


def format_html_ui_status_text(summary: dict[str, Any]) -> str:
    state = "on" if summary.get("enabled") else "off"
    return f"html-ui: {state} (source={summary.get('source')})"


def merge_docs_curator(global_config: dict[str, Any], project_config: dict[str, Any]) -> DocsCuratorSwitch:
    global_layer = _config_object_layer(global_config, "docsCurator", "Invalid docsCurator")
    project_layer = _config_object_layer(project_config, "docsCurator", "Invalid docsCurator")
    if global_layer is not None:
        _require_boolean_enabled(global_layer, default=True)
    if project_layer is not None:
        return DocsCuratorSwitch(
            enabled=_require_boolean_enabled(project_layer, default=True),
            source="project",
            default_enabled=True,
        )
    if global_layer is not None:
        return DocsCuratorSwitch(
            enabled=_require_boolean_enabled(global_layer, default=True),
            source="global",
            default_enabled=True,
        )
    return DocsCuratorSwitch(enabled=True, source="default", default_enabled=True)


def docs_curator_summary(workspace: Path) -> dict[str, Any]:
    resolution = load_devkit_config(workspace)
    return asdict(merge_docs_curator(resolution.global_config, resolution.project_config))


def merge_specrules_switch(global_config: dict[str, Any], project_config: dict[str, Any]) -> SpecrulesSwitch:
    global_layer = _config_object_layer(global_config, "specrules", "Invalid specrules")
    project_layer = _config_object_layer(project_config, "specrules", "Invalid specrules")
    if global_layer is not None:
        _require_boolean_enabled(global_layer, default=True)
    if project_layer is not None:
        _require_boolean_enabled(project_layer, default=True)
    if project_layer is not None and "enabled" in project_layer:
        return SpecrulesSwitch(
            enabled=bool(project_layer["enabled"]),
            source="project",
            default_enabled=True,
        )
    if global_layer is not None and "enabled" in global_layer:
        return SpecrulesSwitch(
            enabled=bool(global_layer["enabled"]),
            source="global",
            default_enabled=True,
        )
    return SpecrulesSwitch(enabled=False, source="default", default_enabled=False)


def _specrules_configured_repo_url(layer: dict[str, Any] | None) -> str | None:
    if layer is None or "repoUrl" not in layer:
        return None
    value = layer["repoUrl"]
    if not isinstance(value, str):
        raise DevkitFlowError(SPECRULES_INVALID_URL_MESSAGE)
    return validate_specrules_git_url(value).normalized


def _specrules_workspace_tree(workspace: Path) -> str:
    gitmodules = workspace / ".gitmodules"
    if gitmodules.is_file():
        try:
            text = gitmodules.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if _SPECRULES_GITMODULES_PATH.search(text):
            return "submodule"
    if (workspace / "specrules").exists():
        return "directory"
    return "none"


def _resolve_specrules_from_configs(
    workspace: Path,
    global_config: dict[str, Any],
    project_config: dict[str, Any],
) -> SpecrulesResolution:
    switch = merge_specrules_switch(global_config, project_config)
    global_layer = _config_object_layer(global_config, "specrules", SPECRULES_INVALID_LAYER)
    project_layer = _config_object_layer(project_config, "specrules", SPECRULES_INVALID_LAYER)
    ignored_global_repo_url = global_layer is not None and "repoUrl" in global_layer
    configured_repo_url = _specrules_configured_repo_url(project_layer)
    has_project_url = configured_repo_url is not None
    if not switch.enabled and has_project_url:
        effective_state = "invalid"
        repo_url: str | None = None
        repo_url_source = "none"
        illegal = True
        illegal_reason: str | None = SPECRULES_ILLEGAL_REASON
    elif not switch.enabled:
        effective_state = "disabled"
        repo_url = None
        repo_url_source = "none"
        illegal = False
        illegal_reason = None
    elif has_project_url:
        effective_state = "custom-repo"
        repo_url = configured_repo_url
        repo_url_source = "project"
        illegal = False
        illegal_reason = None
    else:
        effective_state = "default-repo"
        repo_url = SPECRULES_REPO_URL
        repo_url_source = "default"
        illegal = False
        illegal_reason = None
    workspace_tree = _specrules_workspace_tree(workspace)
    cleanup_needed = workspace_tree != "none" and effective_state in {"disabled", "invalid"}
    return SpecrulesResolution(
        enabled=switch.enabled,
        source=switch.source,
        default_enabled=switch.default_enabled,
        effective_state=effective_state,
        repo_url=repo_url,
        repo_url_source=repo_url_source,
        configured_repo_url=configured_repo_url,
        illegal=illegal,
        illegal_reason=illegal_reason,
        ignored_global_repo_url=ignored_global_repo_url,
        workspace_tree=workspace_tree,
        cleanup_needed=cleanup_needed,
    )


def resolve_specrules(workspace: Path) -> SpecrulesResolution:
    loaded = load_devkit_config(workspace)
    return _resolve_specrules_from_configs(
        workspace, loaded.global_config, loaded.project_config
    )


def specrules_summary(workspace: Path) -> dict[str, Any]:
    return asdict(resolve_specrules(workspace))


def validate_specrules_git_url(raw: str) -> GitUrl:
    if not isinstance(raw, str):
        raise DevkitFlowError(SPECRULES_INVALID_URL_MESSAGE)
    normalized = raw.strip()
    if not normalized:
        raise DevkitFlowError(SPECRULES_INVALID_URL_MESSAGE)
    scp = _SPECRULES_SCP_URL.fullmatch(normalized)
    if scp is not None:
        host, path = scp.group(1), scp.group(2)
        if host and path.strip("/"):
            return GitUrl(raw=raw, normalized=normalized, scheme="ssh")
        raise DevkitFlowError(SPECRULES_INVALID_URL_MESSAGE)
    parsed = urlparse(normalized)
    scheme = (parsed.scheme or "").lower()
    if scheme in {"ssh", "https"} and parsed.hostname and parsed.path.strip("/"):
        return GitUrl(raw=raw, normalized=normalized, scheme=scheme)
    raise DevkitFlowError(SPECRULES_INVALID_URL_MESSAGE)


def specrules_cache_dir_for_url(url: str) -> Path:
    normalized = validate_specrules_git_url(url).normalized
    if normalized == SPECRULES_REPO_URL:
        return Path.home() / SPECRULES_CACHE_RELATIVE_PATH
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return Path.home() / SPECRULES_CUSTOM_CACHE_RELATIVE_PATH / digest


def _persist_specrules_repo_url(raw: str) -> str:
    parsed = validate_specrules_git_url(raw)
    if parsed.scheme == "https":
        parts = urlparse(parsed.normalized)
        if parts.username or parts.password:
            raise DevkitFlowError(
                SPECRULES_INVALID_URL_MESSAGE,
                error_code="SPECRULES_INVALID_REPO_URL",
            )
    return parsed.normalized


def _apply_specrules_configure(config: dict[str, Any], args: argparse.Namespace) -> None:
    layer = config.get("specrules")
    if layer is None:
        layer = {}
    elif not isinstance(layer, dict):
        raise DevkitFlowError(SPECRULES_INVALID_LAYER, error_code="SPECRULES_INVALID_LAYER")
    else:
        layer = dict(layer)

    if args.follow_global:
        layer.pop("enabled", None)
    elif args.enable:
        layer["enabled"] = True
    elif args.disable:
        layer["enabled"] = False

    if args.use_default_repo:
        layer.pop("repoUrl", None)
    elif args.repo_url:
        layer["repoUrl"] = _persist_specrules_repo_url(args.repo_url)

    if layer:
        config["specrules"] = layer
    else:
        config.pop("specrules", None)


def _specrules_configure_message(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if args.follow_global:
        parts.append("Project specrules override removed; following global switch.")
    elif args.enable:
        parts.append(f"Specrules enabled for {args.scope} scope.")
    elif args.disable:
        parts.append(f"Specrules disabled for {args.scope} scope.")
    if args.repo_url:
        parts.append("Project specrules repo URL set.")
    elif args.use_default_repo:
        parts.append("Project specrules repo URL removed.")
    return " ".join(parts)


def command_specrules_configure(args: argparse.Namespace) -> int:
    if args.follow_global and args.scope != "project":
        raise DevkitFlowError(
            SPECRULES_FOLLOW_GLOBAL_SCOPE,
            error_code="SPECRULES_FOLLOW_GLOBAL_SCOPE",
        )
    if (args.repo_url or args.use_default_repo) and args.scope != "project":
        raise DevkitFlowError(
            SPECRULES_REPO_URL_SCOPE,
            error_code="SPECRULES_REPO_URL_SCOPE",
        )
    if args.repo_url:
        _persist_specrules_repo_url(args.repo_url)

    workspace = Path.cwd()
    loaded = load_devkit_config(workspace)
    dry_global = copy.deepcopy(loaded.global_config)
    dry_project = copy.deepcopy(loaded.project_config)
    target = dry_global if args.scope == "global" else dry_project
    _apply_specrules_configure(target, args)
    dry = _resolve_specrules_from_configs(workspace, dry_global, dry_project)
    if dry.effective_state == "invalid":
        raise DevkitFlowError(SPECRULES_ILLEGAL_REASON, error_code="SPECRULES_MUTEX")

    save_devkit_config(workspace, target, args.scope)
    emit(
        {
            "status": "configured",
            "message": _specrules_configure_message(args),
            "specrules": specrules_summary(workspace),
        },
        args.format,
    )
    return 0


def command_specrules_status(args: argparse.Namespace) -> int:
    summary = specrules_summary(Path.cwd())
    emit(
        {
            "status": "ok",
            "message": format_specrules_status_text(summary),
            "specrules": summary,
        },
        args.format,
    )
    return 0


def command_specrules_refresh(args: argparse.Namespace) -> int:
    """Best-effort remote specrules refresh. Exit 1 only when index.md is missing."""
    from devkit_flow.setup import refresh_specrules

    raw = Path(getattr(args, "target", ".") or ".")
    workspace = raw.expanduser()
    workspace = workspace.resolve() if workspace.is_absolute() else (Path.cwd() / workspace).resolve()
    result = refresh_specrules(workspace)
    status = str(result.get("status") or "error")
    emit(
        {
            "status": status,
            "message": str(result.get("message") or ""),
            "specrules": specrules_summary(workspace),
            "refresh": result,
        },
        args.format,
    )
    return 1 if status == "error" else 0


def specrules_doctor(workspace: Path) -> dict[str, Any]:
    try:
        summary = specrules_summary(workspace)
    except DevkitFlowError as error:
        return {
            "status": "warn",
            "message": compact_error(error),
            "fix": "check .devkit/config.json JSON syntax",
        }

    rank = {"ok": 0, "warn": 1, "error": 2}
    state = summary.get("effective_state")
    source = summary.get("source")
    if state == "invalid":
        result: dict[str, Any] = {
            "status": "warn",
            "message": f"illegal: {SPECRULES_ILLEGAL_REASON}",
            "fix": (
                "devkit-flow specrules configure --scope project --enable "
                "or --use-default-repo"
            ),
        }
    elif state == "disabled":
        if summary.get("cleanup_needed"):
            result = {
                "status": "warn",
                "message": (
                    f"disabled (source: {source}); specrules check skipped; leftover present"
                ),
                "fix": "devkit-flow setup",
            }
        else:
            result = {
                "status": "ok",
                "message": f"disabled (source: {source}); specrules check skipped",
            }
    elif (workspace / "specrules" / "rules" / "index.md").is_file():
        result = {
            "status": "ok",
            "message": f"present; {state} (source={source})",
        }
    else:
        result = {
            "status": "error",
            "message": "specrules/rules/index.md is missing",
            "fix": "devkit-flow setup",
        }

    if summary.get("ignored_global_repo_url"):
        current = result["status"]
        result["status"] = current if rank[current] >= rank["warn"] else "warn"
        ignored = "ignored global specrules.repoUrl"
        if ignored not in result["message"]:
            result["message"] = f"{result['message']}; {ignored}"
        if result["status"] == "warn" and not result.get("fix"):
            result["fix"] = "remove specrules.repoUrl from the global config"
    return result


def remove_docs_curator(config: dict[str, Any]) -> None:
    config.pop("docsCurator", None)


def command_docs_curator_configure(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )

    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError(INHERIT_ONLY_PROJECT)
        remove_docs_curator(target_config)
        message = "Project docs-curator override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError(CLEAR_ONLY_GLOBAL)
        remove_docs_curator(target_config)
        message = "Global docs-curator configuration cleared."
    else:
        enabled = bool(args.enable)
        target_config["docsCurator"] = {"enabled": enabled}
        message = f"Docs-curator {'enabled' if enabled else 'disabled'} for {args.scope} scope."

    save_devkit_config(workspace, target_config, args.scope)
    summary = docs_curator_summary(workspace)
    emit(
        {
            "status": "configured",
            "message": message,
            "docs_curator": summary,
        },
        args.format,
    )
    return 0


def command_docs_curator_status(args: argparse.Namespace) -> int:
    summary = docs_curator_summary(Path.cwd())
    emit(
        {
            "status": "ok",
            "message": format_docs_curator_status_text(summary),
            "docs_curator": summary,
        },
        args.format,
    )
    return 0


def format_dashboard_status_text(summary: dict[str, Any]) -> str:
    state = "on" if summary.get("enabled") else "off"
    text = f"dashboard: {state} (source={summary.get('source')})"
    if not summary.get("enabled"):
        text += "; 已关闭：状态写入不再生成/刷新 dashboard.html，存量文件不清理不更新"
    return text


def merge_dashboard_switch(global_config: dict[str, Any], project_config: dict[str, Any]) -> DashboardSwitch:
    global_layer = _config_object_layer(global_config, "dashboard", "Invalid dashboard")
    project_layer = _config_object_layer(project_config, "dashboard", "Invalid dashboard")
    if global_layer is not None:
        _require_boolean_enabled(global_layer, default=True)
    if project_layer is not None:
        return DashboardSwitch(
            enabled=_require_boolean_enabled(project_layer, default=True),
            source="project",
            default_enabled=True,
        )
    if global_layer is not None:
        return DashboardSwitch(
            enabled=_require_boolean_enabled(global_layer, default=True),
            source="global",
            default_enabled=True,
        )
    return DashboardSwitch(enabled=True, source="default", default_enabled=True)


def dashboard_switch_summary(workspace: Path) -> dict[str, Any]:
    resolution = load_devkit_config(workspace)
    return asdict(merge_dashboard_switch(resolution.global_config, resolution.project_config))


def remove_dashboard_switch(config: dict[str, Any]) -> None:
    config.pop("dashboard", None)


def command_dashboard_configure(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )

    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError(INHERIT_ONLY_PROJECT)
        remove_dashboard_switch(target_config)
        message = "Project dashboard override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError(CLEAR_ONLY_GLOBAL)
        remove_dashboard_switch(target_config)
        message = "Global dashboard configuration cleared."
    else:
        enabled = bool(args.enable)
        target_config["dashboard"] = {"enabled": enabled}
        message = f"Dashboard {'enabled' if enabled else 'disabled'} for {args.scope} scope."

    save_devkit_config(workspace, target_config, args.scope)
    summary = dashboard_switch_summary(workspace)
    emit(
        {
            "status": "configured",
            "message": message,
            "dashboard": summary,
        },
        args.format,
    )
    return 0


def command_dashboard_status(args: argparse.Namespace) -> int:
    summary = dashboard_switch_summary(Path.cwd())
    emit(
        {
            "status": "ok",
            "message": format_dashboard_status_text(summary),
            "dashboard": summary,
        },
        args.format,
    )
    return 0


def merge_html_ui(global_config: dict[str, Any], project_config: dict[str, Any]) -> HtmlUiSwitch:
    global_layer = _config_object_layer(global_config, "htmlUi", HTML_UI_INVALID_LAYER)
    project_layer = _config_object_layer(project_config, "htmlUi", HTML_UI_INVALID_LAYER)
    if global_layer is not None:
        _require_boolean_enabled(global_layer, default=False)
    if project_layer is not None:
        return HtmlUiSwitch(
            enabled=_require_boolean_enabled(project_layer, default=False),
            source="project",
            default_enabled=False,
        )
    if global_layer is not None:
        return HtmlUiSwitch(
            enabled=_require_boolean_enabled(global_layer, default=False),
            source="global",
            default_enabled=False,
        )
    return HtmlUiSwitch(enabled=False, source="default", default_enabled=False)


def html_ui_summary(workspace: Path) -> dict[str, Any]:
    resolution = load_devkit_config(workspace)
    return asdict(merge_html_ui(resolution.global_config, resolution.project_config))


def remove_html_ui(config: dict[str, Any]) -> None:
    config.pop("htmlUi", None)


def command_html_ui_configure(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )
    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError(INHERIT_ONLY_PROJECT)
        remove_html_ui(target_config)
        message = "Project HTML UI override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError(CLEAR_ONLY_GLOBAL)
        remove_html_ui(target_config)
        message = "Global HTML UI configuration cleared."
    else:
        enabled = bool(args.enable)
        target_config["htmlUi"] = {"enabled": enabled}
        message = f"HTML UI {'enabled' if enabled else 'disabled'} for {args.scope} scope."
    save_devkit_config(workspace, target_config, args.scope)
    summary = html_ui_summary(workspace)
    emit({"status": "configured", "message": message, "html_ui": summary}, args.format)
    return 0


def command_html_ui_status(args: argparse.Namespace) -> int:
    summary = html_ui_summary(Path.cwd())
    emit(
        {
            "status": "ok",
            "message": format_html_ui_status_text(summary),
            "html_ui": summary,
        },
        args.format,
    )
    return 0


def format_spec_artifacts_status_text(summary: dict[str, Any]) -> str:
    state = "on" if summary.get("enabled") else "off"
    return f"spec-artifacts: {state} (source={summary.get('source')})"


def merge_spec_artifacts(
    global_config: dict[str, Any], project_config: dict[str, Any]
) -> SpecArtifactsSwitch:
    global_layer = _config_object_layer(
        global_config, "specArtifacts", SPEC_ARTIFACTS_INVALID_LAYER
    )
    project_layer = _config_object_layer(
        project_config, "specArtifacts", SPEC_ARTIFACTS_INVALID_LAYER
    )
    if global_layer is not None:
        _require_boolean_enabled(global_layer, default=True)
    if project_layer is not None:
        return SpecArtifactsSwitch(
            enabled=_require_boolean_enabled(project_layer, default=True),
            source="project",
            default_enabled=True,
            scope_glob=SPEC_ARTIFACTS_SCOPE_GLOB,
        )
    if global_layer is not None:
        return SpecArtifactsSwitch(
            enabled=_require_boolean_enabled(global_layer, default=True),
            source="global",
            default_enabled=True,
            scope_glob=SPEC_ARTIFACTS_SCOPE_GLOB,
        )
    return SpecArtifactsSwitch(
        enabled=True,
        source="default",
        default_enabled=True,
        scope_glob=SPEC_ARTIFACTS_SCOPE_GLOB,
    )


def spec_artifacts_summary(workspace: Path) -> dict[str, Any]:
    resolution = load_devkit_config(workspace)
    return asdict(merge_spec_artifacts(resolution.global_config, resolution.project_config))


def spec_dir_included_in_commit(switch: SpecArtifactsSwitch) -> bool:
    return bool(switch.enabled)


def remove_spec_artifacts(config: dict[str, Any]) -> None:
    config.pop("specArtifacts", None)


def spec_artifacts_doctor(workspace: Path) -> dict[str, Any]:
    try:
        summary = spec_artifacts_summary(workspace)
    except DevkitFlowError as error:
        return {
            "status": "warn",
            "message": compact_error(error),
            "fix": "check .devkit/config.json JSON syntax",
        }
    state = "on" if summary["enabled"] else "off"
    return {"status": "ok", "message": f"{state} (source={summary['source']})"}


def command_spec_artifacts_configure(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )
    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError(
                INHERIT_ONLY_PROJECT, error_code="SPEC_ARTIFACTS_INHERIT_SCOPE"
            )
        remove_spec_artifacts(target_config)
        message = "Project spec-artifacts override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError(
                CLEAR_ONLY_GLOBAL, error_code="SPEC_ARTIFACTS_CLEAR_SCOPE"
            )
        remove_spec_artifacts(target_config)
        message = "Global spec-artifacts configuration cleared."
    else:
        enabled = bool(args.enable)
        target_config["specArtifacts"] = {"enabled": enabled}
        message = (
            f"Spec artifacts {'enabled' if enabled else 'disabled'} for {args.scope} scope."
        )
    save_devkit_config(workspace, target_config, args.scope)
    summary = spec_artifacts_summary(workspace)
    emit(
        {
            "status": "configured",
            "message": message,
            "spec_artifacts": summary,
        },
        args.format,
    )
    return 0


def command_spec_artifacts_status(args: argparse.Namespace) -> int:
    summary = spec_artifacts_summary(Path.cwd())
    emit(
        {
            "status": "ok",
            "message": format_spec_artifacts_status_text(summary),
            "spec_artifacts": summary,
        },
        args.format,
    )
    return 0
