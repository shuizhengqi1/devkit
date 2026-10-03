from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from devkit_flow.config import deep_merge_config, load_devkit_config, save_devkit_config
from devkit_flow.constants import DEFAULT_SPEC_ARCHIVE_CONFIG, DONE_STATES, INACTIVE_FEATURE_STATES, LEGACY_COMPLETION_HINTS, SPEC_ARCHIVE_APPROVAL_GATES, SPEC_ARCHIVE_RECEIPT_SCHEMA_VERSION, SPEC_ARCHIVE_RECEIPT_STATES
from devkit_flow.errors import DevkitFlowError
from devkit_flow.flow import flow_now_iso
from devkit_flow.parse_util import feature_name_from_dir, find_features, parse_task_table
from devkit_flow.textio import atomic_write_bytes, atomic_write_text, compact_error, emit, sha256_file


def spec_archive_config_summary(workspace: Path) -> dict[str, Any]:
    resolution = load_devkit_config(workspace)
    global_value = resolution.global_config.get("specArchive")
    project_value = resolution.project_config.get("specArchive")
    global_config = global_value if isinstance(global_value, dict) else {}
    project_config = project_value if isinstance(project_value, dict) else {}
    effective = deep_merge_config(DEFAULT_SPEC_ARCHIVE_CONFIG, global_config)
    effective = deep_merge_config(effective, project_config)
    source = "project" if project_config else "global" if global_config else "default"
    inactive_days = effective.get("inactiveDays", 30)
    if not isinstance(inactive_days, int) or isinstance(inactive_days, bool) or inactive_days < 0:
        raise DevkitFlowError("Invalid specArchive.inactiveDays: expected a non-negative integer")
    for key, allowed in {
        "contentMode": {"compact-copy", "in-place"},
        "knowledgeBaseApproval": {"ask", "auto"},
        "cleanupPolicy": {"keep", "ask"},
        "retention": {"keep", "compact", "git-only"},
    }.items():
        if effective.get(key) not in allowed:
            raise DevkitFlowError(
                f"Invalid specArchive.{key}: expected one of {', '.join(sorted(allowed))}"
            )
    return {**effective, "source": source}


def remove_spec_archive_config(config: dict[str, Any]) -> None:
    config.pop("specArchive", None)


def command_spec_archive_configure(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )
    values = {
        "contentMode": args.content_mode,
        "knowledgeBaseApproval": args.knowledge_base_approval,
        "cleanupPolicy": args.cleanup_policy,
        "retention": args.retention,
        "inactiveDays": args.inactive_days,
    }
    supplied = {key: value for key, value in values.items() if value is not None}
    if args.inherit and args.clear:
        raise DevkitFlowError("--inherit and --clear cannot be combined")
    if args.inactive_days is not None and args.inactive_days < 0:
        raise DevkitFlowError("--inactive-days must be a non-negative integer")
    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError("--inherit is only valid with --scope project")
        if supplied:
            raise DevkitFlowError("--inherit cannot be combined with archive values")
        remove_spec_archive_config(target_config)
        message = "Project spec archive override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError("--clear is only valid with --scope global")
        if supplied:
            raise DevkitFlowError("--clear cannot be combined with archive values")
        remove_spec_archive_config(target_config)
        message = "Global spec archive configuration cleared."
    else:
        if not supplied:
            raise DevkitFlowError(
                "configure requires an archive value, --inherit, or --clear"
            )
        current = target_config.setdefault("specArchive", {})
        if not isinstance(current, dict):
            raise DevkitFlowError("Invalid specArchive configuration: expected an object")
        current.update(supplied)
        message = f"Spec archive configuration updated for {args.scope} scope."
    save_devkit_config(workspace, target_config, args.scope)
    emit(
        {
            "status": "configured",
            "message": message,
            "specArchive": spec_archive_config_summary(workspace),
        },
        args.format,
    )
    return 0


def archive_task_evidence(feature_dir: Path) -> dict[str, Any]:
    task_path = feature_dir / "task.md"
    if not task_path.exists():
        return {"state": "missing", "openTasks": [], "closedTasks": []}
    try:
        table = parse_task_table(task_path)
    except DevkitFlowError as error:
        return {
            "state": "invalid",
            "openTasks": [],
            "closedTasks": [],
            "error": compact_error(error),
        }
    open_tasks: list[str] = []
    closed_tasks: list[str] = []
    for row in table["rows"]:
        task_id = str(row.get("任务ID", "")).strip()
        state = str(row.get("状态", "")).strip()
        if state in DONE_STATES | {"completed"}:
            closed_tasks.append(task_id)
        else:
            open_tasks.append(task_id or "unknown")
    state = "closed" if table["rows"] and not open_tasks else "open"
    return {"state": state, "openTasks": open_tasks, "closedTasks": closed_tasks}


def archive_git_risk(workspace: Path, feature_dir: Path) -> str:
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return "not-a-git-repository"
    relative = feature_dir.relative_to(workspace).as_posix()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", relative],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return "git-status-failed"
    return "dirty-target" if status.stdout.strip() else "none"


