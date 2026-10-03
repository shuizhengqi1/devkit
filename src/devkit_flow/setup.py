from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any
from devkit_flow.codex_agents import codex_agent_registration_check, codex_user_home
from devkit_flow.config import context_source_summary, format_review_gate_timing_status_text, kba_context_source_doctor, load_review_gate_timing_summary, ones_config_summary
from devkit_flow.constants import AGENT_DATA_PAYLOAD_FILES, DEVKIT_CLI_RELATIVE_PATH, LEGACY_RUNTIME_PLUGIN_NAMES, RUNTIME_PLUGIN_NAME, SPECRULES_CLEANUP_BACKUP_NAME, SPECRULES_CLEANUP_MANUAL, SPECRULES_DISABLED_NO_TREE, SPECRULES_DISABLED_REMOVED, SPECRULES_REFRESH_GIT_TIMEOUT_SECONDS, SPECRULES_REPO_URL, SPECRULES_SETUP_ILLEGAL_REASON, SPECRULES_URL_SWITCH_BACKUP_NAME, TELEMETRY_SSO_IDENTITY_CACHE_RELATIVE_PATH, _SPECRULES_GITMODULES_PATH
from devkit_flow.e2e import e2e_doctor
from devkit_flow.errors import DevkitFlowError
from devkit_flow.html_ui import html_ui_doctor
from devkit_flow.models import SpecrulesResolution, UserStopHookEntry, UserStopHookScan
from devkit_flow.review import design_template_doctor, design_template_summary, docs_curator_doctor, review_rounds_doctor, review_rounds_summary
from devkit_flow.selfupdate import extract_embedded_version, find_global_cli
from devkit_flow.switches import docs_curator_summary, html_ui_summary, resolve_specrules, spec_artifacts_doctor, spec_artifacts_summary, specrules_cache_dir_for_url, specrules_doctor, specrules_summary, validate_specrules_git_url
from devkit_flow.templates_md import PROJECT_RULES_TEMPLATE, WORKSPACE_ROUTING_TEMPLATE
from devkit_flow.telemetry import offer_sso_login, telemetry_doctor_check
from devkit_flow.textio import atomic_write_text, compact_error, current_cli_path, emit, run_external, sha256_file
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


def _single_line_git_diagnostic(raw: str, *, limit: int) -> str:
    """Return a bounded Git diagnostic without common credential values.

    Redaction deliberately happens before whitespace compaction and truncation so
    a credential cannot survive merely because its label fell outside the final
    summary window. This helper only changes presentation; callers retain their
    existing status, exit and cache-fallback semantics.
    """
    diagnostic = raw
    diagnostic = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+(?::[^/@\s]*)?@",
        r"\1[REDACTED]@",
        diagnostic,
    )
    diagnostic = re.sub(
        r"(?i)([?&](?:access_?token|token|password|secret|cookie)=)[^&\s]+",
        r"\1[REDACTED]",
        diagnostic,
    )
    diagnostic = re.sub(
        r"(?im)(\b(?:proxy-)?authorization\s*:\s*)[^\r\n]*",
        r"\1[REDACTED]",
        diagnostic,
    )
    diagnostic = re.sub(
        r"(?i)(\b(?:bearer|basic)\s+)[^\s,;]+",
        r"\1[REDACTED]",
        diagnostic,
    )
    diagnostic = re.sub(
        r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]*",
        r"\1[REDACTED]",
        diagnostic,
    )
    return " ".join(diagnostic.split())[:limit]


def _git_result_diagnostic(result: Any, *, limit: int) -> str:
    raw = result.stderr or result.stdout or f"exit {result.returncode}"
    return _single_line_git_diagnostic(raw, limit=limit)


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
    repo_url = resolution.repo_url or SPECRULES_REPO_URL
    specrules_dir = root / "specrules"
    try:
        if resolution.workspace_tree == "submodule":
            return _refresh_specrules_submodule(root, specrules_dir, repo_url)
        return _refresh_specrules_copy(root, specrules_dir, repo_url)
    except Exception as error:
        index_ok = (specrules_dir / "rules" / "index.md").is_file()
        return {
            "status": "current" if index_ok else "error",
            "action": "refresh-failed",
            "message": compact_error(error),
            "repo_url": repo_url,
        }


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
    # npm 全局安装时，__file__ 解析到 <npm-global>/node_modules/devkit-flow/devkit-flow，
    # 数据目录 agents/ 与该文件同包同级。
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
) -> dict[str, Any]:
    """submodule 模式挂载 specrules/。

    - 无 git 或目标不是 git 仓 → 回退 copy 模式并在 reason 注明原因；
    - 已是 submodule（.gitmodules 含 path = specrules）→ url 相同则 update --init，
      不同且脏则 error，不同且干净则 set-url + sync + update（失败回滚旧 url）；
    - 存量普通目录 → 与生效 URL 缓存零差异才转换（改名备份 → add → 清理，失败回滚目录），
      有任何差异或用户额外文件则拒绝迁移并给出指引；
    - 不存在 → ensure_specrules_cache 后 submodule add 生效 URL（需网络，--skip-specrules-pull 时报错）。
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

    cache_ok, note = ensure_specrules_cache(args.skip_specrules_pull, repo_url)
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
    specrules_dir = workspace / "specrules"
    specrules_resolution = resolve_specrules(workspace)
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
        repo_url = specrules_resolution.repo_url or SPECRULES_REPO_URL
        if args.specrules_mode == "submodule":
            actions.append(setup_specrules_submodule(args, workspace, specrules_dir, repo_url=repo_url))
        else:
            actions.append(setup_specrules_copy(args, workspace, specrules_dir, repo_url=repo_url))

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

    failed = [item for item in actions if item["status"] == "error"]
    counts: dict[str, int] = {}
    for item in actions:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    summary = ", ".join(f"{status}: {counts[status]}" for status in sorted(counts))
    lines = [f"setup {'completed with errors' if failed else 'completed'} ({summary})"]
    lines.extend(f"- {item['item']}: {item['status']} — {item['reason']}" for item in actions)
    emit(
        {
            "status": "issues" if failed else "ok",
            "message": "\n".join(lines),
            "target": str(workspace),
            "actions": actions,
        },
        args.format,
    )
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

    # 全局 CLI 位置与版本：PATH（npm 全局 bin）优先，~/.devkit/bin 兜底，两处都无才 error
    global_cli = find_global_cli()
    if global_cli is not None:
        global_version = extract_embedded_version(global_cli)
        matches = sha256_file(global_cli) == sha256_file(current_cli_path())
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

    rank = {"ok": 0, "warn": 1, "error": 2}
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
