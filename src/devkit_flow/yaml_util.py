from __future__ import annotations

import json
import re
from typing import Any
from devkit_flow.constants import SRE_ALLOWED_CAPABILITIES_KEYS, SRE_ALLOWED_DB_KEYS, SRE_ALLOWED_DEFAULTS_KEYS, SRE_ALLOWED_DEPLOY_KEYS, SRE_ALLOWED_ENV_KEYS, SRE_ALLOWED_INFERENCE_KEYS, SRE_ALLOWED_IDGEN_KEYS, SRE_ALLOWED_CONFIG_KEYS, SRE_ALLOWED_LOGS_KEYS, SRE_ALLOWED_REGISTRY_KEYS, SRE_ALLOWED_PROJECT_KEYS, SRE_ALLOWED_ROOT_KEYS, SRE_ALLOWED_SERVICE_KEYS, SRE_ALLOWED_GATEWAY_GROUP_KEYS, SRE_ALLOWED_GATEWAY_KEYS, SRE_EXTENDED_CAPABILITIES, SRE_FORBIDDEN_KEY_TOKENS, SRE_SERVICE_ID_RE, SRE_UNSAFE_YAML


class ConstrainedYamlError(ValueError):
    """Raised when text is outside the supported YAML 1.2 subset."""


def fold_sre_key(name: str) -> str:
    return re.sub(r"[_-]", "", str(name).lower())


def collect_forbidden_keys(value: Any, found: list[str] | None = None) -> list[str]:
    hits = found if found is not None else []
    if isinstance(value, dict):
        for key, child in value.items():
            folded = fold_sre_key(str(key))
            if any(token in folded for token in SRE_FORBIDDEN_KEY_TOKENS):
                hits.append(str(key))
            collect_forbidden_keys(child, hits)
    elif isinstance(value, list):
        for item in value:
            collect_forbidden_keys(item, hits)
    return hits


def _yaml_strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_double:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            return line[:index].rstrip()
    return line.rstrip()


def _yaml_unescape(text: str) -> str:
    return (
        text.replace("\\\\", "\\")
        .replace("\\\"", '"')
        .replace("\\n", "\n")
        .replace("\\t", "\t")
    )


def parse_constrained_yaml_scalar(raw: str) -> Any:
    text = raw.strip()
    if text in {"", "~", "null", "Null", "NULL"}:
        return None
    if text == "[]":
        return []
    if text == "{}":
        return {}
    if text in {"true", "True", "TRUE"}:
        return True
    if text in {"false", "False", "FALSE"}:
        return False
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        inner = text[1:-1]
        return _yaml_unescape(inner) if text[0] == '"' else inner.replace("''", "'")
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", text):
        return int(text)
    return text


