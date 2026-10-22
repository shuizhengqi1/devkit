from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any
from devkit_flow.codex_agents import codex_agent_registration_check, codex_user_home
from devkit_flow.config import context_source_summary, format_review_gate_timing_status_text, kba_context_source_doctor, load_config_file, load_review_gate_timing_summary, ones_config_summary, specrules_cache_dir_for_url, validate_specrules_git_url
from devkit_flow.constants import CONFIG_PATH, AGENT_DATA_PAYLOAD_FILES, DEVKIT_CLI_RELATIVE_PATH, LEGACY_RUNTIME_PLUGIN_NAMES, RUNTIME_PLUGIN_NAME, SPECRULES_CLEANUP_BACKUP_NAME, SPECRULES_CLEANUP_MANUAL, SPECRULES_DISABLED_NO_TREE, SPECRULES_DISABLED_REMOVED, SPECRULES_REFRESH_GIT_TIMEOUT_SECONDS, SPECRULES_REPO_URL, SPECRULES_SETUP_ILLEGAL_REASON, SPECRULES_URL_SWITCH_BACKUP_NAME, TELEMETRY_SSO_IDENTITY_CACHE_RELATIVE_PATH, _SPECRULES_GITMODULES_PATH
from devkit_flow.e2e import e2e_doctor
from devkit_flow.errors import DevkitFlowError, PrReviewExit
from devkit_flow.html_ui import html_ui_doctor
from devkit_flow.models import SpecrulesResolution, UserStopHookEntry, UserStopHookScan
from devkit_flow.project_detect import ProfileCache, ProjectDetector, ProjectProfile
from devkit_flow.rule_sources import (
    RULE_BINDING_SCHEMA_VERSION,
    RULE_PACKAGE_ENTRY,
    RULES_ADVICE_NO_KIND,
    RULES_BACKUP_RELATIVE_DIR,
    RULES_BINDING_MISMATCH,
    RULES_INCOMPATIBLE_SOURCE,
    RULES_INCOMPLETE_SOURCE,
    RULES_MIGRATION_CONFLICT,
    RULES_MIGRATIONS_RELATIVE_DIR,
    RULES_OK_STATUSES,
    RULES_STATUS_ERROR_CODES,
    RULES_TREE_NAME,
    BindingReceipt,
    ManagedFile,
    ManagedTreeMigrator,
    MigrationPlan,
    MigrationReceipt,
    RoutingDecision,
    RuleBinding,
    RuleClosureResolver,
    RulePackageMeta,
    RuleSourceCatalog,
    RuleSourceSelector,
    _rs_entry_digest,
    _rs_lenient_package_fields,
    _rs_now_iso,
    _rs_now_stamp,
    _rs_tree_files,
    _rs_validate_source,
    hold_bindings,
    legacy_refresh_guard,
)
from devkit_flow.review import design_template_doctor, design_template_summary, docs_curator_doctor, review_rounds_doctor, review_rounds_summary
from devkit_flow.selfupdate import cli_code_path, extract_embedded_version, find_global_cli
from devkit_flow.status_cmd import assemble_version_provenance, doctor_detect_project, doctor_project_detection, doctor_rule_binding, doctor_runtime_transactions, relate_source_checkout
from devkit_flow.switches import docs_curator_summary, html_ui_summary, resolve_specrules, spec_artifacts_doctor, spec_artifacts_summary, specrules_doctor, specrules_summary
from devkit_flow.templates_md import PROJECT_RULES_TEMPLATE, WORKSPACE_ROUTING_TEMPLATE
from devkit_flow.telemetry import offer_sso_login, telemetry_doctor_check
from devkit_flow.textio import atomic_write_text, compact_error, current_cli_path, emit, load_support_file_readonly, run_external, runtime_support_root, sha256_file
from devkit_flow.version import NPM_PACKAGE_NAME, NPM_REGISTRY, VERSION
from devkit_flow.workflow_auto import workflow_auto_takeover_doctor_message, workflow_auto_takeover_summary

CODEX_STOP_JSON_EXAMPLE = '{"continue":true,"suppressOutput":true}'
CODEX_USER_STOP_HOOKS_FIX = (
    "Codex Stop hooks must emit valid JSON on stdout; send other logs to stderr. "
    "Empty stdout causes Hook failed / invalid stop hook JSON. "
    f"Minimal example: {CODEX_STOP_JSON_EXAMPLE}. "
    "Keep stdout JSON-only on the success path, or wrap the original command: "
    f'cmd "$@" >&2; echo \'{CODEX_STOP_JSON_EXAMPLE}\'. '
    "This tool does not modify ~/.codex/hooks.json and will not auto-edit "
    "third-party scripts."
)


def specrules_cache_dir(url: str = SPECRULES_REPO_URL) -> Path:
    return specrules_cache_dir_for_url(url)


_GIT_COMMAND_PREFIX = "git command exited non-zero"
# D-56：与 scripts/runtime_cli/host_diagnostics.py 同一提取器，按文件路径只读加载（不写 bytecode、不执行包 __init__）。
_HOST_DIAGNOSTICS: Any = None


def _host_diagnostics_module() -> Any:
    """Load the shared whitelist extractor by file path. A load failure returns None."""
    global _HOST_DIAGNOSTICS
    if _HOST_DIAGNOSTICS is not None:
        return _HOST_DIAGNOSTICS
    try:
        support = runtime_support_root("runtime_cli")
        if support is None:
            return None
        module = load_support_file_readonly(
            support,
            "runtime_cli/host_diagnostics.py",
            "devkit_setup_host_diagnostics",
        )
    except (ImportError, OSError, SyntaxError):
        return None
    _HOST_DIAGNOSTICS = module
    return module


def _git_host_diagnostics(
    stderr: str | bytes | None,
    stdout: str | bytes | None = None,
    *,
    returncode: int | None = None,
) -> Any:
    """``HostDiagnostics`` for a failed Git command, or None when the extractor is unavailable.

    Same split as ``workspace_sync._host_output_diagnostics``: host text only decides which
    closed-enumeration members occurred; it is never copied into any output.
    """
    module = _host_diagnostics_module()
    if module is None:
        return None
    return module.HostDiagnosticExtractor.extract(stdout, stderr, returncode=returncode)


def _single_line_git_diagnostic(
    stderr: str | bytes | None,
    stdout: str | bytes | None = None,
    *,
    returncode: int | None = None,
    limit: int,
) -> str:
    """Render a Git failure from closed-enumeration fields only (D-56).

    ``limit`` is the whole-string budget; fields that do not fit are omitted
    whole (values are never truncated). Without the extractor only the constant
    prefix and the exit code are rendered. Callers keep their existing status,
    exit and cache-fallback semantics.
    """
    module = _host_diagnostics_module()
    if module is None:
        if isinstance(returncode, int) and not isinstance(returncode, bool) and returncode >= 0:
            return f"{_GIT_COMMAND_PREFIX} (exit {returncode})"
        return _GIT_COMMAND_PREFIX
    diagnostics = _git_host_diagnostics(stderr, stdout, returncode=returncode)
    return module.render_host_failure_detail(_GIT_COMMAND_PREFIX, diagnostics, max_length=limit)


def _git_result_diagnostic(result: Any, *, limit: int) -> str:
    return _single_line_git_diagnostic(
        getattr(result, "stderr", None),
        getattr(result, "stdout", None),
        returncode=getattr(result, "returncode", None),
        limit=limit,
    )


def source_checkout_for(workspace: Path, cli_version: str) -> dict[str, Any]:
    """Read only ``<workspace>/src/devkit_flow/version.py``. A missing file stays not-applicable."""
    path = Path(workspace) / "src" / "devkit_flow" / "version.py"
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    if not exists:
        return {"state": "not-applicable", "source_version": None, "relation": None}
    return relate_source_checkout(extract_embedded_version(path), cli_version)


def doctor_version_provenance(workspace: Path, cli_version: str | None = None) -> dict[str, Any]:
    version = VERSION if cli_version is None else cli_version
    checkout = source_checkout_for(workspace, version)
    return assemble_version_provenance(Path(workspace), version, checkout)


def _specrules_cache_origin_path(cache: Path) -> Path:
    return cache / "origin.url"


def _write_specrules_cache_origin(cache: Path, repo_url: str) -> None:
    normalized = validate_specrules_git_url(repo_url).normalized
    if normalized == SPECRULES_REPO_URL:
        return
    _specrules_cache_origin_path(cache).write_text(normalized + "\n", encoding="utf-8")


def _specrules_cache_origin_matches(cache: Path, repo_url: str) -> bool:
    normalized = validate_specrules_git_url(repo_url).normalized
    if normalized == SPECRULES_REPO_URL:
        return True
    origin = _specrules_cache_origin_path(cache)
    if not origin.is_file():
        return False
    try:
        recorded = origin.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not recorded:
        return False
    try:
        return validate_specrules_git_url(recorded).normalized == normalized
    except DevkitFlowError:
        return recorded == normalized


def ensure_specrules_cache(skip_pull: bool, repo_url: str = SPECRULES_REPO_URL) -> tuple[bool, str]:
    """确保本地 specrules 缓存可用（对齐 scripts/workspace_sync.py ensure_specrules_repo）。

    缓存按 URL 分目录：默认仓沿用现网路径；其它 URL 用 specrules-cache/<hash>/，
    并以 origin.url 校验，不匹配则不 pull 错仓。缓存缺失：skip_pull 时不可用，
    否则 git clone；clone 失败删除本次新建目录。已有匹配缓存：skip_pull 时原样使用，
    否则 git pull --ff-only（pull 失败仅告警，继续用本地副本）。永不抛异常，
    返回 (usable, note)，由调用方决定如何报告。
    """
    cache = specrules_cache_dir(repo_url)
    git = shutil.which("git")
    if cache.exists():
        if not _specrules_cache_origin_matches(cache, repo_url):
            suffix = " and --skip-specrules-pull given" if skip_pull else ""
            return False, (
                f"cache {cache} origin.url does not match {repo_url}; "
                f"not pulling the wrong repo{suffix}"
            )
        if skip_pull:
            return True, "pull skipped (--skip-specrules-pull), using local cache"
        if not git:
            return True, "git is not available; using existing local cache"
        result = run_external([git, "-C", str(cache), "pull", "--ff-only"], check=False)
        if result.returncode != 0:
            detail = _git_result_diagnostic(result, limit=120)
            return True, f"git pull failed, using existing local cache: {detail}"
        return True, "cache pulled from remote"
    if skip_pull:
        return False, f"cache {cache} missing and --skip-specrules-pull given"
    if not git:
        return False, "git is not available on PATH; cannot clone the specrules repo"
    cache.parent.mkdir(parents=True, exist_ok=True)
    result = run_external([git, "clone", repo_url, str(cache)], check=False)
    if result.returncode != 0:
        detail = _git_result_diagnostic(result, limit=200)
        if cache.exists():
            shutil.rmtree(cache, ignore_errors=True)
        return False, f"git clone failed: {detail}"
    try:
        _write_specrules_cache_origin(cache, repo_url)
    except OSError as error:
        shutil.rmtree(cache, ignore_errors=True)
        return False, f"git clone failed: cannot write origin.url: {error}"
    return True, "cloned from remote"


def copy_specrules_to_workspace(cache: Path, destination: Path) -> int:
    """把缓存内容拷入目标仓 specrules/（对齐 workspace_sync：仅 *.md，跳过 .git 与根 README.md）。"""
    copied = 0
    for source in sorted(cache.rglob("*.md")):
        relative = source.relative_to(cache)
        if ".git" in relative.parts or relative.as_posix() == "README.md":
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    return copied


def sync_specrules_to_workspace(cache: Path, destination: Path, *, allow_new_files: bool) -> dict[str, list[str]]:
    """目标已有 specrules/ 时的增量同步（对齐 workspace_sync 的存量目录语义）。

    逐文件比对缓存 canonical（仅 *.md，跳过 .git 与根 README.md）与目标：
    内容不同的已有文件 → updated（canonical 覆盖目标侧改动）；内容相同 → identical（不重写）；
    目标缺失的 canonical → 默认 skipped，allow_new_files 时才 created；
    目标里用户自建的额外文件不在 canonical 集合内，永远保留不动（也不列入结果）。
    返回各类别的 specrules 相对路径清单（按路径排序，保证输出稳定）。
    """
    result: dict[str, list[str]] = {"created": [], "updated": [], "identical": [], "skipped": []}
    for source in sorted(cache.rglob("*.md")):
        relative = source.relative_to(cache)
        if ".git" in relative.parts or relative.as_posix() == "README.md":
            continue
        display = f"specrules/{relative.as_posix()}"
        target = destination / relative
        if not target.exists():
            if not allow_new_files:
                result["skipped"].append(display)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            result["created"].append(display)
            continue
        if target.is_file() and target.read_bytes() == source.read_bytes():
            result["identical"].append(display)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        result["updated"].append(display)
    return result


def _specrules_git(
    git: str, repo: Path, args: list[str], *, timeout: float | None = None
) -> Any:
    return run_external(
        [git, "-C", str(repo), *args],
        check=False,
        timeout=timeout if timeout is not None else SPECRULES_REFRESH_GIT_TIMEOUT_SECONDS,
    )


def _specrules_git_sha(git: str, repo: Path, ref: str = "HEAD") -> str | None:
    result = _specrules_git(git, repo, ["rev-parse", "--verify", ref])
    if result.returncode != 0:
        return None
    sha = (result.stdout or "").strip()
    return sha if re.fullmatch(r"[0-9a-f]{7,40}", sha) else None


def _specrules_working_tree_dirty(git: str, specrules_dir: Path) -> bool:
    """True when the inner specrules checkout has local (uncommitted) changes.

    Superproject gitlink drift (recorded SHA behind remote) is not dirty — that
    is the state refresh is meant to advance.
    """
    if not specrules_dir.exists():
        return False
    result = _specrules_git(git, specrules_dir, ["status", "--porcelain"])
    if result.returncode != 0:
        return True
    return any(line.strip() for line in (result.stdout or "").splitlines())


def _specrules_remote_head(git: str, specrules_dir: Path) -> tuple[str, str | None]:
    for ref in ("origin/HEAD", "origin/master", "origin/main"):
        sha = _specrules_git_sha(git, specrules_dir, ref)
        if sha:
            return ref, sha
    return "", None


def _refresh_specrules_submodule(
    workspace: Path, specrules_dir: Path, repo_url: str
) -> dict[str, Any]:
    git = shutil.which("git")
    if not git:
        return {
            "status": "skipped",
            "action": "skipped-no-git",
            "message": "git is not available; keeping local specrules",
            "repo_url": repo_url,
        }
    init = run_external(
        [git, "-C", str(workspace), "submodule", "update", "--init", "specrules"],
        check=False,
        timeout=SPECRULES_REFRESH_GIT_TIMEOUT_SECONDS,
    )
    index = specrules_dir / "rules" / "index.md"
    if not index.is_file():
        return {
            "status": "error",
            "action": "init-failed",
            "message": (
                "specrules submodule is not initialized: "
                f"{_git_result_diagnostic(init, limit=160)}"
            ),
            "repo_url": repo_url,
        }
    if _specrules_working_tree_dirty(git, specrules_dir):
        return {
            "status": "skipped",
            "action": "skipped-dirty",
            "message": (
                "specrules working tree is dirty; refresh skipped to avoid "
                "losing local changes"
            ),
            "repo_url": repo_url,
        }
    before = _specrules_git_sha(git, specrules_dir)
    fetch = _specrules_git(git, specrules_dir, ["fetch", "--quiet"])
    if fetch.returncode != 0:
        return {
            "status": "current",
            "action": "fetch-failed",
            "message": (
                "git fetch failed, using local specrules: "
                f"{_git_result_diagnostic(fetch, limit=160)}"
            ),
            "from": before,
            "to": before,
            "repo_url": repo_url,
        }
    remote_ref, remote_sha = _specrules_remote_head(git, specrules_dir)
    if not remote_sha:
        return {
            "status": "current",
            "action": "no-remote-head",
            "message": "cannot resolve remote HEAD; using local specrules",
            "from": before,
            "to": before,
            "repo_url": repo_url,
        }
    if before == remote_sha:
        short = (before or "")[:7]
        return {
            "status": "current",
            "action": "already-current",
            "message": f"specrules already at {short} ({remote_ref})",
            "from": before,
            "to": remote_sha,
            "repo_url": repo_url,
        }
    ancestor = _specrules_git(
        git, specrules_dir, ["merge-base", "--is-ancestor", "HEAD", remote_sha]
    )
    if ancestor.returncode != 0:
        return {
            "status": "skipped",
            "action": "skipped-diverged",
            "message": (
                f"local specrules diverged from {remote_ref}; refresh skipped"
            ),
            "from": before,
            "to": remote_sha,
            "repo_url": repo_url,
        }
    checkout = _specrules_git(
        git, specrules_dir, ["checkout", "--detach", "--quiet", remote_sha]
    )
    after = _specrules_git_sha(git, specrules_dir) or remote_sha
    if checkout.returncode != 0 or not index.is_file():
        return {
            "status": "current" if index.is_file() else "error",
            "action": "checkout-failed",
            "message": (
                "checkout failed, using local specrules: "
                f"{_git_result_diagnostic(checkout, limit=160)}"
            ),
            "from": before,
            "to": before,
            "repo_url": repo_url,
        }
    return {
        "status": "updated",
        "action": "submodule-ff",
        "message": (
            f"specrules updated {(before or '?')[:7]} → {(after or '?')[:7]} "
            f"({remote_ref})"
        ),
        "from": before,
        "to": after,
        "repo_url": repo_url,
    }