def feature_latest_mtime(feature_dir: Path) -> datetime:
    latest = max(
        (path.stat().st_mtime for path in feature_dir.rglob("*") if path.is_file()),
        default=feature_dir.stat().st_mtime,
    )
    return datetime.fromtimestamp(latest, tz=timezone.utc).astimezone()


def classify_archive_candidate(
    workspace: Path, feature: dict[str, Any]
) -> dict[str, Any]:
    feature_dir = workspace / feature["feature_dir"]
    state = str(feature.get("summary", {}).get("state", "")).strip()
    tasks = archive_task_evidence(feature_dir)
    git_risk = archive_git_risk(workspace, feature_dir)
    reasons: list[str] = []

    if git_risk in {"dirty-target", "git-status-failed"}:
        category = "blocked"
        reasons.append(git_risk)
    elif tasks["state"] == "open":
        category = "blocked"
        reasons.append("open-tasks")
    elif state in INACTIVE_FEATURE_STATES and tasks["state"] == "closed":
        category = "eligible"
        reasons.append("canonical-inactive-state-and-closed-tasks")
    elif state in INACTIVE_FEATURE_STATES:
        category = "review-required"
        reasons.append("inactive-state-without-closed-task-evidence")
    elif any(hint.lower() in state.lower() for hint in LEGACY_COMPLETION_HINTS):
        category = "review-required"
        reasons.append("legacy-completion-state")
    else:
        category = "blocked"
        reasons.append("active-or-unknown-state")

    return {
        "feature": feature["feature"],
        "featureDir": feature["feature_dir"],
        "sourceKind": "current-feature",
        "state": state,
        "category": category,
        "reasons": reasons,
        "tasks": tasks,
        "gitRisk": git_risk,
        "recoveryStatus": "current-files",
        "cleanupTargets": [feature["feature_dir"]],
        "lastModified": feature_latest_mtime(feature_dir).isoformat(timespec="seconds"),
    }


def classify_existing_archive_candidate(workspace: Path, feature_dir: Path) -> dict[str, Any]:
    relative = feature_dir.relative_to(workspace).as_posix()
    git_risk = archive_git_risk(workspace, feature_dir)
    category = "blocked" if git_risk in {"dirty-target", "git-status-failed"} else "review-required"
    reasons = [git_risk] if category == "blocked" else ["existing-archive-requires-adoption"]
    return {
        "feature": feature_name_from_dir(feature_dir),
        "featureDir": relative,
        "sourceKind": "existing-archive",
        "state": "archived",
        "category": category,
        "reasons": reasons,
        "tasks": archive_task_evidence(feature_dir),
        "gitRisk": git_risk,
        "recoveryStatus": "current-files",
        "cleanupTargets": [relative],
        "lastModified": feature_latest_mtime(feature_dir).isoformat(timespec="seconds"),
    }


def git_history_source_files(
    workspace: Path, commit: str, feature_dir: str
) -> list[dict[str, Any]]:
    if not commit:
        return []
    tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit, "--", feature_dir],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if tree.returncode != 0:
        return []
    files: list[dict[str, Any]] = []
    for relative in sorted(line.strip() for line in tree.stdout.splitlines() if line.strip()):
        shown = subprocess.run(
            ["git", "show", f"{commit}:{relative}"],
            cwd=workspace,
            capture_output=True,
        )
        if shown.returncode != 0:
            return []
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(shown.stdout).hexdigest(),
                "bytes": len(shown.stdout),
                "tracked": True,
            }
        )
    return files


def history_archive_candidates(workspace: Path) -> list[dict[str, Any]]:
    index_path = workspace / "docs" / "specIndex.md"
    if not index_path.is_file():
        return []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    path_pattern = re.compile(r"(?P<path>spec/archived/[A-Za-z0-9._/-]+)")
    commit_pattern = re.compile(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{7,40})(?![0-9A-Fa-f])")
    for line_number, line in enumerate(index_path.read_text(encoding="utf-8").splitlines(), start=1):
        path_match = path_pattern.search(line)
        if path_match is None:
            continue
        feature_dir = path_match.group("path").rstrip("/`.,;:)")
        if feature_dir in seen or (workspace / feature_dir).exists():
            continue
        seen.add(feature_dir)
        commits = commit_pattern.findall(line)
        source_commit = ""
        source_files: list[dict[str, Any]] = []
        for commit in commits:
            recovered = git_history_source_files(workspace, commit, feature_dir)
            if recovered:
                source_commit = commit
                source_files = recovered
                break
        recoverable = bool(source_commit and source_files)
        candidates.append(
            {
                "feature": feature_name_from_dir(Path(feature_dir)),
                "featureDir": feature_dir,
                "sourceKind": "git-history",
                "state": "git-history-only",
                "category": "review-required" if recoverable else "blocked",
                "reasons": [
                    "git-history-record-requires-adoption"
                    if recoverable
                    else "recovery-incomplete"
                ],
                "tasks": {"state": "history-only", "openTasks": [], "closedTasks": []},
                "gitRisk": "git-history",
                "recoveryStatus": "recoverable" if recoverable else "recovery-incomplete",
                "sourceCommit": source_commit or (commits[0] if commits else ""),
                "sourceFiles": source_files,
                "indexEvidence": {
                    "path": index_path.relative_to(workspace).as_posix(),
                    "line": line_number,
                },
                "cleanupTargets": [],
                "lastModified": datetime.fromtimestamp(
                    index_path.stat().st_mtime, tz=timezone.utc
                ).astimezone().isoformat(timespec="seconds"),
            }
        )
    return candidates