def _yaml_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_constrained_yaml(text: str) -> Any:
    if SRE_UNSAFE_YAML.search(text):
        raise ConstrainedYamlError("YAML subset forbids anchors, aliases, tags, and merge keys")
    prepared: list[str] = []
    for raw_line in text.splitlines():
        stripped = _yaml_strip_comment(raw_line.rstrip("\n"))
        if stripped.strip() == "":
            continue
        if stripped.lstrip().startswith("\t"):
            raise ConstrainedYamlError("tabs are not allowed")
        prepared.append(stripped)

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(prepared):
            return {}, index
        if _yaml_indent(prepared[index]) < indent:
            return {}, index
        if prepared[index].lstrip().startswith("- "):
            return parse_sequence(index, indent)
        return parse_mapping(index, indent)

    def parse_mapping(index: int, indent: int) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        while index < len(prepared):
            line = prepared[index]
            current = _yaml_indent(line)
            if current < indent:
                break
            if current > indent:
                raise ConstrainedYamlError(f"unexpected indent at: {line.strip()}")
            if line.lstrip().startswith("- "):
                raise ConstrainedYamlError("sequence item is not valid in a mapping")
            body = line[current:]
            if ":" not in body:
                raise ConstrainedYamlError(f"expected mapping entry: {body}")
            key, rest = body.split(":", 1)
            key = key.strip()
            if not key:
                raise ConstrainedYamlError("empty mapping key")
            rest = rest.strip()
            index += 1
            if rest == "":
                if index < len(prepared) and _yaml_indent(prepared[index]) > current:
                    value, index = parse_block(index, _yaml_indent(prepared[index]))
                else:
                    value = None
            else:
                value = parse_constrained_yaml_scalar(rest)
            mapping[key] = value
        return mapping, index

    def parse_sequence(index: int, indent: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while index < len(prepared):
            line = prepared[index]
            current = _yaml_indent(line)
            if current < indent:
                break
            if current > indent:
                raise ConstrainedYamlError(f"unexpected indent at: {line.strip()}")
            body = line[current:]
            if not body.startswith("- "):
                break
            rest = body[2:]
            index += 1
            if rest == "":
                if index < len(prepared) and _yaml_indent(prepared[index]) > current:
                    value, index = parse_block(index, _yaml_indent(prepared[index]))
                else:
                    value = None
            elif rest.endswith(":") or (":" in rest and rest.split(":", 1)[1].strip() == ""):
                key = rest[:-1].strip() if rest.endswith(":") else rest.split(":", 1)[0].strip()
                child: dict[str, Any] = {}
                if index < len(prepared) and _yaml_indent(prepared[index]) > current:
                    nested, index = parse_block(index, _yaml_indent(prepared[index]))
                    child[key] = nested
                else:
                    child[key] = None
                while index < len(prepared) and _yaml_indent(prepared[index]) > current:
                    extra, index = parse_mapping(index, _yaml_indent(prepared[index]))
                    child.update(extra)
                value = child
            elif ":" in rest and not rest.strip().startswith(("'", '"')):
                extra_map, _ = parse_mapping_from_inline(rest)
                while index < len(prepared) and _yaml_indent(prepared[index]) > current:
                    nested, index = parse_mapping(index, _yaml_indent(prepared[index]))
                    extra_map.update(nested)
                value = extra_map
            else:
                value = parse_constrained_yaml_scalar(rest)
            items.append(value)
        return items, index

    def parse_mapping_from_inline(fragment: str) -> tuple[dict[str, Any], int]:
        key, rest = fragment.split(":", 1)
        return {key.strip(): parse_constrained_yaml_scalar(rest)}, 0

    if not prepared:
        return {}
    value, next_index = parse_block(0, _yaml_indent(prepared[0]))
    if next_index != len(prepared):
        raise ConstrainedYamlError("trailing content after top-level document")
    return value


def dump_constrained_yaml(value: Any, indent: int = 0) -> str:
    prefix = " " * indent

    def dump_scalar(item: Any) -> str:
        if item is None:
            return '""'
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, int) and not isinstance(item, bool):
            return str(item)
        text = str(item)
        if text == "" or re.search(r"[:#\n]|^\s|\s$", text) or text.lower() in {"true", "false", "null", "~"}:
            return json.dumps(text, ensure_ascii=False)
        if re.fullmatch(r"-?(0|[1-9][0-9]*)", text):
            return json.dumps(text, ensure_ascii=False)
        return text

    if isinstance(value, dict):
        if not value:
            return prefix + "{}\n" if indent else ""
        chunks: list[str] = []
        for key, child in value.items():
            if isinstance(child, dict):
                if not child:
                    chunks.append(f"{prefix}{key}: {{}}\n")
                else:
                    chunks.append(f"{prefix}{key}:\n{dump_constrained_yaml(child, indent + 2)}")
            elif isinstance(child, list):
                if not child:
                    chunks.append(f"{prefix}{key}: []\n")
                else:
                    chunks.append(f"{prefix}{key}:\n{dump_constrained_yaml(child, indent + 2)}")
            else:
                chunks.append(f"{prefix}{key}: {dump_scalar(child)}\n")
        return "".join(chunks)
    if isinstance(value, list):
        if not value:
            return prefix + "[]\n"
        chunks = []
        for child in value:
            if isinstance(child, dict):
                if not child:
                    chunks.append(f"{prefix}- {{}}\n")
                    continue
                first = True
                for key, nested in child.items():
                    head = "- " if first else "  "
                    first = False
                    if isinstance(nested, (dict, list)):
                        chunks.append(f"{prefix}{head}{key}:\n{dump_constrained_yaml(nested, indent + 4)}")
                    else:
                        chunks.append(f"{prefix}{head}{key}: {dump_scalar(nested)}\n")
            elif isinstance(child, list):
                chunks.append(f"{prefix}-\n{dump_constrained_yaml(child, indent + 2)}")
            else:
                chunks.append(f"{prefix}- {dump_scalar(child)}\n")
        return "".join(chunks)
    return prefix + dump_scalar(value) + "\n"


def _reject_unknown_keys(obj: dict[str, Any], allowed: frozenset[str], prefix: str, errors: list[str]) -> None:
    unknown = sorted(set(obj) - allowed)
    for key in unknown:
        errors.append(f"unknown key {prefix}{key}")


