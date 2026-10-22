from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from devkit_flow.constants import AGENT_DATA_PAYLOAD_FILES, CONSUMER_OMITTED_SKILLS, KIMI_COMMAND_PREAMBLE, KIMI_MANAGER_STEM, KIMI_PLUGIN_ID, KIMI_WORKER_RUNTIME_CONTRACT, LEGACY_RUNTIME_PLUGIN_NAMES, MAINTAINER_ONLY_SKILLS, RUNTIME_PLUGIN_NAME
from devkit_flow.errors import DevkitFlowError
from devkit_flow.setup import resolve_agents_source
from devkit_flow.textio import atomic_write_text, compact_error
from devkit_flow.version import VERSION


def kimi_skill_name(stem: str) -> str:
    if stem == KIMI_MANAGER_STEM:
        return "devkit-fdm"
    return f"devkit-{stem}"


def parse_scalar(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def parse_agent_source(source_path: Path) -> tuple[dict[str, Any], str]:
    text = source_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"agent file is missing frontmatter: {source_path}")

    frontmatter_end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            frontmatter_end = idx
            break
    if frontmatter_end is None:
        raise ValueError(f"agent file has unterminated frontmatter: {source_path}")

    metadata: dict[str, Any] = {}
    frontmatter_lines = lines[1:frontmatter_end]
    idx = 0
    while idx < len(frontmatter_lines):
        raw_line = frontmatter_lines[idx]
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            idx += 1
            continue
        if ":" not in stripped:
            raise ValueError(f"unsupported frontmatter line in {source_path}: {raw_line.rstrip()}")

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            metadata[key] = parse_scalar(value)
            idx += 1
            continue

        items: list[str] = []
        idx += 1
        while idx < len(frontmatter_lines):
            item_line = frontmatter_lines[idx].rstrip("\n")
            item_stripped = item_line.strip()
            if not item_stripped:
                idx += 1
                continue
            if not item_stripped.startswith("- "):
                break
            items.append(parse_scalar(item_stripped[2:].strip()))
            idx += 1
        metadata[key] = items

    for required_key in ("name", "description"):
        value = metadata.get(required_key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"agent file is missing `{required_key}`: {source_path}")

    body = "".join(lines[frontmatter_end + 1 :])
    return metadata, body


def apply_body_replacements(body: str, replacements: list[dict[str, str]]) -> str:
    updated = body
    for replacement in replacements:
        find = replacement["find"]
        replace = replacement["replace"]
        if find not in updated:
            raise ValueError(f"override replacement target not found: {find[:80]!r}")
        updated = updated.replace(find, replace, 1)
    return updated


def strip_markdown_sections(body: str, section_titles: list[str]) -> str:
    if not section_titles:
        return body

    lines = body.splitlines(keepends=True)
    stripped: list[str] = []
    skip = False
    target_headings = {f"## {title}" for title in section_titles}

    for line in lines:
        heading = line.strip()
        if heading.startswith("## "):
            skip = heading in target_headings
            if skip:
                continue
        if not skip:
            stripped.append(line)

    return "".join(stripped).rstrip() + "\n"


def load_kimi_override(stem: str, overrides_dir: Path | None) -> tuple[dict[str, Any], str]:
    override: dict[str, Any] = {}
    append_text = ""
    if overrides_dir is not None:
        json_path = overrides_dir / f"{stem}.json"
        if json_path.exists():
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"Kimi override must be an object: {json_path}")
            override = loaded
        append_path = overrides_dir / f"{stem}.append.md"
        if append_path.exists():
            append_text = append_path.read_text(encoding="utf-8")
    return override, append_text


def adapt_kimi_body(body: str) -> str:
    adapted = body.replace("Claude Code", "Kimi Code")
    for plugin_name in (RUNTIME_PLUGIN_NAME, *LEGACY_RUNTIME_PLUGIN_NAMES):
        adapted = adapted.replace(
            f"{plugin_name}:feature-dev-manager-agent",
            "devkit-fdm",
        )
        adapted = adapted.replace(
            f"{plugin_name}:feature-manager",
            "devkit-fdm",
        )
    adapted = adapted.replace(
        "如果你是 Kimi Code session 主 agent 且用户明确要求升级，可以使用 `Agent` 调用 "
        "`devkit-fdm`；如果你是被调用的 subagent，不要再派生 subagent，只返回上述结构。",
        "如果你是 Kimi Code 主 Agent 且用户明确要求升级，继续由已加载的 "
        "`devkit-fdm` Skill 接管；如果你是 worker，不要再派生子 Agent，只返回上述结构。",
    )
    return adapted


