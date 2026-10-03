from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from devkit_flow.config import deep_merge_config, load_devkit_config, save_devkit_config
from devkit_flow.constants import CLARIFICATION_SCHEMA_VERSION, CLARIFICATION_STATES, DEFAULT_CLARIFICATION_CONFIG
from devkit_flow.errors import DevkitFlowError
from devkit_flow.flow import apply_write_side_effects, flow_now_iso, flow_select_feature, WriteSideEffects
from devkit_flow.textio import atomic_write_text, attach_dashboard_warning, emit, exclusive_file_lock


def clarification_state_path(workspace: Path, feature: dict[str, Any]) -> Path:
    return workspace / feature["feature_dir"] / "clarification-state.json"


def load_clarification_batch(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        batch = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DevkitFlowError(f"Invalid {path}: {error}") from error
    if not isinstance(batch, dict):
        raise DevkitFlowError(f"Invalid {path}: root must be an object")
    if batch.get("schemaVersion") != CLARIFICATION_SCHEMA_VERSION:
        raise DevkitFlowError(
            f"Invalid {path}: unsupported schemaVersion {batch.get('schemaVersion')!r}"
        )
    if batch.get("status") not in CLARIFICATION_STATES:
        raise DevkitFlowError(f"Invalid {path}: unknown status {batch.get('status')!r}")
    if not isinstance(batch.get("questions"), list):
        raise DevkitFlowError(f"Invalid {path}: questions must be an array")
    return batch


def save_clarification_batch(path: Path, batch: dict[str, Any]) -> None:
    batch["updatedAt"] = flow_now_iso()
    atomic_write_text(path, json.dumps(batch, ensure_ascii=False, indent=2) + "\n")


def pending_required_questions(batch: dict[str, Any]) -> list[str]:
    return [
        str(question.get("id", ""))
        for question in batch.get("questions", [])
        if bool(question.get("required")) and not str(question.get("answer", "")).strip()
    ]


def clarification_config_summary(workspace: Path) -> dict[str, Any]:
    resolution = load_devkit_config(workspace)
    global_value = resolution.global_config.get("clarification")
    project_value = resolution.project_config.get("clarification")
    global_config = global_value if isinstance(global_value, dict) else {}
    project_config = project_value if isinstance(project_value, dict) else {}
    effective = deep_merge_config(DEFAULT_CLARIFICATION_CONFIG, global_config)
    effective = deep_merge_config(effective, project_config)
    source = "project" if project_config else "global" if global_config else "default"
    return {
        "mode": effective.get("mode", "batch"),
        "apply": effective.get("apply", "after-all-answered"),
        "source": source,
    }


def remove_clarification_config(config: dict[str, Any]) -> None:
    config.pop("clarification", None)


def command_flow_clarify_configure(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    resolution = load_devkit_config(workspace)
    target_config = copy.deepcopy(
        resolution.global_config if args.scope == "global" else resolution.project_config
    )
    if args.inherit and args.clear:
        raise DevkitFlowError("--inherit and --clear cannot be combined")
    if args.inherit:
        if args.scope != "project":
            raise DevkitFlowError("--inherit is only valid with --scope project")
        if args.mode or args.apply_policy:
            raise DevkitFlowError("--inherit cannot be combined with --mode or --apply")
        remove_clarification_config(target_config)
        message = "Project clarification override removed; global inheritance restored."
    elif args.clear:
        if args.scope != "global":
            raise DevkitFlowError("--clear is only valid with --scope global")
        if args.mode or args.apply_policy:
            raise DevkitFlowError("--clear cannot be combined with --mode or --apply")
        remove_clarification_config(target_config)
        message = "Global clarification configuration cleared."
    else:
        if not args.mode and not args.apply_policy:
            raise DevkitFlowError("configure requires --mode, --apply, --inherit, or --clear")
        current = target_config.setdefault("clarification", {})
        if not isinstance(current, dict):
            raise DevkitFlowError("Invalid clarification configuration: expected an object")
        if args.mode:
            current["mode"] = args.mode
        if args.apply_policy:
            current["apply"] = args.apply_policy
        message = f"Clarification configuration updated for {args.scope} scope."
    save_devkit_config(workspace, target_config, args.scope)
    emit(
        {
            "status": "configured",
            "message": message,
            "clarification": clarification_config_summary(workspace),
        },
        args.format,
    )
    return 0


def clarification_response(
    workspace: Path,
    feature: dict[str, Any],
    batch: dict[str, Any] | None,
    message: str,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "message": message,
        "feature": feature["feature"],
        "batch": batch,
        "pending_required": pending_required_questions(batch) if batch else [],
        "clarification": clarification_config_summary(workspace),
    }


CLARIFICATION_QUESTION_TYPES = {"text", "choice", "multi", "confirm"}


@dataclass(frozen=True)
class EntryError:
    index: int
    field: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "field": self.field, "reason": self.reason}

    def describe(self) -> str:
        return f"entry[{self.index}].{self.field} {self.reason}"


def clarification_question_record(
    *,
    question_id: str,
    question: str,
    category: str = "general",
    required: bool = False,
    depends_on: list[str] | None = None,
    recommendation: str = "",
    source: str = "",
    question_type: str = "text",
    options: list[str] | None = None,
    context: str = "",
) -> dict[str, Any]:
    return {
        "id": question_id,
        "question": question,
        "category": (category or "").strip() or "general",
        "required": bool(required),
        "dependsOn": [str(value).strip() for value in (depends_on or []) if str(value).strip()],
        "recommendation": (recommendation or "").strip(),
        "source": (source or "").strip(),
        "type": question_type,
        "options": list(options or []),
        "context": (context or "").strip(),
        "answer": "",
    }


def new_collecting_batch(batch_id: str) -> dict[str, Any]:
    now = flow_now_iso()
    return {
        "schemaVersion": CLARIFICATION_SCHEMA_VERSION,
        "batchId": batch_id,
        "status": "collecting",
        "questions": [],
        "createdAt": now,
        "updatedAt": now,
        "changeLogId": "",
    }


def presentable_clarification_questions(batch: dict[str, Any]) -> list[dict[str, Any]]:
    presentable: list[dict[str, Any]] = []
    for question in batch.get("questions") or []:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or "").strip()
        question_text = str(question.get("question") or "").strip()
        if question_id and question_text:
            presentable.append(question)
    return presentable


