"""017 progress-mod-focus：feature 会话绑定（接手记录）。

记录「哪个 Claude Code 会话最近一次接手了某个 workspace 下的某个 feature」，供
``status --format json`` 附带 ``session_binding``，让 mod 优先聚焦本会话的 feature。

- 存储：``~/.config/devkit/feature-session-bindings.json``（git 外，按 workspace + feature_dir 唯一）。
- 写入：flow 写命令 / ``feature init`` 成功后、以及分支一致时的 ``flow next``（设计 §4.3.3、D-2）。
- 读取：``status`` 只读，不加锁、不创建任何文件。
- fail-safe：任何异常都吞掉，只返回告警字符串，不打印、不改变命令输出与退出码。

依赖约束：只依赖标准库与 ``session_registry.normalize_workspace`` / ``textio.atomic_write_text``；
不得 import ``flow`` / ``telemetry`` / ``cli``（它们反向消费本模块）。
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from devkit_flow.session_registry import normalize_workspace
from devkit_flow.textio import _release_platform_lock, atomic_write_text
from devkit_flow.textio import platform_lock, try_platform_lock, locked_path

BINDINGS_FILE_NAME = "feature-session-bindings.json"
BINDINGS_LOCK_NAME = "feature-session-bindings.lock"
BINDINGS_SCHEMA_VERSION = 1
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
MAX_BINDINGS = 500
MAX_SESSION_ID_LENGTH = 256
LOCK_TIMEOUT_SECONDS = 2.0
GIT_TIMEOUT_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.02
TAKEOVER_COMMANDS = frozenset({
    "feature init", "flow set", "flow batch-set", "flow accept-risk", "flow task set",
    "flow task ensure-small", "flow confirm", "flow rollback", "flow complete", "flow archive",
    "flow clarify", "flow mode", "flow migrate-steps", "flow next",
})
# flow clarify 下只读/展示类子命令不算接手（K7）。
_CLARIFY_NON_TAKEOVER = frozenset({"present", "status"})
_RECORD_FIELDS = ("workspace", "feature_dir", "feature", "session_id", "bound_at", "command")


def _devkit_dir(home: Optional[Path] = None) -> Path:
    base = home if home is not None else Path.home()
    return Path(base).expanduser() / ".config" / "devkit"


def bindings_path(home: Optional[Path] = None) -> Path:
    return _devkit_dir(home) / BINDINGS_FILE_NAME


def current_session_id(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    env = os.environ if environ is None else environ
    value = env.get(SESSION_ENV)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_SESSION_ID_LENGTH:
        return None
    return value


def current_git_branch(workspace: Path) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    branch = completed.stdout.decode("utf-8", errors="replace").strip()
    return branch or None


def is_takeover(command: str, data: Optional[Dict[str, Any]] = None, *,
                subcommand: Optional[str] = None) -> bool:
    if command not in TAKEOVER_COMMANDS:
        return False
    if command == "flow migrate-steps" and isinstance(data, dict) and data.get("dry_run") is True:
        return False
    if command == "flow clarify" and subcommand in _CLARIFY_NON_TAKEOVER:
        return False
    return True


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _valid_record(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    for key in _RECORD_FIELDS:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            return False
    if len(item["session_id"]) > MAX_SESSION_ID_LENGTH:
        return False
    return _is_positive_int(item.get("seq"))


class _NewerSchema(Exception):
    """文件由更高版本 CLI 写入：只读视为空、不得覆盖。"""


def _parse(text: str) -> Dict[str, Any]:
    """解析文件内容 → {"next_seq": int, "bindings": [...]}；损坏视为空，更高版本抛 _NewerSchema。"""
    empty: Dict[str, Any] = {"next_seq": 1, "bindings": []}
    try:
        payload = json.loads(text)
    except ValueError:
        return empty
    if not isinstance(payload, dict):
        return empty
    version = payload.get("schema_version")
    if not _is_positive_int(version):
        return empty
    if version > BINDINGS_SCHEMA_VERSION:
        raise _NewerSchema()
    raw = payload.get("bindings")
    if not isinstance(raw, list):
        return empty
    bindings = [dict(item) for item in raw if _valid_record(item)]
    next_seq = payload.get("next_seq")
    if not _is_positive_int(next_seq):
        next_seq = 1
    highest = max((item["seq"] for item in bindings), default=0)
    return {"next_seq": max(next_seq, highest + 1), "bindings": bindings}


def _acquire_lock(lock_path: Path, timeout: float) -> int:
    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            if try_platform_lock(descriptor):
                return descriptor
        except OSError as exc:
            # EACCES 在 POSIX 上不一定是竞争，shim 不把它收成 LockBusy；这里维持原轮询。
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                os.close(descriptor)
                raise
        if time.monotonic() >= deadline:
            os.close(descriptor)
            raise TimeoutError("session binding lock busy")
        time.sleep(_LOCK_POLL_SECONDS)


def _workspace_alive(workspace: str) -> bool:
    try:
        return Path(workspace).is_dir()
    except OSError:
        return True


def record_takeover(workspace: Path, feature: Optional[Dict[str, Any]], *, command: str,
                    require_branch_match: bool = False,
                    home: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None,
                    now: Optional[datetime] = None,
                    git_branch: Optional[Callable[[Path], Optional[str]]] = None,
                    lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> Optional[str]:
    """记录一次接手。按规则跳过或成功返回 None；失败返回告警字符串（不打印、不抛出）。"""
    try:
        session_id = current_session_id(environ)
        if session_id is None:
            return None
        if not isinstance(feature, dict):
            return None
        feature_dir = feature.get("feature_dir")
        if not isinstance(feature_dir, str) or not feature_dir.strip():
            return None
        feature_dir = feature_dir.strip()
        if require_branch_match:
            summary = feature.get("summary")
            status_branch = summary.get("branch") if isinstance(summary, dict) else None
            status_branch = status_branch.strip() if isinstance(status_branch, str) else ""
            if not status_branch:
                return None
            probe = git_branch if git_branch is not None else current_git_branch
            if probe(Path(workspace)) != status_branch:
                return None
        name = feature.get("feature")
        name = name.strip() if isinstance(name, str) and name.strip() else feature_dir.rsplit("/", 1)[-1]
        stamp = (now if now is not None else datetime.now().astimezone()).isoformat(timespec="seconds")
        ws = normalize_workspace(workspace)

        devkit_dir = _devkit_dir(home)
        devkit_dir.mkdir(parents=True, exist_ok=True)
        target = devkit_dir / BINDINGS_FILE_NAME
        descriptor = _acquire_lock(devkit_dir / BINDINGS_LOCK_NAME, lock_timeout)
        try:
            try:
                text = target.read_text(encoding="utf-8")
            except FileNotFoundError:
                text = ""
            try:
                state = _parse(text) if text else {"next_seq": 1, "bindings": []}
            except _NewerSchema:
                return "session binding file has newer schema_version; skipped"
            seq = state["next_seq"]
            kept: List[Dict[str, Any]] = [
                item for item in state["bindings"]
                if not (item["workspace"] == ws and item["feature_dir"] == feature_dir)
            ]
            kept.append({
                "workspace": ws,
                "feature_dir": feature_dir,
                "feature": name,
                "session_id": session_id,
                "seq": seq,
                "bound_at": stamp,
                "command": command,
            })
            kept = [item for item in kept if item["workspace"] == ws or _workspace_alive(item["workspace"])]
            if len(kept) > MAX_BINDINGS:
                kept.sort(key=lambda item: item["seq"])
                kept = kept[len(kept) - MAX_BINDINGS:]
            payload = {"schema_version": BINDINGS_SCHEMA_VERSION, "next_seq": seq + 1, "bindings": kept}
            atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        finally:
            try:
                _release_platform_lock(descriptor)
            finally:
                os.close(descriptor)
        return None
    except Exception as exc:  # noqa: BLE001 — fail-safe：绑定失败绝不影响命令
        return f"session binding skipped: {type(exc).__name__}"


def load_bindings(workspace: Path, *, home: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """只读：返回当前 workspace 的 {feature_dir: 记录}；任何异常返回空字典。"""
    try:
        target = bindings_path(home)
        if not target.is_file():
            return {}
        try:
            state = _parse(target.read_text(encoding="utf-8"))
        except _NewerSchema:
            return {}
        ws = normalize_workspace(workspace)
        return {item["feature_dir"]: item for item in state["bindings"] if item["workspace"] == ws}
    except Exception:  # noqa: BLE001
        return {}


def binding_view(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(record, dict):
        return None
    return {"session_id": record["session_id"], "seq": record["seq"], "bound_at": record["bound_at"]}


def _view_for(bindings: Dict[str, Dict[str, Any]], item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    feature_dir = item.get("feature_dir")
    if not isinstance(feature_dir, str):
        return None
    return binding_view(bindings.get(feature_dir.strip()))


def attach_session_bindings(data: Dict[str, Any], workspace: Path, *, home: Optional[Path] = None) -> None:
    """status 输出附加 session_binding（只读、加性）。ok → 顶层；multi → features[] 每个字典项。"""
    if not isinstance(data, dict):
        return
    status = data.get("status")
    if status == "ok":
        try:
            data["session_binding"] = _view_for(load_bindings(workspace, home=home), data)
        except Exception:  # noqa: BLE001
            data["session_binding"] = None
    elif status == "multiple_active_features" and isinstance(data.get("features"), list):
        try:
            bindings = load_bindings(workspace, home=home)
        except Exception:  # noqa: BLE001
            bindings = {}
        for item in data["features"]:
            if isinstance(item, dict):
                try:
                    item["session_binding"] = _view_for(bindings, item)
                except Exception:  # noqa: BLE001
                    item["session_binding"] = None