def kimi_render_frontmatter(fields: dict[str, str | bool]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = value if value.replace("-", "").replace("_", "").isalnum() else json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def split_markdown_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if end is None:
        raise ValueError("unterminated Markdown frontmatter")

    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise ValueError(f"invalid Markdown frontmatter line: {stripped}")
        key, value = stripped.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata, "".join(lines[end + 1 :])


def render_kimi_command(source_path: Path) -> str:
    if not source_path.is_file():
        raise FileNotFoundError(f"command source not found: {source_path}")

    metadata, body = split_markdown_frontmatter(source_path.read_text(encoding="utf-8"))
    description = metadata.get("description", "Devkit workflow command for Kimi Code")
    adapted_body = body
    for plugin_name in (RUNTIME_PLUGIN_NAME, *LEGACY_RUNTIME_PLUGIN_NAMES):
        adapted_body = adapted_body.replace(
            f"{plugin_name}:feature-dev-manager-agent",
            "devkit-fdm",
        )
        adapted_body = adapted_body.replace(
            f"{plugin_name}:feature-manager",
            "devkit-fdm",
        )
    adapted_body = adapted_body.replace("Claude Code", "Kimi Code")
    adapted_body = adapted_body.replace(
        "ask\n`devkit-fdm`",
        "follow the active\n`devkit-fdm` Skill",
    )
    adapted_body = adapted_body.replace(
        "ask `devkit-fdm`",
        "follow the active `devkit-fdm` Skill",
    )
    adapted_body = adapted_body.replace(
        "load and follow the `devkit-fdm` Skill in the current main thread",
        "follow the active `devkit-fdm` Skill in the current main thread",
    )
    return (
        kimi_render_frontmatter({"description": description})
        + "\n"
        + KIMI_COMMAND_PREAMBLE
        + adapted_body.lstrip("\n")
    )


def render_kimi_skill(stem: str, agents_dir: Path, overrides_dir: Path | None) -> str:
    source_path = agents_dir / f"{stem}.md"
    if not source_path.is_file() or source_path.name == "README.md":
        raise FileNotFoundError(f"canonical Agent not found: {source_path}")

    metadata, body = parse_agent_source(source_path)
    override, append_text = load_kimi_override(stem, overrides_dir)

    frontmatter_override = override.get("frontmatter", {})
    if frontmatter_override:
        if not isinstance(frontmatter_override, dict):
            raise ValueError(f"Kimi frontmatter override must be an object: {stem}")
        metadata.update(frontmatter_override)

    replacements = override.get("body_replacements", [])
    if replacements:
        if not isinstance(replacements, list):
            raise ValueError(f"Kimi body_replacements must be a list: {stem}")
        body = apply_body_replacements(body, replacements)

    strip_sections = override.get("strip_sections", [])
    if strip_sections:
        if not isinstance(strip_sections, list) or not all(isinstance(item, str) for item in strip_sections):
            raise ValueError(f"Kimi strip_sections must be a list of strings: {stem}")
        body = strip_markdown_sections(body, strip_sections)

    description = metadata.get("description")
    if not isinstance(description, str) or not description:
        raise ValueError(f"canonical Agent is missing description: {source_path}")

    skill_name = kimi_skill_name(stem)
    is_manager = stem == KIMI_MANAGER_STEM
    fields: dict[str, str | bool] = {
        "name": skill_name,
        "description": description.replace("Claude Code", "Kimi Code"),
        "type": "inline",
        "whenToUse": (
            "When starting, continuing, changing, or completing a Devkit feature workflow in Kimi Code"
            if is_manager
            else f"Only when the Devkit feature manager explicitly delegates the {stem} role"
        ),
        "disableModelInvocation": False,
    }

    body = adapt_kimi_body(body).strip() + "\n"
    if not is_manager:
        body = KIMI_WORKER_RUNTIME_CONTRACT + body
    if append_text:
        body += "\n" + adapt_kimi_body(append_text).strip() + "\n"

    return kimi_render_frontmatter(fields) + body


def first_existing_dir(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def kimi_plugin_manifest() -> dict[str, Any]:
    """managed 布局的 plugin.json：字段对齐仓库 .kimi-plugin/plugin.json，skills/commands 指向插件根内目录。"""
    return {
        "name": KIMI_PLUGIN_ID,
        "version": VERSION,
        "description": "Expose Devkit SDD workflows as Kimi Code Skills and commands.",
        "author": {"name": "Devkit Transaction DevKit"},
        "license": "UNLICENSED",
        "keywords": ["kimi-code", "skills", "sdd", "devkit", "devkit"],
        "skills": ["./skills"],
        "commands": ["./commands"],
        "sessionStart": {"skill": "devkit-fdm"},
        "hooks": [
            {
                "event": "SessionStart",
                "command": 'devkit-flow runtime check --runtime kimi-code --target "${PWD}" --format json',
                "timeout": 30,
            },
            {
                "event": "SessionEnd",
                "command": 'devkit-flow runtime session-end --runtime kimi-code --target "${PWD}" --format json',
                "timeout": 30,
            },
            {
                "event": "UserPromptSubmit",
                "command": 'devkit-flow runtime turn-start --runtime kimi-code --target "${PWD}" --format text',
                "timeout": 30,
            },
        ],
    }


def render_kimi_plugin_tree(
    agents_dir: Path,
    overrides_dir: Path | None,
    commands_dir: Path,
    bundled_skills_dir: Path | None,
    target: Path,
) -> dict[str, Any]:
    """把 kimi 插件内容渲染进 target（.kimi-plugin/plugin.json + skills/ + commands/）。"""
    skills_out = target / "skills"
    commands_out = target / "commands"
    (target / ".kimi-plugin").mkdir(parents=True)
    skills_out.mkdir()
    commands_out.mkdir()
    skills: list[str] = []
    for source_path in sorted(agents_dir.glob("*.md")):
        if source_path.name == "README.md" or source_path.name in AGENT_DATA_PAYLOAD_FILES:
            continue
        skill_name = kimi_skill_name(source_path.stem)
        skill_dir = skills_out / skill_name
        skill_dir.mkdir()
        atomic_write_text(skill_dir / "SKILL.md", render_kimi_skill(source_path.stem, agents_dir, overrides_dir))
        skills.append(skill_name)
    bundled = 0
    if bundled_skills_dir is not None:
        for source_dir in sorted(bundled_skills_dir.iterdir()):
            skill_file = source_dir / "SKILL.md"
            if not source_dir.is_dir() or not skill_file.is_file():
                continue
            if source_dir.name in MAINTAINER_ONLY_SKILLS | CONSUMER_OMITTED_SKILLS:
                continue
            destination = skills_out / source_dir.name
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(skill_file, destination / "SKILL.md")
            skills.append(source_dir.name)
            bundled += 1
    commands: list[str] = []
    for source_path in sorted(commands_dir.glob("*.md")):
        atomic_write_text(commands_out / source_path.name, render_kimi_command(source_path))
        commands.append(source_path.name)
    # 数据载荷传播（D-A2）：dispatch-redlines.md 作为 manager Skill 资源随安装渲染树
    # 分发，落点与 scripts/build_kimi_plugin.py 对称（skill 目录资源位）。
    # fail-safe（D-17）：install 面向存量旧 npm 包/手工布局，载荷缺失时跳过不硬失败；
    # build 侧（scripts/build_kimi_plugin.py）面向本仓源树，保持 fail-closed。
    manager_skill_dir = skills_out / kimi_skill_name(KIMI_MANAGER_STEM)
    for payload_name in sorted(AGENT_DATA_PAYLOAD_FILES):
        payload_source = agents_dir / payload_name
        if payload_source.is_file():
            shutil.copy2(payload_source, manager_skill_dir / payload_name)
    atomic_write_text(
        target / ".kimi-plugin" / "plugin.json",
        json.dumps(kimi_plugin_manifest(), ensure_ascii=False, indent=2) + "\n",
    )
    return {"skills": sorted(skills), "bundled_skills": bundled, "commands": commands}


def replace_directory(source: Path, target: Path) -> None:
    """用渲染好的 source 目录替换 target（先改名旧目录再落位，失败回滚）。"""
    backup = target.with_name(target.name + ".devkit-backup")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.rename(backup)
    try:
        source.rename(target)
    except OSError:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def load_kimi_installed_json() -> tuple[Path, dict[str, Any]]:
    """读取 ~/.kimi-code/plugins/installed.json；缺失给最小结构，损坏抛 DevkitFlowError（什么都不动）。"""
    path = Path.home() / ".kimi-code" / "plugins" / "installed.json"
    if not path.is_file():
        return path, {"version": 1, "plugins": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DevkitFlowError(f"cannot parse {path}: {compact_error(error)}; fix it manually and retry") from error
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise DevkitFlowError(f"invalid {path}: expected an object with a plugins array")
    return path, data


def owned_legacy_kimi_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only legacy entries that point at DevKit's exact managed directory with a valid manifest."""
    owned: list[dict[str, Any]] = []
    for legacy_id in LEGACY_RUNTIME_PLUGIN_NAMES:
        expected_root = Path.home() / ".kimi-code" / "plugins" / "managed" / legacy_id
        for entry in data.get("plugins", []):
            if not isinstance(entry, dict) or entry.get("id") != legacy_id:
                continue
            root_value = entry.get("root")
            if not isinstance(root_value, str) or Path(root_value).expanduser().absolute() != expected_root.absolute():
                continue
            manifest = expected_root / ".kimi-plugin" / "plugin.json"
            try:
                manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if manifest_data.get("name") == legacy_id:
                owned.append(entry)
    return owned


def register_kimi_installed_plugin(
    path: Path,
    data: dict[str, Any],
    managed_dir: Path,
) -> tuple[str, list[str]]:
    """Register the new plugin and atomically retire only verified DevKit-owned legacy entries.

    source 必须是 Kimi 插件 schema 认识的枚举（local-path/zip-url/github）——
    写自定义值会导致 runtime 拒绝加载（2.16.3 的 devkit-flow-cli 教训）。
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    plugins = data["plugins"]
    entry = next((item for item in plugins if isinstance(item, dict) and item.get("id") == KIMI_PLUGIN_ID), None)
    if entry is None:
        entry = {"id": KIMI_PLUGIN_ID, "installedAt": now, "originalSource": str(managed_dir)}
        plugins.append(entry)
        action = "created"
    else:
        action = "updated"
    entry["root"] = str(managed_dir)
    entry["source"] = "local-path"
    entry["enabled"] = True
    entry["updatedAt"] = now
    legacy_entries = owned_legacy_kimi_entries(data)
    migrated_from = sorted({str(item["id"]) for item in legacy_entries})
    if legacy_entries:
        data["plugins"] = [item for item in plugins if item not in legacy_entries]
    if path.is_file():
        shutil.copy2(path, path.with_name("installed.json.bak"))
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return action, migrated_from


def install_kimi_plugin() -> dict[str, Any]:
    """渲染并安装 Kimi managed 插件，返回结果数据（供 plugin install 与 runtime update 共用）。"""
    agents_dir, source_note = resolve_agents_source()
    if agents_dir is None:
        raise DevkitFlowError(source_note)
    parent = agents_dir.parent
    overrides_dir = first_existing_dir([parent / "overrides" / "kimi", parent / "agent-overrides" / "kimi"])
    commands_dir = first_existing_dir([parent / "commands", parent.parent / "commands"])
    if commands_dir is None:
        raise DevkitFlowError(f"no commands source found near the agents source {agents_dir}")
    bundled_skills_dir = first_existing_dir([parent / "skills", parent.parent / "packages" / "skills"])

    managed_dir = Path.home() / ".kimi-code" / "plugins" / "managed" / KIMI_PLUGIN_ID
    # installed.json 先解析校验：损坏时报错且什么都不动
    installed_path, installed_data = load_kimi_installed_json()

    managed_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{KIMI_PLUGIN_ID}.", dir=managed_dir.parent))
    try:
        stats = render_kimi_plugin_tree(agents_dir, overrides_dir, commands_dir, bundled_skills_dir, staging)
    except (OSError, ValueError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise DevkitFlowError(f"failed to render kimi plugin content: {compact_error(error)}") from error
    replace_directory(staging, managed_dir)
    try:
        installed_manifest = json.loads(
            (managed_dir / ".kimi-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise DevkitFlowError(f"new Kimi plugin manifest verification failed: {compact_error(error)}") from error
    if installed_manifest.get("name") != KIMI_PLUGIN_ID or installed_manifest.get("version") != VERSION:
        raise DevkitFlowError("new Kimi plugin manifest verification failed: identity/version mismatch")
    installed_action, migrated_from = register_kimi_installed_plugin(
        installed_path, installed_data, managed_dir
    )
    for legacy_id in migrated_from:
        legacy_dir = managed_dir.parent / legacy_id
        if legacy_dir.is_dir():
            shutil.rmtree(legacy_dir)

    reload_hint = "在 Kimi TUI 执行 /plugins reload 或 /new 使插件生效。"
    lines = [f"plugin install: ok ({KIMI_PLUGIN_ID} → {managed_dir})"]
    lines.append(
        f"- skills: {len(stats['skills'])} (rendered {len(stats['skills']) - stats['bundled_skills']}, "
        f"bundled {stats['bundled_skills']})"
    )
    lines.append(f"- commands: {len(stats['commands'])}")
    lines.append(f"- installed.json: {installed_action}")
    if migrated_from:
        lines.append(f"- migrated legacy plugin: {', '.join(migrated_from)}")
    lines.append(f"- {reload_hint}")
    return {
        "status": "ok",
        "message": "\n".join(lines),
        "runtime": "kimi-code",
        "plugin": KIMI_PLUGIN_ID,
        "managed_dir": str(managed_dir),
        "agents_source": source_note,
        "overrides": str(overrides_dir) if overrides_dir else None,
        "commands_source": str(commands_dir),
        "bundled_skills_source": str(bundled_skills_dir) if bundled_skills_dir else None,
        "skills": stats["skills"],
        "commands": stats["commands"],
        "installed_json": installed_action,
        "migrated_from": migrated_from,
        "next": reload_hint,
    }
