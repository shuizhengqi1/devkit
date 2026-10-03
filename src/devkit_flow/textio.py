from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from devkit_flow.constants import ANSI_ESCAPE, LATEST_CHECK_CACHE_RELATIVE_PATH, MEP_INSTALL_COMMAND, NPM_VIEW_TIMEOUT_SECONDS
from devkit_flow.errors import DevkitFlowError, ExternalCommandError
from devkit_flow.version import NPM_PACKAGE_NAME, NPM_REGISTRY, VERSION


def atomic_write_text(path: Path, content: str) -> None:
    _atomic_replace_bytes(path, content.encode("utf-8"))


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """跨进程排他锁。锁文件放在系统临时目录，不污染目标路径所在目录。"""
    lock_root = Path(tempfile.gettempdir()) / "devkit-flow-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_root.chmod(0o700)
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{digest}.lock"
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(lock_path, 0o600)
    with os.fdopen(lock_descriptor, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def emit(data: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if "message" in data:
        print(data["message"])
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def attach_dashboard_warning(
    data: dict[str, Any],
    refresh: Callable[[Path, dict[str, Any]], str | None] | None,
    workspace: Path,
    feature: dict[str, Any],
) -> None:
    """状态写入命令尾部的 dashboard 自动重渲染统一挂钩（设计 §4.3.3；D-13 cli.py 组装注入）。

    refresh 为 None（未注入）时 no-op；回调契约 fail-safe（返回 warning 或 None），
    此处仍兜底吞掉异常——任何情况下重渲染都不得阻断状态写入命令本身。
    有 warning 时挂到 data["dashboard"]["warning"]（接口文档 §4.8，仅失败时出现）。
    """
    if refresh is None:
        return
    try:
        warning = refresh(workspace, feature)
    except Exception as error:  # 兜底防线：回调失守也不阻断状态写入（D-5）
        warning = f"dashboard 自动重渲染失败（状态写入已完成，不受影响）: {compact_error(error)}"
    if warning:
        data["dashboard"] = {"warning": warning}


def run_external(
    command: list[str],
    *,
    check: bool = True,
    foreground: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an external command.

    ``timeout`` is optional and omitted from subprocess.run when None, so
    existing callers stay unchanged. Timed-out processes degrade as failure:
    ``check=True`` raises ExternalCommandError; ``check=False`` returns a
    non-zero CompletedProcess (identity-chain callers then fall through).
    """
    run_kwargs: dict[str, Any] = {"text": True}
    if timeout is not None:
        run_kwargs["timeout"] = timeout
    try:
        if foreground:
            result = subprocess.run(
                command, stdout=sys.stderr, stderr=sys.stderr, **run_kwargs
            )
        else:
            result = subprocess.run(command, capture_output=True, **run_kwargs)
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        if isinstance(error.stdout, (bytes, bytearray)):
            stdout = error.stdout.decode("utf-8", errors="replace")
        if isinstance(error.stderr, (bytes, bytearray)):
            stderr = error.stderr.decode("utf-8", errors="replace")
        result = subprocess.CompletedProcess(
            list(command),
            124,
            stdout or "",
            stderr or f"timed out after {timeout}s",
        )
        if check:
            raise ExternalCommandError(command, result) from error
        return result
    if check and result.returncode != 0:
        raise ExternalCommandError(command, result)
    return result


def mep_logged_in(*, timeout: float | None = None) -> tuple[bool, str]:
    result = run_external(["external-cli", "ones", "sso", "status"], check=False, timeout=timeout)
    output = ANSI_ESCAPE.sub("", f"{result.stdout}\n{result.stderr}").strip()
    logged_in = (
        result.returncode == 0
        and "已登录" in output
        and "未登录" not in output
        and "失效" not in output
        and "过期" not in output
    )
    return logged_in, output


def mep_current_user(*, timeout: float | None = None) -> str:
    """Return the authenticated ONES MIS without reading or persisting credentials."""
    result = run_external(["external-cli", "ones", "config"], timeout=timeout)
    output = ANSI_ESCAPE.sub("", f"{result.stdout}\n{result.stderr}")
    for pattern in (
        r"MIS\s*(?:账号|account)\s*[：:]\s*([^\s]+)",
        r"(?:当前用户|current\s+user)\s*[：:]\s*([^\s]+)",
    ):
        match = re.search(pattern, output, flags=re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    raise DevkitFlowError(
        "Unable to determine the current ONES login MIS from `external-cli ones config`; "
        "provide --requirement-assignee explicitly."
    )


def ensure_mep_and_login() -> dict[str, Any]:
    installed = False
    if shutil.which("external-cli") is None:
        if shutil.which("npm") is None:
            raise DevkitFlowError("ONES is enabled but npm is not installed; cannot install .")
        run_external(MEP_INSTALL_COMMAND)
        installed = True
        if shutil.which("external-cli") is None:
            raise DevkitFlowError("Installed  but `external-cli` is still not available on PATH.")

    logged_in, _ = mep_logged_in()
    login_performed = False
    if not logged_in:
        run_external(["external-cli", "ones", "sso", "login"], foreground=True)
        login_performed = True
        logged_in, status_output = mep_logged_in()
        if not logged_in:
            raise DevkitFlowError(f"external-cli ones sso login did not establish a valid session: {status_output}")

    return {
        "mep_installed": True,
        "installed_now": installed,
        "logged_in": True,
        "login_performed": login_performed,
    }


def parse_json_output(output: str) -> Any:
    cleaned = ANSI_ESCAPE.sub("", output).strip()
    if not cleaned:
        return {}
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    starts = [index for index in (cleaned.find("{"), cleaned.find("[")) if index >= 0]
    if starts:
        start = min(starts)
        for closing in ("}", "]"):
            end = cleaned.rfind(closing)
            if end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    continue
    raise DevkitFlowError("external-cli returned non-JSON output where JSON was required.")


def run_mep_json(arguments: list[str]) -> Any:
    result = run_external(["external-cli", "ones", *arguments])
    return parse_json_output(result.stdout)


def extract_numeric_id(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("issueId", "workItemId", "id"):
            value = data.get(key)
            if isinstance(value, (str, int)) and str(value).isdigit():
                return str(value)
            if isinstance(value, dict):
                nested = value.get("value", value.get("id"))
                if isinstance(nested, (str, int)) and str(nested).isdigit():
                    return str(nested)
        for key in ("data", "result", "issue", "workItem"):
            if key in data:
                found = extract_numeric_id(data[key])
                if found:
                    return found
        for value in data.values():
            found = extract_numeric_id(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = extract_numeric_id(item)
            if found:
                return found
    return ""


def find_objects_with_type(data: Any, target_type: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(data, dict):
        if data.get("type") == target_type:
            found.append(data)
        for value in data.values():
            found.extend(find_objects_with_type(value, target_type))
    elif isinstance(data, list):
        for item in data:
            found.extend(find_objects_with_type(item, target_type))
    return found


def compact_error(error: Exception) -> str:
    text = ANSI_ESCAPE.sub("", str(error)).replace("\n", " ").strip()
    return text[:160] or error.__class__.__name__


def mep_bootstrapped(*, timeout: float | None = None) -> bool:
    """external-cli 已安装且已登录（只读检查，不触发安装/登录）。"""
    if shutil.which("external-cli") is None:
        return False
    logged_in, _output = mep_logged_in(timeout=timeout)
    return logged_in


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_replace_bytes(path, content)


def _read_umask() -> int:
    """读取当前 umask。os.umask 是进程级全局状态：读必须先 set 再立刻 restore，非线程安全。"""
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _atomic_write_mode(target: Path) -> int:
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        return 0o666 & ~_read_umask()


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    file_descriptor, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temp_file:
            temp_file.write(content)
        os.chmod(temp_path, _atomic_write_mode(path))
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def ones_workitem_url(space_id: str, kind: str, workitem_id: str) -> str:
    """ONES 工作项详情页地址；kind 为 requirement 或 task。"""
    return f"/ones/product/{space_id}/workItem/{kind}/detail/{workitem_id}"


# --- 工作区初始化 / 自更新 / 体检（setup / self-update / doctor） ---------------------
def current_cli_path() -> Path:
    """当前正在运行的 CLI 文件（脚本直跑与测试加载模块两种场景都取 __file__）。"""
    return Path(__file__).resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def npm_view_latest_version(timeout: int = NPM_VIEW_TIMEOUT_SECONDS) -> str | None:
    """npm 通道远端版本探测：npm view <pkg> version --registry=<registry>，stdout 即版本号。

    npm 不在 PATH、命令失败/超时、输出不是 x.y.z 时一律返回 None（调用方按失败静默处理）。
    """
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        result = subprocess.run(
            [npm, "view", NPM_PACKAGE_NAME, "version", f"--registry={NPM_REGISTRY}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    latest = (result.stdout or "").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", latest):
        return None
    return latest


def version_tuple(text: str) -> tuple[int, ...]:
    """x.y.z → (x, y, z)，用于简单的版本号数字元组比较。"""
    return tuple(int(piece) for piece in text.strip().split("."))


def latest_check_cache_path() -> Path:
    return Path.home() / LATEST_CHECK_CACHE_RELATIVE_PATH


def read_latest_check_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("checkedAt"), str):
        return None
    return data


def write_latest_check_cache(path: Path, latest: str | None) -> None:
    payload = {"checkedAt": datetime.now(timezone.utc).isoformat(), "latest": latest}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 缓存写不进去不影响主命令


def maybe_check_latest_update(ttl_hours: float = 4) -> dict[str, Any] | None:
    """检查是否有新版本 devkit-flow（npm 通道）；任何失败都静默返回 None，绝不打断主命令。

    - 远端版本探测走 npm view；npm 不在 PATH 或查询失败时返回 None。
    - 结果缓存 ~/.devkit/latest-check.json：距上次检查 < ttl_hours 直接读缓存，不访问网络；
      失败同样写入失败时间戳（latest 为 null），避免每次命令都重复打网络。
    """
    cache_path = latest_check_cache_path()
    now = datetime.now(timezone.utc)
    cached = read_latest_check_cache(cache_path)
    if cached is not None:
        try:
            checked_at = datetime.fromisoformat(cached["checkedAt"])
        except ValueError:
            checked_at = None
        if checked_at is not None:
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=timezone.utc)
            if now - checked_at < timedelta(hours=ttl_hours):
                latest = cached.get("latest")
                if not isinstance(latest, str) or not re.fullmatch(r"\d+\.\d+\.\d+", latest):
                    return None  # 缓存的是失败时间戳：TTL 内直接放弃，不再联网
                return {
                    "current": VERSION,
                    "latest": latest,
                    "update_available": version_tuple(latest) > version_tuple(VERSION),
                }
    latest = npm_view_latest_version()
    if latest is None:  # npm 不可用/查询失败：静默并记录失败时间戳
        write_latest_check_cache(cache_path, None)
        return None
    write_latest_check_cache(cache_path, latest)
    return {
        "current": VERSION,
        "latest": latest,
        "update_available": version_tuple(latest) > version_tuple(VERSION),
    }


def emit_update_notice(update: dict[str, Any] | None, output_format: str) -> None:
    """text 输出末尾追加一行升级提示；仅在有新版本且非 JSON 输出时打印。"""
    if output_format == "json" or not update or not update.get("update_available"):
        return
    print(
        f"发现新版本 devkit-flow {update['latest']}（当前 {update['current']}），"
        "可执行 devkit-flow self-update 升级。"
    )
