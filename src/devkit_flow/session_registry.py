from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from devkit_flow.constants import (
    DASHBOARD_STEP_TIMINGS_FILE,
    DEVKIT_SESSION_INDEX_LOCK_REL,
    DEVKIT_SESSION_INDEX_REL,
    PR_REVIEW_WATCH_CLAUDE_PID_ENV,
    PR_REVIEW_WATCH_PARENT_CHAIN_DEPTH,
    PR_REVIEW_WATCH_RUNTIME_EXECUTABLES,
    PR_REVIEW_WATCH_SESSION_ENV_VARS,
    PR_REVIEW_WATCH_SID_KEY_MAX_LEN,
    PR_REVIEW_WATCH_SID_KEY_SHA1_LEN,
    STEP_TIMINGS_SCHEMA_VERSION,
)
from devkit_flow.errors import DevkitFlowError
from devkit_flow.parse_util import find_features, select_feature_for_ones
from devkit_flow.textio import (
    LockBusy,
    atomic_write_text,
    compact_error,
    emit,
    locked_path,
    parent_pid_chain,
    process_alive,
    process_start_time,
)

# 中央 session registry（append-only JSONL）：回答「某 workspace 的哪些 runtime 会话
# 加载过 devkit 插件」，供 flow 打标（归属）与 sessions list（反查）消费。
# 前提约定：一个会话只进行一个 feature；同一 workspace 目录不同时开多个会话，
# 归属用「该 workspace 最新 marker」，不做 pid_chain。
REGISTRY_FILE_NAME = "session-registry.jsonl"
HOOK_STDIN_TIMEOUT_SECONDS = 3.0


def registry_path(home: Path | None = None) -> Path:
    """registry 文件路径；home 为用户主目录（默认 Path.home()），测试传临时目录隔离。"""
    base = home if home is not None else Path.home()
    return Path(base).expanduser() / ".config" / "devkit" / REGISTRY_FILE_NAME


def normalize_workspace(workspace: Path | str) -> str:
    """workspace 归一化为绝对路径字符串；resolve 失败时退回 abspath（fail-safe）。"""
    path = Path(workspace).expanduser()
    try:
        return str(path.resolve())
    except OSError:
        return os.path.normpath(os.path.abspath(str(path)))


def append_session_marker(
    *,
    runtime: str,
    workspace: Path | str,
    session_id: str | None = None,
    transcript_path: str | None = None,
    session_file: str | None = None,
    source: str,
    home: Path | None = None,
    pid: int | None = None,
    proc_started_at: str | None = None,
    event: str | None = None,
    reason: str | None = None,
) -> str | None:
    """追加一条 session marker；行小，open append 原子性足够。

    ``pid`` / ``procStartedAt`` / ``event`` / ``reason`` 只在调用方给出时写入（加性，旧行不补键）。
    fail-safe：任何异常静默返回 warning 字符串（正常返回 None），绝不抛出。
    不接受凭据字段。
    """
    try:
        path = registry_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        marker: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "runtime": str(runtime),
            "session_id": session_id or None,
            "transcript_path": transcript_path or None,
            "session_file": session_file or None,
            "workspace": normalize_workspace(workspace),
            "source": source,
        }
        parsed_pid = _parse_pid(pid)
        if parsed_pid is not None:
            marker["pid"] = parsed_pid
        started = _session_optional_text(proc_started_at)
        if started is not None:
            marker["procStartedAt"] = started
        event_text = _session_optional_text(event)
        if event_text is not None:
            marker["event"] = event_text
        if reason is not None:
            marker["reason"] = str(reason)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(marker, ensure_ascii=False) + "\n")
        return None
    except Exception as error:  # fail-safe 兜底：marker 写入失败不得影响主流程
        return f"session marker 写入失败（不影响主流程）: {compact_error(error)}"


def append_session_end(
    *,
    runtime: str,
    workspace: Path | str,
    session_id: str | None = None,
    reason: str,
    home: Path | None = None,
    source: str = "session-end",
) -> str | None:
    """SessionEnd 审计行。hook 接线在 T-28；这里只追加 ``event:"end"``。"""
    return append_session_marker(
        runtime=runtime,
        workspace=workspace,
        session_id=session_id,
        source=source,
        home=home,
        event="end",
        reason=reason,
    )


def iter_markers(workspace: Path | str | None = None, home: Path | None = None) -> list[dict[str, Any]]:
    """按文件顺序读取 marker；坏行跳过，文件缺失返回空列表。workspace 非空时按归一化路径过滤。"""
    try:
        path = registry_path(home)
        if not path.is_file():
            return []
        wanted = normalize_workspace(workspace) if workspace is not None else None
        markers: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                marker = json.loads(line)
            except ValueError:
                continue  # 坏行跳过（截断/半行写入不污染读取）
            if not isinstance(marker, dict):
                continue
            if wanted is not None and marker.get("workspace") != wanted:
                continue
            markers.append(marker)
        return markers
    except Exception:
        return []


def latest_marker(
    workspace: Path | str,
    home: Path | None = None,
    runtime: str | None = None,
) -> dict[str, Any] | None:
    """该 workspace 最新一条 marker（归属约定）；无则 None。

    runtime 非空时按 runtime 过滤（T-08 / 设计 §4.10），避免 SessionEnd 回落跨 runtime 归因。
    None = 不过滤，行为与现网 `latest_marker(workspace)` / `home=` 调用完全一致。
    """
    markers = iter_markers(workspace, home=home)
    if runtime is not None:
        markers = [marker for marker in markers if marker.get("runtime") == runtime]
    return markers[-1] if markers else None


def parse_hook_stdin(text: str) -> dict[str, Any]:
    """容错解析 hook stdin JSON，同时兼容 snake_case（claude/codex/kimi）与 camelCase（grok）。

    返回固定三键（session_id / transcript_path / session_file），字段缺失或解析失败为 None。
    """
    result: dict[str, Any] = {"session_id": None, "transcript_path": None, "session_file": None}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return result
    if not isinstance(data, dict):
        return result

    def pick(*keys: str) -> str | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    result["session_id"] = pick("session_id", "sessionId", "conversationId", "conversation_id")
    result["transcript_path"] = pick("transcript_path", "transcriptPath")
    result["session_file"] = pick("session_file", "sessionFile")
    return result