def clarification_batch_has_presentable_questions(batch: dict[str, Any]) -> bool:
    return bool(presentable_clarification_questions(batch))


def reject_empty_batch(
    batch: dict[str, Any],
    *,
    html_ui: dict[str, Any] | None = None,
    feature: dict[str, Any] | None = None,
) -> dict[str, Any]:
    batch_id = str(batch.get("batchId") or "")
    payload: dict[str, Any] = {
        "status": "empty_batch",
        "message": (
            f"Clarification batch {batch_id} has no questions; "
            "run flow clarify add-batch/add first."
        ),
        "reason": "empty_batch",
        "opened": False,
        "open_error": None,
        "html_path": None,
        "absolute_path": None,
        "pending_required": [],
        "batch": {"batchId": batch_id, "questionCount": 0},
        "html_ui": html_ui or {},
    }
    if feature is not None:
        payload["feature"] = feature.get("feature")
    return payload


def append_questions(batch: dict[str, Any], questions: list[dict[str, Any]]) -> dict[str, Any]:
    batch.setdefault("questions", []).extend(questions)
    return batch


def _entry_options(raw: Any) -> tuple[list[str] | None, str | None]:
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "must be an array"
    cleaned: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            return None, "must be an array of strings"
        item = value.strip()
        if item:
            cleaned.append(item)
    return cleaned, None


def _entry_depends_on(raw: Any) -> tuple[list[str] | None, str | None]:
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "must be an array"
    cleaned: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            return None, "must be an array of strings"
        item = value.strip()
        if item:
            cleaned.append(item)
    return cleaned, None


def _required_str_field(raw: dict[str, Any], field: str) -> tuple[str, str | None]:
    if field not in raw or raw[field] is None:
        return "", "missing"
    value = raw[field]
    if not isinstance(value, str):
        return "", "must be a string"
    stripped = value.strip()
    if not stripped:
        return "", "missing"
    return stripped, None


def _optional_str_field(raw: dict[str, Any], field: str, default: str = "") -> tuple[str, str | None]:
    if field not in raw or raw[field] is None:
        return default, None
    value = raw[field]
    if not isinstance(value, str):
        return default, "must be a string"
    stripped = value.strip()
    return stripped if stripped else default, None


def _optional_bool_field(raw: dict[str, Any], field: str) -> tuple[bool, str | None]:
    if field not in raw:
        return False, None
    value = raw[field]
    if not isinstance(value, bool):
        return False, "must be a boolean"
    return value, None