def _refresh_specrules_copy(
    workspace: Path, specrules_dir: Path, repo_url: str
) -> dict[str, Any]:
    _cache_ok, note = ensure_specrules_cache(False, repo_url)
    cache = specrules_cache_dir(repo_url)
    cache_index = cache / "rules" / "index.md"
    local_index = specrules_dir / "rules" / "index.md"
    if not cache_index.is_file():
        return {
            "status": "current" if local_index.is_file() else "error",
            "action": "cache-unavailable",
            "message": note,
            "repo_url": repo_url,
        }
    if not specrules_dir.exists():
        copied = copy_specrules_to_workspace(cache, specrules_dir)
        return {
            "status": "updated",
            "action": "copy-created",
            "message": f"copied {copied} files from cache ({note})",
            "repo_url": repo_url,
        }
    sync = sync_specrules_to_workspace(cache, specrules_dir, allow_new_files=True)
    counts = {key: len(value) for key, value in sync.items()}
    changed = bool(counts["updated"] or counts["created"])
    return {
        "status": "updated" if changed else "current",
        "action": "copy-sync",
        "message": (
            f"{counts['updated']} updated, {counts['created']} created, "
            f"{counts['identical']} identical ({note})"
        ),
        "files": {key: value for key, value in sync.items() if value},
        "repo_url": repo_url,
    }


def refresh_specrules(workspace: Path) -> dict[str, Any]:
    """Refresh a mounted remote specrules tree. Never raises.

    disabled/invalid/unmounted → skipped. Network or git failure keeps the local
    copy when ``specrules/rules/index.md`` is still present. Submodule dirty or
    diverged trees are skipped so local edits are not discarded.
    """
    root = Path(workspace).expanduser()
    try:
        resolution = resolve_specrules(root)
    except Exception as error:
        return {
            "status": "error",
            "action": "resolve-failed",
            "message": compact_error(error),
        }
    state = resolution.effective_state
    if state in {"disabled", "invalid"}:
        return {
            "status": "skipped",
            "action": f"skipped-{state}",
            "message": f"specrules {state}; refresh skipped",
        }
    if resolution.workspace_tree == "none":
        return {
            "status": "skipped",
            "action": "skipped-not-mounted",
            "message": "specrules is not mounted; run devkit-flow setup",
        }
    # 016 T-05：规则路由判定（读配置 / 回执 / routingMode / legacyTreeAtRisk）整段异常捕获——任何异常按 2.45.0
    # 路径继续刷新，结果形状不变，只在 message 末尾附说明（Never raises）。
    routing_note = ""
    try:
        skipped = legacy_refresh_guard(root)
    except Exception as error:  # noqa: BLE001 - fail-open：判定失败按原方式刷新
        skipped = None
        routing_note = f"; 规则路由判定失败，已按原方式刷新：{compact_error(error)}"
    if skipped is not None:
        return skipped
    repo_url = resolution.repo_url or SPECRULES_REPO_URL
    specrules_dir = root / "specrules"
    try:
        if resolution.workspace_tree == "submodule":
            result = _refresh_specrules_submodule(root, specrules_dir, repo_url)
        else:
            result = _refresh_specrules_copy(root, specrules_dir, repo_url)
    except Exception as error:
        index_ok = (specrules_dir / "rules" / "index.md").is_file()
        result = {
            "status": "current" if index_ok else "error",
            "action": "refresh-failed",
            "message": compact_error(error),
            "repo_url": repo_url,
        }
    if routing_note:
        result["message"] = f"{result.get('message') or ''}{routing_note}"
    return result


def resolve_agents_source() -> tuple[Path | None, str]:
    """解析 cursor agents 源目录：DEVKIT_AGENTS_SOURCE 环境变量 > npm 包数据目录 > 运行 CLI 所在仓库的 packages/agents/。"""
    env_value = os.environ.get("DEVKIT_AGENTS_SOURCE", "").strip()
    if env_value:
        candidate = Path(env_value).expanduser()
        if candidate.is_dir():
            return candidate, f"env DEVKIT_AGENTS_SOURCE={candidate}"
        env_note = f"DEVKIT_AGENTS_SOURCE={candidate} is not a directory; "
    else:
        env_note = ""
    # npm 全局安装时，__file__ 解析到 <npm-global>/node_modules/devkit-flow/devkit_flow_main.py
    # （由同目录薄启动器 devkit-flow 加载，BC-23），数据目录 agents/ 与该文件同包同级。
    npm_candidate = current_cli_path().parent / "agents"
    if npm_candidate.is_dir():
        return npm_candidate, f"npm package {npm_candidate}"
    repo_candidate = current_cli_path().parent.parent / "packages" / "agents"
    if repo_candidate.is_dir():
        return repo_candidate, f"repo {repo_candidate}"
    if len(current_cli_path().parents) > 2:
        source_tree_candidate = current_cli_path().parents[2] / "packages" / "agents"
        if source_tree_candidate.is_dir():
            return source_tree_candidate, f"source tree {source_tree_candidate}"
    return None, env_note + "no agents source available (set DEVKIT_AGENTS_SOURCE or run from the devkit repo)"


def render_cursor_agent_simple(source_path: Path) -> str:
    """简化版 cursor agent 渲染（对应 scripts/workspace_sync.py 的 render_agent(ide="cursor")）。

    仅做 cursor-model 字段处理：frontmatter 保留 name/description，model 取 cursor-model
    优先、其次 model，其余字段（如 tools）丢弃；正文原样保留。workspace_sync 退役后如需
    完整渲染能力再回填。
    """
    lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise DevkitFlowError(f"agent file is missing frontmatter: {source_path}")
    frontmatter_end = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
        None,
    )
    if frontmatter_end is None:
        raise DevkitFlowError(f"agent file has unterminated frontmatter: {source_path}")
    metadata: dict[str, str] = {}
    for raw_line in lines[1:frontmatter_end]:
        match = re.match(r"^([A-Za-z][\w-]*):\s*(\S.*)$", raw_line.strip())
        if match:
            metadata[match.group(1)] = match.group(2).strip()
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    if not name or not description:
        raise DevkitFlowError(f"agent file is missing name/description: {source_path}")
    model = metadata.get("cursor-model") or metadata.get("model")
    frontmatter = ["---", f"name: {name}", f"description: {description}"]
    if model:
        frontmatter.append(f"model: {model}")
    frontmatter.append("---")
    return "\n".join(frontmatter) + "\n" + "".join(lines[frontmatter_end + 1 :])


def setup_action(item: str, path: Path, workspace: Path, status: str, reason: str) -> dict[str, Any]:
    try:
        display_path = path.relative_to(workspace).as_posix()
    except ValueError:
        display_path = str(path)
    return {"item": item, "path": display_path, "status": status, "reason": reason}


