from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from devkit_flow.config import _config_object_layer, _require_boolean_enabled, _specrules_configured_repo_url, load_config_file, load_devkit_config, merge_specrules_switch, save_devkit_config, validate_specrules_git_url
from devkit_flow.constants import CLEAR_ONLY_GLOBAL, CONFIG_PATH, HTML_UI_INVALID_LAYER, INHERIT_ONLY_PROJECT, PROGRESS_BAR_INVALID_LAYER, SPECRULES_FOLLOW_GLOBAL_SCOPE, SPECRULES_ILLEGAL_REASON, SPECRULES_INVALID_LAYER, SPECRULES_INVALID_URL_MESSAGE, SPECRULES_REPO_URL, SPECRULES_REPO_URL_SCOPE, SPEC_ARTIFACTS_INVALID_LAYER, SPEC_ARTIFACTS_SCOPE_GLOB, _SPECRULES_GITMODULES_PATH
from devkit_flow.errors import DevkitFlowError
from devkit_flow.models import DashboardSwitch, DocsCuratorSwitch, HtmlUiSwitch, ProgressBarSwitch, SpecArtifactsSwitch, SpecrulesResolution
from devkit_flow.textio import compact_error, emit
from devkit_flow.project_detect import ProfileCache
from devkit_flow.rule_sources import RULE_PACKAGE_KINDS, BindingReceipt, RuleSourceCatalog, RuleSourceSelector, compute_rule_context


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


