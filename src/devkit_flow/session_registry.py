from __future__ import annotations

import argparse
import json
import os
import select
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from devkit_flow.constants import DASHBOARD_STEP_TIMINGS_FILE, STEP_TIMINGS_SCHEMA_VERSION
from devkit_flow.errors import DevkitFlowError
from devkit_flow.parse_util import find_features, select_feature_for_ones
from devkit_flow.textio import compact_error, emit

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
) -> str | None:
    """追加一条 session marker；行小，open append 原子性足够。

    fail-safe：任何异常静默返回 warning 字符串（正常返回 None），绝不抛出。
    """
    try:
        path = registry_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        marker = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "runtime": str(runtime),
            "session_id": session_id or None,
            "transcript_path": transcript_path or None,
            "session_file": session_file or None,
            "workspace": normalize_workspace(workspace),
            "source": source,
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(marker, ensure_ascii=False) + "\n")
        return None
    except Exception as error:  # fail-safe 兜底：marker 写入失败不得影响主流程
        return f"session marker 写入失败（不影响主流程）: {compact_error(error)}"


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
