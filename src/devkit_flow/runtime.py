from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from devkit_flow.codex_agents import codex_agent_registration_check, codex_agent_registry
from devkit_flow.config import load_devkit_config
from devkit_flow.constants import AGENT_DATA_PAYLOAD_FILES, CANONICAL_RUNTIME_ORDER, CLAUDE_MARKETPLACE_NAME, CODEX_MARKETPLACE_NAME, DEVKIT_CLI_RELATIVE_PATH, LEGACY_CLAUDE_MARKETPLACE_NAMES, LEGACY_RUNTIME_PLUGIN_NAMES, LIFECYCLE_ONLY_RUNTIMES, MIGRATION_CODEX_RUNTIME_PREAMBLE, NPM_INSTALL_TIMEOUT_SECONDS, RUNTIME_COMMAND_TIMEOUT_SECONDS, RUNTIME_NAMES, RUNTIME_PLUGIN_NAME, _UPGRADE_SKIPPABLE_ERROR_CODES
from devkit_flow.errors import DevkitFlowError
from devkit_flow.kimi_plugin import install_kimi_plugin, parse_agent_source, replace_directory
from devkit_flow.models import WorkflowRuntimeSwitch
from devkit_flow.constants import PR_REVIEW_WATCH_HOOK_LOCK_TIMEOUT_MS, PR_REVIEW_WATCH_SESSION_ENV_VARS
class _PublicWatchResult:
    status = "done"
    removed = 0
    rendered = ""
    reservedSeq = 0
    queueId = ""


class WatchRegistry:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        return None


class WatchService:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        return None

    def deregister_session(self, *args: Any, **kwargs: Any) -> _PublicWatchResult:
        return _PublicWatchResult()

    def next_turn_inject(self, *args: Any, **kwargs: Any) -> _PublicWatchResult:
        return _PublicWatchResult()

    def confirm_turn_inject(self, *args: Any, **kwargs: Any) -> None:
        return None


def apply_pr_review_session_start_notice(parts: list[str], *args: Any, **kwargs: Any) -> list[str]:
    del args, kwargs
    return parts