def find_archive_candidates(workspace: Path) -> list[dict[str, Any]]:
    candidates = [
        classify_archive_candidate(workspace, feature) for feature in find_features(workspace)
    ]
    archived_root = workspace / "spec" / "archived"
    if archived_root.is_dir():
        candidates.extend(
            classify_existing_archive_candidate(workspace, child)
            for child in sorted(archived_root.iterdir())
            if child.is_dir()
        )
    candidates.extend(history_archive_candidates(workspace))
    return candidates


def command_spec_archive_scan(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    config = spec_archive_config_summary(workspace)
    inactive_days = config["inactiveDays"] if args.inactive_days is None else args.inactive_days
    if inactive_days < 0:
        raise DevkitFlowError("--inactive-days must be a non-negative integer")
    candidates = find_archive_candidates(workspace)
    if args.feature:
        candidates = [
            item
            for item in candidates
            if args.feature in {item["feature"], Path(item["featureDir"]).name}
        ]
        if not candidates:
            raise DevkitFlowError(f"Feature not found: {args.feature}")
    elif not args.all:
        cutoff = datetime.now().astimezone() - timedelta(days=inactive_days)
        candidates = [
            item
            for item in candidates
            if datetime.fromisoformat(item["lastModified"]) <= cutoff
        ]
    summary = {
        category: sum(1 for item in candidates if item["category"] == category)
        for category in ("eligible", "review-required", "blocked")
    }
    emit(
        {
            "status": "ok",
            "message": f"Scanned {len(candidates)} spec archive candidates.",
            "candidates": candidates,
            "summary": summary,
            "specArchive": config,
        },
        args.format,
    )
    return 0


def spec_archive_runtime_root(workspace: Path) -> Path:
    return workspace / ".sdd-runtime" / "spec-archives"


def validate_archive_id(archive_id: str) -> str:
    value = archive_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise DevkitFlowError(f"Invalid archive id: {archive_id!r}")
    return value


def archive_receipt_dir(workspace: Path, archive_id: str) -> Path:
    return spec_archive_runtime_root(workspace) / validate_archive_id(archive_id)


def archive_receipt_path(workspace: Path, archive_id: str) -> Path:
    return archive_receipt_dir(workspace, archive_id) / "receipt.json"


def save_archive_receipt(workspace: Path, receipt: dict[str, Any]) -> None:
    archive_id = validate_archive_id(str(receipt.get("archiveId", "")))
    path = archive_receipt_path(workspace, archive_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt["updatedAt"] = flow_now_iso()
    atomic_write_text(path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")


def load_archive_receipt(workspace: Path, archive_id: str) -> dict[str, Any]:
    path = archive_receipt_path(workspace, archive_id)
    if not path.exists():
        raise DevkitFlowError(f"Spec archive receipt not found: {archive_id}")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DevkitFlowError(f"Invalid spec archive receipt {path}: {error}") from error
    if not isinstance(receipt, dict):
        raise DevkitFlowError(f"Invalid spec archive receipt {path}: root must be an object")
    if receipt.get("schemaVersion") != SPEC_ARCHIVE_RECEIPT_SCHEMA_VERSION:
        raise DevkitFlowError(f"Unsupported spec archive receipt schema: {path}")
    if receipt.get("state") not in SPEC_ARCHIVE_RECEIPT_STATES:
        raise DevkitFlowError(f"Invalid spec archive receipt state: {receipt.get('state')!r}")
    return receipt


def git_head_commit(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def git_tracks_path(workspace: Path, relative: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def hash_feature_tree(workspace: Path, feature_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(feature_dir.rglob("*")):
        if path.is_symlink():
            raise DevkitFlowError(f"Symlinks are not supported in archive sources: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(workspace).as_posix()
        files.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
                "tracked": git_tracks_path(workspace, relative),
            }
        )
    if not files:
        raise DevkitFlowError(f"Feature directory contains no files: {feature_dir}")
    return files


def active_receipt_for_feature(workspace: Path, feature_dir: str) -> dict[str, Any] | None:
    root = spec_archive_runtime_root(workspace)
    if not root.is_dir():
        return None
    for path in sorted(root.glob("*/receipt.json")):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(receipt, dict)
            and receipt.get("featureDir") == feature_dir
            and receipt.get("state") != "completed"
        ):
            return receipt
    return None


def select_archive_candidate(workspace: Path, requested: str) -> dict[str, Any]:
    matches = [
        candidate
        for candidate in find_archive_candidates(workspace)
        if requested in {candidate["feature"], Path(candidate["featureDir"]).name}
    ]
    if not matches:
        raise DevkitFlowError(f"Feature not found: {requested}")
    if len(matches) > 1:
        raise DevkitFlowError(f"Multiple features match: {requested}")
    return matches[0]


def command_spec_archive_plan(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    config = spec_archive_config_summary(workspace)
    candidate = select_archive_candidate(workspace, args.feature)
    if candidate["category"] == "blocked":
        raise DevkitFlowError(
            f"Feature is blocked from archive planning: {', '.join(candidate['reasons'])}"
        )
    if candidate["category"] == "review-required" and not args.include_review_required:
        raise DevkitFlowError(
            "Feature is review-required; inspect it and pass --include-review-required explicitly"
        )
    existing = active_receipt_for_feature(workspace, candidate["featureDir"])
    if existing is not None:
        raise DevkitFlowError(
            f"An active archive transaction already exists: {existing.get('archiveId')} ({existing.get('state')})"
        )
    now = datetime.now().astimezone()
    archive_id = (
        "SA-"
        + now.strftime("%Y%m%d%H%M%S%f")
        + "-"
        + re.sub(r"[^A-Za-z0-9._-]+", "-", candidate["feature"]).strip("-")
    )
    receipt_dir = archive_receipt_dir(workspace, archive_id)
    staging_dir = receipt_dir / "staging"
    staging_dir.mkdir(parents=True, exist_ok=False)
    content_mode = args.content_mode or config["contentMode"]
    retention = args.retention or config["retention"]
    source_kind = candidate.get("sourceKind", "current-feature")
    if source_kind == "git-history" and retention != "git-only":
        raise DevkitFlowError(
            "Git-history-only migration requires --retention git-only because no working-tree source exists"
        )
    created_at = flow_now_iso()
    source_commit = (
        str(candidate.get("sourceCommit", ""))
        if source_kind == "git-history"
        else git_head_commit(workspace)
    )
    source_files = (
        candidate.get("sourceFiles", [])
        if source_kind == "git-history"
        else hash_feature_tree(workspace, workspace / candidate["featureDir"])
    )
    receipt: dict[str, Any] = {
        "schemaVersion": SPEC_ARCHIVE_RECEIPT_SCHEMA_VERSION,
        "archiveId": archive_id,
        "state": "planned",
        "feature": candidate["feature"],
        "featureDir": candidate["featureDir"],
        "sourceKind": source_kind,
        "candidateCategory": candidate["category"],
        "candidateReasons": candidate["reasons"],
        "sourceCommit": source_commit,
        "sourceFiles": source_files,
        "contentMode": content_mode,
        "retention": retention,
        "knowledgeBaseApproval": config["knowledgeBaseApproval"],
        "cleanupPolicy": config["cleanupPolicy"],
        "stagingDir": str(staging_dir.resolve()),
        "stagedArchiveFiles": [],
        "stagedKnowledgeFiles": [],
        "knowledgeNoopReason": "",
        "approvals": {},
        "createdAt": created_at,
        "updatedAt": created_at,
    }
    save_archive_receipt(workspace, receipt)
    emit(
        {
            "status": "ok",
            "message": f"Created spec archive plan {archive_id}.",
            "receipt": receipt,
        },
        args.format,
    )
    return 0


def staged_files(root: Path) -> list[str]:
    if not root.exists():
        return []
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise DevkitFlowError(f"Symlinks are not allowed in archive staging: {path}")
        if path.is_file():
            files.append(path.relative_to(root).as_posix())
    return files


def command_spec_archive_stage(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    receipt = load_archive_receipt(workspace, args.archive_id)
    if receipt["state"] != "planned":
        raise DevkitFlowError(f"Only a planned receipt can be staged; current state is {receipt['state']}")
    staging_dir = Path(receipt["stagingDir"])
    expected_root = archive_receipt_dir(workspace, receipt["archiveId"]) / "staging"
    if staging_dir.resolve() != expected_root.resolve():
        raise DevkitFlowError("Receipt stagingDir is outside its archive transaction")
    archive_root = staging_dir / "archive"
    summary = archive_root / "ARCHIVE-SUMMARY.md"
    if not summary.is_file() or summary.is_symlink():
        raise DevkitFlowError("Staged archive must contain archive/ARCHIVE-SUMMARY.md")
    knowledge_root = staging_dir / "knowledge"
    knowledge_files = staged_files(knowledge_root)
    noop_reason = args.knowledge_noop_reason.strip()
    if not knowledge_files and not noop_reason:
        raise DevkitFlowError(
            "Staged archive requires knowledge/docs files or --knowledge-noop-reason"
        )
    if any(not path.startswith("docs/") for path in knowledge_files):
        raise DevkitFlowError("Every staged knowledge file must be under knowledge/docs/")
    manifest = {
        "schemaVersion": 1,
        "archiveId": receipt["archiveId"],
        "feature": receipt["feature"],
        "featureDir": receipt["featureDir"],
        "sourceCommit": receipt["sourceCommit"],
        "sourceFiles": receipt["sourceFiles"],
        "contentMode": receipt["contentMode"],
        "retention": receipt["retention"],
        "knowledgeFiles": knowledge_files,
        "knowledgeNoopReason": noop_reason,
    }
    atomic_write_text(
        archive_root / "MANIFEST.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    receipt["stagedArchiveFiles"] = staged_files(archive_root)
    receipt["stagedKnowledgeFiles"] = knowledge_files
    receipt["knowledgeNoopReason"] = noop_reason
    if receipt["knowledgeBaseApproval"] == "auto":
        receipt["approvals"]["knowledge-base"] = {
            "source": "config:auto",
            "approvedAt": flow_now_iso(),
        }
        receipt["state"] = "knowledge-approved"
    else:
        receipt["state"] = "knowledge-awaiting-approval"
    save_archive_receipt(workspace, receipt)
    emit(
        {
            "status": "ok",
            "message": f"Staged spec archive {receipt['archiveId']}.",
            "receipt": receipt,
        },
        args.format,
    )
    return 0


def command_spec_archive_approve(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    receipt = load_archive_receipt(workspace, args.archive_id)
    gate = args.gate
    if gate not in SPEC_ARCHIVE_APPROVAL_GATES:
        raise DevkitFlowError(f"Unknown archive approval gate: {gate}")
    if gate == "knowledge-base":
        if receipt["state"] not in {"knowledge-awaiting-approval", "knowledge-approved"}:
            raise DevkitFlowError(
                f"knowledge-base approval requires knowledge-awaiting-approval; current state is {receipt['state']}"
            )
        receipt["state"] = "knowledge-approved"
    elif receipt["state"] not in {"archived", "cleanup-awaiting-approval"}:
        raise DevkitFlowError(
            f"{gate} approval is only valid after archive apply; current state is {receipt['state']}"
        )
    receipt["approvals"][gate] = {
        "source": "user",
        "approvedAt": flow_now_iso(),
    }
    save_archive_receipt(workspace, receipt)
    emit(
        {
            "status": "ok",
            "message": f"Approved archive gate {gate} for {receipt['archiveId']}.",
            "receipt": receipt,
        },
        args.format,
    )
    return 0


def source_tree_matches_receipt(workspace: Path, receipt: dict[str, Any]) -> bool:
    if receipt.get("sourceKind") == "git-history":
        current = git_history_source_files(
            workspace,
            str(receipt.get("sourceCommit", "")),
            str(receipt.get("featureDir", "")),
        )
        expected = receipt.get("sourceFiles", [])
        comparable = lambda entries: [
            (item.get("path"), item.get("sha256"), item.get("bytes")) for item in entries
        ]
        return comparable(current) == comparable(expected)
    feature_dir = workspace / receipt["featureDir"]
    if not feature_dir.is_dir():
        return False
    current = hash_feature_tree(workspace, feature_dir)
    expected = receipt.get("sourceFiles", [])
    comparable = lambda entries: [
        (item.get("path"), item.get("sha256"), item.get("bytes")) for item in entries
    ]
    return comparable(current) == comparable(expected)


def command_spec_archive_verify(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    receipt = load_archive_receipt(workspace, args.archive_id)
    if receipt["state"] not in {"knowledge-approved", "verified"}:
        raise DevkitFlowError(
            f"Archive verification requires knowledge-approved; current state is {receipt['state']}"
        )
    if "knowledge-base" not in receipt.get("approvals", {}):
        raise DevkitFlowError("Knowledge-base approval evidence is missing")
    if not source_tree_matches_receipt(workspace, receipt):
        raise DevkitFlowError("source changed after plan; create a new archive plan")
    staging_dir = Path(receipt["stagingDir"])
    archive_root = staging_dir / "archive"
    if not (archive_root / "ARCHIVE-SUMMARY.md").is_file():
        raise DevkitFlowError("Staged ARCHIVE-SUMMARY.md is missing")
    if not (archive_root / "MANIFEST.json").is_file():
        raise DevkitFlowError("Staged MANIFEST.json is missing")
    if staged_files(archive_root) != receipt.get("stagedArchiveFiles"):
        raise DevkitFlowError("Staged archive files changed after stage")
    if staged_files(staging_dir / "knowledge") != receipt.get("stagedKnowledgeFiles"):
        raise DevkitFlowError("Staged knowledge files changed after stage")
    receipt["state"] = "verified"
    receipt["verifiedAt"] = flow_now_iso()
    save_archive_receipt(workspace, receipt)
    emit(
        {
            "status": "ok",
            "message": f"Verified spec archive {receipt['archiveId']}.",
            "receipt": receipt,
        },
        args.format,
    )
    return 0


def command_spec_archive_status(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    if args.archive_id:
        receipt = load_archive_receipt(workspace, args.archive_id)
        emit(
            {
                "status": "ok",
                "message": f"Spec archive {receipt['archiveId']}: {receipt['state']}",
                "receipt": receipt,
            },
            args.format,
        )
        return 0
    receipts: list[dict[str, Any]] = []
    root = spec_archive_runtime_root(workspace)
    if root.is_dir():
        for path in sorted(root.glob("*/receipt.json")):
            receipts.append(load_archive_receipt(workspace, path.parent.name))
    emit(
        {
            "status": "ok",
            "message": f"Found {len(receipts)} spec archive receipts.",
            "receipts": receipts,
        },
        args.format,
    )
    return 0


def archive_materialized_dir(workspace: Path, receipt: dict[str, Any]) -> Path:
    value = receipt.get("materializedArchiveDir")
    if isinstance(value, str) and value:
        return workspace / value
    return workspace / "spec" / "archived" / Path(receipt["featureDir"]).name


def copy_staged_knowledge(workspace: Path, receipt: dict[str, Any]) -> None:
    staging_dir = Path(receipt["stagingDir"])
    knowledge_root = staging_dir / "knowledge"
    backup_root = archive_receipt_dir(workspace, receipt["archiveId"]) / "backup" / "knowledge"
    backup_state: list[dict[str, Any]] = []
    applied: list[Path] = []
    try:
        for relative in receipt.get("stagedKnowledgeFiles", []):
            if not isinstance(relative, str) or not relative.startswith("docs/"):
                raise DevkitFlowError(f"Unsafe staged knowledge path: {relative!r}")
            source = knowledge_root / relative
            target = workspace / relative
            if not source.is_file() or source.is_symlink():
                raise DevkitFlowError(f"Staged knowledge file is missing or unsafe: {source}")
            existed = target.is_file()
            if target.exists() and not existed:
                raise DevkitFlowError(f"Knowledge target is not a regular file: {target}")
            if existed:
                backup = backup_root / relative
                atomic_write_bytes(backup, target.read_bytes())
            backup_state.append({"path": relative, "existed": existed})
            atomic_write_bytes(target, source.read_bytes())
            applied.append(target)
    except Exception:
        for entry in reversed(backup_state):
            target = workspace / entry["path"]
            if entry["existed"]:
                backup = backup_root / entry["path"]
                atomic_write_bytes(target, backup.read_bytes())
            elif target.exists():
                target.unlink()
        raise
    receipt["knowledgeBackups"] = backup_state
    receipt["knowledgeAppliedFiles"] = [
        path.relative_to(workspace).as_posix() for path in applied
    ]


def rollback_applied_knowledge(workspace: Path, receipt: dict[str, Any]) -> None:
    backup_root = archive_receipt_dir(workspace, receipt["archiveId"]) / "backup" / "knowledge"
    for entry in reversed(receipt.get("knowledgeBackups", [])):
        target = workspace / entry["path"]
        if entry.get("existed"):
            backup = backup_root / entry["path"]
            atomic_write_bytes(target, backup.read_bytes())
        elif target.is_file():
            target.unlink()
    receipt.pop("knowledgeBackups", None)
    receipt.pop("knowledgeAppliedFiles", None)


def overlay_existing_archive(workspace: Path, receipt: dict[str, Any]) -> Path:
    staging_archive = Path(receipt["stagingDir"]) / "archive"
    target = workspace / receipt["featureDir"]
    if not target.is_dir():
        raise DevkitFlowError(f"Existing archive source is missing: {target}")
    backup_root = archive_receipt_dir(workspace, receipt["archiveId"]) / "backup" / "archive-overlay"
    backup_state: list[dict[str, Any]] = []
    try:
        for relative in staged_files(staging_archive):
            source = staging_archive / relative
            destination = target / relative
            existed = destination.is_file()
            if destination.exists() and not existed:
                raise DevkitFlowError(f"Archive overlay target is not a regular file: {destination}")
            if existed:
                atomic_write_bytes(backup_root / relative, destination.read_bytes())
            backup_state.append({"path": relative, "existed": existed})
            atomic_write_bytes(destination, source.read_bytes())
    except Exception:
        for entry in reversed(backup_state):
            destination = target / entry["path"]
            if entry["existed"]:
                atomic_write_bytes(destination, (backup_root / entry["path"]).read_bytes())
            elif destination.is_file():
                destination.unlink()
        raise
    receipt["archiveOverlayBackups"] = backup_state
    receipt["materializedArchiveDir"] = receipt["featureDir"]
    return target


def materialize_staged_archive(workspace: Path, receipt: dict[str, Any]) -> Path:
    if receipt.get("sourceKind") == "existing-archive":
        return overlay_existing_archive(workspace, receipt)
    staging_archive = Path(receipt["stagingDir"]) / "archive"
    target = workspace / "spec" / "archived" / Path(receipt["featureDir"]).name
    if target.exists():
        raise DevkitFlowError(f"Archive target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.parent / f".{target.name}.{receipt['archiveId']}.tmp"
    if temp_target.exists():
        shutil.rmtree(temp_target)
    try:
        shutil.copytree(staging_archive, temp_target)
        temp_target.replace(target)
    except Exception:
        if temp_target.exists():
            shutil.rmtree(temp_target)
        raise
    receipt["materializedArchiveDir"] = target.relative_to(workspace).as_posix()
    return target


def command_spec_archive_apply(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    receipt = load_archive_receipt(workspace, args.archive_id)
    if receipt["state"] in {
        "archived",
        "cleanup-awaiting-approval",
        "cleaned",
        "completed",
    }:
        emit(
            {
                "status": "ok",
                "message": f"Spec archive {receipt['archiveId']} was already applied.",
                "receipt": receipt,
            },
            args.format,
        )
        return 0
    if receipt["state"] != "verified":
        raise DevkitFlowError(
            f"Archive apply requires verified; current state is {receipt['state']}"
        )
    if not source_tree_matches_receipt(workspace, receipt):
        raise DevkitFlowError("source changed after plan; create a new archive plan")
    if receipt["retention"] == "git-only" and not any(
        path.startswith("docs/history/")
        for path in receipt.get("stagedKnowledgeFiles", [])
    ):
        raise DevkitFlowError("git-only retention requires a staged docs/history restore record")
    copy_staged_knowledge(workspace, receipt)
    try:
        if not (
            receipt.get("sourceKind") == "git-history"
            and receipt["retention"] == "git-only"
        ):
            materialize_staged_archive(workspace, receipt)
    except Exception:
        rollback_applied_knowledge(workspace, receipt)
        raise
    source_kind = receipt.get("sourceKind", "current-feature")
    if source_kind == "existing-archive":
        needs_cleanup = receipt["retention"] == "git-only"
    elif source_kind == "git-history":
        needs_cleanup = False
    else:
        needs_cleanup = receipt["retention"] != "keep" or receipt["contentMode"] == "in-place"
    receipt["state"] = "cleanup-awaiting-approval" if needs_cleanup else "archived"
    receipt["archiveAppliedAt"] = flow_now_iso()
    save_archive_receipt(workspace, receipt)
    emit(
        {
            "status": "ok",
            "message": f"Applied knowledge and archive content for {receipt['archiveId']}.",
            "receipt": receipt,
        },
        args.format,
    )
    return 0


def git_commit_contains_path(workspace: Path, commit: str, relative: str) -> bool:
    if not commit:
        return False
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{relative}"],
        cwd=workspace,
        capture_output=True,
    )
    return result.returncode == 0


def verify_prune_recovery(workspace: Path, receipt: dict[str, Any]) -> None:
    if receipt.get("sourceKind") == "git-history":
        raise DevkitFlowError("Git-history-only migration has no working-tree cleanup target")
    if receipt.get("sourceKind") == "existing-archive":
        source = workspace / receipt["featureDir"]
        current = {item["path"]: item for item in hash_feature_tree(workspace, source)}
        expected = {item["path"]: item for item in receipt.get("sourceFiles", [])}
        staging_archive = Path(receipt["stagingDir"]) / "archive"
        overlay = {
            f"{receipt['featureDir']}/{relative}": staging_archive / relative
            for relative in receipt.get("stagedArchiveFiles", [])
        }
        if set(current) != set(expected) | set(overlay):
            raise DevkitFlowError("existing archive changed outside the staged overlay; cleanup refused")
        for relative, entry in current.items():
            if relative in overlay:
                if entry["sha256"] != sha256_file(overlay[relative]):
                    raise DevkitFlowError(f"staged archive overlay changed before cleanup: {relative}")
            elif entry["sha256"] != expected[relative]["sha256"]:
                raise DevkitFlowError(f"existing archive source changed before cleanup: {relative}")
    else:
        if not source_tree_matches_receipt(workspace, receipt):
            raise DevkitFlowError("source changed after plan; cleanup refused")
        git_risk = archive_git_risk(workspace, workspace / receipt["featureDir"])
        if git_risk != "none":
            raise DevkitFlowError(f"cleanup refused because target Git risk is {git_risk}")
    commit = str(receipt.get("sourceCommit", ""))
    for entry in receipt.get("sourceFiles", []):
        relative = str(entry.get("path", ""))
        if not entry.get("tracked") or not git_commit_contains_path(workspace, commit, relative):
            raise DevkitFlowError(
                f"cleanup refused because source is not recoverable from Git: {relative}"
            )


def command_spec_archive_prune(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    receipt = load_archive_receipt(workspace, args.archive_id)
    if receipt["state"] in {"cleaned", "completed"}:
        emit(
            {
                "status": "ok",
                "message": f"Spec archive {receipt['archiveId']} was already cleaned.",
                "receipt": receipt,
            },
            args.format,
        )
        return 0
    if receipt["state"] not in {"archived", "cleanup-awaiting-approval"}:
        raise DevkitFlowError(
            f"Archive prune requires an applied archive; current state is {receipt['state']}"
        )
    approvals = receipt.get("approvals", {})
    if "cleanup" not in approvals:
        raise DevkitFlowError("cleanup approval is required before prune")
    if receipt["retention"] == "git-only" and "git-only-cleanup" not in approvals:
        raise DevkitFlowError("git-only-cleanup approval is required before prune")
    if receipt.get("cleanupPolicy") == "keep" and receipt["contentMode"] != "in-place":
        raise DevkitFlowError("cleanupPolicy=keep forbids prune for compact-copy archives")
    verify_prune_recovery(workspace, receipt)

    source = workspace / receipt["featureDir"]
    archive_dir = archive_materialized_dir(workspace, receipt)
    content_mode = receipt["contentMode"]
    retention = receipt["retention"]
    if content_mode == "in-place" and retention == "keep":
        temp_source = source.parent / f".{source.name}.{receipt['archiveId']}.compact.tmp"
        if temp_source.exists():
            shutil.rmtree(temp_source)
        shutil.copytree(archive_dir, temp_source)
        shutil.rmtree(source)
        temp_source.replace(source)
        shutil.rmtree(archive_dir)
        receipt["materializedArchiveDir"] = source.relative_to(workspace).as_posix()
    else:
        shutil.rmtree(source)
        if retention == "git-only" and archive_dir.exists():
            shutil.rmtree(archive_dir)
            receipt["materializedArchiveDir"] = ""
    receipt["state"] = "cleaned"
    receipt["cleanedAt"] = flow_now_iso()
    save_archive_receipt(workspace, receipt)
    emit(
        {
            "status": "ok",
            "message": f"Applied approved cleanup for {receipt['archiveId']}.",
            "receipt": receipt,
        },
        args.format,
    )
    return 0


def update_final_archive_manifest(workspace: Path, receipt: dict[str, Any]) -> None:
    archive_dir = archive_materialized_dir(workspace, receipt)
    manifest_path = archive_dir / "MANIFEST.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise DevkitFlowError(f"Invalid materialized archive manifest: {manifest_path}") from error
    manifest.update(
        {
            "state": "completed",
            "completedAt": receipt["completedAt"],
            "approvals": receipt.get("approvals", {}),
            "restore": {
                "sourceCommit": receipt.get("sourceCommit", ""),
                "featureDir": receipt["featureDir"],
                "command": f"devkit-flow spec archive restore --archive-id {receipt['archiveId']}",
            },
        }
    )
    atomic_write_text(
        manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )


def command_spec_archive_finalize(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    receipt = load_archive_receipt(workspace, args.archive_id)
    if receipt["state"] == "completed":
        emit(
            {
                "status": "ok",
                "message": f"Spec archive {receipt['archiveId']} is already completed.",
                "receipt": receipt,
            },
            args.format,
        )
        return 0
    source_kind = receipt.get("sourceKind", "current-feature")
    can_finish_without_prune = receipt["state"] == "archived" and (
        source_kind in {"existing-archive", "git-history"}
        or (
            receipt["retention"] == "keep"
            and receipt["contentMode"] == "compact-copy"
        )
    )
    if receipt["state"] != "cleaned" and not can_finish_without_prune:
        raise DevkitFlowError(
            f"Archive finalize requires cleaned state or non-destructive keep; current state is {receipt['state']}"
        )
    receipt["state"] = "completed"
    receipt["completedAt"] = flow_now_iso()
    save_archive_receipt(workspace, receipt)
    update_final_archive_manifest(workspace, receipt)
    save_archive_receipt(workspace, receipt)
    emit(
        {
            "status": "ok",
            "message": f"Completed spec archive {receipt['archiveId']}.",
            "receipt": receipt,
        },
        args.format,
    )
    return 0


def git_show_file(workspace: Path, commit: str, relative: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{commit}:{relative}"],
        cwd=workspace,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise DevkitFlowError(f"Cannot restore {relative} from {commit}: {detail}")
    return result.stdout


def command_spec_archive_restore(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    receipt = load_archive_receipt(workspace, args.archive_id)
    target = workspace / receipt["featureDir"]
    if target.exists():
        raise DevkitFlowError(f"Restore target already exists: {target}")
    commit = str(receipt.get("sourceCommit", ""))
    if not commit:
        raise DevkitFlowError("Archive receipt has no source Git commit")
    restored: list[str] = []
    try:
        for entry in receipt.get("sourceFiles", []):
            if not entry.get("tracked"):
                raise DevkitFlowError(
                    f"Archive contains a source file that was not tracked: {entry.get('path')}"
                )
            relative = str(entry.get("path", ""))
            path = workspace / relative
            if target.resolve() not in path.resolve().parents:
                raise DevkitFlowError(f"Unsafe restore path outside feature: {relative}")
            atomic_write_bytes(path, git_show_file(workspace, commit, relative))
            restored.append(relative)
    except Exception:
        if target.exists():
            shutil.rmtree(target)
        raise
    emit(
        {
            "status": "ok",
            "message": f"Restored {len(restored)} files from {commit}.",
            "archiveId": receipt["archiveId"],
            "restoredDir": receipt["featureDir"],
            "restoredFiles": restored,
        },
        args.format,
    )
    return 0
