"""Local no-op telemetry.

The public build does not phone home and does not collect identity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class RuntimeContext:
    def __init__(self, name: str, session_id: str | None = None, attribution: str = "unattributed") -> None:
        self.name = name
        self.session_id = session_id
        self.attribution = attribution


class FeatureRef:
    def __init__(self, dir: str | None = None, name: str | None = None) -> None:
        self.dir = dir
        self.name = name


class TelemetryConfig:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.enabled = False


class TelemetryEmitter:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.enabled = False

    def flush_with_snapshot_safe(self, *args: Any, **kwargs: Any) -> None:
        return None

    def enqueue_event_safe(self, *args: Any, **kwargs: Any) -> None:
        return None


def env_session_ref() -> None:
    return None


def schedule_background_flush(workspace: Path) -> None:
    del workspace
    return None


def collect_feature_quality_events(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"status": "skipped", "reason": "telemetry-off"}


def offer_sso_login(*args: Any, **kwargs: Any) -> None:
    return None


def telemetry_doctor_check(workspace: Path) -> dict[str, Any]:
    del workspace
    return {"status": "ok", "message": "telemetry off", "fix": ""}


def telemetry_config_summary(workspace: Path) -> dict[str, Any]:
    del workspace
    return {"enabled": False, "source": "public"}


def session_ended_extended_stats(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {}
