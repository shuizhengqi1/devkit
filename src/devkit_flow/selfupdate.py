from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from devkit_flow.constants import DEVKIT_CLI_RELATIVE_PATH, NPM_INSTALL_TIMEOUT_SECONDS
from devkit_flow.errors import DevkitFlowError
from devkit_flow.textio import compact_error, emit, npm_view_latest_version, version_tuple
from devkit_flow.version import NPM_PACKAGE_NAME, NPM_REGISTRY


def find_global_cli() -> Path | None:
    """定位全局 CLI：PATH 上的 devkit-flow（npm i -g 的 npm 全局 bin）优先，~/.config/devkit/bin 兜底。

    两处都没有时返回 None，由调用方决定报错还是回退到当前运行文件。
    """
    on_path = shutil.which("devkit-flow")
    if on_path:
        return Path(on_path)
    fallback = Path.home() / ".config" / "devkit" / "bin" / "devkit-flow"
    if fallback.is_file():
        return fallback
    return None


# npm 发行形态（016 T-31 / D-91）：PATH 上的 devkit-flow 是薄启动器，代码本体是同级 devkit_flow_main.py。
NPM_CLI_MODULE_FILENAME = "devkit_flow_main.py"


def cli_code_path(path: Path) -> Path:
    """CLI 文件的代码本体：npm 发行形态的启动器 ``devkit-flow`` 取同级模块，其余形态（单文件 bundle）取文件本身。

    与 ``current_cli_path()``（运行中的代码本体）同口径比较内容，用于 doctor 判断全局 CLI 是否就是正在运行的 CLI。
    """
    resolved = Path(path).resolve()
    module = resolved.with_name(NPM_CLI_MODULE_FILENAME)
    if resolved.name == "devkit-flow" and module.is_file():
        return module
    return resolved


def extract_embedded_version(path: Path) -> str | None:
    """不执行文件，从头部文本中提取 VERSION 常量（读不到/没有则 None）。"""
    try:
        with path.open("rb") as stream:
            text = stream.read(4096).decode("utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    return match.group(1) if match else None


def npm_install_latest() -> subprocess.CompletedProcess[str]:
    """npm 通道自更新：npm i -g <pkg>@latest --registry=<registry>（超时/失败由调用方上报）。"""
    npm = shutil.which("npm")
    if not npm:
        raise DevkitFlowError("npm is not available on PATH; cannot self-update via the npm channel")
    return subprocess.run(
        [npm, "i", "-g", f"{NPM_PACKAGE_NAME}@latest", f"--registry={NPM_REGISTRY}"],
        capture_output=True,
        text=True,
        timeout=NPM_INSTALL_TIMEOUT_SECONDS,
    )


def self_update_result(*, check: bool) -> tuple[int, dict[str, Any]]:
    """执行 npm CLI 升级判定并返回结构化结果；由 self-update / upgrade 共用。"""
    installed = shutil.which("devkit-flow")
    current_version = extract_embedded_version(Path(installed)) if installed else None
    remote_version = npm_view_latest_version()
    base: dict[str, Any] = {
        "channel": "npm",
        "package": NPM_PACKAGE_NAME,
        "registry": NPM_REGISTRY,
        "current_version": current_version,
        "remote_version": remote_version,
    }
    if remote_version is None:
        return (
            1,
            {
                **base,
                "status": "error",
                "message": (
                    f"cannot query {NPM_PACKAGE_NAME} from {NPM_REGISTRY} "
                    "(npm missing on PATH, registry unreachable, or package not published)."
                ),
            },
        )
    update_available = current_version is None or version_tuple(remote_version) > version_tuple(current_version)
    if check:
        return (
            0,
            {
                **base,
                "status": "update-available" if update_available else "up-to-date",
                "latest": remote_version,
                "update_available": update_available,
                "message": (
                    f"devkit-flow update available: {current_version or 'not installed'} -> {remote_version}."
                    if update_available
                    else f"devkit-flow is already up to date ({current_version})."
                ),
            },
        )
    if not update_available:
        return (
            0,
            {
                **base,
                "status": "up-to-date",
                "message": f"devkit-flow is already up to date ({current_version}).",
            },
        )
    try:
        result = npm_install_latest()
    except (OSError, subprocess.TimeoutExpired, DevkitFlowError) as error:
        return 1, {**base, "status": "error", "message": f"npm install failed: {compact_error(error)}"}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()[:200]
        return 1, {**base, "status": "error", "message": f"npm install failed: {detail}"}
    status = "updated" if current_version else "installed"
    return (
        0,
        {
            **base,
            "status": status,
            "from_version": current_version,
            "to_version": remote_version,
            "message": (
                f"devkit-flow {status}: {current_version or 'not installed'} -> {remote_version} "
                f"via npm i -g {NPM_PACKAGE_NAME}."
            ),
        },
    )


def command_self_update(args: argparse.Namespace) -> int:
    """npm 通道自更新；--check 只报告，结构化逻辑与统一 upgrade 命令共用。"""
    exit_code, result = self_update_result(check=args.check)
    emit(result, args.format)
    return exit_code
