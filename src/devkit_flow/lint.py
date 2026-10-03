"""devkit-flow lint：目标 Java 仓文本/结构级启发式静态检查（R5 / 设计 §4.4 / D-A1、D-3）。

首版规则集与 ``packages/agents/dispatch-redlines.md`` 的 ``static_check_id`` 一一对应：

- ``naming.data-object-suffix``（RL-08）：数据对象后缀错层、复数集合未用 List 后缀
- ``layers.cross-layer-import``（RL-09）：App 直调 DAO、Domain 依赖本工程 API 包、
  Infra 依赖 BO/DTO、App 层使用 @Transactional
- ``di.bean-host-new``（RL-10）：Spring Bean 宿主 new、``new Foo(this, ...)``、懒创建工厂

定位为「前置防线」：stdlib 实现的启发式检查，漏检由 reviewer 兜底、不误伤——
误报级 finding 标 WARNING 不阻断；BLOCKER > 0 时命令退出码为 1。
隐私口径：finding 只含 file/line/message_excerpt 摘要，不采源码片段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from devkit_flow.errors import DevkitFlowError
from devkit_flow.textio import emit

RULE_NAMING_DATA_OBJECT_SUFFIX = "naming.data-object-suffix"
RULE_LAYERS_CROSS_LAYER_IMPORT = "layers.cross-layer-import"
RULE_DI_BEAN_HOST_NEW = "di.bean-host-new"

LINT_RULE_IDS: tuple[str, ...] = (
    RULE_NAMING_DATA_OBJECT_SUFFIX,
    RULE_LAYERS_CROSS_LAYER_IMPORT,
    RULE_DI_BEAN_HOST_NEW,
)

LINT_GROUPS: dict[str, tuple[str, ...]] = {
    "naming": (RULE_NAMING_DATA_OBJECT_SUFFIX,),
    "layers": (RULE_LAYERS_CROSS_LAYER_IMPORT,),
    "di": (RULE_DI_BEAN_HOST_NEW,),
    "all": LINT_RULE_IDS,
}

SEVERITY_BLOCKER = "BLOCKER"
SEVERITY_WARNING = "WARNING"

_SKIP_DIRS = frozenset({".git", ".idea", ".svn", "node_modules", "target", "build", "dist", "out"})

_CLASS_DECL = re.compile(r"^\s*(?:public\s+|protected\s+|private\s+)?(?:abstract\s+|final\s+|static\s+)*class\s+([A-Za-z_]\w*)")
_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([A-Za-z_][\w.]*)\s*;")
_PACKAGE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;")
_LIST_FIELD = re.compile(r"\bList\s*<[^>;\n]+>\s+([A-Za-z_]\w*)\s*[;=]")
_NEW_HOST_THIS = re.compile(r"\bnew\s+[A-Za-z_]\w*\s*\(\s*this\s*[,\)]")
_NEW_BEAN = re.compile(
    r"\bnew\s+[A-Za-z_]\w*(?:Service|Repository|Proxy|DAO|Manager|Support|Dispatcher|Impl)\s*\("
)
_LAZY_NULL_CHECK = re.compile(r"==\s*null")
_LAZY_NEW_ASSIGN = re.compile(r"=\s*new\s+[A-Za-z_]\w*\s*\(")
_TRANSACTIONAL = re.compile(r"@Transactional\b")

# 数据对象后缀 → 允许层（specrules/00_general/naming/data_object_naming.md）
_SUFFIX_LAYER: tuple[tuple[str, str], ...] = (
    ("Request", "api"),
    ("Response", "api"),
    ("DTO", "api"),
    ("VO", "ui"),
    ("BO", "app"),
    ("DO", "domain"),
    ("Entity", "infra"),
)

_LAYER_TOKENS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("api", ("api",), ("-api",)),
    ("infra", ("infrastructure", "infra", "dal"), ("-infrastructure", "-infra")),
    ("app", ("application",), ("-application",)),
    ("domain", ("domain",), ("-domain",)),
    ("ui", ("starter", "ui", "web", "controller"), ("-starter",)),
)


@dataclass(frozen=True)
class LintFinding:
    rule_id: str
    severity: str
    file: str
    line: int
    message_excerpt: str


def run_lint(rule_ids: list[str], target_root: Path) -> list[LintFinding]:
    """lint 唯一入口：对 target_root 下的 Java 生产源码执行 rule_ids 指定的规则组。"""
    unknown = [rule_id for rule_id in rule_ids if rule_id not in LINT_RULE_IDS]
    if unknown:
        raise DevkitFlowError(
            f"unknown lint rule id(s): {', '.join(unknown)}; known: {', '.join(LINT_RULE_IDS)}"
        )
    root = Path(target_root)
    if not root.is_dir():
        raise DevkitFlowError(f"lint target path is not a directory: {root}")
    selected = frozenset(rule_ids)
    findings: list[LintFinding] = []
    api_prefixes = _collect_api_package_prefixes(root) if RULE_LAYERS_CROSS_LAYER_IMPORT in selected else frozenset()
    for path in _iter_java_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        layers = _layers_of(rel)
        if RULE_NAMING_DATA_OBJECT_SUFFIX in selected:
            _check_naming(rel, lines, layers, findings)
        if RULE_LAYERS_CROSS_LAYER_IMPORT in selected:
            _check_layers(rel, lines, text, layers, api_prefixes, findings)
        if RULE_DI_BEAN_HOST_NEW in selected:
            _check_di(rel, lines, findings)
    findings.sort(key=lambda f: (f.file, f.line, f.rule_id, f.message_excerpt))
    return findings


def command_lint(args: Any) -> int:
    group_rules = LINT_GROUPS[args.lint_command]
    requested = list(args.rule or [])
    unknown = [rule_id for rule_id in requested if rule_id not in LINT_RULE_IDS]
    if unknown:
        raise DevkitFlowError(
            f"unknown lint rule id(s): {', '.join(unknown)}; known: {', '.join(LINT_RULE_IDS)}"
        )
    out_of_group = [rule_id for rule_id in requested if rule_id not in group_rules]
    if out_of_group:
        raise DevkitFlowError(
            f"rule id(s) not in lint group '{args.lint_command}': {', '.join(out_of_group)}; "
            "use `devkit-flow lint all --rule <id>` to select across groups."
        )
    rule_ids = requested or list(group_rules)
    target_root = Path(args.path).resolve() if args.path else Path.cwd()
    findings = run_lint(rule_ids, target_root)
    blocker = sum(1 for f in findings if f.severity == SEVERITY_BLOCKER)
    warning = sum(1 for f in findings if f.severity == SEVERITY_WARNING)
    data: dict[str, Any] = {
        "status": "fail" if blocker > 0 else "pass",
        "findings": [
            {
                "rule_id": f.rule_id,
                "severity": f.severity,
                "file": f.file,
                "line": f.line,
                "message_excerpt": f.message_excerpt,
            }
            for f in findings
        ],
        "summary": {"blocker": blocker, "warning": warning},
    }
    if args.format == "text":
        lines = [f"lint {args.lint_command}: {len(findings)} finding(s), BLOCKER={blocker}, WARNING={warning}"]
        lines.extend(
            f"{f.severity} {f.rule_id} {f.file}:{f.line} {f.message_excerpt}" for f in findings
        )
        data["message"] = "\n".join(lines)
    emit(data, args.format)
    return 1 if blocker > 0 else 0


def _iter_java_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*.java")):
        parts = path.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in parts):
            continue
        lowered = [part.lower() for part in parts]
        # 只扫生产源码：测试源码（src/test/…）允许 new / 错层命名，不误伤。
        if any("src" == part for part in lowered) and any("test" == part for part in lowered):
            continue
        files.append(path)
    return files


def _layers_of(rel_path: str) -> frozenset[str]:
    parts = [part.lower() for part in rel_path.split("/")[:-1]]
    matched: set[str] = set()
    for layer, exact, suffix in _LAYER_TOKENS:
        if any(part in exact or any(part.endswith(sfx) for sfx in suffix) for part in parts):
            matched.add(layer)
    return frozenset(matched)


def _collect_api_package_prefixes(root: Path) -> frozenset[str]:
    """本工程 API 模块包前缀：来自路径含 api 模块段的 .java 文件 package 声明。

    用于区分「Domain 依赖本工程 API 模块」（禁止）与「依赖外层 API 模块」（允许）。
    """
    prefixes: set[str] = set()
    for path in _iter_java_files(root):
        rel = path.relative_to(root).as_posix()
        if "api" not in _layers_of(rel):
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        match = _PACKAGE.search(head)
        if not match:
            continue
        package = match.group(1)
        segments = package.split(".")
        if "api" in segments:
            prefixes.add(".".join(segments[: segments.index("api") + 1]))
        else:
            prefixes.add(package)
    return frozenset(prefixes)


def _check_naming(
    rel: str, lines: list[str], layers: frozenset[str], findings: list[LintFinding]
) -> None:
    for index, line in enumerate(lines, start=1):
        decl = _CLASS_DECL.match(line)
        if decl and len(layers) == 1:
            layer = next(iter(layers))
            name = decl.group(1)
            for suffix, expected in _SUFFIX_LAYER:
                if name.endswith(suffix) and layer != expected:
                    findings.append(
                        LintFinding(
                            RULE_NAMING_DATA_OBJECT_SUFFIX,
                            SEVERITY_BLOCKER,
                            rel,
                            index,
                            f"数据对象后缀 {suffix} 出现在 {layer} 层（{suffix} 应属 {expected} 层，RL-08）",
                        )
                    )
                    break
        field = _LIST_FIELD.search(line)
        if field:
            var = field.group(1)
            if var.endswith("s") and not var.endswith("List"):
                findings.append(
                    LintFinding(
                        RULE_NAMING_DATA_OBJECT_SUFFIX,
                        SEVERITY_WARNING,
                        rel,
                        index,
                        "复数集合命名未用 List 后缀（禁止使用 s/es 复数后缀，RL-08）",
                    )
                )


def _check_layers(
    rel: str,
    lines: list[str],
    text: str,
    layers: frozenset[str],
    api_prefixes: frozenset[str],
    findings: list[LintFinding],
) -> None:
    if len(layers) != 1:
        return
    layer = next(iter(layers))
    for index, line in enumerate(lines, start=1):
        imported = _IMPORT.match(line)
        if not imported:
            continue
        target = imported.group(1)
        segments = target.split(".")
        leaf = segments[-1]
        if layer == "app" and ("dao" in segments or leaf.endswith(("DAO", "DAOImpl"))):
            findings.append(
                LintFinding(
                    RULE_LAYERS_CROSS_LAYER_IMPORT,
                    SEVERITY_BLOCKER,
                    rel,
                    index,
                    "应用层直接依赖 DAO（应经 Repository/DomainService 编排，RL-09）",
                )
            )
        if layer == "domain" and any(target.startswith(prefix + ".") for prefix in api_prefixes):
            findings.append(
                LintFinding(
                    RULE_LAYERS_CROSS_LAYER_IMPORT,
                    SEVERITY_BLOCKER,
                    rel,
                    index,
                    "领域层依赖本工程 API 模块（外层 API 允许，本工程 API 禁止，RL-09）",
                )
            )
        if layer == "infra" and (
            "bo" in segments or "dto" in segments or leaf.endswith(("BO", "DTO"))
        ):
            findings.append(
                LintFinding(
                    RULE_LAYERS_CROSS_LAYER_IMPORT,
                    SEVERITY_BLOCKER,
                    rel,
                    index,
                    "基础设施层依赖上层对象 BO/DTO（禁止依赖领域层以上对象，RL-09）",
                )
            )
    if layer == "app":
        match = _TRANSACTIONAL.search(text)
        if match:
            findings.append(
                LintFinding(
                    RULE_LAYERS_CROSS_LAYER_IMPORT,
                    SEVERITY_BLOCKER,
                    rel,
                    text[: match.start()].count("\n") + 1,
                    "应用层使用 @Transactional（事务边界不应落在 App 层，RL-09）",
                )
            )


def _check_di(rel: str, lines: list[str], findings: list[LintFinding]) -> None:
    for index, line in enumerate(lines, start=1):
        if _NEW_HOST_THIS.search(line):
            findings.append(
                LintFinding(
                    RULE_DI_BEAN_HOST_NEW,
                    SEVERITY_BLOCKER,
                    rel,
                    index,
                    "宿主把自身传入协作类构造器（new Foo(this, ...)），禁止用宿主引用装配/解循环（RL-10）",
                )
            )
        elif _NEW_BEAN.search(line):
            findings.append(
                LintFinding(
                    RULE_DI_BEAN_HOST_NEW,
                    SEVERITY_WARNING,
                    rel,
                    index,
                    "疑似 Spring Bean 宿主 new（纯命名启发式命中，仅凭命名不判违规；确认 Spring 托管后改用 @Resource 字段注入，RL-10）",
                )
            )
        elif _LAZY_NULL_CHECK.search(line) and index < len(lines):
            following = lines[index]
            if _LAZY_NEW_ASSIGN.search(line) or _LAZY_NEW_ASSIGN.search(following):
                findings.append(
                    LintFinding(
                        RULE_DI_BEAN_HOST_NEW,
                        SEVERITY_WARNING,
                        rel,
                        index,
                        "疑似懒创建工厂（== null 判空后 new 装配），装配离开 Spring 容器（RL-10）",
                    )
                )
