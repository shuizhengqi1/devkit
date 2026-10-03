"""Codex-only native role registration binding shared by runtime and doctor."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from devkit_flow.constants import RUNTIME_PLUGIN_NAME
from devkit_flow.errors import DevkitFlowError
from devkit_flow.textio import compact_error, current_cli_path
from devkit_flow.version import VERSION


def codex_user_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".codex"


def codex_agent_registry(plugin_root: Path | None = None) -> Any:
    """Load shipped support from either a source checkout or the npm package."""
    cli_dir = current_cli_path().parent
    source_root = cli_dir.parent.parent if cli_dir.name == "devkit_flow" and cli_dir.parent.name == "src" else None
    support_candidates = [cli_dir / "runtime-support", cli_dir.parent / "scripts"]
    if source_root is not None:
        support_candidates.append(source_root / "scripts")
    support = next((path for path in support_candidates
                    if (path / "codex_agent_registry" / "__init__.py").is_file()), None)
    if support is None:
        raise DevkitFlowError("Codex agent registration support is missing; reinstall devkit-flow")
    if str(support) not in sys.path:
        sys.path.insert(0, str(support))
    from codex_agent_registry import Registry
    if plugin_root is None:
        payload_candidates = [
            cli_dir / "runtime-plugins" / "codex" / RUNTIME_PLUGIN_NAME,
            cli_dir / "runtimes" / "codex" / RUNTIME_PLUGIN_NAME,
            cli_dir.parent / "runtime-plugins" / "codex" / RUNTIME_PLUGIN_NAME,
        ]
        if source_root is not None:
            payload_candidates.append(source_root / "runtime-plugins" / "codex" / RUNTIME_PLUGIN_NAME)
        plugin_root = next((path for path in payload_candidates if path.is_dir()), None)
    if plugin_root is None:
        raise DevkitFlowError("Codex plugin payload is missing; reinstall devkit-flow")
    return Registry(plugin_root=plugin_root, codex_home=codex_user_home(), version=VERSION)


def codex_agent_registration_check() -> dict[str, Any]:
    """Read-only disk verification; it does not claim an existing task reloaded roles."""
    try:
        return codex_agent_registry().check()
    except (OSError, ValueError, ImportError, DevkitFlowError) as error:
        return {
            "ready": False, "role_names": [], "role_count": 0,
            "version": VERSION, "config_path": str(codex_user_home() / "config.toml"),
            "message": f"cannot verify Codex agent registration: {compact_error(error)}",
        }