def pr_review_auth_notice(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    return None


def pr_review_session_line(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    return None


def detached_converge_allows_unload(*args: Any, **kwargs: Any) -> bool:
    del args, kwargs
    return False


def forget_pending_session_end(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    return None


def remember_pending_session_end(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    return None


def replay_pending_session_ends(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    return None


def resume_ended_watches(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    return None
from devkit_flow.selfupdate import extract_embedded_version, find_global_cli, self_update_result
from devkit_flow.session_registry import SessionEndIdentity, SessionIndex, SessionIndexReadError, SessionRef, append_session_end, append_session_marker, capture_session_end_identity, iter_markers, latest_marker, parse_hook_stdin, read_hook_stdin, registry_path, resolve_runtime_pid, sid_key
from devkit_flow.setup import resolve_agents_source, stale_codex_plugin_finding
from devkit_flow.telemetry import TelemetryConfig, TelemetryEmitter, session_ended_extended_stats
from devkit_flow.telemetry_spans import (
    CodexRolloutReader,
    GrokTokenSummarizer,
    KimiTokenSummarizer,
    PiTokenSummarizer,
    SessionTokenSummarizer,
)
from devkit_flow.templates_md import LEGACY_WORKSPACE_ROUTING_TEMPLATE, PRE_WORKFLOW_LEGACY_WORKSPACE_ROUTING_TEMPLATE, WORKSPACE_ROUTING_TEMPLATE
from devkit_flow.textio import atomic_write_text, compact_error, current_cli_path, emit, maybe_check_latest_update, process_start_time, runtime_support_root, sha256_file, spawn_detached, version_tuple
from devkit_flow.version import NPM_PACKAGE_NAME, VERSION
from devkit_flow.workflow_auto import merge_auto_takeover, workflow_session_notice_line


# --- 运行时插件更新（runtime update） ---------------------------------------------------
class RuntimeStepFailed(DevkitFlowError):
    """运行时插件子命令失败：携带步骤记录，由 runtime updater 统一转成 error 结果。"""

    def __init__(self, step: dict[str, Any]):
        super().__init__(step["command"])
        self.step = step


def run_plugin_step(command: list[str]) -> tuple[dict[str, Any], str]:
    """执行一个运行时插件子命令并如实记录，返回 (步骤记录, 完整输出)。

    可执行文件缺失/超时等异常一律转成 ok=False 的记录（exit_code 为 None），绝不抛出；
    记录里的 output 截断到 300 字符，完整输出单独返回供调用方做内容判断（如 list 探测）。
    """
    display = " ".join(command)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=RUNTIME_COMMAND_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": display, "ok": False, "exit_code": None, "output": compact_error(error)}, ""
    output = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
    step = {
        "command": display,
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "output": output[:300],
    }
    return step, output


def run_runtime_step(steps: list[dict[str, Any]], command: list[str]) -> str:
    """执行并记录一步，失败抛 RuntimeStepFailed；成功返回完整输出。"""
    step, output = run_plugin_step(command)
    steps.append(step)
    if not step["ok"]:
        raise RuntimeStepFailed(step)
    return output


def runtime_step_failure_message(failure: RuntimeStepFailed) -> str:
    step = failure.step
    detail = step["output"] or f"exit {step['exit_code']}"
    return f"failed: {step['command']} ({detail})"


def codex_plugin_state(output: str, plugin_name: str = RUNTIME_PLUGIN_NAME) -> dict[str, Any]:
    """Parse `codex plugin list` for one plugin.

    Codex may also list Claude marketplace plugins. Match the Codex
    marketplace-qualified name first; an unqualified line is accepted as
    a fallback. Other `@marketplace` suffixes must not win.
    """
    candidates: list[str] = []
    if "@" in plugin_name:
        candidates.append(plugin_name)
    else:
        candidates.append(f"{plugin_name}@{CODEX_MARKETPLACE_NAME}")
        candidates.append(plugin_name)
    for candidate in candidates:
        match = re.search(
            rf"(?m)^\s*{re.escape(candidate)}\s+installed,\s*(enabled|disabled)\s+(\S+)",
            output,
        )
        if match:
            return {
                "installed": True,
                "enabled": match.group(1) == "enabled",
                "version": match.group(2),
            }
    return {"installed": False, "enabled": False, "version": None}


def codex_plugin_check() -> dict[str, Any]:
    if not shutil.which("codex"):
        return {"runtime": "codex", "active": False, "status": "unavailable", "message": "codex CLI is not on PATH"}
    step, output = run_plugin_step(["codex", "plugin", "list"])
    plugin = codex_plugin_state(output) if step["ok"] else {"installed": False, "enabled": False, "version": None}
    version_matches = plugin["version"] == VERSION
    registration = codex_agent_registration_check()
    agents_ready = bool(registration.get("ready"))
    active = bool(step["ok"] and plugin["enabled"] and version_matches and agents_ready)
    if not step["ok"]:
        status = "error"
        message = "cannot query Codex plugins"
    elif plugin["enabled"] and not version_matches:
        status = "outdated"
        message = f"plugin is enabled but outdated (installed {plugin['version'] or 'unknown'}, expected {VERSION})"
    elif plugin["enabled"] and not agents_ready:
        # Installed-but-not-ready permits repair without falsely reporting a
        # version mismatch. Error would prevent the transaction from starting.
        status = "installed"
        message = (
            "custom agents not ready; run devkit-flow runtime update --runtime codex; "
            + registration.get("message", "registration is incomplete")
        )
    elif active:
        status = "active"
        message = f"plugin is enabled at {VERSION}; {registration.get('role_count', 0)} custom agents registered"
    else:
        status = "inactive"
        message = "plugin is not installed and enabled"
    return {
        "runtime": "codex",
        "active": active,
        "status": status,
        "version": plugin["version"],
        "expected_version": VERSION,
        "version_matches": version_matches,
        "agents_ready": agents_ready,
        "agent_registration": registration,
        "message": message,
        "step": step,
    }


def parse_claude_plugin_list(output: str) -> list[dict[str, Any]]:
    try:
        loaded = json.loads(output)
    except json.JSONDecodeError:
        return []
    return [entry for entry in loaded if isinstance(entry, dict)] if isinstance(loaded, list) else []


def claude_plugin_matches(entries: list[dict[str, Any]], plugin_name: str) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if str(entry.get("id", "")).split("@", 1)[0] == plugin_name
    ]


def claude_plugin_selector_matches(
    entries: list[dict[str, Any]],
    plugin_name: str,
    marketplace_name: str,
) -> list[dict[str, Any]]:
    selector = f"{plugin_name}@{marketplace_name}"
    return [entry for entry in entries if str(entry.get("id", "")) == selector]


def legacy_claude_plugin_matches(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plugin_names = {RUNTIME_PLUGIN_NAME, *LEGACY_RUNTIME_PLUGIN_NAMES}
    legacy_marketplaces = set(LEGACY_CLAUDE_MARKETPLACE_NAMES)
    matches: list[dict[str, Any]] = []
    for entry in entries:
        plugin_name, separator, marketplace_name = str(entry.get("id", "")).partition("@")
        if separator and plugin_name in plugin_names and marketplace_name in legacy_marketplaces:
            matches.append(entry)
    return matches


def claude_marketplace_is_listed(output: str, marketplace_name: str) -> bool:
    """Match Claude marketplace list entries exactly, excluding source paths and prefixes."""
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("❯"):
            candidate = candidate[1:].strip()
        if candidate == marketplace_name:
            return True
    return False


def claude_plugin_check() -> dict[str, Any]:
    if not shutil.which("claude"):
        return {
            "runtime": "claude-code",
            "active": False,
            "status": "unavailable",
            "message": "claude CLI is not on PATH",
        }
    step, output = run_plugin_step(["claude", "plugin", "list", "--json"])
    entries = parse_claude_plugin_list(output) if step["ok"] else []
    matches = [
        entry
        for entry in claude_plugin_selector_matches(
            entries, RUNTIME_PLUGIN_NAME, CLAUDE_MARKETPLACE_NAME
        )
        if entry.get("enabled") is True
    ]
    versions = sorted({str(entry.get("version")) for entry in matches if entry.get("version")})
    version_matches = bool(matches) and all(str(entry.get("version", "")) == VERSION for entry in matches)
    active = bool(step["ok"] and version_matches)
    if not step["ok"]:
        status = "error"
        message = "cannot query Claude plugins"
    elif matches and not version_matches:
        status = "outdated"
        message = f"plugin is enabled but outdated (installed {', '.join(versions) or 'unknown'}, expected {VERSION})"
    elif active:
        status = "active"
        message = f"plugin is installed and enabled at {VERSION}"
    else:
        status = "inactive"
        message = "plugin is not installed and enabled"
    return {
        "runtime": "claude-code",
        "active": active,
        "status": status,
        "version": versions[-1] if versions else None,
        "versions": versions,
        "expected_version": VERSION,
        "version_matches": version_matches,
        "scopes": sorted({str(entry.get("scope")) for entry in matches if entry.get("scope")}),
        "message": message,
        "step": step,
    }


def kimi_plugin_check() -> dict[str, Any]:
    installed_path = Path.home() / ".kimi-code" / "plugins" / "installed.json"
    try:
        data = json.loads(installed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "runtime": "kimi-code",
            "active": False,
            "status": "inactive",
            "message": f"plugin registry is missing or invalid: {installed_path}",
        }
    entries = data.get("plugins", []) if isinstance(data, dict) else []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("id") != RUNTIME_PLUGIN_NAME or entry.get("enabled") is not True:
            continue
        root_value = entry.get("root")
        root = Path(root_value).expanduser() if isinstance(root_value, str) else None
        manifest = root / ".kimi-plugin" / "plugin.json" if root is not None else None
        if manifest is None or not manifest.is_file():
            continue
        try:
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if manifest_data.get("name") != RUNTIME_PLUGIN_NAME:
            continue
        installed_version = str(manifest_data.get("version", ""))
        version_matches = installed_version == VERSION
        return {
            "runtime": "kimi-code",
            "active": version_matches,
            "status": "active" if version_matches else "outdated",
            "version": installed_version or None,
            "expected_version": VERSION,
            "version_matches": version_matches,
            "root": str(root),
            "message": (
                f"plugin is registered, enabled, and valid at {VERSION}"
                if version_matches
                else f"plugin is enabled but outdated (installed {installed_version or 'unknown'}, expected {VERSION})"
            ),
        }
    return {
        "runtime": "kimi-code",
        "active": False,
        "status": "inactive",
        "message": "plugin is not registered, enabled, and backed by a valid managed manifest",
    }


RUNTIME_PLUGIN_CHECKERS = {
    "claude-code": claude_plugin_check,
    "codex": codex_plugin_check,
    "kimi-code": kimi_plugin_check,
}


def runtime_plugin_check(runtime: str) -> dict[str, Any]:
    return RUNTIME_PLUGIN_CHECKERS[runtime]()


def _plugin_version_stale(check: dict[str, Any]) -> bool:
    """True when this runtime's installed plugin lags the current CLI release."""
    status = str(check.get("status") or "").lower()
    if status == "outdated":
        return True
    if status in {"absent", "unavailable", "inactive", "error", "invalid"}:
        return False
    matches = check.get("version_matches")
    if matches is None:
        matches = check.get("versionMatches")
    if matches is False and (check.get("version") or check.get("installed")):
        return True
    message = str(check.get("message") or "").lower()
    return "outdated" in message and "plugin" in message


def session_freshness_notice(runtime: str, check: dict[str, Any]) -> str | None:
    """Short SessionStart prompt when CLI or this-runtime plugin is stale.

    Read-only: never calls upgrade/setup. Fail-safe: any probe error → None.
    """
    try:
        update = maybe_check_latest_update()
    except Exception:
        update = None
    cli_stale = bool(update and update.get("update_available"))
    plugin_stale = _plugin_version_stale(check)
    if not cli_stale and not plugin_stale:
        return None
    bits: list[str] = []
    if cli_stale and isinstance(update, dict):
        bits.append(f"CLI {update.get('current')}→{update.get('latest')}")
    if plugin_stale:
        installed = check.get("version") or "unknown"
        expected = check.get("expected_version") or check.get("expectedVersion") or VERSION
        bits.append(f"{runtime} plugin {installed}≠{expected}")
    command = (
        f"devkit-flow upgrade --runtime {runtime}"
        if cli_stale
        else f"devkit-flow runtime update --runtime {runtime}"
    )
    return f"[devkit] update: {'; '.join(bits)}; run: {command}"


_HOOK_LOCK_SEC = PR_REVIEW_WATCH_HOOK_LOCK_TIMEOUT_MS / 1000.0
_CLEAR_WINDOW_SEC = 120
# /clear 已注销监听，marker 只留条数。converge 不会按原目标恢复。
# 引号包住占位，shell 不会把 < 当重定向、把 # 当注释；cliargs 仍能解析。
_CLEAR_RESTART_COMMAND = "devkit-flow pr-review watch start --lane author --pr '<project>/<repo>#<id>'"
_CLEAR_HINT = (
    "[devkit] pr-review: /clear 结束了上一会话的 %d 条 PR 监听，"
    "如需继续请按原目标重新执行 " + _CLEAR_RESTART_COMMAND
)
_PLAIN_TURN_RUNTIMES = ("kimi-code", "pi")


class _HookTimer:
    """SessionEnd 不调用定时器。占位对象避免 hook 构造真实 launchd / schtasks。"""

    def backend(self) -> str:
        if os.name == "nt":
            return "schtasks"
        return "launchd"


def _hook_platform() -> str:
    if os.name == "nt":
        return "nt"
    if sys.platform == "darwin":
        return "darwin"
    return str(sys.platform)


def _hook_spawn(argv: object, log_path: object) -> bool:
    """开关允许时才派生卸载。不等待子进程（spawn_detached 自己返回）。"""
    if not detached_converge_allows_unload(_hook_platform()):
        return False
    return bool(spawn_detached(argv, log_path))


def _hook_watch_service_at(home: Path) -> WatchService:
    index = SessionIndex(home=home)
    registry = WatchRegistry(home / ".devkit" / "review", session_index=index)
    return WatchService(
        registry,
        _HookTimer(),
        platform=_hook_platform(),
        spawn=_hook_spawn,
        session_index=index,
        home=home,
    )


def _hook_watch_service() -> WatchService:
    return _hook_watch_service_at(Path.home())


def _review_root() -> Path:
    return Path.home() / ".config" / "devkit" / "review"


def _hook_stdin_source(raw: str) -> str | None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    source = data.get("source")
    if isinstance(source, str) and source.strip():
        return source.strip()
    return None


def _clear_restart_hint(runtime: str, pid: int | None, current_session: str | None) -> str | None:
    """同 pid 上一会话 120s 内因 clear 结束且注销过监听时返回 DR-48 提示。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    index = SessionIndex(home=Path.home())
    try:
        document = json.loads(index.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    sessions = document.get("sessions") if isinstance(document, dict) else None
    if not isinstance(sessions, dict):
        return None
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    best_sid = None
    best_ended = None
    prefix = str(runtime) + ":"
    for key, raw in sessions.items():
        if not str(key).startswith(prefix) or not isinstance(raw, dict):
            continue
        session_id = str(key).split(":", 1)[1]
        if current_session and session_id == current_session:
            continue
        if raw.get("pid") != pid or raw.get("endReason") != "clear":
            continue
        ended = _parse_marker_ts(raw.get("endedAt"))
        if ended is None:
            continue
        if (now - ended).total_seconds() > _CLEAR_WINDOW_SEC:
            continue
        if best_ended is None or ended > best_ended:
            best_ended = ended
            best_sid = session_id
    if not best_sid:
        return None
    count = 0
    for marker in iter_markers(home=Path.home()):
        if marker.get("event") != "end":
            continue
        if marker.get("runtime") != runtime or marker.get("session_id") != best_sid:
            continue
        removed = marker.get("deregisteredWatches")
        if type(removed) is int and removed > count:
            count = removed
    if count <= 0:
        return None
    return _CLEAR_HINT % count


def _global_prompt(workspace: Path, clear_hint: str | None, session_line: str | None) -> str:
    parts: list[str] = []
    try:
        notice = pr_review_auth_notice(_review_root())
    except Exception:
        notice = None
    if notice:
        parts.append(notice)
    if clear_hint:
        parts.append(clear_hint)
    if session_line:
        parts.append(session_line)
    return "; ".join(parts)


def _write_claude_inbox(runtime: str, session_id: str, pid: int | None) -> None:
    """token 只进 0600 inbox 文件，不进索引、登记表或日志。"""
    if runtime != "claude-code" or not session_id:
        return
    socket = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "").strip()
    token = os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN", "").strip()
    if not socket or not token:
        return
    path = Path.home() / ".config" / "devkit" / "sessions" / "claude-code" / ("%s.inbox.json" % sid_key(session_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schemaVersion": 1, "socket": socket, "token": token, "pid": pid}
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)
    if os.name != "nt":
        os.chmod(path, 0o600)


def _annotate_deregistered(home: Path, count: int) -> None:
    path = registry_path(home)
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return
    try:
        row = json.loads(lines[-1])
    except ValueError:
        return
    if not isinstance(row, dict) or row.get("event") != "end":
        return
    row["deregisteredWatches"] = int(count)
    lines[-1] = json.dumps(row, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _finalize_session_end(
    runtime: str,
    workspace: Path,
    session_id: str | None,
    reason: str | None,
    identity: SessionEndIdentity | None = None,
) -> None:
    """只用入口捕获的身份写结束标记和注销。这里不再读索引补身份。"""
    if not isinstance(identity, SessionEndIdentity):
        return
    session_id = identity.session_id or session_id
    if not session_id:
        return
    home = Path.home()
    end_reason = reason if isinstance(reason, str) else ""
    if identity.state == "unreadable":
        remember_pending_session_end(home, runtime, session_id, end_reason, captured=identity)
        return
    outcome: dict[str, Any] = {}
    indexed = SessionIndex(home=home).record_end(
        runtime,
        session_id,
        end_reason,
        identity.observed_at,
        lock_timeout=_HOOK_LOCK_SEC,
        expected_identity=identity,
        outcome=outcome,
    )
    status = outcome.get("status")
    if indexed is None and status not in ("rejected", "deferred"):
        status = "failed"
    if status == "rejected":
        forget_pending_session_end(home, runtime, session_id, identity)
        return
    if status != "applied":
        if status != "deferred":
            append_session_end(
                runtime=runtime,
                workspace=workspace,
                session_id=session_id,
                reason=end_reason,
                home=home,
            )
        remember_pending_session_end(home, runtime, session_id, end_reason, captured=identity)
        return
    append_session_end(
        runtime=runtime,
        workspace=workspace,
        session_id=session_id,
        reason=end_reason,
        home=home,
    )
    generation = outcome.get("generation")
    if type(generation) is not int and indexed is not None and type(indexed.generation) is int:
        generation = indexed.generation
    removed = 0
    deregister_done = False
    try:
        result = _hook_watch_service().deregister_session(runtime, session_id, True, generation, identity)
        deregister_done = getattr(result, "status", "") == "done"
        if deregister_done:
            removed = int(getattr(result, "removed", 0) or 0)
    except Exception:
        removed = 0
        deregister_done = False
    if not deregister_done:
        remember_pending_session_end(home, runtime, session_id, end_reason, captured=identity)
    else:
        forget_pending_session_end(home, runtime, session_id, identity)
    if removed > 0:
        _annotate_deregistered(home, removed)


def _hook_session_id(runtime: str) -> str | None:
    parsed = parse_hook_stdin(read_hook_stdin())
    session_id = parsed.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    for name, variable in PR_REVIEW_WATCH_SESSION_ENV_VARS:
        if name != runtime:
            continue
        value = os.environ.get(variable, "").strip()
        return value or None
    return None


def _collect_turn_start_injection(runtime: str) -> tuple:
    """取出待注入文本。游标要等 stdout flush 成功后才确认。"""

    session_id = _hook_session_id(runtime) or ""
    if not session_id:
        return "", 0, "", "", ""
    result = _hook_watch_service().next_turn_inject(runtime, session_id)
    rendered = getattr(result, "rendered", "") or ""
    if not isinstance(rendered, str):
        rendered = ""
    seq = getattr(result, "reservedSeq", 0) or 0
    try:
        seq = int(seq)
    except (TypeError, ValueError):
        seq = 0
    queue_id = str(getattr(result, "queueId", "") or "")
    reservation_id = str(getattr(result, "reservationId", "") or "")
    return rendered, seq, session_id, queue_id, reservation_id


def _collect_turn_start_text(runtime: str) -> str:
    text, _seq, _session_id, _queue_id, _reservation_id = _collect_turn_start_injection(runtime)
    return text


def turn_start_envelope(runtime: str, text: str) -> "dict[str, Any] | str | None":
    """有事件时的信封。无事件：antigravity 为 {}，其余返回 None（调用方不输出）。"""
    if runtime == "antigravity":
        if not text:
            return {}
        return {"injectSteps": [{"userMessage": text}]}
    if not text:
        return None
    if runtime in _PLAIN_TURN_RUNTIMES:
        return text
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }


def _emit_turn_start(runtime: str, text: str, hook_json: bool) -> None:
    if runtime == "grok-build" and not hook_json:
        if text:
            print(text)
            sys.stdout.flush()
        return
    rendered = turn_start_envelope(runtime, text)
    if rendered is None:
        return
    if isinstance(rendered, str):
        print(rendered)
    else:
        print(json.dumps(rendered, ensure_ascii=False))
    sys.stdout.flush()


def _print_check_failure(runtime: str) -> None:
    """hook 内部异常：该端无事件输出，退出码由调用方保持 0。"""
    if runtime == "antigravity":
        print("{}")


def runtime_hook_envelope(
    check: dict[str, Any],
    workspace: Path | None = None,
    hook_event: str | None = None,
    clear_hint: str | None = None,
) -> dict[str, Any]:
    runtime = str(check.get("runtime") or "unknown")
    root = workspace
    if root is None:
        argv = sys.argv
        if "--target" in argv:
            index = argv.index("--target")
            if index + 1 < len(argv) and not str(argv[index + 1]).startswith("-"):
                root = Path(argv[index + 1])
    if root is None:
        root = Path.cwd()
    root = Path(root).expanduser()
    read_failed = False
    try:
        resolution = load_devkit_config(root)
        switch = merge_auto_takeover(
            resolution.global_config,
            resolution.project_config,
            runtime,
        )
    except Exception:
        # D-12: SessionStart 只读路径不得因开关配置失败阻断会话。
        read_failed = True
        switch = WorkflowRuntimeSwitch(runtime=runtime, enabled=True, source="default")
    notice = workflow_session_notice_line(switch)
    state = "ready" if check.get("active") else "not ready"
    message = check.get("message", "")
    parts = [notice]
    freshness = session_freshness_notice(runtime, check)
    if freshness:
        # Keep the update prompt near the front so the 500-char truncate
        # (from the end) does not drop it.
        parts.append(freshness)
    if read_failed:
        parts.append("config-read-failed")
    parts.append(f"[devkit] {runtime} plugin {state}: {message}")
    session_line = pr_review_session_line(root)
    if session_line:
        parts.append(session_line)
    if runtime == "codex":
        parts.append(
            "spec-backed continuation is coordinated by the Codex main thread loading "
            "devkit:feature-dev-manager. If the user explicitly opts out of that manager "
            "and supplies existing design artifacts, the main thread may dispatch "
            "design-to-tasks-agent directly."
        )
        # 010：旧插件残留一行提示，追加在末尾（500 字符截断最多裁掉本提示，
        # 不影响前面的路由文案）；检测只读且任何异常静默。
        try:
            stale_plugin = stale_codex_plugin_finding()
        except Exception:
            stale_plugin = None
        if stale_plugin is not None:
            parts.append(stale_plugin["hint"])
    if runtime == "antigravity":
        if hook_event == "SessionStart":
            message = _global_prompt(root, clear_hint, session_line)
            if not message:
                return {}
            return {"injectSteps": [{"systemMessage": message}]}
        return {"decision": "allow"}
    try:
        parts = apply_pr_review_session_start_notice(parts, _review_root())
    except Exception:
        pass
    if clear_hint:
        auth_first = bool(parts) and str(parts[0]).startswith("[devkit] pr-review: MCode")
        parts.insert(1 if auth_first else 0, clear_hint)
    context = "; ".join(parts)
    return {
        "continue": True,
        "suppressOutput": False if freshness else bool(check.get("active")),
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context[:500],
        },
    }




def record_hook_session_marker(runtime: str, workspace: Path) -> str | None:
    """SessionStart：jsonl marker、会话索引、claude inbox。返回 DR-48 提示或 None。

    fail-safe：任何异常静默，绝不影响 hook envelope 输出与 exit code；无 stdin 数据
    （手工执行 runtime check / 宿主未注入）时不记录。不取 tick.lock，不收敛。
    """
    try:
        raw = read_hook_stdin()
        parsed = parse_hook_stdin(raw)
        session_id = parsed["session_id"]
        if runtime == "grok-build" and not session_id:
            session_id = os.environ.get("GROK_SESSION_ID", "").strip() or None
        elif runtime == "antigravity" and not session_id:
            session_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip() or None
        transcript_path = parsed["transcript_path"]
        if not session_id and not transcript_path:
            return None
        from datetime import datetime, timezone

        resolved = resolve_runtime_pid(runtime, os.environ)
        hint = None
        if _hook_stdin_source(raw) == "clear":
            hint = _clear_restart_hint(runtime, resolved.pid, session_id)
        append_session_marker(
            runtime=runtime,
            workspace=workspace,
            session_id=session_id,
            transcript_path=transcript_path,
            source="hook-stdin",
            pid=resolved.pid,
            proc_started_at=resolved.proc_started_at,
        )
        if session_id:
            try:
                written = SessionIndex(home=Path.home()).record_start(
                    SessionRef(runtime, session_id, resolved.pid, str(workspace)),
                    resolved.proc_started_at,
                    "session-start",
                    datetime.now(timezone.utc),
                    lock_timeout=_HOOK_LOCK_SEC,
                )
                _write_claude_inbox(runtime, session_id, resolved.pid)
                if written is not None:
                    resume_ended_watches(Path.home(), runtime, session_id, True)
            except Exception:
                pass
        # 点位 #13：marker 落盘后 enqueue-only session_started（先落盘再 enqueue，
        # enqueue 抛错也不回滚 marker、不改 exit）。
        _emit_session_event(
            runtime,
            workspace,
            "session_started",
            {
                "trigger": "session-start-hook",
                "transcript_available": bool(transcript_path),
            },
            {"name": runtime, "session_id": session_id},
            command="runtime check",
        )
        return hint
    except Exception:
        return None  # 兜底防线：marker / enqueue 失败绝不阻断 hook


def command_runtime_check(args: argparse.Namespace) -> int:
    runtime = getattr(args, "runtime", None)
    workspace = Path(getattr(args, "target", ".") or ".")
    hint = None
    if runtime in CANONICAL_RUNTIME_ORDER:
        hint = record_hook_session_marker(runtime, workspace)
    if not args.hook_json:
        return command_runtime_lifecycle(args, "check")
    try:
        if args.runtime == "all":
            raise DevkitFlowError("--hook-json requires a single runtime")
        buffer = io.StringIO()
        controller = runtime_lifecycle_controller(getattr(args, "timeout_seconds", 10))
        controller.run(
            operation="check",
            runtime=args.runtime,
            fmt="json",
            force=getattr(args, "force", False),
            timeout_seconds=getattr(args, "timeout_seconds", 10),
            stdout=buffer,
        )
        document = json.loads(buffer.getvalue() or "{}")
        children = document.get("results") if isinstance(document, dict) else None
        child = children[0] if isinstance(children, list) and children else {}
        check = {
            "runtime": args.runtime,
            "active": bool(child.get("active")),
            "message": str(child.get("message") or ""),
            "status": child.get("status"),
            "version": child.get("version"),
            "expected_version": child.get("expectedVersion") or child.get("expected_version"),
            "version_matches": (
                child.get("versionMatches")
                if "versionMatches" in child
                else child.get("version_matches")
            ),
            "installed": child.get("installed"),
        }
        envelope = runtime_hook_envelope(
            check,
            workspace,
            hook_event=getattr(args, "hook_event", None),
            clear_hint=hint,
        )
        print(json.dumps(envelope, ensure_ascii=False))
        return 0
    except Exception:
        _print_check_failure(str(getattr(args, "runtime", "") or ""))
        return 0


def _emit_session_event(
    runtime: str,
    workspace: Path,
    event_type: str,
    payload: dict[str, Any],
    session_ref: dict[str, Any],
    *,
    command: str,
) -> None:
    """enqueue-only 发一条 session 事件（fail-safe：emit_session_event_safe 内部吞异常）。"""
    TelemetryEmitter(Path(workspace), command=command).emit_session_event_safe(
        event_type, payload, session_ref
    )


def _parse_marker_ts(value: Any) -> "datetime | None":
    from datetime import datetime, timezone

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# Codex 0.153 `stop.command.output`（additionalProperties: false）。
# hookSpecificOutput 只在 Codex SessionStart / UserPromptSubmit / PreToolUse 合法。
CODEX_STOP_HOOK_OUTPUT_KEYS = frozenset(
    {
        "continue",
        "decision",
        "reason",
        "stopReason",
        "suppressOutput",
        "systemMessage",
    }
)


def _session_end_hook_envelope(
    hook_event: str = "SessionEnd",
    runtime: str | None = None,
) -> dict[str, Any] | None:
    """`--hook-json` session-end 的 stdout。按宿主 schema 出字段，不套用 Claude 协议。

    - claude-code SessionEnd：只有 continue / suppressOutput（D-45）。
    - claude-code Stop：允许 hookSpecificOutput。
    - codex Stop：0.153 禁止 hookSpecificOutput，多一个键就会 Hook failed。
    - codex SessionEnd：stdout 为空（DR-25，返回 None，调用方不打印）。
    - kimi-code session-end 走 `--format json`（{status: ok}），不进本函数。
    - grok-build / pi 无 Stop/SessionEnd hook；未知 runtime 也不发 Claude 专用字段。
    """
    if hook_event != "Stop" and runtime == "codex":
        return None
    envelope: dict[str, Any] = {"continue": True, "suppressOutput": True}
    if hook_event == "Stop" and runtime == "claude-code":
        envelope["hookSpecificOutput"] = {
            "hookEventName": "Stop",
            "additionalContext": "",
        }
    return envelope


def _collect_session_ended(
    runtime: str,
    workspace: Path,
    hook_event: str = "SessionEnd",
    explicit_session_id: str | None = None,
) -> None:
    """解析 stdin / 归因回落 / duration / tokens，enqueue-only 一条 session_ended。

    hook_event：SessionEnd（默认）或 Stop。Stop → payload trigger=stop-hook；
    --runtime claude-code 的 tokens = SessionTokenSummarizer；transcript 缺失
    / 解析失败 → tokens null、token_source none（与 schema session_ended 一致）。
    --runtime codex 的 tokens = CodexRolloutReader.cumulative_tokens（末条
    total_token_usage 经 TokenMapper，D-1），token_source=codex-transcript；
    rollout 不可得 → tokens null、token_source none。
    """
    from datetime import datetime, timezone

    # 复用模块级 read_hook_stdin / parse_hook_stdin（保留 hook 测试对 runtime.* 的 mock 缝）。
    raw = read_hook_stdin()
    parsed = parse_hook_stdin(raw)
    stdin_session_id = parsed["session_id"]
    explicit = explicit_session_id.strip() if isinstance(explicit_session_id, str) else ""
    stdin_id = stdin_session_id.strip() if isinstance(stdin_session_id, str) else ""
    deregister_id = explicit or stdin_id or None
    session_id = deregister_id
    transcript_path = parsed["transcript_path"]
    reason: str | None = None
    # 身份只在入口捕获。marker 回落只给遥测，不能改这次要结束的实例。
    ending = None
    if hook_event != "Stop" and deregister_id:
        ending = capture_session_end_identity(
            SessionIndex(home=Path.home()),
            runtime,
            deregister_id,
            datetime.now(timezone.utc),
        )
    try:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                candidate = data.get("reason")
                if isinstance(candidate, str) and candidate.strip():
                    # 隐私不变式 D-22：session_ended.reason 与 gate_event.reason_excerpt 对齐，
                    # 均为受控自由文本，硬截断至 200 字（schema maxLength:200 对应）。
                    reason = candidate[:200]
        except (TypeError, ValueError):
            reason = None

        fallback = None
        if not session_id:
            # stdin 缺 session_id：按 runtime 过滤回落 registry，绝不跨 runtime 归因（§4.10）。
            fallback = latest_marker(workspace, runtime=runtime)
            if fallback is not None:
                session_id = fallback.get("session_id")
                if not transcript_path:
                    transcript_path = fallback.get("transcript_path")

        duration_ms: int | None = None
        if session_id:
            matching = [
                marker
                for marker in iter_markers(workspace)
                if marker.get("runtime") == runtime and marker.get("session_id") == session_id
            ]
            if matching:
                first_ts = _parse_marker_ts(matching[0].get("ts"))
                if first_ts is not None:
                    # duration = hook 时刻 − 首条 marker ts（不取末条 marker）。
                    duration_ms = int(
                        (datetime.now(timezone.utc) - first_ts).total_seconds() * 1000
                    )
                if not transcript_path:
                    transcript_path = matching[0].get("transcript_path")

        cfg = TelemetryConfig.resolve(workspace)
        if not cfg.detailed:
            token_source = "none"
            tokens = None
        elif runtime == "claude-code":
            tokens = SessionTokenSummarizer().summarize(transcript_path, budget=None)
            token_source = "claude-transcript" if tokens is not None else "none"
        elif runtime == "codex":
            tokens = CodexRolloutReader().cumulative_tokens(transcript_path)
            # D-26 ③：尾窗 miss 的 truncated 前缀旧值不得上报
            if tokens is not None and getattr(tokens, "truncated", False):
                try:
                    sys.stderr.write("warning: codex rollout truncated\n")
                except Exception:
                    pass
                tokens = None
                token_source = "none"
            else:
                token_source = "codex-transcript" if tokens is not None else "none"
        elif runtime == "grok-build":
            grok_path = transcript_path or (fallback.get("session_file") if fallback else None)
            tokens = GrokTokenSummarizer().summarize(grok_path, budget=None)
            token_source = "grok-transcript" if tokens is not None else "none"
        elif runtime == "kimi-code":
            kimi_path = transcript_path or (fallback.get("session_file") if fallback else None)
            tokens = KimiTokenSummarizer().summarize(
                kimi_path, budget=None, workspace=str(workspace), marker=fallback
            )
            token_source = "kimi-transcript" if tokens is not None else "none"
        elif runtime == "pi":
            pi_path = transcript_path or (fallback.get("session_file") if fallback else None)
            tokens = PiTokenSummarizer().summarize(pi_path, budget=None)
            token_source = "pi-transcript" if tokens is not None else "none"
        elif runtime == "antigravity":
            token_source = "antigravity-transcript" if transcript_path else "none"
            tokens = None
        else:
            token_source = "none"
            tokens = None

        if hook_event == "Stop":
            # 旧 hook 仍每轮触发。只记遥测，调用方不得据此注销监听。
            ended_trigger = "stop-hook"
        else:
            # DR-25：codex 改挂 SessionEnd 后与其它端同为每会话一条，沿用 schema 既有枚举。
            ended_trigger = "session-end-hook"
        payload: dict[str, Any] = {
            "trigger": ended_trigger,
            "reason": reason,
            "duration_ms": duration_ms,
            "token_source": token_source,
            "tokens": tokens,
        }
        # 014 R22, 015 R3：session_ended 扩墙钟解释字段（tool_calls/subagent_spawns/
        # context_pct_max/compactions/env_events/turns/cost_usd/api_duration_ms），
        # 宿主会话文件 best-effort 计数、不可得为 null；fail-safe，解析异常不影响既有五键。
        # 详细模式关闭时跳过大文件 IO（R9, D-1）。
        if cfg.detailed:
            try:
                extended = session_ended_extended_stats(
                    runtime, session_id, transcript_path, workspace, marker=fallback
                )
                if isinstance(extended, dict):
                    payload.update(extended)
            except Exception:
                pass
        _emit_session_event(
            runtime,
            workspace,
            "session_ended",
            payload,
            {
                "name": runtime,
                "session_id": session_id,
                "attribution": "attributed" if session_id else "unattributed",
            },
            command="runtime session-end",
        )
    finally:
        if ending is not None:
            try:
                _finalize_session_end(runtime, workspace, ending.session_id, reason, ending)
            except Exception:
                pass


def command_session_mark(args: argparse.Namespace) -> int:
    """内部命令：宿主回调（pi extension 等）写入一条 session marker 并 enqueue session_started。fail-safe，始终 exit 0。"""
    workspace = Path(args.target or ".")
    warning = append_session_marker(
        runtime=args.runtime,
        workspace=workspace,
        session_id=args.session_id,
        transcript_path=args.transcript_path,
        session_file=args.session_file,
        source="host-callback",
    )
    if warning is None:
        # enqueue-only；emit_session_event_safe 内部吞异常，绝不阻断 host-callback 主流程。
        # 本命令自 session_registry 移入 runtime（平台层）：registry 只做 marker 读写，
        # 不反向依赖 telemetry，消解 telemetry → spans → registry → telemetry 静态 import 环。
        _emit_session_event(
            args.runtime,
            workspace,
            "session_started",
            {
                "trigger": "session-mark",
                "transcript_available": bool(args.transcript_path),
            },
            {"name": args.runtime, "session_id": args.session_id},
            command="session-mark",
        )
    data: dict[str, Any] = {
        "status": "ok",
        "message": "session marker recorded" if warning is None else warning,
    }
    if warning:
        data["warning"] = warning
    _register_marked_session(args, workspace)
    emit(data, args.format)
    return 0


def _register_marked_session(args: argparse.Namespace, workspace: Path) -> None:
    """Pi 启动回调只走 session-mark。同步补会话索引，供后续身份解析使用。"""

    session_id = getattr(args, "session_id", None)
    if not isinstance(session_id, str) or not session_id.strip():
        return
    runtime = getattr(args, "runtime", None)
    if not isinstance(runtime, str) or not runtime.strip():
        return
    pid = getattr(args, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        pid = os.getppid()
    try:
        from datetime import datetime, timezone

        written = SessionIndex(home=Path.home()).record_start(
            SessionRef(runtime, session_id.strip(), pid, str(workspace)),
            process_start_time(pid),
            "session-mark",
            datetime.now(timezone.utc),
            lock_timeout=_HOOK_LOCK_SEC,
        )
        if written is not None:
            resume_ended_watches(Path.home(), runtime, session_id.strip(), True)
    except Exception:
        return


def command_runtime_session_end(args: argparse.Namespace) -> int:
    """SessionEnd hook 内部命令（接口文档 §4.4 / 设计文档 §4.10 点位 #14）。

    enqueue-only 零 HTTP，恒 exit 0；enabled 门控在 emit_session_event_safe 内部，
    disabled 时只跳过 enqueue，hook 输出照旧。
    """
    runtime = getattr(args, "runtime", None) or "unknown"
    workspace = Path(getattr(args, "target", ".") or ".")
    hook_event = getattr(args, "hook_event", None) or "SessionEnd"
    explicit = getattr(args, "session_id", None)
    try:
        _collect_session_ended(runtime, workspace, hook_event, explicit)
    except Exception:
        pass  # 兜底防线：enqueue 失败绝不影响 hook 输出 / exit 0
    envelope = _session_end_hook_envelope(hook_event, runtime)
    # DR-25：空 stdout 只属于 ``--hook-json`` 且宿主不要 envelope 的路径（codex SessionEnd）。
    # 兼容回放与非 hook 调用不带 ``--hook-json``，仍打印 ``{"status":"ok"}``。
    if getattr(args, "hook_json", False):
        if envelope is not None:
            print(json.dumps(envelope, ensure_ascii=False))
        return 0
    print(json.dumps({"status": "ok"}, ensure_ascii=False))
    return 0


def command_runtime_turn_start(args: argparse.Namespace) -> int:
    """UserPromptSubmit：注入队列摘录。任何异常退出码 0，按该端无事件形态输出。"""
    runtime = str(getattr(args, "runtime", "") or "")
    hook_json = bool(getattr(args, "hook_json", False))
    if runtime == "grok-build" and getattr(args, "format", "text") != "text":
        hook_json = True
    text = ""
    seq = 0
    session_id = ""
    queue_id = ""
    reservation_id = ""
    try:
        text, seq, session_id, queue_id, reservation_id = _collect_turn_start_injection(runtime)
    except Exception:
        text, seq, session_id, queue_id, reservation_id = "", 0, "", "", ""
    emitted = False
    try:
        _emit_turn_start(runtime, text, hook_json)
        emitted = True
    except Exception:
        emitted = False
    if seq > 0 and session_id:
        try:
            _hook_watch_service().confirm_turn_inject(
                runtime,
                session_id,
                seq,
                emitted and bool(text),
                queue_id,
                reservation_id,
            )
        except Exception:
            pass
    return 0


def render_legacy_codex_agent(metadata: dict[str, Any], body: str) -> str:
    name = metadata["name"]
    description = metadata["description"]
    model = metadata.get("codex-model") or metadata.get("model")
    effort = "medium" if model == "sonnet" else "high"
    sanitized = body
    for plugin_name in (RUNTIME_PLUGIN_NAME, *LEGACY_RUNTIME_PLUGIN_NAMES):
        for spacing in ("`Agent`调用", "`Agent` 调用"):
            sanitized = sanitized.replace(
                f"如果你是 Claude Code session 主 agent 且用户明确要求升级，可以使用 {spacing} "
                f"`{plugin_name}:feature-dev-manager-agent`；如果你是被调用的 subagent，"
                "不要再派生 subagent，只返回上述结构。",
                "如果需要升级标准流程，返回上述结构并停止；不要在 Codex worker agent 内继续派生 subagent。",
            )
    instructions = MIGRATION_CODEX_RUNTIME_PREAMBLE.rstrip() + "\n\n" + sanitized.lstrip()
    if not instructions.endswith("\n"):
        instructions += "\n"
    return "\n".join(
        [
            f"name = {json.dumps(name, ensure_ascii=False)}",
            f"description = {json.dumps(description, ensure_ascii=False)}",
            f"model_reasoning_effort = {json.dumps(effort)}",
            "developer_instructions = '''\n" + instructions + "'''",
        ]
    ) + "\n"


def bundled_skills_source() -> Path | None:
    candidates = [
        current_cli_path().parent / "skills",
        current_cli_path().parent.parent / "packages" / "skills",
    ]
    return next((candidate for candidate in candidates if candidate.is_dir()), None)


def bundled_commands_source() -> Path | None:
    candidates = [
        current_cli_path().parent / "commands",
        current_cli_path().parent.parent / "commands",
    ]
    return next((candidate for candidate in candidates if candidate.is_dir()), None)


def bundled_kimi_adapter_source() -> Path | None:
    candidates = [
        current_cli_path().parent / "runtime-templates" / "kimi-code" / "AGENTS.md",
        current_cli_path().parent.parent / "references" / "assets" / "workspace-template" / ".kimi-code" / "AGENTS.md",
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def probe_cli_version(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.fullmatch(r"devkit-flow\s+(\d+\.\d+\.\d+)\s*", result.stdout or "")
    return match.group(1) if result.returncode == 0 and match else None


def is_generated_project_cli(path: Path) -> bool:
    if extract_embedded_version(path) is None:
        return False
    try:
        return NPM_PACKAGE_NAME.encode() in path.read_bytes()[:16384]
    except OSError:
        return False


def historical_runtime_state_candidates(workspace: Path) -> list[dict[str, Any]]:
    """Read only cryptographic evidence written by legacy DevKit sync.

    A state entry is useful only for a narrowly allowlisted historical output path.
    The recorded digest still has to match the current file, so edits made after
    the sync are always preserved.
    """
    state_path = workspace / ".devkit" / "runtime-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    runtimes = state.get("runtimes", {}) if isinstance(state, dict) else {}
    if not isinstance(runtimes, dict):
        return []
    candidates: list[dict[str, Any]] = []
    for recorded_runtime, runtime_state in runtimes.items():
        if recorded_runtime not in RUNTIME_NAMES or not isinstance(runtime_state, dict):
            continue
        files = runtime_state.get("files", {})
        if not isinstance(files, dict):
            continue
        for raw_relative, digest in files.items():
            if not isinstance(raw_relative, str) or not isinstance(digest, str):
                continue
            relative = Path(raw_relative)
            if relative.is_absolute() or ".." in relative.parts or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                continue
            parts = relative.parts
            required: tuple[str, ...] | None = None
            if (
                recorded_runtime == "codex"
                and len(parts) == 3
                and parts[:2] == (".codex", "agents")
                and relative.suffix == ".toml"
            ):
                required = ("codex",)
            elif (
                recorded_runtime == "claude-code"
                and len(parts) == 3
                and parts[:2] == (".claude", "commands")
                and relative.suffix == ".md"
            ):
                required = ("claude-code",)
            elif recorded_runtime == "kimi-code" and parts == (".kimi-code", "AGENTS.md"):
                required = ("kimi-code",)
            elif recorded_runtime in {"codex", "kimi-code"} and len(parts) >= 4 and parts[:2] == (".agents", "skills"):
                required = ("codex", "kimi-code")
            if required is None or not (workspace / relative).is_file():
                continue
            candidates.append(
                {
                    "path": relative,
                    "expected_digests": {digest.lower()},
                    "runtime_state_digests": {digest.lower()},
                    "runtimes": required,
                    "evidence": {"runtime-state"},
                }
            )
    return candidates


def legacy_generated_files(workspace: Path) -> list[dict[str, Any]]:
    candidates: dict[Path, dict[str, Any]] = {}

    def add_candidate(relative: Path, content: bytes, runtimes: tuple[str, ...]) -> None:
        entry = candidates.setdefault(
            relative,
            {
                "path": relative,
                "expected_digests": set(),
                "runtime_state_digests": set(),
                "runtimes": runtimes,
                "evidence": set(),
            },
        )
        entry["expected_digests"].add(hashlib.sha256(content).hexdigest())
        entry["evidence"].add("canonical")

    agents_dir, _ = resolve_agents_source()
    if agents_dir is not None:
        for source_path in sorted(agents_dir.glob("*.md")):
            if source_path.name == "README.md" or source_path.name in AGENT_DATA_PAYLOAD_FILES:
                continue
            metadata, body = parse_agent_source(source_path)
            if metadata.get("name") == "feature-dev-manager-agent":
                continue
            relative = Path(".codex") / "agents" / f"{source_path.stem}.toml"
            add_candidate(relative, render_legacy_codex_agent(metadata, body).encode(), ("codex",))
    skills_dir = bundled_skills_source()
    if skills_dir is not None:
        for source in sorted(path for path in skills_dir.rglob("*") if path.is_file()):
            if source.parent == skills_dir and source.name in {"README.md", "VERSION"}:
                continue
            relative = Path(".agents") / "skills" / source.relative_to(skills_dir)
            add_candidate(relative, source.read_bytes(), ("codex", "kimi-code"))
    commands_dir = bundled_commands_source()
    if commands_dir is not None:
        for source in sorted(commands_dir.glob("*.md")):
            add_candidate(Path(".claude") / "commands" / source.name, source.read_bytes(), ("claude-code",))
    kimi_adapter = bundled_kimi_adapter_source()
    if kimi_adapter is not None:
        add_candidate(Path(".kimi-code") / "AGENTS.md", kimi_adapter.read_bytes(), ("kimi-code",))
    for historical in historical_runtime_state_candidates(workspace):
        relative = historical["path"]
        entry = candidates.setdefault(relative, historical)
        if entry is not historical:
            entry["expected_digests"].update(historical["expected_digests"])
            entry["runtime_state_digests"].update(historical["runtime_state_digests"])
            entry["evidence"].update(historical["evidence"])
    return [candidate for candidate in candidates.values() if (workspace / candidate["path"]).is_file()]


def prune_empty_generated_parents(path: Path, workspace: Path) -> None:
    allowed_roots = {
        workspace / ".codex",
        workspace / ".claude",
        workspace / ".kimi-code",
        workspace / ".agents",
        workspace / ".devkit" / "bin",
    }
    current = path.parent
    while current != workspace and any(current == root or root in current.parents for root in allowed_roots):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def migration_confirmation(args: argparse.Namespace) -> bool:
    if not args.apply:
        return False
    if args.yes:
        return True
    if not sys.stdin.isatty():
        return False
    response = input("Delete only verified DevKit-generated legacy files? [y/N] ").strip().lower()
    return response in {"y", "yes"}


def command_migrate_legacy_workspace(args: argparse.Namespace) -> int:
    if args.runtime == "pi":
        return command_migrate_legacy_workspace_pi(args)
    workspace = Path(args.target).expanduser().resolve()
    selected = set(RUNTIME_NAMES if args.runtime == "all" else (args.runtime,))
    generated = legacy_generated_files(workspace)
    required = set(selected)
    for candidate in generated:
        required.update(candidate["runtimes"])
    plugin_checks = {runtime: runtime_plugin_check(runtime) for runtime in sorted(required)}
    confirmed = migration_confirmation(args)
    actions: list[dict[str, Any]] = []

    project_cli = workspace / DEVKIT_CLI_RELATIVE_PATH
    if project_cli.is_file():
        relative = DEVKIT_CLI_RELATIVE_PATH.as_posix()
        global_cli = find_global_cli()
        global_version = probe_cli_version(global_cli)
        project_version = extract_embedded_version(project_cli)
        eligible = bool(
            is_generated_project_cli(project_cli)
            and global_version
            and project_version
            and version_tuple(global_version) >= version_tuple(project_version)
            and global_cli is not None
            and global_cli.resolve() != project_cli.resolve()
        )
        if not eligible:
            actions.append(
                {
                    "path": relative,
                    "status": "blocked",
                    "reason": "cannot prove this is a generated project CLI with a working global CLI at least as new",
                }
            )
        elif not args.apply:
            actions.append({"path": relative, "status": "ready", "reason": f"global CLI {global_version} is ready"})
        elif not confirmed:
            actions.append({"path": relative, "status": "blocked", "reason": "deletion was not confirmed"})
        else:
            project_cli.unlink()
            prune_empty_generated_parents(project_cli, workspace)
            actions.append({"path": relative, "status": "deleted", "reason": f"global CLI {global_version} is ready"})

    for candidate in generated:
        path = workspace / candidate["path"]
        relative = candidate["path"].as_posix()
        current_digest = sha256_file(path)
        if current_digest not in candidate["expected_digests"]:
            actions.append({"path": relative, "status": "preserved", "reason": "content differs from the DevKit-generated file"})
            continue
        evidence = (
            "runtime-state digest"
            if current_digest in candidate.get("runtime_state_digests", set())
            else "canonical content"
        )
        missing = [runtime for runtime in candidate["runtimes"] if not plugin_checks[runtime]["active"]]
        if missing:
            required_names = ", ".join(candidate["runtimes"])
            actions.append(
                {
                    "path": relative,
                    "status": "blocked",
                    "reason": f"requires active plugins for all consumers: {required_names}",
                }
            )
            continue
        if not args.apply:
            actions.append({"path": relative, "status": "ready", "reason": f"exact generated content ({evidence}) and plugins active"})
            continue
        if not confirmed:
            actions.append({"path": relative, "status": "blocked", "reason": "deletion was not confirmed"})
            continue
        path.unlink()
        prune_empty_generated_parents(path, workspace)
        actions.append({"path": relative, "status": "deleted", "reason": f"exact generated content ({evidence}) and plugins active"})

    for name in ("AGENTS.md", "CLAUDE.md"):
        path = workspace / name
        if not path.is_file():
            continue
        current = path.read_text(encoding="utf-8")
        legacy_texts = {
            LEGACY_WORKSPACE_ROUTING_TEMPLATE.format(filename=name),
            PRE_WORKFLOW_LEGACY_WORKSPACE_ROUTING_TEMPLATE.format(filename=name),
        }
        if current not in legacy_texts:
            continue
        if args.apply and confirmed:
            atomic_write_text(path, WORKSPACE_ROUTING_TEMPLATE.format(filename=name))
            actions.append({"path": name, "status": "updated", "reason": "migrated the exact legacy routing template"})
        else:
            actions.append({"path": name, "status": "ready", "reason": "exact legacy routing template"})

    blocked = [action for action in actions if action["status"] == "blocked"]
    changed = [action for action in actions if action["status"] in {"deleted", "updated"}]
    status = "issues" if blocked else ("updated" if changed else "ready")
    message = f"legacy workspace migration: {status} ({len(changed)} changed, {len(blocked)} blocked)"
    emit(
        {
            "status": status,
            "message": message,
            "target": str(workspace),
            "runtime": args.runtime,
            "apply": args.apply,
            "confirmed": confirmed,
            "plugin_checks": plugin_checks,
            "actions": actions,
        },
        args.format,
    )
    return 1 if args.apply and blocked else 0


def runtime_plugin_source(runtime: str) -> Path:
    cli_dir = current_cli_path().parent
    candidates = [
        cli_dir / "runtime-plugins" / runtime / RUNTIME_PLUGIN_NAME,
        cli_dir / "runtimes" / runtime / RUNTIME_PLUGIN_NAME,
        cli_dir.parent / "runtime-plugins" / runtime / RUNTIME_PLUGIN_NAME,
    ]
    source = next((candidate for candidate in candidates if candidate.is_dir()), None)
    if source is None:
        tried = ", ".join(str(candidate) for candidate in candidates)
        raise DevkitFlowError(
            f"isolated {runtime} plugin payload is missing; tried {tried}. "
            "Reinstall devkit-flow or run `npm run runtime:build` in the source repository."
        )
    return source


def runtime_marketplace_root(runtime: str) -> Path:
    root = Path.home() / ".config" / "devkit" / "runtimes" / runtime / "marketplace"
    # Claude identifies a local marketplace by both its registered source path and
    # manifest name. Reusing the legacy source path prevents a renamed marketplace
    # from being registered, even after the manifest content changes.
    if runtime == "claude-code":
        return root / CLAUDE_MARKETPLACE_NAME
    return root


def codex_local_marketplace_manifest() -> dict[str, Any]:
    return {
        "name": CODEX_MARKETPLACE_NAME,
        "interface": {"displayName": "Devkit Transaction DevKit"},
        "plugins": [
            {
                "name": RUNTIME_PLUGIN_NAME,
                "source": {"source": "local", "path": f"./plugins/{RUNTIME_PLUGIN_NAME}"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL", "products": ["CODEX"]},
                "category": "Developer Tools",
            }
        ],
    }


def claude_local_marketplace_manifest() -> dict[str, Any]:
    return {
        "name": CLAUDE_MARKETPLACE_NAME,
        "owner": {"name": "Devkit Transaction DevKit"},
        "metadata": {
            "description": "CLI-managed marketplace for the Devkit Transaction DevKit plugin.",
            "version": VERSION,
        },
        "plugins": [
            {
                "name": RUNTIME_PLUGIN_NAME,
                "source": f"./plugins/{RUNTIME_PLUGIN_NAME}",
                "description": "Expose Devkit transaction development agents and skills as a Claude Code plugin.",
                "version": VERSION,
            }
        ],
    }


def materialize_runtime_marketplace(runtime: str) -> dict[str, Any]:
    source = runtime_plugin_source(runtime)
    target = runtime_marketplace_root(runtime)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{runtime}-marketplace.", dir=target.parent))
    try:
        shutil.copytree(source, staging / "plugins" / RUNTIME_PLUGIN_NAME)
        if runtime == "codex":
            manifest_path = staging / ".agents" / "plugins" / "marketplace.json"
            manifest = codex_local_marketplace_manifest()
        elif runtime == "claude-code":
            manifest_path = staging / ".claude-plugin" / "marketplace.json"
            manifest = claude_local_marketplace_manifest()
        else:
            raise DevkitFlowError(f"local marketplace is unsupported for runtime: {runtime}")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        replace_directory(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    final_manifest = (
        target / ".agents" / "plugins" / "marketplace.json"
        if runtime == "codex"
        else target / ".claude-plugin" / "marketplace.json"
    )
    return {
        "runtime": runtime,
        "root": str(target),
        "manifest": str(final_manifest),
        "plugin": str(target / "plugins" / RUNTIME_PLUGIN_NAME),
        "version": VERSION,
    }


def inspect_owned_legacy_claude_source(
    source: Path,
    marketplace_name: str,
    *,
    flat_layout: bool,
) -> tuple[list[Path] | None, str | None]:
    """Prove a legacy source is an intact DevKit-generated marketplace before deletion."""
    if source.is_symlink():
        return None, "marketplace source is a symlink"
    manifest_path = source / ".claude-plugin" / "marketplace.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None, "missing regular marketplace manifest"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, f"cannot read marketplace manifest: {compact_error(error)}"
    if not isinstance(manifest, dict) or manifest.get("name") != marketplace_name:
        return None, "marketplace manifest identity does not match the legacy source"
    plugins = manifest.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        return None, "marketplace manifest has no plugin ownership evidence"
    allowed_names = {RUNTIME_PLUGIN_NAME, *LEGACY_RUNTIME_PLUGIN_NAMES}
    plugin_names = {
        str(entry.get("name", "")).strip()
        for entry in plugins
        if isinstance(entry, dict)
    }
    unknown_names = sorted(name for name in plugin_names if name not in allowed_names)
    if unknown_names:
        return None, f"marketplace contains non-DevKit plugins: {', '.join(unknown_names)}"
    if len(plugin_names) != len(plugins) or not plugin_names:
        return None, "marketplace contains malformed plugin entries"
    if not flat_layout:
        try:
            top_level = {path.name for path in source.iterdir()}
        except OSError as error:
            return None, f"cannot inspect marketplace source: {compact_error(error)}"
        unexpected = sorted(top_level - {".claude-plugin", "plugins"})
        if unexpected:
            return None, f"marketplace source contains unowned top-level content: {', '.join(unexpected)}"
    manifest_dir = source / ".claude-plugin"
    try:
        manifest_entries = {path.name for path in manifest_dir.iterdir()}
    except OSError as error:
        return None, f"cannot inspect marketplace metadata: {compact_error(error)}"
    if manifest_entries != {"marketplace.json"}:
        return None, "marketplace metadata contains unowned content"
    plugins_dir = source / "plugins"
    if not plugins_dir.is_dir() or plugins_dir.is_symlink():
        return None, "missing regular marketplace plugins directory"
    try:
        installed_names = {path.name for path in plugins_dir.iterdir()}
    except OSError as error:
        return None, f"cannot inspect marketplace plugins: {compact_error(error)}"
    if installed_names != plugin_names:
        return None, "marketplace plugin directories do not match its manifest"
    owned_plugin_roots: list[Path] = []
    for entry in plugins:
        assert isinstance(entry, dict)
        plugin_name = str(entry.get("name", "")).strip()
        if entry.get("source") != f"./plugins/{plugin_name}":
            return None, f"plugin source is not the DevKit-managed path: {plugin_name}"
        plugin_root = plugins_dir / plugin_name
        if plugin_root.is_symlink():
            return None, f"plugin source is a symlink: {plugin_name}"
        plugin_manifest = plugin_root / ".claude-plugin" / "plugin.json"
        generated_marker = plugin_root / ".devkit-generated.json"
        try:
            plugin_data = json.loads(plugin_manifest.read_text(encoding="utf-8"))
            marker_data = json.loads(generated_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return None, f"plugin ownership evidence is unreadable for {plugin_name}: {compact_error(error)}"
        if not isinstance(plugin_data, dict) or plugin_data.get("name") != plugin_name:
            return None, f"plugin manifest identity mismatch: {plugin_name}"
        if not isinstance(marker_data, dict) or marker_data.get("generator") != "devkit-flow":
            return None, f"plugin is not marked as DevKit-generated: {plugin_name}"
        if marker_data.get("runtime") != "claude-code":
            return None, f"plugin runtime ownership mismatch: {plugin_name}"
        owned_plugin_roots.append(plugin_root)
    return owned_plugin_roots, None


def cleanup_owned_legacy_claude_sources() -> tuple[list[str], list[dict[str, str]]]:
    """Remove only proven CLI-owned legacy source layouts after the new plugin is verified."""
    base = Path.home() / ".config" / "devkit" / "runtimes" / "claude-code" / "marketplace"
    candidates: list[tuple[Path, str, bool]] = [
        (base / marketplace_name, marketplace_name, False)
        for marketplace_name in LEGACY_CLAUDE_MARKETPLACE_NAMES
    ]
    flat_manifest = base / ".claude-plugin" / "marketplace.json"
    if flat_manifest.is_file():
        try:
            flat_data = json.loads(flat_manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            flat_data = None
        flat_name = str(flat_data.get("name", "")) if isinstance(flat_data, dict) else ""
        if flat_name in LEGACY_CLAUDE_MARKETPLACE_NAMES:
            candidates.append((base, flat_name, True))
    removed: list[str] = []
    preserved: list[dict[str, str]] = []
    for source, marketplace_name, flat_layout in candidates:
        if not source.exists():
            continue
        owned_plugin_roots, reason = inspect_owned_legacy_claude_source(
            source, marketplace_name, flat_layout=flat_layout
        )
        if owned_plugin_roots is None:
            preserved.append({"path": str(source), "reason": reason or "ownership is unproven"})
            continue
        try:
            if flat_layout:
                for plugin_root in owned_plugin_roots:
                    shutil.rmtree(plugin_root)
                    removed.append(str(plugin_root))
                plugins_dir = source / "plugins"
                if plugins_dir.is_dir() and not any(plugins_dir.iterdir()):
                    plugins_dir.rmdir()
                manifest_dir = source / ".claude-plugin"
                shutil.rmtree(manifest_dir)
                removed.append(str(manifest_dir))
            else:
                shutil.rmtree(source)
                removed.append(str(source))
        except OSError as error:
            preserved.append(
                {"path": str(source), "reason": f"failed to remove owned source: {compact_error(error)}"}
            )
    return removed, preserved


def claude_runtime_update() -> dict[str, Any]:
    """claude-code：先物化 CLI 携带的本地 marketplace，再由 Claude 安装/刷新缓存。"""
    result: dict[str, Any] = {"runtime": "claude-code", "action": "none", "steps": []}
    if not shutil.which("claude"):
        return {**result, "status": "error", "message": "claude CLI is not on PATH; skipped"}
    try:
        marketplace = materialize_runtime_marketplace("claude-code")
    except (OSError, DevkitFlowError) as error:
        return {**result, "status": "error", "message": f"cannot materialize CLI-managed plugin: {compact_error(error)}"}
    result["marketplace"] = marketplace
    steps: list[dict[str, Any]] = result["steps"]
    try:
        output = run_runtime_step(steps, ["claude", "plugin", "marketplace", "list"])
        before_output = run_runtime_step(steps, ["claude", "plugin", "list", "--json"])
        before_entries = parse_claude_plugin_list(before_output)
        current_entries = claude_plugin_selector_matches(
            before_entries, RUNTIME_PLUGIN_NAME, CLAUDE_MARKETPLACE_NAME
        )
        current_user_entries = [
            entry for entry in current_entries if str(entry.get("scope", "user")) == "user"
        ]
        current_scopes = sorted(
            {str(entry.get("scope", "user")).strip() or "user" for entry in current_entries}
        )
        legacy_entries = legacy_claude_plugin_matches(before_entries)
        legacy_marketplaces = [
            marketplace_name
            for marketplace_name in LEGACY_CLAUDE_MARKETPLACE_NAMES
            if claude_marketplace_is_listed(output, marketplace_name)
        ]
        if not claude_marketplace_is_listed(output, CLAUDE_MARKETPLACE_NAME):
            run_runtime_step(steps, ["claude", "plugin", "marketplace", "add", marketplace["manifest"]])
        else:
            run_runtime_step(
                steps, ["claude", "plugin", "marketplace", "update", CLAUDE_MARKETPLACE_NAME]
            )
        for scope in current_scopes:
            run_runtime_step(
                steps,
                ["claude", "plugin", "update", "-s", scope, f"{RUNTIME_PLUGIN_NAME}@{CLAUDE_MARKETPLACE_NAME}"],
            )
        if not current_user_entries:
            run_runtime_step(
                steps,
                ["claude", "plugin", "install", f"{RUNTIME_PLUGIN_NAME}@{CLAUDE_MARKETPLACE_NAME}", "--scope", "user"],
            )
        verified_output = run_runtime_step(steps, ["claude", "plugin", "list", "--json"])
        verified_all_entries = parse_claude_plugin_list(verified_output)
        verified_current_entries = [
            entry
            for entry in claude_plugin_selector_matches(
                verified_all_entries, RUNTIME_PLUGIN_NAME, CLAUDE_MARKETPLACE_NAME
            )
            if entry.get("enabled") is True
        ]
        verified_user_entries = [
            entry
            for entry in verified_current_entries
            if str(entry.get("scope", "user")) == "user" and str(entry.get("version", "")) == VERSION
        ]
        stale_entries = [
            entry for entry in verified_current_entries if str(entry.get("version", "")) != VERSION
        ]
        if not verified_user_entries or stale_entries:
            raise DevkitFlowError(
                f"Claude plugin verification failed: expected every enabled {RUNTIME_PLUGIN_NAME} entry "
                f"at {VERSION}, including user scope"
            )
        remaining_legacy_entries = legacy_claude_plugin_matches(verified_all_entries)
        removed_legacy = [
            str(entry.get("id", "")).split("@", 1)[0]
            for entry in legacy_entries
            if str(entry.get("id", "")).strip()
        ]
        for entry in remaining_legacy_entries:
            plugin_id = str(entry.get("id", "")).strip()
            if not plugin_id:
                continue
            scope = str(entry.get("scope", "user")).strip() or "user"
            run_runtime_step(
                steps,
                ["claude", "plugin", "uninstall", plugin_id, "--scope", scope, "-y"],
            )
            removed_legacy.append(plugin_id.split("@", 1)[0])
        for marketplace_name in legacy_marketplaces:
            run_runtime_step(
                steps, ["claude", "plugin", "marketplace", "remove", marketplace_name]
            )
        removed_legacy_sources, preserved_legacy_sources = cleanup_owned_legacy_claude_sources()
    except RuntimeStepFailed as failure:
        return {**result, "status": "error", "message": runtime_step_failure_message(failure)}
    except DevkitFlowError as error:
        return {**result, "status": "error", "message": str(error)}
    action = (
        "migrated"
        if legacy_entries or legacy_marketplaces
        else ("updated" if current_user_entries else "installed")
    )
    return {
        **result,
        "status": "ok",
        "action": action,
        "migrated_from": sorted(set(removed_legacy)),
        "removed_legacy_sources": removed_legacy_sources,
        "preserved_legacy_sources": preserved_legacy_sources,
        "message": (
            f"{RUNTIME_PLUGIN_NAME} {action} (marketplace {CLAUDE_MARKETPLACE_NAME}); "
            f"removed {len(removed_legacy_sources)} owned legacy source path(s), "
            f"preserved {len(preserved_legacy_sources)} unverified source path(s); "
            "end the old Claude Code session and start a new session to load it"
        ),
    }


def codex_runtime_update() -> dict[str, Any]:
    """Install the plugin, then register its workers in Codex's native role config."""
    result: dict[str, Any] = {"runtime": "codex", "action": "none", "steps": []}
    if not shutil.which("codex"):
        return {**result, "status": "error", "message": "codex CLI is not on PATH; skipped"}
    try:
        registry = codex_agent_registry(runtime_plugin_source("codex"))
        registry.preflight()
    except (OSError, ValueError, ImportError, DevkitFlowError) as error:
        return {**result, "status": "error", "message": f"Codex agent registration preflight failed: {compact_error(error)}"}
    try:
        marketplace = materialize_runtime_marketplace("codex")
    except (OSError, DevkitFlowError) as error:
        return {**result, "status": "error", "message": f"cannot materialize CLI-managed plugin: {compact_error(error)}"}
    result["marketplace"] = marketplace
    steps: list[dict[str, Any]] = result["steps"]
    try:
        marketplace_output = run_runtime_step(steps, ["codex", "plugin", "marketplace", "list"])
        plugin_output = run_runtime_step(steps, ["codex", "plugin", "list"])
        existing_plugin = codex_plugin_state(plugin_output)
        legacy_plugins = {
            legacy_name: codex_plugin_state(plugin_output, legacy_name)
            for legacy_name in LEGACY_RUNTIME_PLUGIN_NAMES
        }
        if existing_plugin["installed"]:
            run_runtime_step(
                steps, ["codex", "plugin", "remove", f"{RUNTIME_PLUGIN_NAME}@{CODEX_MARKETPLACE_NAME}"]
            )
        if CODEX_MARKETPLACE_NAME in marketplace_output:
            run_runtime_step(steps, ["codex", "plugin", "marketplace", "remove", CODEX_MARKETPLACE_NAME])
        run_runtime_step(steps, ["codex", "plugin", "marketplace", "add", marketplace["root"]])
        run_runtime_step(steps, ["codex", "plugin", "add", f"{RUNTIME_PLUGIN_NAME}@{CODEX_MARKETPLACE_NAME}"])
        verified_output = run_runtime_step(steps, ["codex", "plugin", "list"])
    except RuntimeStepFailed as failure:
        return {**result, "status": "error", "message": runtime_step_failure_message(failure)}
    verified = codex_plugin_state(verified_output)
    if not verified["enabled"] or verified["version"] != VERSION:
        installed_version = verified["version"] or "missing"
        return {
            **result,
            "status": "error",
            "action": "reinstalled" if existing_plugin["installed"] else "installed",
            "version": installed_version,
            "expected_version": VERSION,
            "message": f"Codex plugin verification failed: installed {installed_version}, expected enabled {VERSION}",
        }
    removed_legacy: list[str] = []
    try:
        for legacy_name, legacy_plugin in legacy_plugins.items():
            if not legacy_plugin["installed"]:
                continue
            run_runtime_step(
                steps, ["codex", "plugin", "remove", f"{legacy_name}@{CODEX_MARKETPLACE_NAME}"]
            )
            removed_legacy.append(legacy_name)
    except RuntimeStepFailed as failure:
        return {
            **result,
            "status": "error",
            "action": "installed-new-preserved-legacy",
            "version": verified["version"],
            "expected_version": VERSION,
            "message": runtime_step_failure_message(failure),
        }
    try:
        # Re-read config after Codex's own plugin commands. Immutable role generations
        # preserve the previous workers if this atomic registration commit fails.
        registration = registry.install()
        if not registration.get("ready"):
            registry.rollback()
            raise DevkitFlowError(registration.get("message", "custom agent verification failed"))
    except (OSError, ValueError, DevkitFlowError) as error:
        return {
            **result, "status": "error", "action": "plugin-installed-agents-not-ready",
            "version": verified["version"], "expected_version": VERSION,
            "message": f"Codex agent registration failed: {compact_error(error)}",
        }
    action = "migrated" if removed_legacy else ("reinstalled" if existing_plugin["installed"] else "installed")
    return {
        **result,
        "status": "ok",
        "action": action,
        "version": verified["version"],
        "expected_version": VERSION,
        "migrated_from": removed_legacy,
        "agent_registration": registration,
        "message": (
            f"{RUNTIME_PLUGIN_NAME} {action} at {VERSION} (marketplace {CODEX_MARKETPLACE_NAME}); "
            f"{registration.get('role_count', 0)} custom agents registered; "
            "fully exit and restart Codex, then start a new task to load it"
        ),
    }


def kimi_runtime_update() -> dict[str, Any]:
    """kimi-code：渲染并安装 managed 插件（runtime update 与 plugin install 共用同一逻辑）。"""
    try:
        result = install_kimi_plugin()
    except DevkitFlowError as error:
        return {
            "runtime": "kimi-code",
            "status": "error",
            "action": "none",
            "steps": [],
            "message": f"kimi plugin install failed: {error}",
        }
    action = (
        "migrated"
        if result.get("migrated_from")
        else "updated"
        if result.get("installed_json") == "updated"
        else "installed"
    )
    return {
        "runtime": "kimi-code",
        "status": "ok",
        "action": action,
        "migrated_from": result.get("migrated_from", []),
        "steps": [{"command": "devkit-flow plugin install --runtime kimi-code", "ok": True, "exit_code": 0, "output": result["message"]}],
        "message": result["message"],
    }


RUNTIME_UPDATERS = {
    "claude-code": claude_runtime_update,
    "codex": codex_runtime_update,
    "kimi-code": kimi_runtime_update,
}


def runtime_lifecycle_support() -> Path:
    """定位 runtime lifecycle 支撑树（发行包 runtime-support/、源码仓 scripts/ 或 python -m 源码态）。"""
    support = runtime_support_root("runtime_cli", current_cli_path())
    if support is not None:
        return support
    raise DevkitFlowError(
        "runtime lifecycle support payload is missing; reinstall devkit-flow "
        "or run from the source repository"
    )


def runtime_lifecycle_repo_root(support: Path) -> Path:
    for candidate in (support.parent, current_cli_path().parent):
        if (candidate / "runtime-plugins").is_dir():
            return candidate
    raise DevkitFlowError("runtime plugin payload tree is missing")


def runtime_lifecycle_controller(timeout_seconds: int = 10) -> Any:
    support = runtime_lifecycle_support()
    from runtime_cli.composition import ProductionComposition
    from runtime_cli.controller import LifecycleCliController

    repo_root = runtime_lifecycle_repo_root(support)
    schema_dir = repo_root / "schemas"
    if not schema_dir.is_dir():
        schema_dir = support / "schemas"
    registry_path = repo_root / "runtime-registry.json"
    if not registry_path.is_file():
        registry_path = support / "runtime-registry.json"
    composition = ProductionComposition(
        cli_path=current_cli_path(),
        repo_root=repo_root,
        schema_dir=schema_dir,
        registry_path=registry_path,
        projector_catalog_path=support / "fixtures" / "grok-projector" / "catalog.json",
        home_dir=Path.home(),
        state_root=Path.home() / ".config" / "devkit" / "runtime-lifecycle",
        timeout_seconds=timeout_seconds,
    )
    return LifecycleCliController(composition, schema_dir)


def command_runtime_lifecycle(args: argparse.Namespace, operation: str) -> int:
    controller = runtime_lifecycle_controller(getattr(args, "timeout_seconds", 10))
    return controller.run(
        operation=operation,
        runtime=args.runtime,
        fmt=args.format,
        force=getattr(args, "force", False),
        timeout_seconds=getattr(args, "timeout_seconds", 10),
        target_artifact_id=getattr(args, "to", None),
        transaction_id=getattr(args, "transaction_id", None),
    )


def command_runtime_install(args: argparse.Namespace) -> int:
    return command_runtime_lifecycle(args, "install")


def command_runtime_uninstall(args: argparse.Namespace) -> int:
    return command_runtime_lifecycle(args, "uninstall")


def command_runtime_rollback(args: argparse.Namespace) -> int:
    return command_runtime_lifecycle(args, "rollback")


def command_runtime_recover(args: argparse.Namespace) -> int:
    return command_runtime_lifecycle(args, "recover")


def _upgrade_error_codes(child: dict[str, Any]) -> tuple[str, ...]:
    codes: list[str] = []
    error = child.get("error")
    while isinstance(error, dict):
        code = error.get("errorCode")
        if isinstance(code, str) and code:
            codes.append(code)
        error = error.get("cause")
    return tuple(codes)


def _upgrade_child_error_code(child: dict[str, Any]) -> str | None:
    codes = _upgrade_error_codes(child)
    return codes[0] if codes else None


# 016 T-19（T-17 转入）：`upgrade --runtime all` 下 pi 的 registry 预检拒绝
# （R-27，E_ARTIFACT_NOT_FOUND @ stage=preflight）不得因 aggregate 把所有
# preflight 失败标成 hostSupported=false 而被静默计为 skipped。
# 016 T-26（D-72，与 D-67 同口径）：未完成事务的预检拒绝（E_RECOVERY_REQUIRED
# @ stage=preflight）同样阻断——需先显式 recover，不得计为 skipped。
_UPGRADE_PREFLIGHT_BLOCKING_ERROR_CODES = frozenset({"E_ARTIFACT_NOT_FOUND", "E_RECOVERY_REQUIRED"})
UPGRADE_OUTCOME_SUCCESS = "success"
UPGRADE_OUTCOME_SKIPPED = "skipped"
UPGRADE_OUTCOME_FAILED = "failed"
UPGRADE_OUTCOME_PREFLIGHT_FAILED = "preflight-failed"


def _upgrade_child_preflight_failure(child: dict[str, Any]) -> dict[str, Any] | None:
    """沿 error.cause 链找 stage=preflight 且错误码属于阻断集合的一层。"""
    error = child.get("error")
    depth = 0
    while isinstance(error, dict) and depth < 32:
        if (
            error.get("stage") == "preflight"
            and error.get("errorCode") in _UPGRADE_PREFLIGHT_BLOCKING_ERROR_CODES
        ):
            return error
        error = error.get("cause")
        depth += 1
    return None


def _upgrade_child_skippable(child: dict[str, Any]) -> bool:
    """宿主不可用类失败是否可跳过。

    只在 ``upgrade_child_outcome`` 判过 ``_upgrade_child_preflight_failure``
    之后调用（阻断性预检失败优先，不在此重复判定——T-19 P2-4 单一判定点）。
    """
    if child.get("hostSupported") is False:
        return True
    return bool(_UPGRADE_SKIPPABLE_ERROR_CODES.intersection(_upgrade_error_codes(child)))


def upgrade_child_outcome(child: dict[str, Any]) -> str:
    """单个 runtime 子结果的归类：success / skipped / preflight-failed / failed。"""
    status = child.get("status")
    if status == "success":
        return UPGRADE_OUTCOME_SUCCESS
    if _upgrade_child_preflight_failure(child) is not None:
        return UPGRADE_OUTCOME_PREFLIGHT_FAILED
    if _upgrade_child_skippable(child):
        return UPGRADE_OUTCOME_SKIPPED
    if status == "failed" or _upgrade_error_codes(child):
        return UPGRADE_OUTCOME_FAILED
    return UPGRADE_OUTCOME_SUCCESS


def upgrade_runtime_outcomes(results: list[Any]) -> list[dict[str, Any]]:
    """`runtime_outcomes` 字段：每个 runtime 的归类、最内层错误码 / stage / detail。"""
    outcomes: list[dict[str, Any]] = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        outcome = upgrade_child_outcome(raw)
        innermost: dict[str, Any] | None = None
        error = raw.get("error")
        depth = 0
        while isinstance(error, dict) and depth < 32:
            innermost = error
            error = error.get("cause")
            depth += 1
        entry: dict[str, Any] = {
            "runtime": str(raw.get("runtime") or "unknown"),
            "outcome": outcome,
            "errorCode": (innermost or {}).get("errorCode"),
            "stage": (innermost or {}).get("stage"),
            "detail": _upgrade_child_detail(raw) if outcome != UPGRADE_OUTCOME_SUCCESS else None,
        }
        outcomes.append(entry)
    return outcomes


def _upgrade_child_detail(child: dict[str, Any]) -> str:
    """最内层原因优先（016 T-19，R-26 初衷）：沿 error.cause 链取最深一层的
    非空 detail（如 pi 宿主命令诊断），而不是外层 E_OPERATION_ROLLED_BACK 的
    固定文案；整条链都没有 detail 时回退 message / status。"""
    error = child.get("error")
    detail = ""
    depth = 0
    while isinstance(error, dict) and depth < 32:
        candidate = error.get("detail")
        if candidate:
            detail = str(candidate)
        error = error.get("cause")
        depth += 1
    if detail:
        return detail
    return str(child.get("message") or child.get("status") or "")


def classify_upgrade_children(
    results: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """把 lifecycle update 子结果分成成功 / 可跳过 / 阻断失败。"""
    succeeded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        outcome = upgrade_child_outcome(raw)
        if outcome == UPGRADE_OUTCOME_SKIPPED:
            skipped.append(raw)
        elif outcome in (UPGRADE_OUTCOME_FAILED, UPGRADE_OUTCOME_PREFLIGHT_FAILED):
            blocking.append(raw)
        else:
            succeeded.append(raw)
    return succeeded, skipped, blocking


def render_upgrade_message(
    overall: str,
    cli_status: str,
    runtime_document: dict[str, Any],
) -> str:
    lines = [f"upgrade: {overall}; CLI {cli_status}"]
    results = runtime_document.get("results")
    if not isinstance(results, list) or not results:
        extra = runtime_document.get("message")
        error = runtime_document.get("error")
        if extra:
            lines.append(str(extra))
        elif isinstance(error, dict):
            code = error.get("errorCode")
            detail = error.get("detail")
            if code and detail:
                lines.append(f"{code}: {detail}")
            elif detail:
                lines.append(str(detail))
            elif code:
                lines.append(str(code))
        return "\n".join(lines)
    for child in results:
        if not isinstance(child, dict):
            continue
        runtime = str(child.get("runtime") or "unknown")
        detail = _upgrade_child_detail(child)
        outcome = upgrade_child_outcome(child)
        if outcome == UPGRADE_OUTCOME_SUCCESS:
            variant = child.get("resultVariant") or "success"
            suffix = f" — {detail}" if detail else ""
            lines.append(f"- {runtime}: {variant}{suffix}")
        elif outcome == UPGRADE_OUTCOME_SKIPPED:
            lines.append(f"- {runtime}: skipped — {detail}")
        elif outcome == UPGRADE_OUTCOME_PREFLIGHT_FAILED:
            preflight = _upgrade_child_preflight_failure(child) or {}
            code = preflight.get("errorCode") or "E_ARTIFACT_NOT_FOUND"
            lines.append(f"- {runtime}: preflight-failed — {code}: {detail}")
        else:
            lines.append(f"- {runtime}: failed — {detail}")
    return "\n".join(lines)


def command_upgrade_lifecycle(args: argparse.Namespace) -> int:
    """五端 upgrade：CLI 自升级后走同一 canonical check/update 用例。"""
    cli_exit, cli_result = self_update_result(check=args.check)
    if args.check:
        children: list[dict[str, Any]] = []
        if not cli_exit:
            buffer = io.StringIO()
            controller = runtime_lifecycle_controller()
            controller.run(
                operation="check", runtime=args.runtime, fmt="json", stdout=buffer,
            )
            document = json.loads(buffer.getvalue() or "{}")
            raw_children = document.get("results") if isinstance(document, dict) else None
            if isinstance(raw_children, list):
                children = [child for child in raw_children if isinstance(child, dict)]
        active = bool(children) and all(child.get("active") for child in children)
        status = "error" if cli_exit else ("ready" if active else "warning")
        emit(
            {
                "status": status,
                "message": f"upgrade check: {status}; CLI {cli_result.get('status')}",
                "check": True,
                "runtime": args.runtime,
                "cli": cli_result,
                "runtime_checks": children,
            },
            args.format,
        )
        return cli_exit
    if cli_exit:
        emit(
            {
                "status": "error",
                "message": f"upgrade stopped before runtime sync: {cli_result.get('message', 'CLI update failed')}",
                "runtime": args.runtime,
                "cli": cli_result,
                "runtime_update": None,
            },
            args.format,
        )
        return 1
    runtime_exit: int
    runtime_document: dict[str, Any]
    if cli_result.get("status") in {"installed", "updated"}:
        installed_cli = find_global_cli()
        if installed_cli is None:
            runtime_exit = 1
            runtime_document = {
                "status": "failed",
                "message": "npm reported success, but the upgraded global devkit-flow is not runnable",
            }
        else:
            try:
                update_argv = [
                    str(installed_cli), "runtime", "update", "--runtime", args.runtime, "--format", "json",
                ]
                if getattr(args, "force", False):
                    update_argv.append("--force")
                completed = subprocess.run(
                    update_argv,
                    capture_output=True,
                    text=True,
                    timeout=NPM_INSTALL_TIMEOUT_SECONDS,
                )
                runtime_exit = completed.returncode
                runtime_document = json.loads(completed.stdout) if completed.stdout.strip() else {}
                if not isinstance(runtime_document, dict) or not runtime_document:
                    raise ValueError("upgraded CLI returned no runtime update result")
            except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
                runtime_exit = 1
                runtime_document = {
                    "status": "failed",
                    "message": f"cannot run runtime sync from the upgraded CLI: {compact_error(error)}",
                }
    else:
        buffer = io.StringIO()
        controller = runtime_lifecycle_controller()
        runtime_exit = controller.run(
            operation="update", runtime=args.runtime, fmt="json",
            force=getattr(args, "force", False), stdout=buffer,
        )
        runtime_document = json.loads(buffer.getvalue())
    if not isinstance(runtime_document, dict):
        runtime_document = {
            "status": "failed",
            "message": "runtime update returned an unexpected document",
        }
        runtime_exit = 1
    results = runtime_document.get("results")
    if args.runtime == "all" and isinstance(results, list):
        _succeeded, skipped, blocking = classify_upgrade_children(results)
        if blocking:
            overall = "error"
            runtime_exit = 1
        elif skipped:
            overall = "warning"
            runtime_exit = 0
        else:
            overall = "ok"
            runtime_exit = 0
    else:
        overall = "error" if runtime_exit else "ok"
    emit(
        {
            "status": overall,
            "message": render_upgrade_message(
                overall, str(cli_result.get("status") or ""), runtime_document
            ),
            "runtime": args.runtime,
            "cli": cli_result,
            "runtime_update": runtime_document,
            # 016 T-19：每个 runtime 的归类（success / skipped /
            # preflight-failed / failed）与最内层错误；pi registry 预检失败在
            # all 下记 preflight-failed 并阻断，不再静默计为 skipped
            "runtime_outcomes": upgrade_runtime_outcomes(
                results if isinstance(results, list) else []
            ),
        },
        args.format,
    )
    return 1 if runtime_exit else 0


def command_migrate_legacy_workspace_pi(args: argparse.Namespace) -> int:
    """受保护 migration（T11 栈）：dry-run 只读计划，--apply 经 WorkspaceStore 应用。"""
    support = runtime_lifecycle_support()
    from migration.composition import build_migration_stack
    from runtime_lifecycle import SystemTrustedClock

    repo_root = runtime_lifecycle_repo_root(support)
    workspace = Path(args.target).expanduser().resolve()
    skills_root = workspace / ".agents" / "skills"
    candidates = sorted(
        path.relative_to(workspace).as_posix()
        for path in skills_root.rglob("*")
        if path.is_file()
    ) if skills_root.is_dir() else []
    if not candidates:
        emit(
            {
                "status": "ready",
                "message": "no shared .agents/skills candidates to migrate",
                "target": str(workspace),
                "runtime": "pi",
                "apply": bool(args.apply),
            },
            args.format,
        )
        return 0
    lifecycle_root = Path.home() / ".config" / "devkit" / "runtime-lifecycle"
    stack = build_migration_stack(
        workspace,
        lifecycle_root / "workspace-store",
        lifecycle_root / "workspace-blobs",
        repo_root / "schemas",
        SystemTrustedClock(),
        repo_root / "packages" / "skills",
    )
    plan = stack.service.plan("pi", workspace, tuple(candidates))
    if not args.apply:
        emit(plan, args.format)
        return 0
    if plan.get("status") != "ready":
        emit(plan, args.format)
        print(
            "migration plan has report-only entries; nothing was written",
            file=sys.stderr,
        )
        return 1
    if not migration_confirmation(args):
        emit(plan, args.format)
        print("migration was not confirmed; nothing was written", file=sys.stderr)
        return 1
    result = stack.service.apply(
        "pi", workspace, tuple(candidates), f"devkit-flow-{uuid.uuid4().hex[:12]}",
    )
    emit(result, args.format)
    return 0 if result.get("status") == "success" else 1


def runtime_update_plan(selected: list[str]) -> list[dict[str, Any]]:
    """--print 模式：列出各 runtime 将执行的命令（含触发条件），不执行任何子进程。"""
    plan: list[dict[str, Any]] = []
    if "claude-code" in selected:
        plan.append(
            {
                "runtime": "claude-code",
                "status": "printed",
                "commands": [
                    {"command": "claude plugin marketplace list", "condition": "always"},
                    {"command": "claude plugin list --json", "condition": "inspect current and legacy plugins"},
                    {
                        "command": f"claude plugin marketplace add {runtime_marketplace_root('claude-code') / '.claude-plugin' / 'marketplace.json'}",
                        "condition": f"marketplace {CLAUDE_MARKETPLACE_NAME} missing",
                    },
                    {
                        "command": f"claude plugin marketplace update {CLAUDE_MARKETPLACE_NAME}",
                        "condition": f"marketplace {CLAUDE_MARKETPLACE_NAME} present",
                    },
                    {
                        "command": f"claude plugin install {RUNTIME_PLUGIN_NAME}@{CLAUDE_MARKETPLACE_NAME} --scope user",
                        "condition": f"user-scope plugin {RUNTIME_PLUGIN_NAME} missing",
                    },
                    {
                        "command": f"claude plugin update -s user {RUNTIME_PLUGIN_NAME}@{CLAUDE_MARKETPLACE_NAME}",
                        "condition": f"user-scope plugin {RUNTIME_PLUGIN_NAME} present",
                    },
                    {
                        "command": "claude plugin list --json",
                        "condition": f"verify {RUNTIME_PLUGIN_NAME} is enabled at {VERSION}",
                    },
                    *[
                        {
                            "command": f"claude plugin uninstall {legacy_name}@{legacy_marketplace} --scope <installed-scope> -y",
                            "condition": f"after {RUNTIME_PLUGIN_NAME} is verified enabled at {VERSION}",
                        }
                        for legacy_marketplace in LEGACY_CLAUDE_MARKETPLACE_NAMES
                        for legacy_name in LEGACY_RUNTIME_PLUGIN_NAMES
                    ],
                    *[
                        {
                            "command": f"claude plugin uninstall {RUNTIME_PLUGIN_NAME}@{legacy_marketplace} --scope <installed-scope> -y",
                            "condition": f"after {RUNTIME_PLUGIN_NAME} is verified enabled at {VERSION}",
                        }
                        for legacy_marketplace in LEGACY_CLAUDE_MARKETPLACE_NAMES
                    ],
                    *[
                        {
                            "command": f"claude plugin marketplace remove {legacy_marketplace}",
                            "condition": "after legacy DevKit plugins are removed",
                        }
                        for legacy_marketplace in LEGACY_CLAUDE_MARKETPLACE_NAMES
                    ],
                ],
                "message": "print only; nothing executed",
            }
        )
    if "codex" in selected:
        plan.append(
            {
                "runtime": "codex",
                "status": "printed",
                "commands": [
                    {"command": "codex plugin marketplace list", "condition": "always"},
                    {"command": "codex plugin list", "condition": "always"},
                    {
                        "command": f"codex plugin remove {RUNTIME_PLUGIN_NAME}@{CODEX_MARKETPLACE_NAME}",
                        "condition": f"plugin {RUNTIME_PLUGIN_NAME} present",
                    },
                    {
                        "command": f"codex plugin marketplace remove {CODEX_MARKETPLACE_NAME}",
                        "condition": f"marketplace {CODEX_MARKETPLACE_NAME} present",
                    },
                    {
                        "command": f"codex plugin marketplace add {runtime_marketplace_root('codex')}",
                        "condition": "always",
                    },
                    {
                        "command": f"codex plugin add {RUNTIME_PLUGIN_NAME}@{CODEX_MARKETPLACE_NAME}",
                        "condition": "always",
                    },
                    {
                        "command": "codex plugin list",
                        "condition": f"verify installed version is {VERSION}",
                    },
                    *[
                        {
                            "command": f"codex plugin remove {legacy_name}@{CODEX_MARKETPLACE_NAME}",
                            "condition": f"after {RUNTIME_PLUGIN_NAME} is verified enabled at {VERSION}",
                        }
                        for legacy_name in LEGACY_RUNTIME_PLUGIN_NAMES
                    ],
                ],
                "message": "print only; nothing executed",
            }
        )
    if "kimi-code" in selected:
        plan.append(
            {
                "runtime": "kimi-code",
                "status": "printed",
                "commands": [
                    {
                        "command": "devkit-flow plugin install --runtime kimi-code",
                        "condition": "always",
                    }
                ],
                "message": "renders and installs the managed Kimi plugin; then /plugins reload or /new in the TUI",
            }
        )
    return plan


def command_runtime_update(args: argparse.Namespace) -> int:
    """按 runtime 聚合执行插件安装/更新；任一 runtime 失败不阻塞其余，整体 exit 1。"""
    if args.print_only:
        selected = (
            list(CANONICAL_RUNTIME_ORDER)
            if args.runtime == "all"
            else [args.runtime]
        )
        emit(
            {
                "status": "printed",
                "operation": "update",
                "requestedRuntime": args.runtime,
                "message": "runtime update --print (unified lifecycle, nothing executed)",
                "results": [
                    {"runtime": name, "path": "lifecycle", "operation": "update"}
                    for name in selected
                ],
            },
            args.format,
        )
        return 0
    return command_runtime_lifecycle(args, "update")


def runtime_update_result(runtime: str) -> tuple[int, dict[str, Any]]:
    """执行 runtime 同步并返回结构化结果；由 runtime update / upgrade 共用。"""
    selected = list(RUNTIME_NAMES) if runtime == "all" else [runtime]
    results = [RUNTIME_UPDATERS[name]() for name in selected]
    counts: dict[str, int] = {}
    for entry in results:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    summary = ", ".join(f"{status}: {counts[status]}" for status in sorted(counts))
    failed = [entry for entry in results if entry["status"] == "error"]
    overall = "error" if failed else "ok"
    lines = [f"runtime update: {overall} ({summary})"]
    lines.extend(f"- {entry['runtime']}: {entry['status']} — {entry['message']}" for entry in results)
    return (
        1 if failed else 0,
        {"status": overall, "message": "\n".join(lines), "runtime": runtime, "results": results},
    )


def command_upgrade(args: argparse.Namespace) -> int:
    """CLI 是唯一升级源：先升级/检查 CLI，再由该 release 同步所选 runtime 插件。

    升级成功且非 --check 时顺带做遥测身份半自动绑定（真人终端问一句；非 TTY / json 只留一行提示）。
    """
    code = command_upgrade_lifecycle(args)
    if code == 0 and not getattr(args, "check", False):
        offer = offer_sso_login(
            Path.cwd(),
            fmt=getattr(args, "format", "text"),
            interactive=not getattr(args, "no_sso_login", False),
        )
        if offer and getattr(args, "format", "text") != "json":
            sys.stderr.write(f"- sso-identity: {offer.get('status')} — {offer.get('reason')}\n")
    return code


def command_plugin_install(args: argparse.Namespace) -> int:
    """[DEPRECATED] 统一插件安装。旧命令入口，平滑兼容转发至 runtime lifecycle install。"""
    if getattr(args, "format", "text") != "json":
        sys.stderr.write(
            "[WARN] 'devkit-flow plugin install' is deprecated. "
            "Please use 'devkit-flow runtime install' instead.\n"
        )
    if args.runtime in LIFECYCLE_ONLY_RUNTIMES:
        return command_runtime_lifecycle(args, "install")
    emit(install_kimi_plugin(), args.format)
    return 0
