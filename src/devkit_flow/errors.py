from __future__ import annotations

import subprocess
from typing import Any


class DevkitFlowError(RuntimeError):
    def __init__(self, *args: object, error_code: str | None = None, **kwargs: object):
        super().__init__(*args, **kwargs)
        self.error_code = error_code


class ExternalCommandError(DevkitFlowError):
    def __init__(self, command: list[str], result: subprocess.CompletedProcess[str]):
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        super().__init__(f"{' '.join(command)}: {detail}")
        self.command = command
        self.result = result


class OnesTransitionFieldsRequired(DevkitFlowError):
    def __init__(
        self,
        issue_id: str,
        issue_type: str,
        desired_state: str,
        config_group: str,
        missing_fields: list[dict[str, Any]],
    ):
        self.issue_id = issue_id
        self.issue_type = issue_type
        self.desired_state = desired_state
        self.config_group = config_group
        self.missing_fields = missing_fields
        names = ", ".join(
            f"{field.get('name', field.get('variable', '必填字段'))}"
            f"({field.get('variable', '-')})"
            for field in missing_fields
        )
        super().__init__(
            f"ONES {issue_type} {issue_id} transition to {desired_state} needs user input: {names}"
        )

    def payload(self, scope: str) -> dict[str, Any]:
        option = {
            "requirement": "--requirement-field",
            "designTask": "--design-task-field",
            "task": "--task-field",
        }[self.config_group]
        arguments = " ".join(
            f"{option} '{field.get('name', field.get('variable', 'FIELD'))}=<value>'"
            for field in self.missing_fields
            if not str(field.get("variable", "")).startswith("__")
        )
        configure_command = (
            f"devkit-flow ones configure --scope project --enable {arguments}".strip()
            if arguments
            else ""
        )
        return {
            "scope": scope,
            "group": self.config_group,
            "issue_id": self.issue_id,
            "issue_type": self.issue_type,
            "desired_state": self.desired_state,
            "missing_fields": self.missing_fields,
            "configure_command": configure_command,
        }