def copy_cli_into_workspace(source: Path, destination: Path) -> None:
    """同目录临时文件 + replace 的原子拷贝，并保证可执行位。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.name + ".tmp")
    shutil.copy2(source, temp_path)
    temp_path.chmod(0o755)
    temp_path.replace(destination)


def setup_specrules_copy(
    args: argparse.Namespace,
    workspace: Path,
    specrules_dir: Path,
    *,
    repo_url: str = SPECRULES_REPO_URL,
) -> dict[str, Any]:
    """copy 模式：目标无 rules/index.md 全量拷贝；已有则增量同步（更新漂移文件、
    缺失 canonical 默认跳过、用户额外文件保留，对齐 workspace_sync 存量语义）。"""
    incremental = (specrules_dir / "rules" / "index.md").is_file()
    cache_ok, note = ensure_specrules_cache(args.skip_specrules_pull, repo_url)
    cache = specrules_cache_dir(repo_url)
    if not cache_ok:
        return setup_action("specrules", specrules_dir, workspace, "error", note)
    if not (cache / "rules" / "index.md").is_file():
        return setup_action(
            "specrules", specrules_dir, workspace, "error", f"specrules cache {cache} is incomplete (missing rules/index.md)"
        )
    if not incremental:
        copied = copy_specrules_to_workspace(cache, specrules_dir)
        return setup_action(
            "specrules", specrules_dir, workspace, "created", f"copied {copied} files from {cache} ({note})"
        )
    sync = sync_specrules_to_workspace(cache, specrules_dir, allow_new_files=args.allow_new_specrules_files)
    counts = {key: len(value) for key, value in sync.items()}
    status = "updated" if counts["updated"] or counts["created"] else "skipped"
    reason = (
        f"{counts['updated']} updated, {counts['created']} created, "
        f"{counts['identical']} identical, {counts['skipped']} skipped-new ({note})"
    )
    if counts["skipped"]:
        reason += "; pass --allow-new-specrules-files to create missing canonical files"
    action = setup_action("specrules", specrules_dir, workspace, status, reason)
    action["files"] = sync
    return action


def specrules_migration_diffs(cache: Path, target: Path) -> list[str]:
    """存量普通目录与 canonical 缓存的差异清单（空列表才允许迁移转 submodule）。

    canonical 集合对齐 copy 语义：仅 *.md，跳过 .git 与根 README.md；目标侧任何不在
    canonical 集合内的文件（含用户自建文件）都算 extra 差异。
    """
    canonical: dict[str, Path] = {}
    for source in sorted(cache.rglob("*.md")):
        relative = source.relative_to(cache)
        if ".git" in relative.parts or relative.as_posix() == "README.md":
            continue
        canonical[relative.as_posix()] = source
    target_files = {
        path.relative_to(target).as_posix(): path for path in sorted(target.rglob("*")) if path.is_file()
    }
    diffs: list[str] = []
    for relative, source in canonical.items():
        target_file = target_files.get(relative)
        if target_file is None:
            diffs.append(f"missing: specrules/{relative}")
        elif target_file.read_bytes() != source.read_bytes():
            diffs.append(f"differs: specrules/{relative}")
    for relative in target_files:
        if relative not in canonical:
            diffs.append(f"extra: specrules/{relative}")
    return sorted(diffs)


def _gitmodules_specrules_url(workspace: Path) -> str:
    gitmodules = workspace / ".gitmodules"
    if not gitmodules.is_file():
        return ""
    try:
        lines = gitmodules.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[submodule"):
            in_section = "specrules" in stripped
            continue
        if in_section and stripped.startswith("url") and "=" in stripped:
            return stripped.split("=", 1)[1].strip()
    return ""


def _specrules_git_urls_match(left: str, right: str) -> bool:
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False
    try:
        return validate_specrules_git_url(left).normalized == validate_specrules_git_url(right).normalized
    except DevkitFlowError:
        return left == right


def _switch_specrules_submodule_url(
    git: str,
    workspace: Path,
    specrules_dir: Path,
    *,
    old_url: str,
    new_url: str,
) -> dict[str, Any]:
    gitmodules = workspace / ".gitmodules"
    gitmodules_backup = gitmodules.read_text(encoding="utf-8") if gitmodules.is_file() else None
    backup = workspace / SPECRULES_URL_SWITCH_BACKUP_NAME
    if backup.exists():
        return setup_action(
            "specrules",
            specrules_dir,
            workspace,
            "error",
            f"url-switch backup path {backup} already exists; remove it and retry",
        )
    if specrules_dir.exists():
        shutil.copytree(specrules_dir, backup)

    def rollback(detail: str) -> dict[str, Any]:
        run_external(
            [git, "-C", str(workspace), "submodule", "set-url", "specrules", old_url],
            check=False,
        )
        run_external(
            [git, "-C", str(workspace), "submodule", "sync", "specrules"],
            check=False,
        )
        _restore_text_file(gitmodules, gitmodules_backup)
        if backup.exists():
            if specrules_dir.exists():
                shutil.rmtree(specrules_dir, ignore_errors=True)
            backup.rename(specrules_dir)
        run_external(
            [git, "-C", str(workspace), "submodule", "update", "--init", "specrules"],
            check=False,
        )
        return setup_action(
            "specrules",
            specrules_dir,
            workspace,
            "error",
            f"failed to switch specrules submodule from {old_url} to {new_url}: {detail}",
        )

    changed = run_external(
        [git, "-C", str(workspace), "submodule", "set-url", "specrules", new_url],
        check=False,
    )
    if changed.returncode != 0:
        detail = _git_result_diagnostic(changed, limit=200)
        return rollback(detail)
    sync = run_external(
        [git, "-C", str(workspace), "submodule", "sync", "specrules"],
        check=False,
    )
    if sync.returncode != 0:
        detail = _git_result_diagnostic(sync, limit=200)
        return rollback(detail)
    update = run_external(
        [git, "-C", str(workspace), "submodule", "update", "--init", "specrules"],
        check=False,
    )
    if update.returncode != 0:
        detail = _git_result_diagnostic(update, limit=200)
        return rollback(detail)
    if not (specrules_dir / "rules" / "index.md").is_file():
        return rollback("specrules/rules/index.md is missing after update --init")
    if backup.exists():
        shutil.rmtree(backup)
    return setup_action(
        "specrules",
        specrules_dir,
        workspace,
        "updated",
        f"updated submodule url to {new_url}; ran git submodule sync and update --init specrules",
    )


def add_specrules_submodule(
    git: str,
    workspace: Path,
    specrules_dir: Path,
    *,
    status: str,
    note: str,
    repo_url: str = SPECRULES_REPO_URL,
) -> dict[str, Any]:
    """git submodule add + update --init 并验证 rules/index.md；绝不替用户 commit。"""
    add = run_external(
        [git, "-C", str(workspace), "submodule", "add", repo_url, "specrules"], check=False
    )
    if add.returncode != 0:
        detail = _git_result_diagnostic(add, limit=200)
        return setup_action("specrules", specrules_dir, workspace, "error", f"git submodule add failed: {detail}")
    update = run_external(
        [git, "-C", str(workspace), "submodule", "update", "--init", "specrules"], check=False
    )
    if update.returncode != 0:
        detail = _git_result_diagnostic(update, limit=200)
        return setup_action("specrules", specrules_dir, workspace, "error", f"git submodule update --init failed: {detail}")
    if not (specrules_dir / "rules" / "index.md").is_file():
        return setup_action(
            "specrules", specrules_dir, workspace, "error", "submodule mounted but specrules/rules/index.md is missing"
        )
    return setup_action(
        "specrules",
        specrules_dir,
        workspace,
        status,
        f"mounted as git submodule from {repo_url} ({note}); "
        ".gitmodules and specrules/ are staged — 请自行提交，CLI 不会代为 commit",
    )


def setup_specrules_submodule(
    args: argparse.Namespace,
    workspace: Path,
    specrules_dir: Path,
    *,
    repo_url: str = SPECRULES_REPO_URL,
    cache_skip_pull: bool | None = None,
) -> dict[str, Any]:
    """submodule 模式挂载 specrules/。

    - 无 git 或目标不是 git 仓 → 回退 copy 模式并在 reason 注明原因；
    - 已是 submodule（.gitmodules 含 path = specrules）→ url 相同则 update --init，
      不同且脏则 error，不同且干净则 set-url + sync + update（失败回滚旧 url）；
    - 存量普通目录 → 与生效 URL 缓存零差异才转换（改名备份 → add → 清理，失败回滚目录），
      有任何差异或用户额外文件则拒绝迁移并给出指引；
    - 不存在 → ensure_specrules_cache 后 submodule add 生效 URL（需网络，--skip-specrules-pull 时报错）。

    ``cache_skip_pull``（016 T-05，``RuleMountExecutor`` 传入）：只覆盖本函数内 ``ensure_specrules_cache`` 的
    ``skip_pull``（同一次 setup 已拉取过该 URL 时不再 pull）；需要网络的 submodule 操作仍按
    ``args.skip_specrules_pull`` 判定。缺省 None 时与 2.45.0 完全相同。
    """
    git = shutil.which("git")
    if not git:
        action = setup_specrules_copy(args, workspace, specrules_dir, repo_url=repo_url)
        action["reason"] = f"git is not available; fell back to copy mode — {action['reason']}"
        return action
    probe = run_external([git, "-C", str(workspace), "rev-parse", "--is-inside-work-tree"], check=False)
    if probe.returncode != 0:
        action = setup_specrules_copy(args, workspace, specrules_dir, repo_url=repo_url)
        action["reason"] = f"{workspace} is not a git repository; fell back to copy mode — {action['reason']}"
        return action

    gitmodules = workspace / ".gitmodules"
    registered = gitmodules.is_file() and re.search(
        r"(?m)^\s*path\s*=\s*specrules\s*$", gitmodules.read_text(encoding="utf-8")
    )
    if registered:
        current_url = _gitmodules_specrules_url(workspace)
        if _specrules_git_urls_match(current_url, repo_url):
            update = run_external(
                [git, "-C", str(workspace), "submodule", "update", "--init", "specrules"], check=False
            )
            if update.returncode != 0:
                detail = _git_result_diagnostic(update, limit=200)
                return setup_action(
                    "specrules", specrules_dir, workspace, "error",
                    f"git submodule update --init failed: {detail}",
                )
            if not (specrules_dir / "rules" / "index.md").is_file():
                return setup_action(
                    "specrules", specrules_dir, workspace, "error",
                    "registered submodule but specrules/rules/index.md is missing after update --init",
                )
            return setup_action(
                "specrules", specrules_dir, workspace, "skipped",
                "already a registered submodule; ran git submodule update --init specrules",
            )
        if args.skip_specrules_pull:
            return setup_action(
                "specrules", specrules_dir, workspace, "error",
                "switching specrules submodule URL needs network access; "
                "re-run without --skip-specrules-pull",
            )
        porcelain = run_external(
            [git, "-C", str(workspace), "status", "--porcelain", "--", "specrules"],
            check=False,
        )
        if porcelain.returncode != 0:
            detail = _git_result_diagnostic(porcelain, limit=200)
            return setup_action(
                "specrules", specrules_dir, workspace, "error",
                f"cannot prove specrules submodule is clean ({detail}); "
                f"refusing to switch url from {current_url} to {repo_url}",
            )
        dirty_lines = [line for line in (porcelain.stdout or "").splitlines() if line.strip()]
        if dirty_lines:
            samples = _specrules_diff_sample_reason(_porcelain_specrules_samples(dirty_lines))
            return setup_action(
                "specrules", specrules_dir, workspace, "error",
                f"specrules submodule is dirty; refusing to switch url from {current_url} to {repo_url}: "
                f"{samples}",
            )
        return _switch_specrules_submodule_url(
            git, workspace, specrules_dir, old_url=current_url, new_url=repo_url
        )

    cache_ok, note = ensure_specrules_cache(
        args.skip_specrules_pull if cache_skip_pull is None else cache_skip_pull, repo_url
    )
    cache = specrules_cache_dir(repo_url)
    if not cache_ok:
        return setup_action("specrules", specrules_dir, workspace, "error", note)
    if not (cache / "rules" / "index.md").is_file():
        return setup_action(
            "specrules", specrules_dir, workspace, "error", f"specrules cache {cache} is incomplete (missing rules/index.md)"
        )

    if specrules_dir.exists():
        diffs = specrules_migration_diffs(cache, specrules_dir)
        if diffs:
            sample = "; ".join(diffs[:3])
            more = f" (+{len(diffs) - 3} more)" if len(diffs) > 3 else ""
            return setup_action(
                "specrules", specrules_dir, workspace, "error",
                f"specrules/ is a plain directory with local differences; migration to submodule refused: "
                f"{sample}{more}. Resolve them manually first, or pass --specrules-mode copy to keep the copy mode.",
            )
        if args.skip_specrules_pull:
            return setup_action(
                "specrules", specrules_dir, workspace, "error",
                "converting specrules/ to a submodule needs network access (git clone); "
                "re-run without --skip-specrules-pull, or pass --specrules-mode copy",
            )
        # 零差异：改名备份 → submodule add → 清理备份；任何失败都回滚目录，绝不丢用户文件
        backup = specrules_dir.with_name("specrules.devkit-migration-backup")
        if backup.exists():
            return setup_action(
                "specrules", specrules_dir, workspace, "error",
                f"migration backup path {backup} already exists; remove it and retry",
            )
        specrules_dir.rename(backup)
        action = add_specrules_submodule(
            git, workspace, specrules_dir, status="converted", note=note, repo_url=repo_url
        )
        if action["status"] == "error":
            if specrules_dir.exists():
                shutil.rmtree(specrules_dir, ignore_errors=True)
            backup.rename(specrules_dir)
            action["reason"] += "; the original directory was restored"
        else:
            shutil.rmtree(backup)
        return action

    if args.skip_specrules_pull:
        return setup_action(
            "specrules", specrules_dir, workspace, "error",
            "git submodule add needs network access (git clone); "
            "re-run without --skip-specrules-pull, or pass --specrules-mode copy",
        )
    return add_specrules_submodule(
        git, workspace, specrules_dir, status="created", note=note, repo_url=repo_url
    )


def _specrules_diff_sample_reason(diffs: list[str]) -> str:
    sample = "; ".join(diffs[:3])
    more = f" (+{len(diffs) - 3} more)" if len(diffs) > 3 else ""
    return f"{sample}{more}"


def _disabled_cleanup_error_reason(detail: str, diffs: list[str] | None = None) -> str:
    parts = [f"disabled; {detail}"]
    if diffs:
        parts.append(_specrules_diff_sample_reason(diffs))
    parts.append(SPECRULES_CLEANUP_MANUAL)
    return "; ".join(parts)


def _restore_text_file(path: Path, original: str | None) -> None:
    if original is None:
        if path.is_file():
            path.unlink()
        return
    path.write_text(original, encoding="utf-8")


def _specrules_cleanup_cache(resolution: SpecrulesResolution) -> Path:
    url = resolution.configured_repo_url or SPECRULES_REPO_URL
    return specrules_cache_dir_for_url(url)


def _porcelain_specrules_samples(lines: list[str]) -> list[str]:
    samples: list[str] = []
    for line in lines:
        path = line[3:].strip() if len(line) > 3 else line.strip()
        path = path.strip('"')
        if path:
            samples.append(f"differs: {path}")
    return samples


def _strip_specrules_gitmodules_section(text: str) -> str:
    return re.sub(r'(?ms)^\[submodule "specrules"\][^\[]*', "", text)


def _cleanup_specrules_directory(
    workspace: Path, specrules_dir: Path, resolution: SpecrulesResolution
) -> dict[str, Any]:
    cache = _specrules_cleanup_cache(resolution)
    if not cache.is_dir() or not (cache / "rules" / "index.md").is_file():
        return setup_action(
            "specrules",
            specrules_dir,
            workspace,
            "error",
            _disabled_cleanup_error_reason("cannot prove specrules/ is clean (cache unavailable)"),
        )
    diffs = specrules_migration_diffs(cache, specrules_dir)
    if diffs:
        return setup_action(
            "specrules",
            specrules_dir,
            workspace,
            "error",
            _disabled_cleanup_error_reason("specrules/ has local differences", diffs),
        )
    shutil.rmtree(specrules_dir)
    return setup_action("specrules", specrules_dir, workspace, "updated", SPECRULES_DISABLED_REMOVED)


def _cleanup_specrules_submodule(workspace: Path, specrules_dir: Path) -> dict[str, Any]:
    git = shutil.which("git")
    gitmodules = workspace / ".gitmodules"
    if not git:
        return setup_action(
            "specrules",
            specrules_dir,
            workspace,
            "error",
            _disabled_cleanup_error_reason("cannot prove specrules/ is clean (git unavailable)"),
        )
    status = run_external(
        [git, "-C", str(workspace), "status", "--porcelain", "--", "specrules"],
        check=False,
    )
    if status.returncode != 0:
        detail = _git_result_diagnostic(status, limit=200)
        return setup_action(
            "specrules",
            specrules_dir,
            workspace,
            "error",
            _disabled_cleanup_error_reason(f"cannot prove specrules/ is clean ({detail})"),
        )
    dirty_lines = [line for line in (status.stdout or "").splitlines() if line.strip()]
    if dirty_lines:
        return setup_action(
            "specrules",
            specrules_dir,
            workspace,
            "error",
            _disabled_cleanup_error_reason(
                "specrules/ has local changes",
                _porcelain_specrules_samples(dirty_lines),
            ),
        )

    gitmodules_backup = gitmodules.read_text(encoding="utf-8") if gitmodules.is_file() else None
    backup = workspace / SPECRULES_CLEANUP_BACKUP_NAME
    if backup.exists():
        return setup_action(
            "specrules",
            specrules_dir,
            workspace,
            "error",
            _disabled_cleanup_error_reason(f"cleanup backup path {backup} already exists"),
        )
    if specrules_dir.exists():
        shutil.copytree(specrules_dir, backup)

    def rollback(detail: str) -> dict[str, Any]:
        _restore_text_file(gitmodules, gitmodules_backup)
        if specrules_dir.exists() and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        elif backup.exists() and not specrules_dir.exists():
            backup.rename(specrules_dir)
        return setup_action(
            "specrules",
            specrules_dir,
            workspace,
            "error",
            _disabled_cleanup_error_reason(f"failed to remove specrules submodule ({detail})"),
        )

    deinit = run_external(
        [git, "-C", str(workspace), "submodule", "deinit", "-f", "specrules"],
        check=False,
    )
    if deinit.returncode != 0:
        detail = _git_result_diagnostic(deinit, limit=200)
        return rollback(detail)
    remove = run_external([git, "-C", str(workspace), "rm", "-f", "specrules"], check=False)
    if remove.returncode != 0:
        detail = _git_result_diagnostic(remove, limit=200)
        return rollback(detail)

    if gitmodules.is_file():
        remaining = gitmodules.read_text(encoding="utf-8")
        if _SPECRULES_GITMODULES_PATH.search(remaining):
            stripped = _strip_specrules_gitmodules_section(remaining)
            if stripped.strip():
                gitmodules.write_text(stripped, encoding="utf-8")
            else:
                gitmodules.unlink()
    if specrules_dir.exists():
        shutil.rmtree(specrules_dir)
    if backup.exists():
        shutil.rmtree(backup)
    return setup_action("specrules", specrules_dir, workspace, "updated", SPECRULES_DISABLED_REMOVED)


def cleanup_specrules_tree(workspace: Path, resolution: SpecrulesResolution) -> dict[str, Any]:
    """关闭态清理 specrules 树：干净则移除，脏或无法证明干净则 error 且不丢文件。不代 commit。"""
    specrules_dir = workspace / "specrules"
    tree = resolution.workspace_tree
    if tree == "none":
        return setup_action("specrules", specrules_dir, workspace, "skipped", SPECRULES_DISABLED_NO_TREE)
    if tree == "submodule":
        return _cleanup_specrules_submodule(workspace, specrules_dir)
    return _cleanup_specrules_directory(workspace, specrules_dir, resolution)


# ---------------------------------------------------------------------------
# 016 T-05：规则源路由接入 setup（设计 §4.2.1、§4.7.1.3～§4.7.1.5，接口 §2.5 / §8.3 / §8.4）
# ---------------------------------------------------------------------------

RULES_STAGING_RELATIVE_DIR = Path(".devkit") / "specrules-staging"
# 成功挂上默认源时沿用原句。默认源也失败时由 reconcile_legacy_fallback_advice 换成与实际一致的句子（G-10）。
RULES_ADVICE_PULL_FAILED_MOUNTED = (
    "本次已按旧路径挂载默认 Java 规则，之后不会自动重试隐式采用。"
    "修复后执行 specrules catalog configure --routing auto 再 setup（会备份现有树）"
)
RULES_ADVICE_PULL_FAILED = "{kind} 源拉取失败：{reason}；" + RULES_ADVICE_PULL_FAILED_MOUNTED
RULES_ADVICE_IMPLICIT_LEGACY_MOUNTED = "已按旧路径挂载（与 2.45.0 一致）"
RULES_ADVICE_NON_JAVA_GUARD = (
    "现有 specrules/ 声明了非 Java 规则包且绑定回执缺失：保留现有规则树、不覆盖。"
    "执行 specrules catalog configure --routing auto 后 setup 恢复采用；"
    "或 --routing legacy 后 setup 显式回到 Java 规则（自动备份或记录现有树）"
)


RULES_NOTE_REUSED_CACHE = "reused the rule cache pulled earlier in this setup"
RULES_FALLBACK_COPY_ON_SUBMODULE = (
    "specrules/ is a git submodule: explicit legacy fallback with --specrules-mode copy would overwrite files inside "
    "the submodule and is refused; re-run setup without --specrules-mode copy (switches the submodule back to the "
    "Java source), or remove the submodule first (git submodule deinit -f specrules && git rm -f specrules) and setup again"
)


class RuleMountFailure(Exception):
    """``RuleMountExecutor.mount`` 的挂载失败（拉取 / 校验 / 换树失败）；原树已按备份还原或未被改动。

    ``rollback_problems`` 非空表示挂载失败后执行方内部的撤销本身也未完成（双重故障，Claude R2-P2-b）：问题项结构化上传，
    由 ``SetupRuleRouting._mount_all`` 与外层回滚的问题合并，不得再声称原树未改动。"""

    def __init__(self, message: str, *, error_code: str | None = None, rollback_problems: list[str] | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.rollback_problems = list(rollback_problems or [])


def _mount_failure_code(reason: str) -> str:
    """挂载失败无 ``RULES_*`` 码时的映射（arch P2-6，接口 §4.2）：submodule 脏 → 迁移冲突；其余（缓存不可用、拉取 /
    检出失败、换树 IO 失败）→ 规则源未能完整挂载 ``RULES_INCOMPLETE_SOURCE``。不新增错误码（D-23）。"""
    if "dirty" in reason or "local changes" in reason:
        return RULES_MIGRATION_CONFLICT
    return RULES_INCOMPLETE_SOURCE


class _SubmoduleSnapshot:
    """specrules submodule 的可恢复快照（D-35 ②、arch P2-3 / codex P2-1、codex R2 P1）：``.gitmodules`` 文本、父仓索引中
    ``.gitmodules`` / ``specrules`` 两条记录（含 gitlink）、子模块检出提交与分支、挂载前树是否存在、
    ``.git/modules/specrules`` 是否存在、``.git/config`` 中 ``submodule.specrules.*`` 全部条目、URL 切换备份目录是否存在。

    ``restore`` 只撤销本次新建 / 改动的部分：挂载前无树时移除工作树；``.git/modules/specrules`` 仅在快照时不存在才删除
    （工作目录缺失不代表子模块仓库是新建的——登记、gitlink 与仅本地的提交可能仍在，codex R2 P1）；``.git/config`` 子模块节
    按快照原样重写（原本没有则删除）；已有树时按还原后的 ``.gitmodules`` 同步 URL 并检出原提交（原在分支上则回到该分支）。
    不代 commit。
    """

    PATHS = (".gitmodules", RULES_TREE_NAME)

    def __init__(self, git: str, workspace: Path, *, treeExisted: bool, gitmodules: str | None,
                 indexEntries: dict[str, tuple[str, str]], head: str | None, headRef: str | None = None,
                 modulesDir: Path | None = None, modulesExisted: bool = False,
                 configEntries: list[tuple[str, str]] | None = None, switchBackupExisted: bool = False,
                 modulesHead: str | None = None, modulesOrigin: str | None = None):
        self.git = git
        self.workspace = workspace
        self.treeExisted = treeExisted
        self.gitmodules = gitmodules
        self.indexEntries = indexEntries
        self.head = head
        self.headRef = headRef
        self.modulesDir = modulesDir
        self.modulesExisted = modulesExisted
        self.configEntries = list(configEntries or [])
        self.switchBackupExisted = switchBackupExisted
        # 工作树缺失、子模块仓库仍在时：仓库 HEAD 原文（分支引用或提交号）与 remote.origin.url
        self.modulesHead = modulesHead
        self.modulesOrigin = modulesOrigin

    @classmethod
    def capture(cls, workspace: Path) -> "_SubmoduleSnapshot | None":
        git = shutil.which("git")
        if not git:
            return None
        probe = run_external([git, "-C", str(workspace), "rev-parse", "--is-inside-work-tree"], check=False)
        if probe.returncode != 0:
            return None
        tree = workspace / RULES_TREE_NAME
        gitmodules = workspace / ".gitmodules"
        try:
            text = gitmodules.read_text(encoding="utf-8") if gitmodules.is_file() else None
        except (OSError, UnicodeDecodeError):
            text = None
        entries: dict[str, tuple[str, str]] = {}
        listing = run_external([git, "-C", str(workspace), "ls-files", "-s", "--", *cls.PATHS], check=False)
        for line in (listing.stdout or "").splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if path in cls.PATHS and len(parts) >= 2:
                entries[path] = (parts[0], parts[1])
        head = headRef = None
        if os.path.lexists(tree / ".git"):
            head = _specrules_git_sha(git, tree)
            ref = run_external([git, "-C", str(tree), "symbolic-ref", "-q", "HEAD"], check=False)
            headRef = (ref.stdout or "").strip() or None if ref.returncode == 0 else None
        git_dir = (run_external([git, "-C", str(workspace), "rev-parse", "--git-dir"], check=False).stdout or "").strip()
        modules_dir = None
        if git_dir:
            modules_dir = (Path(git_dir) if os.path.isabs(git_dir) else workspace / git_dir) / "modules" / RULES_TREE_NAME
        modules_existed = modules_dir is not None and os.path.lexists(modules_dir)
        modules_head = modules_origin = None
        if modules_existed and modules_dir is not None and not os.path.lexists(tree):
            modules_head = cls._read_head(modules_dir)
            origin = run_external([git, "config", "--file", str(modules_dir / "config"), "--get", "remote.origin.url"], check=False)
            modules_origin = (origin.stdout or "").strip() or None
        config: list[tuple[str, str]] = []
        listing = run_external(
            [git, "-C", str(workspace), "config", "--local", "--get-regexp", rf"^submodule\.{RULES_TREE_NAME}\."], check=False
        )
        for line in (listing.stdout or "").splitlines():
            key, _, value = line.partition(" ")
            if key:
                config.append((key, value))
        return cls(
            git,
            workspace,
            treeExisted=os.path.lexists(tree),
            gitmodules=text,
            indexEntries=entries,
            head=head,
            headRef=headRef,
            modulesDir=modules_dir,
            modulesExisted=modules_existed,
            configEntries=config,
            switchBackupExisted=os.path.lexists(workspace / SPECRULES_URL_SWITCH_BACKUP_NAME),
            modulesHead=modules_head,
            modulesOrigin=modules_origin,
        )

    @staticmethod
    def _read_head(git_dir: Path) -> str | None:
        try:
            return (git_dir / "HEAD").read_text(encoding="utf-8").strip() or None
        except (OSError, UnicodeDecodeError):
            return None

    @property
    def gitlink(self) -> str | None:
        entry = self.indexEntries.get(RULES_TREE_NAME)
        return entry[1] if entry is not None and entry[0] == "160000" else None

    def _git(self, *args: str, cwd: Path | None = None) -> Any:
        return run_external([self.git, "-C", str(cwd or self.workspace), *args], check=False)

    def _restore_config(self, problems: list[str]) -> None:
        section = f"submodule.{RULES_TREE_NAME}"
        current = self._git("config", "--local", "--get-regexp", rf"^submodule\.{RULES_TREE_NAME}\.")
        now = [tuple(line.partition(" ")[::2]) for line in (current.stdout or "").splitlines() if line.strip()]
        if now == self.configEntries:
            return
        if now:
            self._git("config", "--local", "--remove-section", section)
        for key, value in self.configEntries:
            if self._git("config", "--local", "--add", key, value).returncode != 0:
                problems.append(f".git/config {key}")

    def unchanged(self) -> bool:
        """当前状态与快照逐项相同（未发生任何改动）时为真：失败发生在改动之前，无需撤销、也不应声称已回滚。"""
        current = _SubmoduleSnapshot.capture(self.workspace)
        if current is None:
            return False
        modules_head = self._read_head(self.modulesDir) if self.modulesHead and self.modulesDir is not None else None
        return (
            current.treeExisted == self.treeExisted
            and current.gitmodules == self.gitmodules
            and current.indexEntries == self.indexEntries
            and current.head == self.head
            and current.headRef == self.headRef
            and current.modulesExisted == self.modulesExisted
            and current.configEntries == self.configEntries
            and current.switchBackupExisted == self.switchBackupExisted
            and modules_head == self.modulesHead
        )

    def _restore_modules_repo(self, problems: list[str]) -> None:
        """工作树原本缺失、子模块仓库原本存在：仓库保留不删，只把本次检出 / 切源改动的 HEAD 与 origin URL 放回原值。
        工作树已移除时仓库的 ``core.worktree`` 指向不存在的目录，git 命令会失败——HEAD 直接按原文写回，配置用 ``--file``。"""
        modules = self.modulesDir
        if modules is None:
            return
        if self.modulesHead and self._read_head(modules) != self.modulesHead:
            try:
                atomic_write_text(modules / "HEAD", self.modulesHead + "\n")
            except OSError as error:
                problems.append(f"{modules} HEAD: {error}")
        if self.modulesOrigin:
            config = str(modules / "config")
            current = run_external([self.git, "config", "--file", config, "--get", "remote.origin.url"], check=False)
            if (current.stdout or "").strip() != self.modulesOrigin and run_external(
                [self.git, "config", "--file", config, "remote.origin.url", self.modulesOrigin], check=False
            ).returncode != 0:
                problems.append(f"{modules} remote.origin.url")

    def restore(self) -> list[str]:
        """恢复快照；返回未能恢复的步骤（空列表 = 完整恢复）。"""
        problems: list[str] = []
        tree = self.workspace / RULES_TREE_NAME
        try:
            _restore_text_file(self.workspace / ".gitmodules", self.gitmodules)
        except OSError as error:
            problems.append(f".gitmodules: {error}")
        switch_backup = self.workspace / SPECRULES_URL_SWITCH_BACKUP_NAME
        if not self.switchBackupExisted and os.path.lexists(switch_backup):
            # URL 切换原语中途异常留下的备份：原树缺失则放回原位，否则删除（子模块切换前已证明干净，内容可由 git 恢复）
            try:
                if self.treeExisted and not os.path.lexists(tree):
                    switch_backup.rename(tree)
                else:
                    shutil.rmtree(switch_backup)
            except OSError as error:
                problems.append(f"{SPECRULES_URL_SWITCH_BACKUP_NAME}: {error}")
        self._restore_config(problems)
        if not self.treeExisted:
            try:
                if tree.is_symlink() or tree.is_file():
                    tree.unlink()
                elif tree.exists():
                    shutil.rmtree(tree)
            except OSError as error:
                problems.append(f"{RULES_TREE_NAME}/: {error}")
            if self.modulesDir is not None and not self.modulesExisted and os.path.lexists(self.modulesDir):
                shutil.rmtree(self.modulesDir, ignore_errors=True)
                if os.path.lexists(self.modulesDir):
                    problems.append(f"remove {self.modulesDir}")
            elif self.modulesDir is not None and self.modulesExisted:
                self._restore_modules_repo(problems)
        else:
            if self._git("submodule", "sync", "--", RULES_TREE_NAME).returncode != 0:
                problems.append("git submodule sync")
            if self.head and os.path.lexists(tree / ".git"):
                current = _specrules_git_sha(self.git, tree)
                ref = self._git("symbolic-ref", "-q", "HEAD", cwd=tree)
                current_ref = (ref.stdout or "").strip() or None if ref.returncode == 0 else None
                if current != self.head or current_ref != self.headRef:
                    branch = self.headRef[len("refs/heads/"):] if self.headRef and self.headRef.startswith("refs/heads/") else None
                    if branch and _specrules_git_sha(self.git, tree, self.headRef) == self.head:
                        target = ("checkout", "-q", branch)
                    else:
                        target = ("checkout", "-q", "--detach", self.head)
                    if self._git(*target, cwd=tree).returncode != 0:
                        problems.append(f"checkout {self.head[:12]}")
        for path in self.PATHS:
            entry = self.indexEntries.get(path)
            if entry is not None:
                result = self._git("update-index", "--add", "--cacheinfo", f"{entry[0]},{entry[1]},{path}")
            else:
                result = self._git("update-index", "--force-remove", "--", path)
            if result.returncode != 0:
                problems.append(f"index entry {path}")
        return problems


class _DevkitResidue:
    """隐式首次采用回落时的「不留痕」（codex P2-5）：记录挂载前 ``.devkit/`` 及迁移 / 备份目录的状态，回落 legacy 时
    删除本次 setup 新增的迁移回执与空目录，使工作区与 2.45.0 一致（失败原因已进 ``rule_routing.advice``）。
    只按快照差集删除，绝不触碰用户已有文件（如 ``.devkit/config.json``）。"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.devkitExisted = os.path.lexists(workspace / ".devkit")
        self.dirs = {rel: os.path.lexists(workspace / rel) for rel in (RULES_MIGRATIONS_RELATIVE_DIR, RULES_BACKUP_RELATIVE_DIR, RULES_STAGING_RELATIVE_DIR)}
        migrations = workspace / RULES_MIGRATIONS_RELATIVE_DIR
        self.migrations = set(os.listdir(migrations)) if migrations.is_dir() else set()

    def clean(self) -> None:
        migrations = self.workspace / RULES_MIGRATIONS_RELATIVE_DIR
        if migrations.is_dir():
            for name in set(os.listdir(migrations)) - self.migrations:
                try:
                    (migrations / name).unlink()
                except OSError:
                    pass
        for rel, existed in self.dirs.items():
            if not existed:
                try:
                    (self.workspace / rel).rmdir()
                except OSError:
                    pass
        if not self.devkitExisted:
            try:
                (self.workspace / ".devkit").rmdir()
            except OSError:
                pass