def _require_object(value: Any, path: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object")
        return None
    return value


def validate_sre_profile(data: Any) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return False, ["root must be an object"]
    forbidden = collect_forbidden_keys(data)
    if forbidden:
        errors.extend(f"forbidden key {key}" for key in forbidden)
    _reject_unknown_keys(data, SRE_ALLOWED_ROOT_KEYS, "/", errors)
    if "schemaVersion" not in data:
        errors.append("missing schemaVersion")
    elif data.get("schemaVersion") != 1:
        errors.append("schemaVersion must be 1")
    project = data.get("project")
    if project is None:
        errors.append("missing project")
    else:
        project_obj = _require_object(project, "project", errors)
        if project_obj is not None:
            _reject_unknown_keys(project_obj, SRE_ALLOWED_PROJECT_KEYS, "project.", errors)
            name = project_obj.get("name")
            if not isinstance(name, str) or not name.strip():
                errors.append("project.name must be a non-empty string")
            repo = project_obj.get("repo")
            if repo is not None and not isinstance(repo, str):
                errors.append("project.repo must be a string")
    defaults = data.get("defaults")
    if defaults is not None:
        defaults_obj = _require_object(defaults, "defaults", errors)
        if defaults_obj is not None:
            _reject_unknown_keys(defaults_obj, SRE_ALLOWED_DEFAULTS_KEYS, "defaults.", errors)
            for field in ("env", "branch", "lane"):
                if field in defaults_obj and not isinstance(defaults_obj[field], str):
                    errors.append(f"defaults.{field} must be a string")
    if "services" not in data:
        errors.append("missing services")
        services: list[Any] = []
    elif not isinstance(data.get("services"), list):
        errors.append("services must be an array")
        services = []
    else:
        services = data["services"]
    for index, service in enumerate(services):
        service_obj = _require_object(service, f"services[{index}]", errors)
        if service_obj is None:
            continue
        _reject_unknown_keys(service_obj, SRE_ALLOWED_SERVICE_KEYS, f"services[{index}].", errors)
        service_id = service_obj.get("id")
        if not isinstance(service_id, str) or not SRE_SERVICE_ID_RE.fullmatch(service_id):
            errors.append(f"services[{index}].id is invalid")
        for field in ("name", "service", "registryKey"):
            if field in service_obj and not isinstance(service_obj[field], str):
                errors.append(f"services[{index}].{field} must be a string")
        environments = service_obj.get("environments")
        if environments is None:
            continue
        env_obj = _require_object(environments, f"services[{index}].environments", errors)
        if env_obj is None:
            continue
        for env_name, env_value in env_obj.items():
            env_path = f"services[{index}].environments.{env_name}"
            env_block = _require_object(env_value, env_path, errors)
            if env_block is None:
                continue
            _reject_unknown_keys(env_block, SRE_ALLOWED_ENV_KEYS, f"{env_path}.", errors)
            for block_name, allowed in (
                ("registry", SRE_ALLOWED_REGISTRY_KEYS),
                ("logs", SRE_ALLOWED_LOGS_KEYS),
                ("config", SRE_ALLOWED_CONFIG_KEYS),
                ("db", SRE_ALLOWED_DB_KEYS),
                ("deploy", SRE_ALLOWED_DEPLOY_KEYS),
                ("idgen", SRE_ALLOWED_IDGEN_KEYS),
            ):
                if block_name not in env_block:
                    continue
                block = _require_object(env_block[block_name], f"{env_path}.{block_name}", errors)
                if block is None:
                    continue
                _reject_unknown_keys(block, allowed, f"{env_path}.{block_name}.", errors)
                for field, field_value in block.items():
                    if not isinstance(field_value, str):
                        errors.append(f"{env_path}.{block_name}.{field} must be a string")
            if "gateway" in env_block:
                gateway_path = f"{env_path}.gateway"
                gateway = _require_object(env_block["gateway"], gateway_path, errors)
                if gateway is not None:
                    _reject_unknown_keys(gateway, SRE_ALLOWED_GATEWAY_KEYS, f"{gateway_path}.", errors)
                    if "templateApi" in gateway and not isinstance(gateway["templateApi"], str):
                        errors.append(f"{gateway_path}.templateApi must be a string")
                    if "groups" in gateway:
                        groups = gateway["groups"]
                        groups_path = f"{gateway_path}.groups"
                        if not isinstance(groups, list):
                            errors.append(f"{groups_path} must be an object array")
                        else:
                            for group_index, group in enumerate(groups):
                                group_path = f"{groups_path}[{group_index}]"
                                group_obj = _require_object(group, group_path, errors)
                                if group_obj is None:
                                    continue
                                _reject_unknown_keys(
                                    group_obj, SRE_ALLOWED_GATEWAY_GROUP_KEYS, f"{group_path}.", errors
                                )
                                for field, field_value in group_obj.items():
                                    if not isinstance(field_value, str):
                                        errors.append(f"{group_path}.{field} must be a string")
    capabilities = data.get("capabilities")
    if capabilities is not None:
        cap_obj = _require_object(capabilities, "capabilities", errors)
        if cap_obj is not None:
            _reject_unknown_keys(cap_obj, SRE_ALLOWED_CAPABILITIES_KEYS, "capabilities.", errors)
            extended = cap_obj.get("extended")
            if extended is not None:
                if not isinstance(extended, list):
                    errors.append("capabilities.extended must be an array")
                else:
                    for item in extended:
                        if item not in SRE_EXTENDED_CAPABILITIES:
                            errors.append(f"unknown extended capability {item}")
    inference = data.get("inference")
    if inference is not None:
        inf_obj = _require_object(inference, "inference", errors)
        if inf_obj is not None:
            _reject_unknown_keys(inf_obj, SRE_ALLOWED_INFERENCE_KEYS, "inference.", errors)
            if "confirmed" in inf_obj and not isinstance(inf_obj["confirmed"], bool):
                errors.append("inference.confirmed must be a boolean")
            if "notes" in inf_obj and not isinstance(inf_obj["notes"], str):
                errors.append("inference.notes must be a string")
            sources = inf_obj.get("sources")
            if sources is not None:
                if not isinstance(sources, list) or any(not isinstance(item, str) for item in sources):
                    errors.append("inference.sources must be a string array")
    return not errors, errors