def _binding_receipt_summary(receipt: BindingReceipt | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    if not receipt.readable:
        return {"readable": False, "problem": receipt.problem, "adoptedBy": None, "boundAt": None, "modules": []}
    return {
        "readable": True,
        "problem": None,
        "adoptedBy": receipt.adoptedBy,
        "boundAt": receipt.boundAt,
        "modules": [
            {
                "module": item.module,
                "kind": item.kind,
                "repoUrl": item.repoUrl,
                "commit": item.commit,
                "packageVersion": item.packageVersion,
                "distribution": item.distribution,
            }
            for item in receipt.modules
        ],
    }


def specrules_routing_preview(workspace: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """``specrules status`` 的加性键（设计 §4.7.1.11）：``rule_routing`` 为六步判定预览（画像取自缓存，不识别、
    不拉取、不写文件），``binding`` 为绑定回执摘要（无回执为 null）。任何异常只体现在 ``rule_routing.error``。"""
    receipt: BindingReceipt | None = None
    try:
        receipt = BindingReceipt.load(workspace)
        global_config, _ = load_config_file(Path.home() / ".config" / "devkit" / "config.json")
        project_config, _ = load_config_file(workspace / CONFIG_PATH)
        catalog = RuleSourceCatalog.fromConfig(global_config, project_config)
        profile = ProfileCache().read(workspace)
        decision = RuleSourceSelector().routingMode(
            workspace, catalog, profile, receipt, catalog.sourceDirs(), allowImplicitAdoption=False
        )
        routing: dict[str, Any] = {
            "mode": decision.mode,
            "routing_source": decision.source,
            "adoptedBy": decision.adoptedBy,
            "advice": list(decision.advice),
        }
    except Exception as error:  # noqa: BLE001 - 加性诊断键不得让既有命令失败
        routing = {"mode": None, "routing_source": None, "adoptedBy": None, "advice": [], "error": compact_error(error)}
    return routing, _binding_receipt_summary(receipt)


def command_specrules_status(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    summary = specrules_summary(workspace)
    routing, binding = specrules_routing_preview(workspace)
    emit(
        {
            "status": "ok",
            "message": format_specrules_status_text(summary),
            "specrules": summary,
            # 016 T-05：只加键（既有键与 message 不变）
            "rule_routing": routing,
            "binding": binding,
        },
        args.format,
    )
    return 0


# ---------------------------------------------------------------------------
# 016 T-05：specrules catalog status | configure（接口 §2.4）
# ---------------------------------------------------------------------------


def _raw_catalog_entries(config: dict[str, Any]) -> dict[str, Any]:
    """D-32 ④：禁用态原样列出用户写入的 catalog 条目（不校验、不报错，标 validated=false）。"""
    rules = config.get("ruleSources") if isinstance(config, dict) else None
    catalog = rules.get("catalog") if isinstance(rules, dict) else None
    if not isinstance(catalog, dict):
        return {}
    listed: dict[str, Any] = {}
    for kind, entry in catalog.items():
        repo_url = entry.get("repoUrl") if isinstance(entry, dict) else entry
        listed[str(kind)] = {"repoUrl": repo_url, "validated": False}
    return listed


def specrules_catalog_summary(workspace: Path) -> dict[str, Any]:
    """``specrules catalog status`` 主体：catalog 两层与合并结果、路由预览、绑定预览（只读已有缓存，不挂载、不拉取）。"""
    global_config, _ = load_config_file(Path.home() / ".config" / "devkit" / "config.json")
    project_config, _ = load_config_file(workspace / CONFIG_PATH)
    catalog = RuleSourceCatalog.fromConfig(global_config, project_config)
    if not catalog.specrulesEnabled:
        return {
            "catalog": {
                "global": _raw_catalog_entries(global_config),
                "project": _raw_catalog_entries(project_config),
                "merged": {},
            },
            "routing_mode": "disabled",
            "routing_source": "disabled",
            "adopted_by": None,
            "modules": [],
            "advice": ["specrules.enabled=false：ruleSources 不生效，catalog 条目未校验"]
            + (["specrules 配置非法：enabled=false 与项目 repoUrl 并存"] if catalog.specrulesInvalid else []),
        }
    merged: dict[str, Any] = {}
    for kind in RULE_PACKAGE_KINDS:
        if kind in catalog.projectCatalog:
            merged[kind] = {"repoUrl": catalog.projectCatalog[kind], "source": "project"}
        elif kind in catalog.globalCatalog:
            merged[kind] = {"repoUrl": catalog.globalCatalog[kind], "source": "global"}
        elif catalog.urlFor(kind) is not None:
            merged[kind] = {"repoUrl": catalog.urlFor(kind), "source": "default"}
    context, decision = compute_rule_context(workspace)
    modules = []
    for item in context.modules:
        binding = item.binding
        modules.append(
            {
                "module": binding.module,
                "kind": binding.kind,
                "bindingStatus": binding.bindingStatus,
                "repoUrl": binding.repoUrl,
                "packageVersion": binding.packageVersion,
                "entry": binding.entry,
                "advice": list(binding.advice),
            }
        )
    return {
        "catalog": {
            "global": {kind: {"repoUrl": url, "validated": True} for kind, url in catalog.globalCatalog.items()},
            "project": {kind: {"repoUrl": url, "validated": True} for kind, url in catalog.projectCatalog.items()},
            "merged": merged,
        },
        "routing_mode": decision.mode,
        "routing_source": decision.source,
        "adopted_by": decision.adoptedBy,
        "modules": modules,
        "advice": list(decision.advice),
    }


def _format_catalog_status_text(summary: dict[str, Any]) -> str:
    adopted = f", {summary['adopted_by']}" if summary.get("adopted_by") else ""
    lines = [f"rule routing: {summary['routing_mode']} ({summary['routing_source']}{adopted})"]
    for kind, entry in summary["catalog"]["merged"].items():
        lines.append(f"  catalog {kind}: {entry['repoUrl']} (source={entry['source']})")
    for scope in ("global", "project"):
        for kind, entry in summary["catalog"][scope].items():
            if not entry.get("validated", True):
                lines.append(f"  catalog {kind} ({scope}, not validated): {entry['repoUrl']}")
    for module in summary["modules"]:
        lines.append(f"  - {module['module']} [{module['kind'] or '-'}] {module['bindingStatus']}")
    lines.extend(f"  advice: {item}" for item in summary["advice"])
    return "\n".join(lines)


def command_specrules_catalog_status(args: argparse.Namespace) -> int:
    summary = specrules_catalog_summary(Path.cwd())
    emit({"status": "ok", "message": _format_catalog_status_text(summary), **summary}, args.format)
    return 0


def command_specrules_catalog_configure(args: argparse.Namespace) -> int:
    """只写配置、不挂载（挂载在下一次 setup）；校验失败 exit 1 且配置不写入（接口 §2.4）。"""
    kind = getattr(args, "kind", None)
    repo_url = getattr(args, "repo_url", None)
    remove = bool(getattr(args, "remove", False))
    routing = getattr(args, "routing", None)
    module = getattr(args, "module", None)
    scope = getattr(args, "scope", "project") or "project"
    if (routing is not None or module is not None) and scope == "global":
        raise DevkitFlowError(
            "--routing / --module are project-only; use --scope project",
            error_code="SPECRULES_CATALOG_PROJECT_ONLY",
        )
    if remove and repo_url is not None:
        raise DevkitFlowError("--remove cannot be combined with --repo-url", error_code="SPECRULES_CATALOG_MUTEX")
    if (repo_url is not None or remove or module is not None) and kind is None:
        raise DevkitFlowError(
            "--repo-url / --remove / --module require --kind", error_code="SPECRULES_CATALOG_KIND_REQUIRED"
        )
    if repo_url is not None:
        # 校验规则同 specrules configure --repo-url（ssh / https / scp）；catalog 命令按接口 §2.4 统一带错误码
        try:
            _persist_specrules_repo_url(repo_url)
        except DevkitFlowError as error:
            if error.error_code:
                raise
            raise DevkitFlowError(str(error), error_code="SPECRULES_INVALID_REPO_URL") from error
    workspace = Path.cwd()
    loaded = load_devkit_config(workspace)
    target = loaded.global_config if scope == "global" else loaded.project_config
    if kind is not None and module is None and repo_url is None and not remove:
        raise DevkitFlowError(
            "--kind needs --repo-url or --remove (or --module for a module binding)",
            error_code="SPECRULES_CATALOG_KIND_REQUIRED",
        )
    patched = RuleSourceCatalog.configurePatch(
        target, kind, repo_url, remove, routing=routing, module=module
    )
    save_devkit_config(workspace, patched, scope)
    changes = []
    if routing is not None:
        changes.append(f"routing={routing}")
    if module is not None:
        changes.append(f"module {module} {'removed' if remove else f'→ {kind}' + (f' {repo_url}' if repo_url else '')}")
    elif kind is not None:
        changes.append(f"catalog {kind} {'removed' if remove else f'→ {repo_url}'}")
    message = f"rule source catalog configured ({scope}): {', '.join(changes)}; run devkit-flow setup to apply"
    try:
        summary = specrules_catalog_summary(workspace)
    except DevkitFlowError as error:
        summary = {"error": compact_error(error)}
    emit({"status": "configured", "message": message, "scope": scope, **summary}, args.format)
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


def format_progress_bar_status_text(summary: dict[str, Any]) -> str:
    state = "on" if summary.get("enabled") else "off"
    text = f"progress-bar: {state} (source={summary.get('source')})"
    if not summary.get("enabled"):
        text += "; 已关闭：Claude Code 输入框上方不再显示 DevKit 进度行，/devkit 面板与 token 记账不受影响"
    return text


def merge_progress_bar(global_config: dict[str, Any], project_config: dict[str, Any]) -> ProgressBarSwitch:
    global_layer = _config_object_layer(global_config, "progressBar", PROGRESS_BAR_INVALID_LAYER)
    project_layer = _config_object_layer(project_config, "progressBar", PROGRESS_BAR_INVALID_LAYER)
    if global_layer is not None:
        _require_boolean_enabled(global_layer, default=True)
    if project_layer is not None:
        return ProgressBarSwitch(
            enabled=_require_boolean_enabled(project_layer, default=True),
            source="project",
            default_enabled=True,
        )
    if global_layer is not None:
        return ProgressBarSwitch(
            enabled=_require_boolean_enabled(global_layer, default=True),
            source="global",
            default_enabled=True,
        )
    return ProgressBarSwitch(enabled=True, source="default", default_enabled=True)


def progress_bar_summary(workspace: Path) -> dict[str, Any]:
    resolution = load_devkit_config(workspace)
    return asdict(merge_progress_bar(resolution.global_config, resolution.project_config))


def remove_progress_bar(config: dict[str, Any]) -> None:
    config.pop("progressBar", None)


def command_progress_bar_configure(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )
    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError(INHERIT_ONLY_PROJECT)
        remove_progress_bar(target_config)
        message = "Project progress bar override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError(CLEAR_ONLY_GLOBAL)
        remove_progress_bar(target_config)
        message = "Global progress bar configuration cleared."
    else:
        enabled = bool(args.enable)
        target_config["progressBar"] = {"enabled": enabled}
        message = f"Progress bar {'enabled' if enabled else 'disabled'} for {args.scope} scope."
    save_devkit_config(workspace, target_config, args.scope)
    summary = progress_bar_summary(workspace)
    emit({"status": "configured", "message": message, "progress_bar": summary}, args.format)
    return 0


def command_progress_bar_status(args: argparse.Namespace) -> int:
    summary = progress_bar_summary(Path.cwd())
    emit(
        {
            "status": "ok",
            "message": format_progress_bar_status_text(summary),
            "progress_bar": summary,
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