def _rule_mount_rel(binding: RuleBinding) -> str:
    entry = binding.entry or f"{RULES_TREE_NAME}/{RULE_PACKAGE_ENTRY}"
    suffix = "/" + RULE_PACKAGE_ENTRY
    return entry[: -len(suffix)] if entry.endswith(suffix) else RULES_TREE_NAME


def _specrules_gitmodules_registered(workspace: Path) -> bool:
    gitmodules = workspace / ".gitmodules"
    try:
        return gitmodules.is_file() and bool(_SPECRULES_GITMODULES_PATH.search(gitmodules.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError):
        return False


def _cache_commit(cache: Path) -> str | None:
    """规则缓存当前提交（只读本地 git，不联网）；无 git 或非 git 目录返回 None。"""
    git = shutil.which("git")
    if not git or not (cache / ".git").exists():
        return None
    try:
        return _specrules_git_sha(git, cache)
    except Exception:  # noqa: BLE001 - 提交号只是回执元数据，取不到不影响挂载
        return None


def _rollback_incomplete_hint(problems: list[str]) -> str:
    """挂载失败后回滚本身也未完成（双重故障，R2-P2-b）时的如实说明与恢复建议。"""
    return (
        f"回滚不完整（{'; '.join(problems)}）：规则树 / 子模块登记 / 绑定回执可能已部分改动，"
        "请用 git status 与 git diff -- .gitmodules specrules .devkit 核对；子模块可 git checkout -- .gitmodules 后 "
        "git submodule sync specrules && git submodule update --init specrules 回到已提交状态"
    )


class RuleMountExecutor:
    """按 ``RuleSourceSelector.select`` 产出的绑定挂载规则包（设计 §4.7.1.5）。

    复用既有挂载原语：取源用 ``ensure_specrules_cache``（``skipPull`` 为真时只用已有缓存，同一次 setup 已拉取过的
    URL 不再 pull）；copy 分发把缓存拷到工作区内临时目录后交 ``ManagedTreeMigrator.commit`` 事务换树（备份 / 还原 /
    写回执与迁移回执）；submodule 分发复用 ``setup_specrules_submodule``（含切源回滚、dirty 检查），切换后对检出树做
    与缓存相同的源校验（kind / 协议 / 零链接），不合格按快照撤销 add / 切换；成功后填好绑定的受管清单（记录入口，
    D-31 ④），绑定回执由 ``SetupRuleRouting._mount_all`` 在全部挂载成功后统一写入（内容不变不重写，D-32 ①、arch P2-4）。
    """

    def __init__(self, args: argparse.Namespace, workspace: Path, migrator: ManagedTreeMigrator):
        self.args = args
        self.workspace = workspace
        self.migrator = migrator
        # 最近一次 copy 挂载的备份目录（无原树或 no-op 时为 None），供多挂载点中途失败时回滚
        self.lastBackup: Path | None = None
        self.lastChanged = False
        # 最近一次 copy 挂载写出的 applied 迁移回执（回滚时改写为 rolled-back，arch P2-9）
        self.lastMigrationPath: Path | None = None
        # 最近一次 submodule 挂载的撤销动作（后续步骤失败时由 _mount_all 回滚调用）
        self.lastUndo: Any = None

    def _note(self, text: str, skipPull: bool) -> str:
        """``skipPull`` 来自本次 setup 已拉取过该 URL（而非用户传 ``--skip-specrules-pull``）时改写缓存说明（arch P2-7）。"""
        if not skipPull or self.args.skip_specrules_pull:
            return text
        return text.replace("pull skipped (--skip-specrules-pull), using local cache", RULES_NOTE_REUSED_CACHE).replace(
            "missing and --skip-specrules-pull given", "missing (the pull earlier in this setup did not produce it)"
        )

    def mount(
        self,
        binding: RuleBinding,
        plan: MigrationPlan,
        mountPoint: str,
        skipPull: bool,
        receipt: BindingReceipt,
    ) -> dict[str, Any]:
        url = binding.repoUrl or SPECRULES_REPO_URL
        tree = self.workspace / mountPoint
        self.lastBackup = None
        self.lastChanged = False
        self.lastMigrationPath = None
        self.lastUndo = None
        if binding.distribution == "submodule":
            return self._mount_submodule(binding, url, tree, skipPull, receipt)
        cache_ok, note = ensure_specrules_cache(skipPull, url)
        note = self._note(note, skipPull)
        cache = specrules_cache_dir(url)
        if not cache_ok:
            raise RuleMountFailure(note, error_code=RULES_INCOMPLETE_SOURCE)
        status, meta, advice = _rs_validate_source(cache, binding.kind, explicit=True)
        if status != "bound" or meta is None:
            raise RuleMountFailure(
                f"rule source {url} {status}: {'; '.join(advice) or 'validation failed'}",
                error_code=RULES_STATUS_ERROR_CODES.get(status),
            )
        binding.packageVersion = meta.version
        binding.protocol = meta.protocol
        binding.commit = _cache_commit(cache)
        binding.offline = bool(self.args.skip_specrules_pull)
        staging_root = self.workspace / RULES_STAGING_RELATIVE_DIR
        staged = staging_root / f"{_rs_now_stamp()}-{binding.module.replace('/', '_') or 'root'}"
        existed = os.path.lexists(tree)
        try:
            staged.mkdir(parents=True, exist_ok=False)
            copy_specrules_to_workspace(cache, staged)
            unchanged = _rule_tree_digests(staged) if existed and plan.applicable and not plan.conflicts else None
            if unchanged is not None and unchanged == _rule_tree_digests(tree):
                # 已挂载树与源逐文件一致：不换树、不产生备份，只刷新回执中的受管清单（重复 setup 幂等）。
                # 顺序沿用 _rule_tree_digests / _rs_tree_files 的插入序，与 commit 写入的清单一致；
                # 按路径重排会让第二次 setup 因顺序不同重写回执。
                binding.managedFiles = [ManagedFile(f"{mountPoint}/{rel}", digest) for rel, digest in unchanged.items()]
                return setup_action(
                    "specrules",
                    tree,
                    self.workspace,
                    "skipped",
                    f"{binding.kind} rules {meta.version or '-'} from {url} already mounted and up to date (copy, {note})",
                )
            migration = self.migrator.commit(plan, staged, receipt)
            self.lastChanged = True
            self.lastMigrationPath = self.migrator.lastMigrationPath
            if migration.backupPath:
                self.lastBackup = self.workspace / migration.backupPath.rstrip("/")
        except DevkitFlowError as error:
            raise RuleMountFailure(compact_error(error), error_code=error.error_code) from error
        except OSError as error:
            raise RuleMountFailure(f"rule tree swap failed: {error}", error_code=RULES_INCOMPLETE_SOURCE) from error
        finally:
            shutil.rmtree(staged, ignore_errors=True)
            try:
                staging_root.rmdir()
            except OSError:
                pass
        backup = f"; previous tree backed up to {migration.backupPath}" if migration.backupPath else ""
        return setup_action(
            "specrules",
            tree,
            self.workspace,
            "updated" if existed else "created",
            f"mounted {binding.kind} rules {meta.version or '-'} from {url} (copy, {note}){backup}",
        )

    def _mount_submodule(
        self, binding: RuleBinding, url: str, tree: Path, skipPull: bool, receipt: BindingReceipt
    ) -> dict[str, Any]:
        """submodule 分发挂载。撤销动作在拍快照后立即登记（codex R2 / arch P2-3 重开）：既有原语在 ``submodule add`` /
        切源之后才返回的错误（如检出后缺 ``rules/index.md``）、原语内异常、切换后校验失败、记录清单失败，都按快照撤销；
        撤销后清空 ``lastUndo``，避免 ``_mount_all`` 重复撤销。成功返回时 ``lastUndo`` 保留，供后续回执写入失败时回滚。"""
        existed = os.path.lexists(tree)
        snapshot = _SubmoduleSnapshot.capture(self.workspace)

        def undo() -> list[str]:
            if snapshot is not None:
                return snapshot.restore()
            if not existed and tree.is_dir() and not tree.is_symlink():
                shutil.rmtree(tree, ignore_errors=True)  # 非 git 工作区：既有原语回退为全新 copy，移除即可
            return []

        self.lastUndo = undo
        undo_problems: list[str] = []

        def rolled_back(message: str) -> str:
            """撤销并返回带结果的文案；撤销未完成的问题项记入 ``undo_problems``，由 ``failure`` 结构化上传（R2-P2-b）。"""
            self.lastUndo = None
            if snapshot is not None and snapshot.unchanged():
                return message  # 失败发生在任何改动之前（如 dirty / 需联网）：无需撤销
            try:
                problems = undo()
            except Exception as error:  # noqa: BLE001 - 撤销尽力而为，问题如实报告
                problems = [compact_error(error)]
            undo_problems.extend(f"submodule: {item}" for item in problems)
            tail = "submodule change rolled back" if not problems else f"rollback incomplete ({'; '.join(problems)})"
            return f"{message}; {tail}"

        def failure(message: str, error_code: str | None) -> RuleMountFailure:
            text = rolled_back(message)
            return RuleMountFailure(text, error_code=error_code, rollback_problems=undo_problems)

        try:
            action = setup_specrules_submodule(
                self.args, self.workspace, tree, repo_url=url, cache_skip_pull=skipPull or bool(self.args.skip_specrules_pull)
            )
        except Exception as error:  # noqa: BLE001 - 原语内异常（copytree / rename 等 OSError）同样按快照撤销
            raise failure(
                f"submodule mount failed: {type(error).__name__}: {compact_error(error)}", RULES_INCOMPLETE_SOURCE
            ) from error
        action["reason"] = self._note(action["reason"], skipPull)
        if action["status"] == "error":
            # 既有原语可能在 add / 切源之后才报错（未自行撤销 gitlink / .gitmodules / .git/modules）：按快照撤销
            raise failure(action["reason"], _mount_failure_code(action["reason"]))
        try:
            # codex P2-1 / arch P2-3：检出树与缓存可能漂移——切换后按同一口径校验（kind / 协议 / 零链接），不合格撤销
            status, meta, advice = _rs_validate_source(tree, binding.kind, explicit=True)
            if status != "bound" or meta is None:
                detail = "; ".join(advice) or "validation failed"
                raise RuleMountFailure(
                    f"rule source {url} checked out into specrules/ is {status}: {detail}",
                    error_code=RULES_STATUS_ERROR_CODES.get(status, RULES_INCOMPATIBLE_SOURCE),
                )
            binding.packageVersion = meta.version
            binding.protocol = meta.protocol
            binding.offline = bool(self.args.skip_specrules_pull)
            index = tree / RULE_PACKAGE_ENTRY
            if not os.path.lexists(tree / ".git"):
                # 非 git 工作区：setup_specrules_submodule 已按既有语义回退为 copy 拷贝（无原树时全新创建）——
                # 回执如实记 copy 分发与完整受管清单，供后续迁移计划比对
                binding.distribution = "copy"
                binding.commit = _cache_commit(specrules_cache_dir(url))
                binding.managedFiles = [
                    ManagedFile(_rule_rel(path, self.workspace), _rs_entry_digest(path)) for path in _rs_tree_files(tree)
                ]
            else:
                git = shutil.which("git")
                binding.commit = _specrules_git_sha(git, tree) if git else None
                binding.managedFiles = [ManagedFile(_rule_rel(index, self.workspace), sha256_file(index))]
        except RuleMountFailure as error:
            raise failure(str(error), error.error_code) from error
        except OSError as error:
            raise failure(f"cannot record mounted rule files: {error}", RULES_INCOMPLETE_SOURCE) from error
        except Exception as error:  # noqa: BLE001 - 其余异常同样先撤销再上抛
            raise failure(f"{type(error).__name__}: {compact_error(error)}", RULES_INCOMPLETE_SOURCE) from error
        self.lastChanged = action["status"] != "skipped"
        return action


def _rule_rel(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path, base)).as_posix()