def read_hook_stdin(timeout: float = HOOK_STDIN_TIMEOUT_SECONDS) -> str:
    """限时读取 hook 宿主经管道注入的 stdin JSON；TTY / 无数据 / 异常一律返回 ""。

    仅在 stdin 非 TTY 时尝试：select 等到数据或超时（上限 timeout 秒），随后读到 EOF；
    宿主关闭管道即返回，无 stdin 数据的手工调用最多等待一个 timeout，绝不卡死。
    """
    try:
        stream = sys.stdin
        if stream is None or stream.isatty():
            return ""
        fd = stream.fileno()
    except Exception:
        return ""
    try:
        chunks: list[bytes] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break  # EOF（宿主已关闭管道）
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")
    except Exception:
        return ""


def locate_session_file(marker: dict[str, Any], home: Path | None = None) -> str | None:
    """按 runtime 规则定位 session 文件/目录（best-effort；失败或无法推导返回 None）。

    - claude-code / codex：transcript_path 直指；
    - kimi-code：~/.kimi-code/session_index.jsonl 按 sessionId 反查 sessionDir；
    - grok-build：transcript_path，或遍历 ~/.grok/sessions/*/<session_id>/；
    - pi：marker 里的 session_file（--no-session 时为 None）。
    """
    try:
        transcript = marker.get("transcript_path")
        if isinstance(transcript, str) and transcript.strip():
            return str(Path(transcript).expanduser())
        session_file = marker.get("session_file")
        if isinstance(session_file, str) and session_file.strip():
            return str(Path(session_file).expanduser())
        session_id = marker.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        base = (home if home is not None else Path.home()).expanduser()
        runtime = str(marker.get("runtime") or "")
        if runtime == "kimi-code":
            index_path = base / ".kimi-code" / "session_index.jsonl"
            for line in index_path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("sessionId") == session_id:
                    session_dir = entry.get("sessionDir")
                    if isinstance(session_dir, str) and session_dir.strip():
                        return str(Path(session_dir).expanduser())
            return None
        if runtime == "grok-build":
            sessions_root = base / ".grok" / "sessions"
            if not sessions_root.is_dir():
                return None
            for project_dir in sessions_root.iterdir():
                candidate = project_dir / session_id
                if candidate.is_dir():
                    return str(candidate)
            return None
        if runtime == "antigravity":
            for app_dir in ("antigravity-cli", "antigravity", "antigravity-ide"):
                brain_transcript = base / ".gemini" / app_dir / "brain" / session_id / ".system_generated" / "logs" / "transcript.jsonl"
                if brain_transcript.is_file():
                    return str(brain_transcript)
            return None
        return None
    except Exception:
        return None