def validate_batch_file(
    payload: dict[str, Any],
    existing_batch: dict[str, Any] | None,
) -> tuple[list[EntryError], list[dict[str, Any]]]:
    errors: list[EntryError] = []
    built: list[dict[str, Any]] = []
    seen_ids = {
        str(item.get("id"))
        for item in (existing_batch or {}).get("questions") or []
        if str(item.get("id") or "").strip()
    }
    questions = payload.get("questions") if isinstance(payload, dict) else None
    if not isinstance(questions, list):
        return errors, built
    for index, raw in enumerate(questions):
        if not isinstance(raw, dict):
            errors.append(EntryError(index, "entry", "not an object"))
            continue
        question_id, id_error = _required_str_field(raw, "id")
        if id_error:
            errors.append(EntryError(index, "id", id_error))
        elif question_id in seen_ids:
            errors.append(EntryError(index, "id", "duplicate"))
        question_text, question_error = _required_str_field(raw, "question")
        if question_error:
            errors.append(EntryError(index, "question", question_error))
        required, required_error = _optional_bool_field(raw, "required")
        if required_error:
            errors.append(EntryError(index, "required", required_error))
        question_type = str(raw.get("type") or "text").strip() or "text"
        if question_type not in CLARIFICATION_QUESTION_TYPES:
            errors.append(
                EntryError(index, "type", "must be text, choice, multi, or confirm")
            )
        options, options_error = _entry_options(raw.get("options"))
        if options_error:
            errors.append(EntryError(index, "options", options_error))
            options = []
        elif question_type in {"choice", "multi"} and len(options or []) < 2:
            errors.append(
                EntryError(index, "options", f"{question_type} requires >=2 options")
            )
        elif question_type in {"text", "confirm"} and options:
            errors.append(EntryError(index, "options", "text/confirm cannot include options"))
        depends_on, depends_error = _entry_depends_on(raw.get("dependsOn"))
        if depends_error:
            errors.append(EntryError(index, "dependsOn", depends_error))
            depends_on = []
        category, category_error = _optional_str_field(raw, "category", "general")
        if category_error:
            errors.append(EntryError(index, "category", category_error))
        recommendation, recommendation_error = _optional_str_field(raw, "recommendation")
        if recommendation_error:
            errors.append(EntryError(index, "recommendation", recommendation_error))
        source, source_error = _optional_str_field(raw, "source")
        if source_error:
            errors.append(EntryError(index, "source", source_error))
        context, context_error = _optional_str_field(raw, "context")
        if context_error:
            errors.append(EntryError(index, "context", context_error))
        if question_id:
            seen_ids.add(question_id)
        if any(item.index == index for item in errors):
            continue
        built.append(
            clarification_question_record(
                question_id=question_id,
                question=question_text,
                category=category,
                required=required,
                depends_on=depends_on,
                recommendation=recommendation,
                source=source,
                question_type=question_type,
                options=options,
                context=context,
            )
        )
    return errors, built


def _add_batch_error(
    reason: str,
    message: str,
    *,
    errors: list[EntryError] | None = None,
) -> dict[str, Any]:
    return {
        "status": "error",
        "reason": reason,
        "message": message,
        "errors": [item.as_dict() for item in (errors or [])],
    }


def _emit_add_batch_error(
    args: argparse.Namespace,
    reason: str,
    message: str,
    *,
    errors: list[EntryError] | None = None,
) -> int:
    emit(_add_batch_error(reason, message, errors=errors), args.format)
    return 1


def _add_batch_id(payload: dict[str, Any]) -> str:
    raw = payload.get("batchId")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return f"CB-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}"


def command_flow_clarify_add_batch(
    args: argparse.Namespace,
    *,
    dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None,
    side_effects: WriteSideEffects | None = None,
) -> int:
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    file_path = Path(str(args.file))
    if not file_path.is_file():
        return _emit_add_batch_error(
            args,
            "invalid_file",
            f"add-batch file not found: {file_path}",
        )
    try:
        raw_text = file_path.read_text(encoding="utf-8")
        payload = json.loads(raw_text)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return _emit_add_batch_error(
            args,
            "invalid_file",
            f"add-batch file is not valid JSON: {error}",
        )
    if not isinstance(payload, dict):
        return _emit_add_batch_error(
            args,
            "invalid_file",
            "add-batch file root must be an object",
        )
    questions = payload.get("questions")
    if not isinstance(questions, list):
        return _emit_add_batch_error(
            args,
            "invalid_file",
            "add-batch file questions must be an array",
        )
    if not questions:
        return _emit_add_batch_error(
            args,
            "empty_file",
            "add-batch file has no questions.",
        )

    path = clarification_state_path(workspace, feature)
    with exclusive_file_lock(path):
        batch = load_clarification_batch(path)
        batch_opened = False
        if batch is None:
            batch = new_collecting_batch(_add_batch_id(payload))
            batch_opened = True
        elif batch.get("status") != "collecting":
            status = str(batch.get("status") or "")
            message = (
                f"Clarification batch {batch.get('batchId')} is closed"
                if status == "closed"
                else "Questions can only be added while the batch is collecting"
            )
            return _emit_add_batch_error(args, "batch_closed", message)

        errors, built = validate_batch_file(payload, None if batch_opened else batch)
        if errors:
            message = "; ".join(item.describe() for item in errors) + "."
            return _emit_add_batch_error(args, "invalid_entry", message, errors=errors)

        append_questions(batch, built)
        save_clarification_batch(path, batch)
        added_ids = [str(item["id"]) for item in built]
        data = {
            "status": "ok",
            "message": f"Added {len(added_ids)} questions to batch {batch['batchId']}.",
            "feature": feature["feature"],
            "batch_id": batch["batchId"],
            "batch_opened": batch_opened,
            "added_question_ids": added_ids,
            "question_count": len(batch.get("questions") or []),
            "pending_required": pending_required_questions(batch),
        }
        data["_events"] = []
        data = apply_write_side_effects(data, feature, side_effects)
        attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
        emit(data, args.format)
        return 0