def _rule_tree_digests(tree: Path) -> dict[str, str] | None:
    """规则树全部条目 → 摘要（相对树根；符号链接按链接文本）；读取失败返回 None（按「有变化」处理）。"""
    try:
        return {_rule_rel(path, tree): _rs_entry_digest(path) for path in _rs_tree_files(tree)}
    except OSError:
        return None


def _receipt_equivalent(previous: bytes | None, receipt: BindingReceipt) -> bool:
    """回执内容（忽略 ``boundAt``）与磁盘上的旧回执一致时不重写，保证重复 setup 不改回执字节。"""
    if previous is None:
        return False
    try:
        old = json.loads(previous.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(old, dict):
        return False
    new = receipt.toJson()
    old.pop("boundAt", None)
    new.pop("boundAt", None)
    return old == new


class SetupRuleRouting:
    """``setup`` 的规则源路由编排：读配置与回执 → 只读识别 → 第 5 步前拉取 → 六步判定 → 挂载或回落。

    ``run`` 返回 specrules action；返回 None 表示走 2.45.0 原 resolve / mount（legacy 不变路径）。
    """

    def __init__(self, args: argparse.Namespace, workspace: Path, resolution: SpecrulesResolution):
        self.args = args
        self.workspace = workspace
        self.resolution = resolution
        self.selector = RuleSourceSelector()
        self.catalog: RuleSourceCatalog | None = None
        self.receipt: BindingReceipt | None = None
        self.profile: ProjectProfile | None = None
        self.decision = RoutingDecision("legacy", None, "legacy-default", [])
        self.bindings: list[RuleBinding] = []
        self.advice: list[str] = []
        self.error_code: str | None = None
        self.pulledUrls: set[str] = set()
        self.pullFailures: list[str] = []
        self.detectionError: str | None = None
        self.configError: DevkitFlowError | None = None
        self.engaged = False
        # 显式 legacy 回退写出的 applied 迁移回执（放弃回退时 advice 提示一并删除，S-3）
        self.fallbackMigrationPath: Path | None = None

    # -- 判定 ----------------------------------------------------------------

    def prepare(self) -> None:
        global_config, _ = load_config_file(Path.home() / ".config" / "devkit" / "config.json")
        project_config, _ = load_config_file(self.workspace / CONFIG_PATH)
        try:
            self.catalog = RuleSourceCatalog.fromConfig(global_config, project_config)
        except DevkitFlowError as error:
            rules = project_config.get("ruleSources") if isinstance(project_config, dict) else None
            self.catalog = None
            if isinstance(rules, dict) and rules.get("routing") == "auto":
                # 显式采用的配置非法：失败关闭——由 run() 产出 specrules error action（保持 actions 形状，arch P2-13）
                self.configError = error
                self.decision = RoutingDecision("adopted", "explicit", "project-config", [])
            else:
                self.advice.append(f"规则路由配置无法解析，已按旧路径执行：{compact_error(error)}")
        self.receipt = BindingReceipt.load(self.workspace)
        try:
            self.profile = ProjectDetector().detect(self.workspace)
        except Exception as error:  # noqa: BLE001 - 识别失败只诊断，不阻断 legacy
            self.profile = None
            self.detectionError = compact_error(error)
            self.advice.append(f"项目识别失败：{self.detectionError}")
        if self.profile is not None and not self.profile.budget.exhausted:
            try:
                ProfileCache().write(self.profile)
            except OSError:
                pass  # 画像缓存只是加速，写失败不影响 setup
        if self.catalog is None:
            return
        self._pull_for_implicit_adoption()
        self.decision = self.selector.routingMode(
            self.workspace,
            self.catalog,
            self.profile,
            self.receipt,
            self.catalog.sourceDirs(),
            allowImplicitAdoption=True,
        )
        if self.pullFailures and self.decision.mode == "legacy":
            self.decision.advice.extend(self.pullFailures)

    def _pull_for_implicit_adoption(self) -> None:
        """第 5 步条件 ①② 成立且画像 confirmed、每个非附属模块的 kind 都有用户写入源时，先拉取这些源到缓存
        （``--skip-specrules-pull`` 时只用已有缓存）。2.45.0 输入没有 catalog 键，不会多出网络操作。"""
        catalog, profile = self.catalog, self.profile
        if (
            catalog is None
            or not catalog.specrulesEnabled
            or catalog.routing is not None
            or self.receipt is not None
            or not catalog.hasUserCatalog()
            or os.path.lexists(self.workspace / RULES_TREE_NAME)
            or catalog.hasProjectSpecrulesConfig
            or profile is None
            or profile.status != "confirmed"
        ):
            return
        for module in profile.modules:
            if profile.status == "confirmed" and module.status != "confirmed":
                continue  # D-25 附属模块不为其求 kind
            kind = module.ruleKind()
            if kind is None or not catalog.isUserConfigured(kind):
                return  # 第 5 步条件 ③ 必然不成立：不联网
        for kind in self.selector.implicitAdoptionKinds(profile, catalog):
            url = catalog.urlFor(kind)
            if url is None or url in self.pulledUrls:
                continue
            ok, note = ensure_specrules_cache(bool(self.args.skip_specrules_pull), url)
            if not self.args.skip_specrules_pull:
                self.pulledUrls.add(url)
            if not ok:
                self.pullFailures.append(RULES_ADVICE_PULL_FAILED.format(kind=kind, reason=note))

    # -- 执行 ----------------------------------------------------------------

    def legacy_mount(self) -> dict[str, Any]:
        """2.45.0 原 resolve / mount（不变）。"""
        specrules_dir = self.workspace / RULES_TREE_NAME
        repo_url = self.resolution.repo_url or SPECRULES_REPO_URL
        if self.args.specrules_mode == "submodule":
            return setup_specrules_submodule(self.args, self.workspace, specrules_dir, repo_url=repo_url)
        return setup_specrules_copy(self.args, self.workspace, specrules_dir, repo_url=repo_url)

    def reconcile_legacy_fallback_advice(self, action: dict[str, Any]) -> None:
        """G-10：隐式回落的 advice 在 legacy 挂载之前就声称「已挂上默认 Java 规则」。

        默认源实际挂载失败（空缓存且不可达，exit 1、无树）时改成与结果一致的句子。
        挂载成功时原句不动（树已在，之后不会自动重试隐式采用）。"""
        if action.get("status") != "error":
            return
        detail = str(action.get("reason") or "legacy mount failed")
        replacement = (
            f"旧路径的默认 Java 规则也未能挂载（{detail}）。本次未挂载任何规则树。"
            "网络或默认源恢复后再次 setup 会重试隐式采用；"
            "或执行 specrules catalog configure --routing auto 后再 setup"
        )
        advice = self.decision.advice
        for index, item in enumerate(advice):
            if RULES_ADVICE_PULL_FAILED_MOUNTED in item:
                advice[index] = item.replace(RULES_ADVICE_PULL_FAILED_MOUNTED, replacement)
            elif RULES_ADVICE_IMPLICIT_LEGACY_MOUNTED in item:
                advice[index] = item.replace(RULES_ADVICE_IMPLICIT_LEGACY_MOUNTED, replacement)

    def run(self) -> dict[str, Any] | None:
        decision = self.decision
        if self.configError is not None:
            self.engaged = True
            self.error_code = self.configError.error_code
            decision.advice.append("ruleSources.routing=auto 但规则路由配置非法：修正 .devkit/config.json 的 ruleSources 后再 setup")
            return self._error_action(
                f"invalid ruleSources config with routing=auto: {compact_error(self.configError)}; "
                "specrules tree and binding receipt left unchanged"
            )
        if self.catalog is None or decision.mode == "disabled":
            return None
        result = self._route(decision)
        self.engaged = result is not None or decision.mode == "adopted" or bool(self.pullFailures) or any(
            item.startswith(("隐式采用", "已采用保持")) for item in decision.advice
        )
        return result

    def _route(self, decision: RoutingDecision) -> dict[str, Any] | None:
        if decision.mode == "legacy":
            at_risk = self.selector.legacyTreeAtRisk(self.workspace, self.catalog, self.receipt)
            if at_risk and decision.source == "project-config":
                return self._explicit_legacy_fallback()
            if at_risk and decision.source == "legacy-default":
                decision.advice.append(RULES_ADVICE_NON_JAVA_GUARD)
                return setup_action(
                    "specrules",
                    self.workspace / RULES_TREE_NAME,
                    self.workspace,
                    "skipped",
                    "kept existing non-Java specrules tree (binding receipt missing); see rule_routing.advice",
                )
            return None
        if decision.adoptedBy == "explicit":
            return self._adopt()
        try:
            return self._adopt()
        except Exception as error:  # noqa: BLE001 - implicit 永不 exit 1（codex P2-3）
            return self._implicit_failure(f"规则路由异常：{compact_error(error)}")

    def _explicit_legacy_fallback(self) -> dict[str, Any]:
        """第 3 步显式 legacy 回退：真正回到 Java 源后才归档回执并写迁移回执；任一步失败恢复原状并报告未完成（exit 1）。

        - copy 分发：现有树改名备份 → 原 resolve / mount → 校验回退后树不再声明非 Java kind → 写迁移回执 → 归档回执；
        - submodule 分发（D-35 ②）：见 ``_submodule_legacy_fallback``；submodule 树上拒绝 ``--specrules-mode copy``。
        """
        tree = self.workspace / RULES_TREE_NAME
        stamp = _rs_now_stamp()
        recorded = None
        if self.receipt is not None and self.receipt.readable:
            recorded = self.receipt.moduleEntry(".") or (self.receipt.modules[0] if self.receipt.modules else None)
        from_kind = (_rs_lenient_package_fields(tree / RULE_PACKAGE_ENTRY) or {}).get("kind")
        if os.path.lexists(tree / ".git") or _specrules_gitmodules_registered(self.workspace):
            return self._submodule_legacy_fallback(stamp, recorded, from_kind)
        migrator = ManagedTreeMigrator(self.workspace, stamp=lambda: stamp)
        existed = os.path.lexists(tree)
        backup: Path | None = migrator.backup(tree, stamp) if existed else None

        def revert() -> None:
            if backup is not None:
                migrator.restore(backup, tree)
            elif not existed and tree.is_dir() and not tree.is_symlink():
                shutil.rmtree(tree, ignore_errors=True)

        try:
            action = self.legacy_mount()
        except Exception:
            revert()
            raise
        if action["status"] == "error":
            revert()
            if backup is not None:
                action["reason"] += "; explicit legacy fallback failed, the original specrules/ was restored"
            self.error_code = _mount_failure_code(action["reason"])
            action["error_code"] = self.error_code
            return action
        if RuleSourceSelector.isNonJavaTree(self.workspace):
            revert()
            return self._fallback_error(
                f"{self.resolution.repo_url or SPECRULES_REPO_URL} still declares a non-Java rule package",
                RULES_INCOMPATIBLE_SOURCE,
            )
        to_fields = _rs_lenient_package_fields(tree / RULE_PACKAGE_ENTRY) or {}
        backup_rel = (_rule_rel(backup, self.workspace) + "/") if backup is not None else None
        archived = self._finish_fallback(
            stamp,
            fromSource={
                "kind": recorded.kind if recorded is not None else from_kind,
                "repoUrl": recorded.repoUrl if recorded is not None else None,
                "commit": recorded.commit if recorded is not None else None,
            },
            toSource={
                "kind": to_fields.get("kind") or "java-backend",
                "repoUrl": self.resolution.repo_url or SPECRULES_REPO_URL,
                "commit": None,
            },
            backupPath=backup_rel,
            revert=revert,
        )
        if isinstance(archived, dict):
            return archived
        notes = ["显式 legacy 回退完成"]
        if backup_rel:
            notes.append(f"原规则树已备份到 {backup_rel}")
        if archived is not None:
            notes.append(f"绑定回执已归档到 {_rule_rel(archived, self.workspace)}")
        notes.append("改回 --routing auto 后 setup 可重新采用")
        self.decision.advice.append("；".join(notes))
        return action

    def _finish_fallback(
        self, stamp: str, *, fromSource: dict[str, Any], toSource: dict[str, Any], backupPath: str | None, revert: Any
    ) -> Path | None | dict[str, Any]:
        """回退收尾：先写迁移回执再归档绑定回执；任一步 IO 失败 → 删除本次迁移回执、恢复原树（回执仍在原位），
        返回 error action（下次 setup 从原状态重来，不会把 Java 树再备份一次，arch P2-5）。"""
        written: Path | None = None
        try:
            written = MigrationReceipt(
                fromSource=fromSource, toSource=toSource, result="applied", backupPath=backupPath, at=_rs_now_iso()
            ).write(self.workspace, stamp)
            archived = ManagedTreeMigrator(self.workspace).archiveReceipt(self.workspace, stamp)
            self.fallbackMigrationPath = written
            return archived
        except OSError as error:
            if written is not None:
                try:
                    written.unlink()
                except OSError:
                    pass
            problems = revert() or []
            return self._fallback_error(f"cannot record the fallback ({error})", RULES_INCOMPLETE_SOURCE, problems=problems)

    def _fallback_error(self, detail: str, code: str, *, problems: list[str] | None = None) -> dict[str, Any]:
        """回退未完成。``problems`` 非空（恢复本身失败的双重故障）时如实写明恢复不完整与处理建议，不再声称原状未改动。"""
        self.error_code = code
        if problems:
            self.decision.advice.append(
                f"显式 legacy 回退未完成，且恢复原状不完整（{'; '.join(problems)}）：规则树 / .gitmodules / 子模块登记可能处于中间状态。"
                "请先用 git status 与 git diff --cached -- .gitmodules specrules 核对，必要时 git checkout -- .gitmodules、"
                "git submodule sync specrules && git submodule update --init specrules 回到已提交状态；"
                "绑定回执仍在原位（.devkit/specrules-binding.json），修复后再 setup，或改回 specrules catalog configure --routing auto"
            )
            return self._error_action(
                f"explicit legacy fallback not completed: {detail}; restore incomplete ({'; '.join(problems)})"
            )
        self.decision.advice.append(
            "显式 legacy 回退未完成：原规则树与绑定回执未改动；修复后再 setup，或改回 specrules catalog configure --routing auto"
        )
        return self._error_action(f"explicit legacy fallback not completed: {detail}; original specrules/ and binding receipt restored")

    def _submodule_legacy_fallback(self, stamp: str, recorded: RuleBinding | None, from_kind: str | None) -> dict[str, Any]:
        """D-35 ②：备份原 gitlink 与子模块 URL → 既有 submodule 切换逻辑切到 Java 源 → gitlink 仍钉在原提交时取 Java 源
        远端 HEAD 并检出、``git add`` 暂存新 gitlink（不代 commit）→ 校验回退后树为 java-backend → 写迁移回执、归档回执。
        任一步失败恢复原 gitlink / URL / ``.gitmodules`` 并报告未完成（exit 1）。"""
        tree = self.workspace / RULES_TREE_NAME
        if self.args.specrules_mode != "submodule":
            self.error_code = RULES_MIGRATION_CONFLICT
            self.decision.advice.append("specrules/ 是 git submodule：去掉 --specrules-mode copy 重跑 setup 回退到 Java 源，或先移除 submodule 再 setup")
            return self._error_action(RULES_FALLBACK_COPY_ON_SUBMODULE)
        snapshot = _SubmoduleSnapshot.capture(self.workspace)
        if snapshot is None:
            return self._fallback_error("git is unavailable or the workspace is not a git repository", RULES_INCOMPLETE_SOURCE)
        git = snapshot.git
        old_url = _gitmodules_specrules_url(self.workspace)
        old_link = snapshot.gitlink or snapshot.head
        if os.path.lexists(tree / ".git") and _specrules_working_tree_dirty(git, tree):
            return self._fallback_error("specrules submodule has local changes; commit or discard them first", RULES_MIGRATION_CONFLICT)
        # Claude R2-P2-c：回退要 git add .gitmodules；它若已有本次之外的未暂存修改，add 会把用户改动一并暂存——
        # 失败关闭（与子模块脏同口径），不产生半暂存状态，由用户先处理再重跑
        unstaged = run_external([git, "-C", str(self.workspace), "diff", "--quiet", "--", ".gitmodules"], check=False)
        if unstaged.returncode != 0:
            self.error_code = RULES_MIGRATION_CONFLICT
            self.decision.advice.append(
                ".gitmodules 有未暂存的修改：回退需要 git add .gitmodules，会把这些修改一并暂存，已拒绝且未改动任何文件。"
                "先 git add .gitmodules（保留）或 git checkout -- .gitmodules（放弃）后再 setup"
            )
            return self._error_action(
                f"{RULES_MIGRATION_CONFLICT}: .gitmodules has unstaged changes; explicit legacy fallback would stage them "
                "together with the submodule URL switch — stage or discard them first, then re-run setup"
            )

        def revert() -> list[str]:
            return snapshot.restore()

        def failed(detail: str, code: str) -> dict[str, Any]:
            return self._fallback_error(detail, code, problems=revert())

        url = self.resolution.repo_url or SPECRULES_REPO_URL
        try:
            action = self.legacy_mount()  # 既有切换：URL 不同 → dirty 检查 + set-url + sync + update --init
            if action["status"] == "error":
                return failed(action["reason"], _mount_failure_code(action["reason"]))
            if RuleSourceSelector.isNonJavaTree(self.workspace):
                # gitlink 仍钉在原（非 Java）提交：取 Java 源远端 HEAD 检出
                fetch = run_external([git, "-C", str(tree), "fetch", "-q", "origin", "HEAD"], check=False)
                if fetch.returncode != 0:
                    return failed(f"git fetch of {url} failed: {_git_result_diagnostic(fetch, limit=200)}", RULES_INCOMPLETE_SOURCE)
                checkout = run_external([git, "-C", str(tree), "checkout", "-q", "--detach", "FETCH_HEAD"], check=False)
                if checkout.returncode != 0:
                    return failed(f"checkout of {url} HEAD failed: {_git_result_diagnostic(checkout, limit=200)}", RULES_INCOMPLETE_SOURCE)
            status, meta, advice = _rs_validate_source(tree, "java-backend", explicit=True)
            if status != "bound" or meta is None:
                return failed(
                    f"{url} is not a usable java-backend rule package ({status}: {'; '.join(advice) or 'validation failed'})",
                    RULES_STATUS_ERROR_CODES.get(status, RULES_INCOMPATIBLE_SOURCE),
                )
            staged = run_external([git, "-C", str(self.workspace), "add", "--", ".gitmodules", RULES_TREE_NAME], check=False)
            if staged.returncode != 0:
                return failed(f"git add of the new gitlink failed: {_git_result_diagnostic(staged, limit=200)}", RULES_INCOMPLETE_SOURCE)
            new_link = _specrules_git_sha(git, tree)
        except Exception:
            # Claude R2-P2-a：与 copy 分发一致——原语 / 检出中的异常（如切源时 copytree / rename 的 OSError）先恢复快照再上抛
            snapshot.restore()
            raise
        archived = self._finish_fallback(
            stamp,
            fromSource={
                "kind": recorded.kind if recorded is not None else from_kind,
                "repoUrl": (recorded.repoUrl if recorded is not None else None) or old_url or None,
                "commit": old_link or (recorded.commit if recorded is not None else None),
            },
            toSource={"kind": meta.kind, "repoUrl": url, "commit": new_link},
            backupPath=None,
            revert=revert,
        )
        if isinstance(archived, dict):
            return archived
        moved = f"gitlink {(old_link or '-')[:12]} → {(new_link or '-')[:12]}"
        notes = [
            "显式 legacy 回退完成",
            f"specrules submodule 已切回 {url}（{moved}，已 git add，请自行提交 .gitmodules 与 specrules）",
            # S-1：可直接复制的提交命令
            'git commit -m "specrules: back to Java rules" -- .gitmodules specrules',
            # S-2：放弃暂存时 git reset 不会改 .git/config 与子模块 origin，需要 sync
            "如放弃此次回退：git reset -q -- .gitmodules specrules && git checkout -- .gitmodules 后执行 "
            "git submodule sync specrules && git submodule update --init specrules",
        ]
        if archived is not None:
            notes.append(f"绑定回执已归档到 {_rule_rel(archived, self.workspace)}（放弃回退时一并移回 .devkit/specrules-binding.json）")
        if self.fallbackMigrationPath is not None:
            # S-3：放弃回退后 result=applied 的迁移回执与实际状态不符——提示一并删除，避免审计记录残留
            notes.append(
                f"迁移回执 {_rule_rel(self.fallbackMigrationPath, self.workspace)}（result=applied）记录了本次回退，"
                "放弃回退时一并删除该文件"
            )
        notes.append("改回 --routing auto 后 setup 可重新采用")
        self.decision.advice.append("；".join(notes))
        action = dict(action)
        action["status"] = "updated"
        action["reason"] = f"{action['reason']}; switched specrules submodule back to the Java source ({moved}, staged)"
        return action

    def _adopt(self) -> dict[str, Any] | None:
        decision, profile = self.decision, self.profile
        explicit = decision.adoptedBy == "explicit"
        if explicit and (profile is None or profile.status != "confirmed"):
            # D-23 ①：挂载前门槛——不挂载、不改现有树、不写也不更新回执
            self.bindings = self.selector.explicitGateBindings(profile) if profile is not None else []
            self.error_code = RULES_BINDING_MISMATCH
            state = profile.status if profile is not None else f"detection failed ({self.detectionError})"
            for binding in self.bindings[:1]:
                decision.advice.extend(binding.advice)
            if profile is None:
                decision.advice.append("routing=auto 要求画像 confirmed；或执行 specrules catalog configure --routing legacy 后 setup")
            return self._error_action(
                f"{RULES_BINDING_MISMATCH}: routing=auto requires a confirmed project profile (got {state}); "
                "specrules tree and binding receipt left unchanged"
            )
        if profile is None:
            return self._implicit_failure(f"项目识别失败：{self.detectionError}")
        bindings = self._select_with_pull(profile)
        failing = [item for item in bindings if item.bindingStatus not in RULES_OK_STATUSES]
        if failing:
            first = failing[0]
            reason = f"模块 {first.module} 选源为 {first.bindingStatus}" + (f"：{first.advice[-1]}" if first.advice else "")
            if explicit:
                self.bindings = bindings
                self.error_code = RULES_STATUS_ERROR_CODES.get(first.bindingStatus, RULES_BINDING_MISMATCH)
                decision.advice.append(reason)
                return self._error_action(f"{self.error_code}: {reason}; specrules tree left unchanged")
            return self._implicit_failure(reason)
        return self._mount_all(profile, bindings)

    def _select_with_pull(self, profile: ProjectProfile) -> list[RuleBinding]:
        """先按现有缓存选源得出各模块 URL，拉取（同一次 setup 每个 URL 至多一次），再在更新后的缓存上重新选源。"""
        catalog = self.catalog
        assert catalog is not None
        first = self.selector.select(profile, catalog, catalog.sourceDirs())
        for binding in first:
            url = binding.repoUrl
            if url is None or binding.bindingStatus in ("unbound-no-kind", "binding-mismatch", "missing-source"):
                continue
            if url in self.pulledUrls:
                continue
            ensure_specrules_cache(bool(self.args.skip_specrules_pull), url)
            if not self.args.skip_specrules_pull:
                self.pulledUrls.add(url)
        return self.selector.select(profile, catalog, catalog.sourceDirs())

    def _distribution(self, binding: RuleBinding, mount: str) -> str:
        if mount != RULES_TREE_NAME:
            return "copy"  # 按模块挂载点只支持 copy（submodule 原语只管理根 specrules/）
        tree = self.workspace / mount
        if os.path.lexists(tree / ".git") or _specrules_gitmodules_registered(self.workspace):
            return "submodule"
        if os.path.lexists(tree):
            return "copy"  # 保持现有普通目录的分发方式，不在采用时顺带转换为 submodule
        previous = self.receipt.moduleEntry(binding.module) if self.receipt is not None and self.receipt.readable else None
        if previous is not None and previous.distribution in ("copy", "submodule"):
            return previous.distribution
        return "submodule" if self.args.specrules_mode == "submodule" else "copy"

    def _mount_all(self, profile: ProjectProfile, bindings: list[RuleBinding]) -> dict[str, Any] | None:
        decision = self.decision
        mixed = profile.classification == "mixed"
        bound = [item for item in bindings if item.bindingStatus == "bound"]
        groups: list[tuple[RuleBinding, list[RuleBinding]]] = []
        if mixed:
            # 每个模块是独立挂载点：回执只记 target 一条，受管清单用挂载结果的完整文件
            # （与单模块根条目同一口径，D-31 ④ 的「模块条目只记入口」只适用于非 mixed）。
            # members 不得再放入 target。同一对象会进回执两次，下面的入口覆盖还会把
            # RuleMountExecutor 填好的完整清单收成只剩 rules/index.md，第二次 setup 将其余文件判为 unknown-owner（G-8）。
            groups = [(item, []) for item in bound]
        elif bound:
            root = next((item for item in bound if item.module == "."), None)
            if root is None:
                template = bound[0]
                root = RuleBinding(
                    module=".",
                    kind=template.kind,
                    repoUrl=template.repoUrl,
                    bindingStatus="bound",
                    packageVersion=template.packageVersion,
                    protocol=template.protocol,
                    entry=f"{RULES_TREE_NAME}/{RULE_PACKAGE_ENTRY}",
                )
            groups = [(root, [item for item in bound if item is not root])]
        migrator = ManagedTreeMigrator(self.workspace)
        receipt_modules: list[RuleBinding] = []
        plans: list[tuple[RuleBinding, list[RuleBinding], MigrationPlan, str]] = []
        legacy_dir = specrules_cache_dir(self.resolution.repo_url or SPECRULES_REPO_URL)
        usable_receipt = self.receipt if self.receipt is not None and self.receipt.readable else None
        for target, members in groups:
            mount = _rule_mount_rel(target)
            target.distribution = self._distribution(target, mount)
            plan = migrator.plan(
                self.workspace,
                usable_receipt,
                target,
                legacySourceDir=legacy_dir if usable_receipt is None and legacy_dir.is_dir() else None,
            )
            plans.append((target, members, plan, mount))
            receipt_modules.append(target)
            receipt_modules.extend(members)
        receipt = BindingReceipt(
            schemaVersion=RULE_BINDING_SCHEMA_VERSION,
            routingMode="adopted",
            adoptedBy=decision.adoptedBy or "implicit",
            profileFingerprint=profile.fingerprint,
            boundAt=_rs_now_iso(),
            modules=receipt_modules,
        )
        conflicted = [(target, plan) for target, _, plan, _ in plans if plan.conflicts]
        if conflicted:
            target, plan = conflicted[0]
            listing = ", ".join(f"{item.path} ({item.reason})" for item in plan.conflicts[:5])
            more = f" (+{len(plan.conflicts) - 5} more)" if len(plan.conflicts) > 5 else ""
            hint = (
                "受管树被删除：恢复 {mount}/，或确认后删除 .devkit/specrules-binding.json 再 setup 重挂".format(
                    mount=plan.mountPoint
                )
                if plan.treeDeleted
                else "规则树中有改动的受管文件或未知归属文件：处理后再 setup"
            )
            reason = f"{RULES_MIGRATION_CONFLICT}: {listing}{more}; {hint}"
            if decision.adoptedBy == "explicit":
                try:
                    migrator.recordNotApplied(plan, receipt)  # 只写 result=not-applied 的迁移回执，不动树（codex P2-7）
                except OSError as error:
                    decision.advice.append(f"迁移回执写入失败：{error}")
                self.bindings = bindings
                self.error_code = RULES_MIGRATION_CONFLICT
                decision.advice.append(hint)
                return self._error_action(reason)
            return self._implicit_failure(reason)
        previous_receipt = self._receipt_bytes()
        residue = _DevkitResidue(self.workspace)
        executor = RuleMountExecutor(self.args, self.workspace, migrator)
        completed: list[tuple[str, Path | None, bool, Path | None]] = []
        undo_submodule: Any = None
        actions: list[dict[str, Any]] = []
        # submodule（只可能是根挂载点）放最后：copy 挂载失败时仍可按备份整体回滚
        ordered = sorted(plans, key=lambda item: item[0].distribution == "submodule")
        try:
            for target, members, plan, mount in ordered:
                skip = bool(self.args.skip_specrules_pull) or (target.repoUrl or SPECRULES_REPO_URL) in self.pulledUrls
                existed = os.path.lexists(self.workspace / mount)
                action = executor.mount(target, plan, mount, skip, receipt)
                if executor.lastChanged and target.distribution != "submodule":
                    completed.append((mount, executor.lastBackup, existed, executor.lastMigrationPath))
                if executor.lastUndo is not None:
                    undo_submodule = executor.lastUndo
                actions.append(action)
                entry_rel = f"{mount}/{RULE_PACKAGE_ENTRY}"
                entry_file = self.workspace / entry_rel
                # 非 mixed 的模块条目共享根挂载点，只记录入口（D-31 ④）。
                # target 自己的完整清单由 RuleMountExecutor / commit 写入，这里不得覆盖。
                for member in members:
                    if member is target:
                        continue
                    member.commit, member.packageVersion, member.protocol = target.commit, target.packageVersion, target.protocol
                    member.distribution, member.offline, member.repoUrl = target.distribution, target.offline, target.repoUrl
                    member.managedFiles = [ManagedFile(entry_rel, sha256_file(entry_file))] if entry_file.is_file() else []
            # 绑定回执唯一写点（copy 的 commit 事务内也会写一次）：内容（忽略 boundAt）不变不重写，重复 setup 幂等
            if not _receipt_equivalent(previous_receipt, receipt):
                receipt.write(self.workspace)
        except Exception as error:  # noqa: BLE001 - 任何挂载期异常都先回滚；implicit 永不 exit 1（codex P2-3）
            failure = error if isinstance(error, RuleMountFailure) else RuleMountFailure(
                f"{type(error).__name__}: {compact_error(error)}"
            )
            # R2-P2-b：执行方内部撤销（submodule 快照恢复）的未完成项结构化上传，与外层回滚的问题合并
            outer = self._rollback(completed, previous_receipt, undo_submodule)
            problems = [*failure.rollback_problems, *outer]
            if decision.adoptedBy == "explicit":
                self.bindings = bindings
                known = set(RULES_STATUS_ERROR_CODES.values()) | {RULES_MIGRATION_CONFLICT}
                self.error_code = failure.error_code if failure.error_code in known else _mount_failure_code(str(failure))
                decision.advice.append(f"挂载失败：{failure}")
                if problems:
                    decision.advice.append(_rollback_incomplete_hint(problems))
                if not problems:
                    left = "; specrules tree left unchanged"
                elif outer:
                    left = f"; rollback incomplete: {'; '.join(outer)}"
                else:
                    left = ""  # 内部撤销未完成已写在 failure 文案中（rollback incomplete (...)），不再声称原树未改动
                return self._error_action(f"rule mount failed: {failure}{left}")
            if decision.source == "implicit" and not problems:
                # 首次采用回落：不留痕，与 2.45.0 工作区一致（codex P2-5）。回滚不完整时保留迁移回执作为核对线索
                residue.clean()
            return self._implicit_failure(f"挂载失败：{failure}", mount_failed=True, rollback_problems=problems)
        self.bindings = bindings
        for item in bindings:
            if item.bindingStatus == "unbound-no-kind":
                item.advice = [RULES_ADVICE_NO_KIND]
        if not actions:
            return setup_action(
                "specrules",
                self.workspace / RULES_TREE_NAME,
                self.workspace,
                "skipped",
                "no module has a dedicated rule source (unbound-no-kind); no rule package mounted, binding receipt written",
            )
        if len(actions) == 1:
            return actions[0]
        merged = dict(actions[0])
        merged["reason"] = "; ".join(f"{item['path']}: {item['reason']}" for item in actions)
        statuses = {item["status"] for item in actions}
        if statuses == {"created"}:
            merged["status"] = "created"
        elif statuses == {"skipped"}:
            merged["status"] = "skipped"  # mixed 多挂载点全部 no-op 时仍是 skipped（D-32 ①）
        else:
            merged["status"] = "updated"
        return merged

    def _receipt_bytes(self) -> bytes | None:
        path = BindingReceipt.pathFor(self.workspace)
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError:
            return None

    def _rollback(
        self,
        completed: list[tuple[str, Path | None, bool, Path | None]],
        previous: bytes | None,
        undo_submodule: Any = None,
    ) -> list[str]:
        """挂载中途失败：撤销 submodule 挂载（快照恢复），已完成的 copy 挂载按其备份还原（原本无树则移除新树），
        其 applied 迁移回执改写为 rolled-back（arch P2-9），回执恢复原字节。返回未能恢复的步骤。"""
        problems: list[str] = []
        if undo_submodule is not None:
            try:
                problems.extend(undo_submodule() or [])
            except Exception as error:  # noqa: BLE001 - 回滚尽力而为，问题如实报告
                problems.append(f"submodule: {compact_error(error)}")
        for mount, backup, existed, migration in reversed(completed):
            tree = self.workspace / mount
            try:
                if backup is not None and backup.is_dir():
                    ManagedTreeMigrator(self.workspace).restore(backup, tree)
                elif not existed and tree.is_dir() and not tree.is_symlink():
                    shutil.rmtree(tree, ignore_errors=True)
            except (OSError, DevkitFlowError) as error:
                problems.append(f"{mount}: {compact_error(error)}")
            if migration is not None and migration.is_file():
                try:
                    record = json.loads(migration.read_text(encoding="utf-8"))
                    record["result"] = "rolled-back"
                    record["backupPath"] = None
                    record["at"] = _rs_now_iso()
                    atomic_write_text(migration, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
                except (OSError, ValueError, UnicodeDecodeError):
                    pass  # 审计记录改写失败不掩盖原始错误
        path = BindingReceipt.pathFor(self.workspace)
        try:
            if previous is not None:
                atomic_write_text(path, previous.decode("utf-8"))
            elif os.path.lexists(path):
                path.unlink()
        except (OSError, UnicodeDecodeError) as error:
            problems.append(f"binding receipt: {error}")
        return problems

    def _implicit_failure(
        self, reason: str, *, mount_failed: bool = False, rollback_problems: list[str] | None = None
    ) -> dict[str, Any] | None:
        """implicit 永不 exit 1：首次采用回落 legacy 原挂载；已采用保持则原树不动、不更新回执，绑定按回执与现有树。

        ``rollback_problems`` 非空（挂载失败后回滚本身也失败的双重故障，Claude R2-P2-b）时，advice 如实写明回滚不完整
        与恢复建议，不再声称「原规则树与绑定回执未改动」/「与 2.45.0 一致」。"""
        decision = self.decision
        incomplete = _rollback_incomplete_hint(rollback_problems) if rollback_problems else ""
        if decision.source == "implicit":
            prefix = "隐式采用挂载失败" if mount_failed else "隐式采用未完成"
            decision.mode, decision.adoptedBy, decision.source = "legacy", None, "legacy-default"
            if incomplete:
                # R2-P2-b：回滚不完整时规则树 / 子模块可能残留半挂载状态——不在其上叠加旧路径挂载（会把残留判为 dirty
                # 而 exit 1，或覆盖残留造成二次改动）；本次跳过 specrules 挂载，exit 0，按 advice 恢复后再 setup
                decision.advice.append(f"{prefix}：{reason}；{incomplete}；本次未按旧路径挂载，恢复后再 setup")
                return setup_action(
                    "specrules",
                    self.workspace / RULES_TREE_NAME,
                    self.workspace,
                    "skipped",
                    "implicit rule mount failed and its rollback was incomplete; legacy mount not attempted on the "
                    "partially restored tree; see rule_routing.advice",
                )
            decision.advice.append(f"{prefix}：{reason}；{RULES_ADVICE_IMPLICIT_LEGACY_MOUNTED}")
            return None
        if incomplete:
            decision.advice.append(
                f"已采用保持：{reason}；{incomplete}，再 setup（或 specrules catalog configure --routing legacy 后 setup）"
            )
        else:
            decision.advice.append(
                f"已采用保持：{reason}；原规则树与绑定回执未改动（修复源后再 setup，或 specrules catalog configure --routing legacy 后 setup）"
            )
        self.bindings = self._hold_preview()
        return setup_action(
            "specrules",
            self.workspace / RULES_TREE_NAME,
            self.workspace,
            "skipped",
            "rule mount failed and its rollback was incomplete; see rule_routing.advice"
            if incomplete
            else "kept the adopted rule tree and binding receipt unchanged; see rule_routing.advice",
        )

    def _hold_preview(self) -> list[RuleBinding]:
        profile, catalog, receipt = self.profile, self.catalog, self.receipt
        if profile is None or catalog is None or receipt is None:
            return []
        try:
            selected = self.selector.select(profile, catalog, catalog.sourceDirs())
            held = hold_bindings(self.workspace, selected, receipt, profile)
            context = RuleClosureResolver(self.workspace).resolve(self.decision, held, profile)
            return [item.binding for item in context.modules]
        except Exception as error:  # noqa: BLE001 - 预览只诊断
            self.decision.advice.append(f"绑定预览失败：{compact_error(error)}")
            return []

    def _error_action(self, reason: str) -> dict[str, Any]:
        action = setup_action("specrules", self.workspace / RULES_TREE_NAME, self.workspace, "error", reason)
        if self.error_code:
            action["error_code"] = self.error_code
        return action

    # -- 输出 ----------------------------------------------------------------

    def message_lines(self) -> list[str]:
        """文本输出的路由摘要：只在路由实际介入（adopted、显式回退、非 Java 树守卫、隐式采用回落）时追加，
        2.45.0 工作区（legacy-default 且未介入）的 message 与原输出逐字一致。"""
        if not self.engaged:
            return []
        decision = self.decision
        adopted = f", {decision.adoptedBy}" if decision.adoptedBy else ""
        lines = [f"- rule-routing: {decision.mode} ({decision.source}{adopted})"]
        lines.extend(f"  advice: {item}" for item in self.routing_json()["advice"])
        return lines

    def detection_json(self) -> dict[str, Any] | None:
        profile = self.profile
        if profile is None:
            return None
        return {
            "classification": profile.classification,
            "status": profile.status,
            "modules": [{"id": module.id, "classification": module.classification} for module in profile.modules],
            "fingerprint": profile.fingerprint,
        }

    def routing_json(self) -> dict[str, Any]:
        decision = self.decision
        bindings = self.bindings
        if not bindings and self.profile is not None and decision.mode in ("legacy", "disabled"):
            status = "disabled" if decision.mode == "disabled" else "legacy-diagnostic"
            bindings = [RuleBinding(module=module.id, kind=None, bindingStatus=status) for module in self.profile.modules]
        advice: list[str] = []
        unbound = [f"模块 {item.module}：{RULES_ADVICE_NO_KIND}" for item in bindings if item.bindingStatus == "unbound-no-kind"]
        for item in [*self.advice, *decision.advice, *unbound]:
            if item not in advice:
                advice.append(item)
        return {
            "mode": decision.mode,
            "routing_source": decision.source,
            "adoptedBy": decision.adoptedBy,
            "bindings": [
                {"module": item.module, "kind": item.kind, "repoUrl": item.repoUrl, "status": item.bindingStatus}
                for item in bindings
            ],
            "advice": advice,
        }


def _checkout_origin_url(workspace: Path) -> str:
    """只读 ``.git/config`` 里的 origin URL。没有 remote 时不调用 git，避免兼容沙箱多出事件。"""
    git_path = workspace / ".git"
    config_path: Path | None = None
    if git_path.is_dir():
        config_path = git_path / "config"
    elif git_path.is_file():
        try:
            gitdir_text = git_path.read_text(encoding="utf-8")
        except OSError:
            return ""
        for raw in gitdir_text.splitlines():
            stripped = raw.strip()
            if not stripped.startswith("gitdir:"):
                continue
            git_dir = Path(stripped.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = workspace / git_dir
            config_path = git_dir / "config"
            break
    if config_path is None or not config_path.is_file():
        return ""
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    in_origin = False
    # git 允许 `[section]` 后写 `;` / `#` 行注释，段头不能要求整行以 `]` 结尾。
    for raw in lines:
        stripped = raw.strip()
        header = re.match(r"^\[(.*?)\]", stripped)
        if header:
            in_origin = header.group(1).strip() in {'remote "origin"', "remote 'origin'"}
            continue
        if not in_origin or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip().lower() == "url":
            return value.strip()
    return ""


def _pr_review_repos_action(workspace: Path) -> dict[str, Any] | None:
    """Public build does not register repositories with a review service."""
    del workspace
    return None


def command_setup(args: argparse.Namespace) -> int:
    workspace = Path(args.target).expanduser().resolve()
    if workspace.exists() and not workspace.is_dir():
        raise DevkitFlowError(f"setup target is not a directory: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    actions: list[dict[str, Any]] = []

    # 1. CLI 只使用全局安装。setup 不再向业务仓复制可执行文件；存量副本由 migrate 显式清理。
    global_cli = find_global_cli()
    if global_cli is not None:
        actions.append(setup_action("global-cli", global_cli, workspace, "skipped", f"using global CLI {global_cli}"))
    else:
        actions.append(
            setup_action(
                "global-cli",
                current_cli_path(),
                workspace,
                "skipped",
                "running CLI is usable for this setup, but install devkit-flow globally before later workflow commands",
            )
        )

    # 只提示，不隐式删除。真正清理由 migrate 重新校验生成证据、Plugin 状态和用户确认。
    legacy_markers = [
        workspace / DEVKIT_CLI_RELATIVE_PATH,
        workspace / ".kimi-code" / "AGENTS.md",
    ]
    has_legacy_runtime_files = any(path.is_file() for path in legacy_markers) or any(
        root.is_dir() and any(root.rglob(pattern))
        for root, pattern in (
            (workspace / ".codex" / "agents", "*.toml"),
            (workspace / ".claude" / "commands", "*.md"),
            (workspace / ".agents" / "skills", "SKILL.md"),
        )
    )
    if has_legacy_runtime_files:
        actions.append(
            setup_action(
                "legacy-workspace",
                workspace,
                workspace,
                "skipped",
                "legacy DevKit runtime files detected; preview with `devkit-flow migrate legacy-workspace "
                "--runtime all`, then confirm removal with the user before adding `--apply`",
            )
        )

    # 2. specrules/：先 resolve 生效态。invalid 拒绝挂载且不按关闭清理；
    #    disabled 走 skip-remove；开启路径按 Resolution.repo_url 挂载。
    #    016 T-05：resolve 之后做规则源路由判定（只读识别 + 六步判定）；legacy / disabled 走原路径不变，
    #    adopted 由 SetupRuleRouting 选源、冲突检查、RuleMountExecutor 挂载并写绑定回执。
    specrules_dir = workspace / "specrules"
    specrules_resolution = resolve_specrules(workspace)
    rule_routing = SetupRuleRouting(args, workspace, specrules_resolution)
    rule_routing.prepare()
    if specrules_resolution.effective_state == "invalid":
        actions.append(
            setup_action(
                "specrules",
                specrules_dir,
                workspace,
                "error",
                SPECRULES_SETUP_ILLEGAL_REASON,
            )
        )
    elif specrules_resolution.effective_state == "disabled":
        actions.append(cleanup_specrules_tree(workspace, specrules_resolution))
    else:
        routed = rule_routing.run()
        if routed is None:
            routed = rule_routing.legacy_mount()
            rule_routing.reconcile_legacy_fallback_advice(routed)
        actions.append(routed)

    # 3. AGENTS.md / CLAUDE.md：仅当不存在时写入极简路由版，已存在绝不覆盖
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = workspace / name
        if path.exists():
            actions.append(setup_action(name, path, workspace, "skipped", "already exists; not overwritten"))
        else:
            atomic_write_text(path, WORKSPACE_ROUTING_TEMPLATE.format(filename=name))
            actions.append(setup_action(name, path, workspace, "created", "wrote minimal routing pointer"))

    # 4. project-rules/project-rules.md：不存在则写极简骨架
    rules_path = workspace / "project-rules" / "project-rules.md"
    if rules_path.exists():
        actions.append(setup_action("project-rules", rules_path, workspace, "skipped", "already exists; not overwritten"))
    else:
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(rules_path, PROJECT_RULES_TEMPLATE)
        actions.append(setup_action("project-rules", rules_path, workspace, "created", "wrote skeleton template"))

    # 5. --with-cursor：渲染 .cursor/agents/（内容一致的文件跳过，保证幂等）
    if args.with_cursor:
        cursor_dir = workspace / ".cursor" / "agents"
        agents_source, source_note = resolve_agents_source()
        if agents_source is None:
            actions.append(setup_action("cursor-agents", cursor_dir, workspace, "skipped", source_note))
        else:
            created = updated = unchanged = 0
            for source_path in sorted(agents_source.glob("*.md")):
                if source_path.name == "README.md" or source_path.name in AGENT_DATA_PAYLOAD_FILES:
                    continue
                rendered = render_cursor_agent_simple(source_path)
                target_path = cursor_dir / source_path.name
                if target_path.is_file() and target_path.read_text(encoding="utf-8") == rendered:
                    unchanged += 1
                    continue
                if target_path.exists():
                    updated += 1
                else:
                    created += 1
                cursor_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_text(target_path, rendered)
            status = "created" if created else ("updated" if updated else "skipped")
            actions.append(
                setup_action(
                    "cursor-agents",
                    cursor_dir,
                    workspace,
                    status,
                    f"{created} created, {updated} updated, {unchanged} unchanged (source: {source_note})",
                )
            )

    # 6. 遥测身份半自动绑定：真人终端问一句是否 SSO 登录；非 TTY / json / --no-sso-login 只留提示。
    sso_offer = offer_sso_login(
        workspace,
        fmt=args.format,
        interactive=not getattr(args, "no_sso_login", False),
    )
    if sso_offer is not None:
        actions.append(
            setup_action(
                "sso-identity",
                Path.home() / TELEMETRY_SSO_IDENTITY_CACHE_RELATIVE_PATH,
                workspace,
                "created" if sso_offer.get("status") == "bound" else "skipped",
                str(sso_offer.get("reason") or ""),
            )
        )

    repos_action = _pr_review_repos_action(workspace)
    if repos_action is not None:
        actions.append(repos_action)

    failed = [item for item in actions if item["status"] == "error"]
    counts: dict[str, int] = {}
    for item in actions:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    summary = ", ".join(f"{status}: {counts[status]}" for status in sorted(counts))
    lines = [f"setup {'completed with errors' if failed else 'completed'} ({summary})"]
    lines.extend(f"- {item['item']}: {item['status']} — {item['reason']}" for item in actions)
    lines.extend(rule_routing.message_lines())
    payload: dict[str, Any] = {
        "status": "issues" if failed else "ok",
        "message": "\n".join(lines),
        "target": str(workspace),
        "actions": actions,
        # 016 T-05：新增诊断键（legacy / disabled 下只加键，既有键与退出码不变）
        "project_detection": rule_routing.detection_json(),
        "rule_routing": rule_routing.routing_json(),
    }
    if rule_routing.error_code:
        payload["error_code"] = rule_routing.error_code
    emit(payload, args.format)
    return 1 if failed else 0


# runtime check（SessionStart 横幅）复用的旧插件残留一行提示；≤70 字符以避开
# additionalContext 500 字符截断（runtime_hook_envelope 尾部追加，详见 doctor fix）。
CODEX_STALE_PLUGIN_HINT = "stale legacy codex plugin enabled; run devkit-flow doctor to clean up"

# 只匹配 [plugins."name@marketplace"] 一级节头；子表（plugins."x".y）不认作插件条目。
_CODEX_PLUGIN_SECTION_RE = re.compile(r"^plugins\.(?:\"([^\"]+)\"|'([^']+)')$")
_CODEX_SECTION_HEADER_RE = re.compile(r"^\[([^\[\]]+)\]\s*(?:#.*)?$")
_CODEX_ENABLED_LINE_RE = re.compile(r"^enabled\s*=\s*(true|false)\s*(?:#.*)?$")


def parse_codex_plugin_entries(text: str) -> dict[str, bool]:
    """从 codex config.toml 文本提取 {插件条目: enabled}；enabled 键缺省按 true。

    刻意用容错的行扫描而非严格 TOML 解析（本仓无第三方依赖、需兼容 py3.9 无
    tomllib）：识别不了的行一律跳过，坏文件表现为“找不到条目”而非报错。
    """
    entries: dict[str, bool] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        header = _CODEX_SECTION_HEADER_RE.match(line)
        if header:
            current = None
            section = _CODEX_PLUGIN_SECTION_RE.match(header.group(1).strip())
            if section:
                current = section.group(1) or section.group(2)
                entries.setdefault(current, True)
            continue
        if current is not None:
            enabled = _CODEX_ENABLED_LINE_RE.match(line)
            if enabled:
                entries[current] = enabled.group(1) == "true"
    return entries


def stale_codex_plugin_finding(config_path: Path | None = None) -> dict[str, Any] | None:
    """检测宿主 ~/.codex/config.toml 中新旧插件命名并存且均 enabled（只读）。

    主命中：同 marketplace 下旧名与新名条目均 enabled（会导致 SessionStart hook
    重复执行）；其余 enabled 的旧名条目附带列出。文件不存在/不可读/解码失败一律
    返回 None（静默通过，不产生检查项）。禁止在此改写宿主配置。
    """
    path = config_path if config_path is not None else Path.home() / ".codex" / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    entries = parse_codex_plugin_entries(text)
    pairs: list[tuple[str, str]] = []
    extras: list[str] = []
    for key, enabled in entries.items():
        name, _, marketplace = key.partition("@")
        if name not in LEGACY_RUNTIME_PLUGIN_NAMES or not enabled:
            continue
        current_key = f"{RUNTIME_PLUGIN_NAME}@{marketplace}" if marketplace else RUNTIME_PLUGIN_NAME
        if entries.get(current_key):
            pairs.append((key, current_key))
        else:
            extras.append(key)
    if not pairs:
        return None
    coexist = "; ".join(f"'{legacy}' and '{current}' are both enabled" for legacy, current in pairs)
    message = (
        f"legacy and current plugin naming coexist in {path}: {coexist}; "
        "SessionStart hooks run once per enabled entry (duplicate banner)"
    )
    if extras:
        message += "; other enabled legacy entries: " + ", ".join(extras)
    sections = ", ".join(f'[plugins."{legacy}"]' for legacy in [p[0] for p in pairs] + extras)
    fix = (
        f"edit {path} and set enabled = false under {sections} (or delete those sections); "
        "stale hooks.state entries expire on their own once the legacy plugin is disabled"
    )
    return {
        "config_path": str(path),
        "pairs": pairs,
        "extras": extras,
        "message": message,
        "fix": fix,
        "hint": CODEX_STALE_PLUGIN_HINT,
    }


def _plugin_like_stop_command(command: str) -> bool:
    """插件自身 session-end 与用户 Stop 段里的同源拷贝。

    DR-25：插件投影命令是 ``--hook-event SessionEnd``。用户若仍把旧插件命令
    （``--hook-event Stop``）留在 Stop 段，同样含 session-end + ``--hook-json``，
    也算 plugin_like，不因空 stdout 误诊为第三方 Stop。
    """
    return "devkit-flow runtime session-end" in command and "--hook-json" in command


def _collect_hook_commands(node: Any, commands: list[str]) -> None:
    stack: list[Any] = [node]
    remaining = 10000
    while stack and remaining > 0:
        current = stack.pop()
        remaining -= 1
        if isinstance(current, dict):
            command = current.get("command")
            if isinstance(command, str):
                commands.append(command)
            for key, value in current.items():
                if key != "command":
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)


def _ok_stop_scan(
    path: Path,
    *,
    message: str,
    plugin_like_count: int = 0,
    third_party_count: int = 0,
    entries: tuple[UserStopHookEntry, ...] = (),
) -> UserStopHookScan:
    return UserStopHookScan(
        user_hooks_path=str(path),
        plugin_like_count=plugin_like_count,
        third_party_count=third_party_count,
        file_modified=False,
        check_status="ok",
        message=message,
        fix="",
        entries=entries,
    )


def _warn_stop_scan(
    path: Path,
    *,
    message: str,
    plugin_like_count: int = 0,
    third_party_count: int = 0,
    entries: tuple[UserStopHookEntry, ...] = (),
) -> UserStopHookScan:
    return UserStopHookScan(
        user_hooks_path=str(path),
        plugin_like_count=plugin_like_count,
        third_party_count=third_party_count,
        file_modified=False,
        check_status="warn",
        message=message,
        fix=CODEX_USER_STOP_HOOKS_FIX,
        entries=entries,
    )


def scan_user_codex_stop_hooks(home: Path) -> UserStopHookScan:
    """只读扫描 ~/.codex/hooks.json 的 Stop 段；不执行命令、不改文件。"""
    path = Path(home) / ".codex" / "hooks.json"
    try:
        return _scan_user_codex_stop_hooks(path)
    except Exception:
        return _warn_stop_scan(
            path, message="user Codex hooks.json is not a valid JSON object"
        )


def _scan_user_codex_stop_hooks(path: Path) -> UserStopHookScan:
    if not path.is_file():
        return _ok_stop_scan(path, message="no user Codex hooks.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _warn_stop_scan(
            path, message="user Codex hooks.json is not a valid JSON object"
        )
    if not isinstance(payload, dict):
        return _warn_stop_scan(
            path, message="user Codex hooks.json is not a valid JSON object"
        )
    hooks = payload.get("hooks")
    stop = hooks.get("Stop") if isinstance(hooks, dict) else None
    if stop is None:
        return _ok_stop_scan(path, message="no user Codex Stop hooks")
    commands: list[str] = []
    _collect_hook_commands(stop, commands)
    entries = tuple(
        UserStopHookEntry(
            command=command,
            kind="plugin_like" if _plugin_like_stop_command(command) else "third_party",
        )
        for command in commands
    )
    plugin_like = tuple(item for item in entries if item.kind == "plugin_like")
    third_party = tuple(item for item in entries if item.kind == "third_party")
    if not third_party:
        return _ok_stop_scan(
            path,
            message=(
                "plugin-like Codex Stop hooks present"
                if plugin_like
                else "no user Codex Stop hooks"
            ),
            plugin_like_count=len(plugin_like),
            entries=entries,
        )
    listed = ", ".join(item.command for item in third_party)
    return _warn_stop_scan(
        path,
        message=(
            "third-party Codex Stop hooks may emit empty stdout "
            f"({listed}); Codex Stop requires JSON on stdout"
        ),
        plugin_like_count=len(plugin_like),
        third_party_count=len(third_party),
        entries=entries,
    )


def command_doctor(args: argparse.Namespace) -> int:
    """只读体检：任何一项失败都只报告不修改，命令本身永远 exit 0。"""
    workspace = Path(args.target).expanduser().resolve()
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, message: str, fix: str = "", **extra: Any) -> None:
        check = {"name": name, "status": status, "message": message}
        if fix:
            check["fix"] = fix
        check.update(extra)
        checks.append(check)

    # 存量工作区 CLI：仅报告迁移提示，不再作为 readiness 条件。
    cli_path = workspace / DEVKIT_CLI_RELATIVE_PATH
    if cli_path.is_file():
        version = extract_embedded_version(cli_path)
        add(
            "legacy-workspace-cli",
            "warn",
            f"legacy project CLI present (version {version or 'unknown'}); global CLI is the supported entrypoint",
            fix="devkit-flow migrate legacy-workspace --runtime all --apply",
        )

    specrules_summary_payload: dict[str, Any] | None
    try:
        specrules_summary_payload = specrules_summary(workspace)
    except DevkitFlowError:
        specrules_summary_payload = None
    specrules_check = specrules_doctor(workspace)
    add(
        "specrules",
        specrules_check["status"],
        specrules_check["message"],
        fix=specrules_check.get("fix", ""),
    )

    # AGENTS.md 路由文件
    if (workspace / "AGENTS.md").exists():
        add("agents-md", "ok", "AGENTS.md present")
    else:
        add("agents-md", "error", "AGENTS.md is missing", fix="devkit-flow setup")

    # spec/ 可写（不存在时可由 feature init 按需创建，前提是工作区可写）
    spec_dir = workspace / "spec"
    if spec_dir.is_dir() and os.access(spec_dir, os.W_OK):
        add("spec-dir", "ok", "spec/ is writable")
    elif spec_dir.exists():
        add("spec-dir", "error", "spec/ exists but is not a writable directory")
    elif os.access(workspace, os.W_OK):
        add("spec-dir", "ok", "spec/ does not exist yet; workspace is writable so it can be created on demand")
    else:
        add("spec-dir", "error", "workspace is not writable; spec/ cannot be created")

    # 全局 CLI 位置与版本：PATH（npm 全局 bin）优先，~/.config/devkit/bin 兜底，两处都无才 error
    global_cli = find_global_cli()
    if global_cli is not None:
        global_version = extract_embedded_version(global_cli)
        matches = sha256_file(cli_code_path(global_cli)) == sha256_file(current_cli_path())
        add(
            "global-cli",
            "ok",
            f"{global_cli} (version {global_version or 'unknown'}, {'matches' if matches else 'differs from'} the running CLI)",
            version=global_version,
        )
    else:
        add(
            "global-cli",
            "error",
            "devkit-flow is neither on PATH nor installed at ~/.config/devkit/bin/devkit-flow",
            fix=(
                "npm i -g devkit-flow@latest --registry=https://registry.npmjs.org, "
                "then keep it fresh with devkit-flow self-update"
            ),
        )

    # self-update 渠道：内网 npm 包（唯一通道）
    add("self-update-channel", "ok", f"npm channel: {NPM_PACKAGE_NAME} ({NPM_REGISTRY})")

    # ONES 配置摘要
    try:
        ones = ones_config_summary(workspace)
    except DevkitFlowError as error:
        ones = None
        add("ones", "warn", f"cannot read ONES config: {compact_error(error)}", fix="check .devkit/config.json JSON syntax")
    if ones is not None:
        if ones["enabled"]:
            ones_message = f"enabled (space {ones['space_id'] or '-'}, source: {ones['source']})"
        elif ones["configured"]:
            ones_message = f"configured but disabled (source: {ones['source']})"
        else:
            ones_message = "not configured (optional)"
        add("ones", "ok", ones_message)

    # KBA 是可选外部上下文源。关闭时不探测 mtskills；开启时只读检查，缺失不自动安装。
    try:
        kba_summary = context_source_summary(workspace)
        kba_check = kba_context_source_doctor(workspace)
    except DevkitFlowError as error:
        kba_summary = None
        kba_check = {
            "status": "warn",
            "message": f"cannot read KBA context source config: {compact_error(error)}",
            "fix": "check .devkit/config.json JSON syntax",
        }
    add(
        "kba-context-source",
        kba_check["status"],
        kba_check["message"],
        fix=kba_check.get("fix", ""),
    )

    try:
        workflow_matrix = workflow_auto_takeover_summary(workspace)
        add(
            "workflow-auto-takeover",
            "ok",
            workflow_auto_takeover_doctor_message(workflow_matrix),
        )
    except DevkitFlowError as error:
        workflow_matrix = None
        add(
            "workflow-auto-takeover",
            "warn",
            compact_error(error),
            fix="check .devkit/config.json JSON syntax",
        )

    docs_curator_summary_payload: dict[str, Any] | None
    try:
        docs_curator_summary_payload = docs_curator_summary(workspace)
        docs_curator_check = docs_curator_doctor(workspace)
    except DevkitFlowError as error:
        docs_curator_summary_payload = None
        docs_curator_check = {
            "status": "warn",
            "message": compact_error(error),
            "fix": "check .devkit/config.json JSON syntax",
        }
    add(
        "docs-curator",
        docs_curator_check["status"],
        docs_curator_check["message"],
        fix=docs_curator_check.get("fix", ""),
    )

    html_ui_summary_payload: dict[str, Any] | None
    try:
        html_ui_summary_payload = html_ui_summary(workspace)
        html_ui_check = html_ui_doctor(workspace)
    except DevkitFlowError as error:
        html_ui_summary_payload = None
        html_ui_check = {
            "status": "warn",
            "message": compact_error(error),
            "fix": "check .devkit/config.json JSON syntax",
        }
    add(
        "html-ui",
        html_ui_check["status"],
        html_ui_check["message"],
        fix=html_ui_check.get("fix", ""),
    )

    spec_artifacts_summary_payload: dict[str, Any] | None
    try:
        spec_artifacts_summary_payload = spec_artifacts_summary(workspace)
    except DevkitFlowError:
        spec_artifacts_summary_payload = None
    spec_artifacts_check = spec_artifacts_doctor(workspace)
    add(
        "spec-artifacts",
        spec_artifacts_check["status"],
        spec_artifacts_check["message"],
        fix=spec_artifacts_check.get("fix", ""),
    )

    review_rounds_summary_payload: dict[str, Any] | None
    try:
        review_rounds_summary_payload = review_rounds_summary(workspace)
        review_rounds_check = review_rounds_doctor(workspace)
    except DevkitFlowError as error:
        review_rounds_summary_payload = None
        review_rounds_check = {
            "status": "warn",
            "message": compact_error(error),
            "fix": "check .devkit/config.json JSON syntax",
        }
    add(
        "review-rounds",
        review_rounds_check["status"],
        review_rounds_check["message"],
        fix=review_rounds_check.get("fix", ""),
    )

    try:
        review_gate_timing = load_review_gate_timing_summary(workspace)
        warnings = review_gate_timing["warnings"]
        add(
            "review-gate-timing",
            "warn" if warnings else "ok",
            format_review_gate_timing_status_text(review_gate_timing),
            fix="\n".join(warning.split("fix with: ", 1)[1] for warning in warnings),
        )
    except DevkitFlowError as error:
        add(
            "review-gate-timing",
            "warn",
            compact_error(error),
            fix="check .devkit/config.json JSON syntax",
        )

    design_template_summary_payload: dict[str, Any] | None
    try:
        design_template_summary_payload = design_template_summary(workspace)
        design_template_check = design_template_doctor(workspace, summary=design_template_summary_payload)
    except DevkitFlowError as error:
        design_template_summary_payload = None
        design_template_check = {
            "status": "warn",
            "message": compact_error(error),
            "fix": "check .devkit/config.json JSON syntax",
        }
    add(
        "design-template",
        design_template_check["status"],
        design_template_check["message"],
        fix=design_template_check.get("fix", ""),
    )

    # e2e 准出门只读检查（接口文档 §4.7）：配置可读且 sourceType 合法 → ok；
    # gateFile 当前不存在 → warn；配置非法按既有配置项同口径 fail-closed（warn + fix）。
    try:
        e2e_check = e2e_doctor(workspace)
    except DevkitFlowError as error:
        e2e_check = {
            "status": "warn",
            "message": f"cannot read e2e config: {compact_error(error)}",
            "fix": "check .devkit/config.json JSON syntax",
        }
    add(
        "e2e",
        e2e_check["status"],
        e2e_check["message"],
        fix=e2e_check.get("fix", ""),
    )

    try:
        telemetry_check = telemetry_doctor_check(workspace)
    except Exception as error:
        telemetry_check = {
            "status": "warn",
            "message": f"cannot inspect telemetry: {compact_error(error)}",
            "fix": "check .devkit/config.json JSON syntax",
        }
    add(
        "telemetry",
        telemetry_check.get("status", "ok"),
        telemetry_check.get("message", ""),
        fix=telemetry_check.get("fix", ""),
    )

    if shutil.which("codex") or codex_user_home().exists():
        registration = codex_agent_registration_check()
        add(
            "codex-agent-registration",
            "ok" if registration.get("ready") else "warn",
            registration.get("message", "custom agent registration is incomplete"),
            fix="" if registration.get("ready") else "devkit-flow runtime update --runtime codex",
            registration=registration,
        )

    # 宿主 codex 插件新旧命名并存检测（010）：只读；未命中/不可读/解析失败零输出。
    try:
        stale_plugin = stale_codex_plugin_finding()
    except Exception:
        stale_plugin = None
    if stale_plugin is not None:
        add("codex-stale-plugin", "warn", stale_plugin["message"], fix=stale_plugin["fix"])

    stop_scan = scan_user_codex_stop_hooks(Path.home())
    add(
        "codex-user-stop-hooks",
        stop_scan.check_status,
        stop_scan.message,
        fix=stop_scan.fix,
        user_hooks_path=stop_scan.user_hooks_path,
        plugin_like_count=stop_scan.plugin_like_count,
        third_party_count=stop_scan.third_party_count,
        file_modified=stop_scan.file_modified,
    )

    def add_readonly(name: str, builder: Any) -> None:
        try:
            result = builder()
        except Exception as error:
            add(name, "warn", f"{name} failed: {type(error).__name__}", detail={})
            return
        detail = result.get("detail")
        if not isinstance(detail, dict):
            detail = {}
        add(
            name,
            str(result.get("status") or "warn"),
            str(result.get("message") or ""),
            fix=str(result.get("fix") or ""),
            detail=detail,
        )

    # D-73 ①：只识别一次，两项检查共用同一份内存画像（各自识别会各自重置预算，且可能得到不同结果）。
    detection = doctor_detect_project(workspace)
    add_readonly("project-detection", lambda: doctor_project_detection(workspace, detection))
    add_readonly("rule-binding", lambda: doctor_rule_binding(workspace, detection))
    add_readonly("runtime-lifecycle-transactions", doctor_runtime_transactions)
    add_readonly("version-provenance", lambda: doctor_version_provenance(workspace))
    rank = {"ok": 0, "info": 0, "warn": 1, "error": 2}
    worst = max((rank[check["status"]] for check in checks), default=0)
    overall = {0: "ok", 1: "warn", 2: "error"}[worst]
    lines = [f"doctor: {overall} ({workspace})"]
    for check in checks:
        line = f"- [{check['status']}] {check['name']}: {check['message']}"
        if check.get("fix"):
            line += f" (fix: {check['fix']})"
        lines.append(line)
    emit(
        {
            "status": overall,
            "message": "\n".join(lines),
            "target": str(workspace),
            "version": VERSION,
            "checks": checks,
            "ones": ones,
            "context_sources": {"kba": kba_summary},
            "workflow": {"auto_takeover": workflow_matrix} if workflow_matrix is not None else None,
            "specrules": specrules_summary_payload,
            "docs_curator": docs_curator_summary_payload,
            "html_ui": html_ui_summary_payload,
            "spec_artifacts": spec_artifacts_summary_payload,
            "review_rounds": review_rounds_summary_payload,
            "design_template": design_template_summary_payload,
        },
        args.format,
    )
    return 0