def flow_session_ids(feature_dir: Path) -> set[str]:
    """从 feature 目录的 .step-timings.json 收集 cycle 上出现过 session 的 session_id（fail-safe）。"""
    ids: set[str] = set()
    try:
        loaded = json.loads((feature_dir / DASHBOARD_STEP_TIMINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ids
    if not isinstance(loaded, dict) or loaded.get("schema") != STEP_TIMINGS_SCHEMA_VERSION:
        return ids
    steps = loaded.get("steps")
    if not isinstance(steps, dict):
        return ids
    for step_entry in steps.values():
        cycles = step_entry.get("cycles") if isinstance(step_entry, dict) else None
        if not isinstance(cycles, list):
            continue
        for cycle in cycles:
            session = cycle.get("session") if isinstance(cycle, dict) else None
            if isinstance(session, dict):
                session_id = session.get("session_id")
                if isinstance(session_id, str) and session_id:
                    ids.add(session_id)
    return ids


def list_workspace_sessions(
    workspace: Path | str,
    *,
    drove_session_ids: set[str] | None = None,
    home: Path | None = None,
) -> list[dict[str, Any]]:
    """registry 中该 workspace 的 marker 按 (runtime, session_id) 去重列出（保留最新一条的字段）。"""
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for index, marker in enumerate(iter_markers(workspace, home=home)):
        runtime = str(marker.get("runtime") or "unknown")
        session_id = marker.get("session_id")
        key = (runtime, session_id) if isinstance(session_id, str) and session_id else (runtime, f"#{index}")
        entry = deduped.get(key)
        ts = str(marker.get("ts") or "")
        if entry is None:
            entry = {
                "runtime": runtime,
                "session_id": session_id if isinstance(session_id, str) else None,
                "first_seen": ts,
                "last_seen": ts,
                "transcript_path": None,
                "session_file": None,
                "source": None,
            }
            deduped[key] = entry
        entry["last_seen"] = ts
        for field in ("transcript_path", "session_file", "source"):
            if marker.get(field):
                entry[field] = marker[field]
    sessions: list[dict[str, Any]] = []
    for entry in deduped.values():
        file_path = locate_session_file(entry, home=home)
        info: dict[str, Any] = {
            **entry,
            "session_file_path": file_path,
            "session_file_exists": bool(file_path) and Path(file_path).exists(),
            "session_file_bytes": None,
        }
        if file_path and info["session_file_exists"]:
            try:
                candidate = Path(file_path)
                info["session_file_bytes"] = (
                    candidate.stat().st_size if candidate.is_file()
                    else sum(path.stat().st_size for path in candidate.rglob("*") if path.is_file())
                )
            except OSError:
                info["session_file_bytes"] = None
        if drove_session_ids is not None:
            info["drove_flow"] = bool(entry["session_id"]) and entry["session_id"] in drove_session_ids
        sessions.append(info)
    sessions.sort(key=lambda item: (item["last_seen"], item["runtime"]))
    return sessions


def command_sessions_list(args: argparse.Namespace) -> int:
    workspace = Path(args.target or ".").expanduser().resolve()
    drove_session_ids: set[str] | None = None
    feature_dir: str | None = None
    if args.feature:
        selected = select_feature_for_ones(find_features(workspace), args.feature)
        feature_dir = selected["feature_dir"]
        drove_session_ids = flow_session_ids(workspace / feature_dir)
    sessions = list_workspace_sessions(workspace, drove_session_ids=drove_session_ids)
    if sessions:
        lines = []
        for item in sessions:
            file_text = item["session_file_path"] or "-"
            if item["session_file_path"]:
                file_text += " (exists)" if item["session_file_exists"] else " (missing)"
            drove = ""
            if drove_session_ids is not None:
                drove = "  drove_flow=yes" if item.get("drove_flow") else "  drove_flow=no"
            lines.append(
                f"{item['runtime']}  {item['session_id'] or '-'}  {item['last_seen']}  {file_text}{drove}"
            )
        message = "\n".join(lines)
    else:
        message = f"该 workspace 尚未记录任何 runtime 会话: {workspace}"
    data: dict[str, Any] = {
        "status": "ok",
        "message": message,
        "workspace": str(workspace),
        "sessions": sessions,
    }
    if feature_dir is not None:
        data["feature_dir"] = feature_dir
    emit(data, args.format)
    return 0


def discover_workspace_sessions(
    workspace: Path | str,
    *,
    home: Path | None = None,
    budget: Any | None = None,
    max_days: int = 7,
) -> list[dict[str, Any]]:
    """扫描本地 Claude、Codex、Grok、Kimi、Pi 的默认存储根目录。

    匹配当前工作区路径，主动反查已落盘的 session marker：
    fail-safe：任何异常吞噬，绝不抛出破坏主流程。
    """
    from urllib.parse import quote
    from datetime import timezone

    base_home = Path(home).expanduser() if home is not None else Path.home()
    target_ws = normalize_workspace(workspace)
    now_ts = time.time()
    cutoff_ts = now_ts - (max_days * 86400)
    discovered: list[dict[str, Any]] = []

    def _is_expired() -> bool:
        if budget is not None:
            checker = getattr(budget, "expired", None)
            if callable(checker) and checker():
                return True
        return False

    # 1. Claude Code
    try:
        claude_encoded = target_ws.replace("/", "-").replace("_", "-")
        claude_proj = base_home / ".claude" / "projects" / claude_encoded
        if claude_proj.is_dir():
            for f in sorted(claude_proj.rglob("*.jsonl")):
                if _is_expired():
                    break
                try:
                    st = f.stat()
                    if st.st_mtime < cutoff_ts:
                        continue
                    sess_id = f.stem
                    ts_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                    discovered.append({
                        "ts": ts_iso,
                        "runtime": "claude-code",
                        "session_id": sess_id,
                        "transcript_path": str(f.resolve()),
                        "session_file": None,
                        "workspace": target_ws,
                        "source": "cwd-discovery",
                    })
                except Exception:
                    continue
    except Exception:
        pass

    if _is_expired():
        return discovered

    # 2. Codex
    try:
        codex_root = base_home / ".codex" / "sessions"
        if codex_root.is_dir():
            for f in sorted(codex_root.rglob("rollout-*.jsonl")):
                if _is_expired():
                    break
                try:
                    st = f.stat()
                    if st.st_mtime < cutoff_ts:
                        continue
                    with f.open("r", encoding="utf-8", errors="replace") as fh:
                        head = fh.readline()
                    if not head.strip():
                        continue
                    meta = json.loads(head)
                    if not isinstance(meta, dict) or meta.get("type") != "session_meta":
                        continue
                    payload = meta.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    cwd_val = normalize_workspace(payload.get("cwd") or "")
                    if cwd_val != target_ws:
                        continue
                    sess_id = str(payload.get("session_id") or f.stem.replace("rollout-", ""))
                    ts_iso = meta.get("timestamp") or datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                    discovered.append({
                        "ts": ts_iso,
                        "runtime": "codex",
                        "session_id": sess_id,
                        "transcript_path": str(f.resolve()),
                        "session_file": None,
                        "workspace": target_ws,
                        "source": "cwd-discovery",
                    })
                except Exception:
                    continue
    except Exception:
        pass

    if _is_expired():
        return discovered

    # 3. Grok Build
    try:
        grok_root = base_home / ".grok" / "sessions" / quote(target_ws, safe="")
        if grok_root.is_dir():
            for p in sorted(grok_root.iterdir()):
                if _is_expired():
                    break
                if not p.is_dir():
                    continue
                try:
                    upd = p / "updates.jsonl"
                    st = p.stat()
                    if st.st_mtime < cutoff_ts:
                        continue
                    ts_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                    discovered.append({
                        "ts": ts_iso,
                        "runtime": "grok-build",
                        "session_id": p.name,
                        "transcript_path": str(upd.resolve()) if upd.exists() else None,
                        "session_file": str(p.resolve()),
                        "workspace": target_ws,
                        "source": "cwd-discovery",
                    })
                except Exception:
                    continue
    except Exception:
        pass

    if _is_expired():
        return discovered

    # 4. Kimi Code
    try:
        kimi_idx = base_home / ".kimi-code" / "session_index.jsonl"
        if kimi_idx.is_file():
            for line in kimi_idx.read_text(encoding="utf-8", errors="replace").splitlines():
                if _is_expired():
                    break
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        continue
                    work_dir = normalize_workspace(rec.get("workDir") or "")
                    if work_dir != target_ws:
                        continue
                    sess_dir_str = rec.get("sessionDir")
                    if not sess_dir_str:
                        continue
                    sess_dir = Path(sess_dir_str)
                    if not sess_dir.exists():
                        continue
                    sess_id = str(rec.get("sessionId") or sess_dir.name)
                    wires = list(sess_dir.rglob("wire.jsonl"))
                    wire_path = str(wires[0].resolve()) if wires else None
                    ts_iso = (
                        rec.get("updatedAt")
                        or rec.get("createdAt")
                        or datetime.fromtimestamp(sess_dir.stat().st_mtime, tz=timezone.utc).isoformat()
                    )
                    discovered.append({
                        "ts": ts_iso,
                        "runtime": "kimi-code",
                        "session_id": sess_id,
                        "transcript_path": wire_path,
                        "session_file": str(sess_dir.resolve()),
                        "workspace": target_ws,
                        "source": "cwd-discovery",
                    })
                except Exception:
                    continue
    except Exception:
        pass

    if _is_expired():
        return discovered

    # 5. Pi Agent
    try:
        pi_dirs = [
            Path(workspace) / ".pi" / "sessions",
            base_home / ".pi" / "sessions",
        ]
        for pi_dir in pi_dirs:
            if _is_expired():
                break
            if not pi_dir.is_dir():
                continue
            for f in sorted(pi_dir.rglob("*.json*")):
                if _is_expired():
                    break
                try:
                    st = f.stat()
                    if st.st_mtime < cutoff_ts:
                        continue
                    ts_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                    discovered.append({
                        "ts": ts_iso,
                        "runtime": "pi",
                        "session_id": f.stem,
                        "transcript_path": str(f.resolve()),
                        "session_file": str(f.resolve()),
                        "workspace": target_ws,
                        "source": "cwd-discovery",
                    })
                except Exception:
                    continue
    except Exception:
        pass

    def _sort_key(m: dict[str, Any]) -> str:
        return str(m.get("ts") or "")

    discovered.sort(key=_sort_key, reverse=True)
    return discovered


# ---------------------------------------------------------------------------
# 会话索引、存活判定、当前会话解析（DR-6 / DR-7 / DR-9）
# 只持 session-index.lock（锁序 4），持锁期间只做文件读写。
# ---------------------------------------------------------------------------

INDEX_SCHEMA_VERSION = 1
_ENDED_PRUNE_AFTER = timedelta(days=7)
_SEEN_PRUNE_AFTER = timedelta(days=30)
_DEFAULT_INDEX_LOCK_TIMEOUT = 2.0
_SID_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_KNOWN_RUNTIMES = frozenset(runtime for runtime, _variable in PR_REVIEW_WATCH_SESSION_ENV_VARS)


def _process_basename(name: object) -> str:
    text = str(name).strip().replace("\\", "/")
    base = text.rsplit("/", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base.casefold()


_EXECUTABLES = {
    runtime: frozenset(_process_basename(name) for name in names)
    for runtime, names in PR_REVIEW_WATCH_RUNTIME_EXECUTABLES
}


class SessionIndexReadError(Exception):
    """会话索引存在但读不出来。判定方把它当成 unknown，不覆盖原文件。"""


def session_key(runtime: str, session_id: str) -> str:
    return f"{runtime}:{session_id}"


def sid_key(session_id: str) -> str:
    """sessionId 里非 [A-Za-z0-9._-] 换成 _；超过 80 字符截断后再加 sha1 前 8 位。"""
    cleaned = _SID_UNSAFE.sub("_", str(session_id))
    if len(cleaned) <= PR_REVIEW_WATCH_SID_KEY_MAX_LEN:
        return cleaned
    digest = hashlib.sha1(str(session_id).encode("utf-8")).hexdigest()
    return cleaned[:PR_REVIEW_WATCH_SID_KEY_MAX_LEN] + digest[:PR_REVIEW_WATCH_SID_KEY_SHA1_LEN]


def _session_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _parse_pid(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def _session_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _same_live_instance(previous: SessionIndexEntry, ref: SessionRef, proc_started_at: str | None) -> bool:
    """同一进程才沿用代次。没有可用 PID 时无法证明还是原来的实例。"""

    if _present(previous.ended_at):
        return False
    new_pid = _parse_pid(ref.pid)
    old_pid = previous.pid
    if new_pid is None or old_pid is None or new_pid != old_pid:
        return False
    new_started = _session_optional_text(proc_started_at)
    old_started = previous.proc_started_at
    if new_started and old_started and new_started != old_started:
        return False
    return True


def _next_generation(previous: SessionIndexEntry | None) -> int:
    if previous is None:
        return 1
    current = previous.generation if isinstance(previous.generation, int) else 0
    if isinstance(current, bool) or current < 1:
        return 1
    return current + 1


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = _session_optional_text(value)
    if text is None:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _coerce_now(now: object) -> datetime:
    parsed = _parse_time(now)
    if parsed is None:
        return datetime.now(timezone.utc)
    return parsed


def _format_time(now: object) -> str:
    return _coerce_now(now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _present(value: object) -> bool:
    return _session_optional_text(value) is not None


def _split_session_key(key: str) -> tuple[str, str] | None:
    if ":" not in key:
        return None
    runtime, session_id = key.split(":", 1)
    if not runtime or not session_id:
        return None
    return runtime, session_id


def _same_instant(left: str, right: str) -> bool:
    parsed_left = _parse_time(left)
    parsed_right = _parse_time(right)
    if parsed_left is not None and parsed_right is not None:
        return parsed_left == parsed_right
    return left.strip() == right.strip()


@dataclass
class SessionRef:
    runtime: str
    session_id: str
    pid: int | None = None
    workspace: str = ""

    def key(self) -> str:
        return session_key(self.runtime, self.session_id)


@dataclass
class SessionIndexEntry:
    runtime: str
    session_id: str
    pid: int | None = None
    proc_started_at: str | None = None
    workspace: str = ""
    started_at: str = ""
    ended_at: str | None = None
    end_reason: str | None = None
    last_seen_at: str = ""
    source: str = ""
    generation: int = 0

    def key(self) -> str:
        return session_key(self.runtime, self.session_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "procStartedAt": self.proc_started_at,
            "workspace": self.workspace,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "endReason": self.end_reason,
            "lastSeenAt": self.last_seen_at,
            "source": self.source,
            "generation": self.generation,
        }


@dataclass(frozen=True)
class SessionEndIdentity:
    """SessionEnd 入口捕获的实例身份。generation 为 None 时不得当成 0。"""

    runtime: str
    session_id: str
    generation: int | None
    pid: int | None
    proc_started_at: str | None
    observed_at: str
    state: str

    def end_event_key(self) -> str:
        generation = self.generation if type(self.generation) is int else None
        pid = self.pid if type(self.pid) is int else None
        started = self.proc_started_at if isinstance(self.proc_started_at, str) else ""
        return json.dumps(
            [self.state, self.runtime, self.session_id, generation, pid, started, self.observed_at],
            ensure_ascii=False,
            separators=(",", ":"),
        )


def capture_session_end_identity(
    index: SessionIndex,
    runtime: str,
    session_id: str,
    now: object,
) -> SessionEndIdentity:
    """在遥测之前读一次索引。读失败是 unreadable，没有行是 absent，都不回落最新 marker。"""

    observed = _format_time(now)
    try:
        entry = index.get(runtime, session_id)
    except SessionIndexReadError:
        return SessionEndIdentity(str(runtime), str(session_id), None, None, None, observed, "unreadable")
    if entry is None:
        return SessionEndIdentity(str(runtime), str(session_id), None, None, None, observed, "absent")
    generation = entry.generation if type(entry.generation) is int else None
    pid = entry.pid if type(entry.pid) is int else None
    started = entry.proc_started_at if isinstance(entry.proc_started_at, str) and entry.proc_started_at else None
    return SessionEndIdentity(str(runtime), str(session_id), generation, pid, started, observed, "known")


def session_end_process_contradicts(entry: SessionIndexEntry | None, identity: SessionEndIdentity) -> bool:
    """两边都有的 pid 或进程启动时间对不上才算矛盾。缺一边不是矛盾。"""

    if entry is None:
        return False
    if type(identity.pid) is int and type(entry.pid) is int and identity.pid != entry.pid:
        return True
    left = identity.proc_started_at if isinstance(identity.proc_started_at, str) else ""
    right = entry.proc_started_at if isinstance(entry.proc_started_at, str) else ""
    return bool(left and right and left != right)


def captured_end_verdict(entry: SessionIndexEntry | None, identity: SessionEndIdentity) -> str:
    """apply 可以写结束标记；reject 原样返回；defer 不写。absent 遇见已有行就是 reject。"""

    state = identity.state
    if state == "unreadable" or state not in ("known", "absent"):
        return "defer"
    if state == "absent":
        return "apply" if entry is None else "reject"
    if entry is None:
        return "apply"
    if type(identity.generation) is not int or entry.generation != identity.generation:
        return "reject"
    if session_end_process_contradicts(entry, identity):
        return "reject"
    return "apply"


def captured_end_may_deregister(
    entry: SessionIndexEntry | None,
    identity: SessionEndIdentity | None,
    generation: object,
) -> bool:
    """注销前的最后一道实例校验。对不上就还没改监听、队列和 inbox。"""

    if entry is None or type(generation) is not int or type(entry.generation) is not int:
        return False
    if entry.generation != generation:
        return False
    if identity is None:
        return True
    if identity.state == "unreadable":
        return False
    if identity.state == "known":
        if type(identity.generation) is not int or identity.generation != generation:
            return False
        return not session_end_process_contradicts(entry, identity)
    if identity.state == "absent":
        return bool(entry.ended_at)
    return False


def _note_end_outcome(outcome: dict[str, Any] | None, status: str, entry: SessionIndexEntry | None) -> None:
    if not isinstance(outcome, dict):
        return
    outcome["status"] = status
    generation = entry.generation if entry is not None else None
    if type(generation) is int:
        outcome["generation"] = generation


@dataclass(frozen=True)
class ResolvedPid:
    pid: int | None
    proc_started_at: str | None


@dataclass(frozen=True)
class LivenessVerdict:
    state: str
    basis: str
    lease_expires_at: str | None


def _entry_from_raw(runtime: str, session_id: str, raw: object) -> SessionIndexEntry | None:
    if not isinstance(raw, dict):
        return None
    return SessionIndexEntry(
        runtime=runtime,
        session_id=session_id,
        pid=_parse_pid(raw.get("pid")),
        proc_started_at=_session_optional_text(raw.get("procStartedAt")),
        workspace=_session_optional_text(raw.get("workspace")) or "",
        started_at=_session_optional_text(raw.get("startedAt")) or "",
        ended_at=_session_optional_text(raw.get("endedAt")),
        end_reason=_session_optional_text(raw.get("endReason")),
        last_seen_at=_session_optional_text(raw.get("lastSeenAt")) or "",
        source=_session_optional_text(raw.get("source")) or "",
        generation=_session_generation(raw.get("generation")),
    )


def _should_prune(entry: SessionIndexEntry, now: datetime) -> bool:
    ended = _parse_time(entry.ended_at)
    if ended is not None and now - ended > _ENDED_PRUNE_AFTER:
        return True
    seen = _parse_time(entry.last_seen_at)
    if seen is not None and now - seen > _SEEN_PRUNE_AFTER:
        return True
    return False


def _prune_entries(
    entries: dict[str, SessionIndexEntry], now: datetime
) -> dict[str, SessionIndexEntry]:
    return {key: entry for key, entry in entries.items() if not _should_prune(entry, now)}


class SessionIndex:
    """``~/.config/devkit/session-index.json``。读者不加锁；写方法自持 ``session-index.lock``。"""

    def __init__(self, path: Path | None = None, *, home: Path | None = None) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            base = Path(home).expanduser() if home is not None else Path.home()
            self.path = base / ".devkit" / DEVKIT_SESSION_INDEX_REL
        self.lock_path = self.path.parent / DEVKIT_SESSION_INDEX_LOCK_REL

    def record_start(
        self,
        ref: SessionRef,
        proc_started_at: str | None,
        source: str,
        now: object,
        lock_timeout: float | None = None,
    ) -> SessionIndexEntry | None:
        try:
            return self._with_lock(
                lock_timeout,
                lambda: self.record_start_locked(ref, proc_started_at, source, now),
            )
        except (SessionIndexReadError, OSError):
            return None

    def record_start_locked(
        self,
        ref: SessionRef,
        proc_started_at: str | None,
        source: str,
        now: object,
    ) -> SessionIndexEntry:
        now_text = _format_time(now)
        entries = self._read_entries()
        key = session_key(ref.runtime, ref.session_id)
        previous = entries.get(key)
        same = previous is not None and _same_live_instance(previous, ref, proc_started_at)
        if same and previous is not None:
            started_at = previous.started_at or now_text
            generation = previous.generation if isinstance(previous.generation, int) else 0
        else:
            started_at = now_text
            generation = _next_generation(previous)
        workspace = ref.workspace
        if workspace:
            workspace = normalize_workspace(workspace)
        entry = SessionIndexEntry(
            runtime=str(ref.runtime),
            session_id=str(ref.session_id),
            pid=_parse_pid(ref.pid),
            proc_started_at=_session_optional_text(proc_started_at),
            workspace=workspace or "",
            started_at=started_at,
            ended_at=None,
            end_reason=None,
            last_seen_at=now_text,
            source=str(source or ""),
            generation=generation,
        )
        entries[key] = entry
        self._write_entries(_prune_entries(entries, _coerce_now(now)))
        return entry

    def record_end(
        self,
        runtime: str,
        session_id: str,
        reason: str,
        now: object,
        lock_timeout: float | None = None,
        expected_generation: int | None = None,
        expected_identity: SessionEndIdentity | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> SessionIndexEntry | None:
        try:
            written = self._with_lock(
                lock_timeout,
                lambda: self.record_end_locked(
                    runtime,
                    session_id,
                    reason,
                    now,
                    expected_generation,
                    expected_identity,
                    outcome,
                ),
            )
        except (SessionIndexReadError, OSError):
            _note_end_outcome(outcome, "failed", None)
            return None
        if written is None and (
            not isinstance(outcome, dict) or outcome.get("status") not in ("deferred", "rejected")
        ):
            _note_end_outcome(outcome, "failed", None)
        return written

    def record_end_locked(
        self,
        runtime: str,
        session_id: str,
        reason: str,
        now: object,
        expected_generation: int | None = None,
        expected_identity: SessionEndIdentity | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> SessionIndexEntry | None:
        now_text = _format_time(now)
        entries = self._read_entries()
        key = session_key(runtime, session_id)
        previous = entries.get(key)
        if expected_identity is not None:
            verdict = captured_end_verdict(previous, expected_identity)
            if verdict == "reject":
                _note_end_outcome(outcome, "rejected", previous)
                return previous
            if verdict == "defer":
                _note_end_outcome(outcome, "deferred", previous)
                return previous
            if previous is None:
                if expected_identity.state == "known":
                    captured_generation = expected_identity.generation
                    generation = captured_generation if type(captured_generation) is int and captured_generation >= 1 else 1
                    pid = expected_identity.pid if type(expected_identity.pid) is int else None
                    started = expected_identity.proc_started_at
                    if not isinstance(started, str) or not started:
                        started = None
                else:
                    generation = 1
                    pid = None
                    started = None
                entry = SessionIndexEntry(
                    runtime=str(runtime),
                    session_id=str(session_id),
                    pid=pid,
                    proc_started_at=started,
                    workspace="",
                    started_at=now_text,
                    ended_at=now_text,
                    end_reason=None if reason is None else str(reason),
                    last_seen_at=now_text,
                    source="session-end",
                    generation=generation,
                )
            else:
                previous.ended_at = now_text
                previous.end_reason = None if reason is None else str(reason)
                previous.last_seen_at = now_text
                entry = previous
            entries[key] = entry
            self._write_entries(_prune_entries(entries, _coerce_now(now)))
            _note_end_outcome(outcome, "applied", entry)
            return entry
        if (
            previous is not None
            and isinstance(expected_generation, int)
            and not isinstance(expected_generation, bool)
            and previous.generation != expected_generation
        ):
            _note_end_outcome(outcome, "rejected", previous)
            return previous
        if previous is None:
            generation = expected_generation if isinstance(expected_generation, int) and expected_generation >= 1 else 1
            entry = SessionIndexEntry(
                runtime=str(runtime),
                session_id=str(session_id),
                pid=None,
                proc_started_at=None,
                workspace="",
                started_at=now_text,
                ended_at=now_text,
                end_reason=None if reason is None else str(reason),
                last_seen_at=now_text,
                source="session-end",
                generation=generation,
            )
        else:
            previous.ended_at = now_text
            previous.end_reason = None if reason is None else str(reason)
            previous.last_seen_at = now_text
            entry = previous
        entries[key] = entry
        self._write_entries(_prune_entries(entries, _coerce_now(now)))
        return entry

    def touch(
        self,
        runtime: str,
        session_id: str,
        now: object,
        throttle_seconds: float = 60,
        lock_timeout: float | None = None,
    ) -> bool:
        try:
            wrote = self._with_lock(
                lock_timeout,
                lambda: self.touch_locked(runtime, session_id, now, throttle_seconds),
            )
        except (SessionIndexReadError, OSError):
            return False
        return bool(wrote)

    def touch_locked(
        self,
        runtime: str,
        session_id: str,
        now: object,
        throttle_seconds: float = 60,
    ) -> bool:
        """未到节流窗口、会话不存在或已结束时不写。真正写入时才清理过期条目。"""
        entries = self._read_entries()
        key = session_key(runtime, session_id)
        entry = entries.get(key)
        if entry is None or _present(entry.ended_at):
            return False
        now_dt = _coerce_now(now)
        seen = _parse_time(entry.last_seen_at)
        window = timedelta(seconds=float(throttle_seconds))
        if seen is not None and now_dt - seen < window:
            return False
        entry.last_seen_at = _format_time(now_dt)
        entries[key] = entry
        self._write_entries(_prune_entries(entries, now_dt))
        return True

    def get(self, runtime: str, session_id: str) -> SessionIndexEntry | None:
        return self._read_entries().get(session_key(runtime, session_id))

    def find_by_pid(self, runtime: str, pid: object) -> SessionIndexEntry | None:
        wanted = _parse_pid(pid)
        if wanted is None:
            return None
        matches = [
            entry
            for entry in self.live_entries()
            if entry.runtime == runtime and entry.pid == wanted
        ]
        if not matches:
            return None
        matches.sort(key=lambda item: item.last_seen_at, reverse=True)
        return matches[0]

    def live_entries(self) -> list[SessionIndexEntry]:
        rows = [entry for entry in self._read_entries().values() if not _present(entry.ended_at)]
        rows.sort(key=lambda item: (item.last_seen_at, item.key()))
        return rows

    def prune_locked(self, now: object) -> int:
        entries = self._read_entries()
        kept = _prune_entries(entries, _coerce_now(now))
        removed = len(entries) - len(kept)
        if removed:
            self._write_entries(kept)
        return removed

    def _with_lock(self, lock_timeout: float | None, fn: Callable[[], Any]) -> Any:
        timeout = _DEFAULT_INDEX_LOCK_TIMEOUT if lock_timeout is None else float(lock_timeout)
        try:
            if timeout <= 0:
                context = locked_path(self.lock_path, blocking=False)
            else:
                context = locked_path(self.lock_path, blocking=True, timeout=timeout)
            with context:
                return fn()
        except LockBusy:
            return None

    def _read_sessions(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise SessionIndexReadError("session index unreadable") from error
        try:
            document = json.loads(text)
        except ValueError as error:
            raise SessionIndexReadError("session index is not json") from error
        if not isinstance(document, dict):
            raise SessionIndexReadError("session index is not an object")
        sessions = document.get("sessions", {})
        if not isinstance(sessions, dict):
            raise SessionIndexReadError("sessions is not an object")
        return sessions

    def _read_entries(self) -> dict[str, SessionIndexEntry]:
        entries: dict[str, SessionIndexEntry] = {}
        for key, raw in self._read_sessions().items():
            parsed = _split_session_key(str(key))
            if parsed is None:
                continue
            entry = _entry_from_raw(parsed[0], parsed[1], raw)
            if entry is not None:
                entries[str(key)] = entry
        return entries

    def _write_entries(self, entries: Mapping[str, SessionIndexEntry]) -> None:
        sessions = {key: entries[key].to_json() for key in sorted(entries)}
        payload = {"schemaVersion": INDEX_SCHEMA_VERSION, "sessions": sessions}
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, text)


def _lease_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _lease_expiry(last_seen_at: object, lease_ttl_seconds: object) -> str | None:
    moment = _parse_time(last_seen_at)
    ttl = _lease_seconds(lease_ttl_seconds)
    if moment is None or ttl is None:
        return None
    return _format_time(moment + timedelta(seconds=ttl))


_DEFAULT_PROCESS_ALIVE = process_alive
_DEFAULT_PROCESS_START_TIME = process_start_time


class LivenessJudge:
    """唯一存活判定。``process_alive`` / ``process_start_time`` 默认为 textio，测试可注入。"""

    def __init__(
        self,
        process_alive: Callable[[int], bool | None] | None = None,
        process_start_time: Callable[[int], str | None] | None = None,
    ) -> None:
        self.process_alive = _DEFAULT_PROCESS_ALIVE if process_alive is None else process_alive
        self.process_start_time = (
            _DEFAULT_PROCESS_START_TIME if process_start_time is None else process_start_time
        )

    def judge(
        self,
        entry: SessionIndexEntry | None,
        now: object,
        lease_ttl_seconds: object,
    ) -> LivenessVerdict:
        if entry is None:
            return LivenessVerdict("unknown", "no-entry", None)
        expiry = _lease_expiry(entry.last_seen_at, lease_ttl_seconds)
        if _present(entry.ended_at):
            return LivenessVerdict("ended", "ended-marker", expiry)
        pid = entry.pid if isinstance(entry.pid, int) and entry.pid > 0 else None
        if pid is not None:
            alive = self.process_alive(pid)
            if alive is False:
                return LivenessVerdict("ended", "pid-dead", expiry)
            if alive is True:
                recorded = _session_optional_text(entry.proc_started_at)
                if recorded is not None:
                    actual = self.process_start_time(pid)
                    actual_text = _session_optional_text(actual)
                    if actual_text is not None and not _same_instant(recorded, actual_text):
                        return LivenessVerdict("ended", "pid-reused", expiry)
                return LivenessVerdict("alive", "pid-alive", expiry)
        return self._lease_verdict(now, expiry)

    def judge_indexed(
        self,
        index: SessionIndex,
        runtime: str,
        session_id: str,
        now: object,
        lease_ttl_seconds: object,
    ) -> LivenessVerdict:
        """索引读失败 → unknown。键不存在走 ``judge(None)``，同样是 unknown，basis 不同。"""
        try:
            entry = index.get(runtime, session_id)
        except SessionIndexReadError:
            return LivenessVerdict("unknown", "index-unreadable", None)
        return self.judge(entry, now, lease_ttl_seconds)

    def _lease_verdict(self, now: object, expiry: str | None) -> LivenessVerdict:
        now_dt = _coerce_now(now)
        expires_at = _parse_time(expiry)
        if expires_at is None or now_dt >= expires_at:
            return LivenessVerdict("ended", "lease-expired", expiry)
        return LivenessVerdict("alive", "lease", expiry)


def _chain_rows(
    current_pid: int | None,
    parent_chain: Callable[..., Sequence[tuple[int, int, str]]] | None,
) -> list[tuple[int, int, str]]:
    chain_fn = parent_chain if parent_chain is not None else parent_pid_chain
    pid = current_pid if isinstance(current_pid, int) and current_pid > 0 else os.getpid()
    try:
        rows = chain_fn(pid, PR_REVIEW_WATCH_PARENT_CHAIN_DEPTH)
    except Exception:
        return []
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        return []
    cleaned: list[tuple[int, int, str]] = []
    for row in list(rows)[:PR_REVIEW_WATCH_PARENT_CHAIN_DEPTH]:
        if not isinstance(row, tuple) or len(row) < 3:
            continue
        row_pid = row[0]
        if not isinstance(row_pid, int) or row_pid <= 0:
            continue
        cleaned.append((row_pid, row[1] if isinstance(row[1], int) else 0, str(row[2])))
    return cleaned


def _pid_from_chain(
    runtime: str,
    current_pid: int | None,
    parent_chain: Callable[..., Sequence[tuple[int, int, str]]] | None,
) -> int | None:
    names = _EXECUTABLES.get(runtime)
    if not names:
        return None
    for row_pid, _ppid, name in _chain_rows(current_pid, parent_chain):
        if _process_basename(name) in names:
            return row_pid
    return None


def resolve_runtime_pid(
    runtime: str,
    environ: Mapping[str, str] | None = None,
    *,
    current_pid: int | None = None,
    parent_chain: Callable[..., Sequence[tuple[int, int, str]]] | None = None,
    start_time: Callable[[int], str | None] | None = None,
) -> ResolvedPid:
    """claude-code 先读 ``CLAUDE_PID``，否则沿父进程链至多 8 层匹配可执行名。同时取启动时间。"""
    env = os.environ if environ is None else environ
    pid: int | None = None
    if runtime == "claude-code" and isinstance(env, Mapping):
        pid = _parse_pid(env.get(PR_REVIEW_WATCH_CLAUDE_PID_ENV))
    if pid is None:
        pid = _pid_from_chain(runtime, current_pid, parent_chain)
    if pid is None:
        return ResolvedPid(None, None)
    start_fn = start_time if start_time is not None else process_start_time
    started: str | None = None
    try:
        raw_started = start_fn(pid)
    except Exception:
        raw_started = None
    if isinstance(raw_started, str) and raw_started.strip():
        started = raw_started.strip()
    return ResolvedPid(pid, started)


def _explicit_pair(
    explicit: object,
    explicit_runtime: str | None,
    explicit_session_id: str | None,
) -> tuple[str, str, str]:
    runtime: object = explicit_runtime
    session_id: object = explicit_session_id
    if explicit is not None:
        if isinstance(explicit, SessionRef):
            runtime = explicit.runtime
            session_id = explicit.session_id
        elif isinstance(explicit, (tuple, list)) and len(explicit) == 2:
            runtime = explicit[0]
            session_id = explicit[1]
        elif isinstance(explicit, dict):
            runtime = explicit.get("runtime", runtime)
            session_id = explicit.get("session_id", explicit.get("sessionId", session_id))
        else:
            return "invalid", "", ""
    runtime_text = runtime.strip() if isinstance(runtime, str) else ""
    session_text = session_id.strip() if isinstance(session_id, str) else ""
    if not runtime_text and not session_text:
        return "absent", "", ""
    if not runtime_text or not session_text:
        return "invalid", "", ""
    return "given", runtime_text, session_text


def _env_sessions(environ: Mapping[str, str] | None) -> list[tuple[str, str]]:
    if not isinstance(environ, Mapping):
        return []
    found: list[tuple[str, str]] = []
    for runtime, variable in PR_REVIEW_WATCH_SESSION_ENV_VARS:
        value = environ.get(variable)
        if isinstance(value, str) and value.strip():
            found.append((runtime, value.strip()))
    return found


def _ref_from_entry(entry: SessionIndexEntry) -> SessionRef:
    return SessionRef(
        runtime=entry.runtime,
        session_id=entry.session_id,
        pid=entry.pid,
        workspace=entry.workspace,
    )


def _lookup_entry(
    index: SessionIndex, runtime: str, session_id: str
) -> tuple[SessionIndexEntry | None, bool]:
    try:
        return index.get(runtime, session_id), False
    except SessionIndexReadError:
        return None, True


def resolve_current_session(
    environ: Mapping[str, str] | None = None,
    explicit: object = None,
    *,
    explicit_runtime: str | None = None,
    explicit_session_id: str | None = None,
    home: Path | None = None,
    index: SessionIndex | None = None,
    current_pid: int | None = None,
    parent_chain: Callable[..., Sequence[tuple[int, int, str]]] | None = None,
    start_time: Callable[[int], str | None] | None = None,
) -> SessionRef | None:
    """四步解析。失败返回 None，调用方抛 ``PRV-026``。

    claude 子 agent（``CLAUDE_CODE_CHILD_SESSION=1``）的 ``CLAUDE_CODE_SESSION_ID``
    已是父会话 id，按该 id 登记，不另造子会话。
    """
    env = os.environ if environ is None else environ
    store = index if index is not None else SessionIndex(home=home)
    kind, runtime, session_id = _explicit_pair(explicit, explicit_runtime, explicit_session_id)
    if kind == "invalid":
        return None
    if kind == "given":
        if runtime not in _KNOWN_RUNTIMES:
            return None
        entry, failed = _lookup_entry(store, runtime, session_id)
        if failed or entry is None or _present(entry.ended_at):
            return None
        return _ref_from_entry(entry)

    found = _env_sessions(env if isinstance(env, Mapping) else None)
    if len(found) == 1:
        env_runtime, env_session = found[0]
        return _ref_from_single_env(
            store, env_runtime, env_session, env, current_pid, parent_chain, start_time
        )
    if len(found) > 1:
        return _ref_from_nearest_env(
            store, found, current_pid, parent_chain
        )
    return _ref_from_parent_chain(store, current_pid, parent_chain)


def _ref_from_single_env(
    store: SessionIndex,
    runtime: str,
    session_id: str,
    environ: Mapping[str, str],
    current_pid: int | None,
    parent_chain: Callable[..., Sequence[tuple[int, int, str]]] | None,
    start_time: Callable[[int], str | None] | None,
) -> SessionRef:
    entry, _failed = _lookup_entry(store, runtime, session_id)
    if entry is not None and not _present(entry.ended_at) and entry.pid is not None:
        return _ref_from_entry(entry)
    resolved = resolve_runtime_pid(
        runtime,
        environ,
        current_pid=current_pid,
        parent_chain=parent_chain,
        start_time=start_time,
    )
    workspace = entry.workspace if entry is not None else ""
    return SessionRef(runtime, session_id, resolved.pid, workspace)


def _ref_from_nearest_env(
    store: SessionIndex,
    found: Sequence[tuple[str, str]],
    current_pid: int | None,
    parent_chain: Callable[..., Sequence[tuple[int, int, str]]] | None,
) -> SessionRef | None:
    distance: dict[int, int] = {}
    for index, (row_pid, _ppid, _name) in enumerate(_chain_rows(current_pid, parent_chain)):
        if row_pid not in distance:
            distance[row_pid] = index
    best: SessionIndexEntry | None = None
    best_distance: int | None = None
    for runtime, session_id in found:
        entry, failed = _lookup_entry(store, runtime, session_id)
        if failed:
            return None
        if entry is None or _present(entry.ended_at) or entry.pid is None:
            continue
        if entry.pid not in distance:
            continue
        candidate = distance[entry.pid]
        if best_distance is None or candidate < best_distance:
            best = entry
            best_distance = candidate
    if best is None:
        return None
    return _ref_from_entry(best)


def _ref_from_parent_chain(
    store: SessionIndex,
    current_pid: int | None,
    parent_chain: Callable[..., Sequence[tuple[int, int, str]]] | None,
) -> SessionRef | None:
    try:
        entries = store.live_entries()
    except SessionIndexReadError:
        return None
    by_pid: dict[int, SessionIndexEntry] = {}
    for entry in entries:
        if entry.pid is None:
            continue
        current = by_pid.get(entry.pid)
        if current is None or entry.last_seen_at > (current.last_seen_at or ""):
            by_pid[entry.pid] = entry
    for row_pid, _ppid, _name in _chain_rows(current_pid, parent_chain):
        entry = by_pid.get(row_pid)
        if entry is not None:
            return _ref_from_entry(entry)
    return None
