"""规则源选择、规则包协议与闭包解析（feature 016 T-04；设计 §4.7.1.3～§4.7.1.6，接口 §2.3 / §3.3 / §4.2 / §6 / §8.3 / §8.4，类图 §3）。

纯计算 + 回执读写：不挂载、不拉取、不起子进程、不联网。实际拉取与挂载由 ``setup.RuleMountExecutor``（T-05）执行。

模块边界（设计 §4.7.0.1）：本模块只 import ``version``、``constants``、``errors``、``textio``、``config``、
``yaml_util``、``project_detect``，**不得** import ``switches`` / ``setup``（``switches`` 与 ``setup`` 反向消费本模块；
specrules 开关合并与项目 ``repoUrl`` 读取复用 ``config.merge_specrules_switch`` /
``config._specrules_configured_repo_url``，与 ``specrules status`` 的 ``effective_state`` 同一实现）。
``version`` 边用于 ``minDevkitFlow`` 比较，它在 ``MODULE_ORDER`` 首位，拓扑上恒更靠前。

实现口径（文档留白处的解释，T-05 / T-07 按此消费）：

1. ``closure[]`` 不含入口 index 本身；入口在每个模块条目的 ``entry`` 字段。
2. ``legacy`` 闭包 = 入口 index 直接引用的、存在于包内的 ``.md`` 文件全集（深度 1，不过滤，``missing=[]``）；
   ``adopted`` 闭包从入口按 Markdown 链接**传递**遍历，按阶段与 ``appliesTo`` 过滤。
3. 阶段过滤只作用于入口 index 的直接链接：index 的二级标题含「设计阶段」（或 design）的节只属 ``design``，
   含「开发阶段 / 编码阶段」（或 coding / development）的节只属 ``coding``，其余节（§1 前置、§4 横切等）两阶段共用；
   传递链接不再按阶段过滤。``reason`` 形如 ``index §1`` / ``linked from <path>``。
4. 挂载点：项目 ``classification=mixed`` 时非根模块为 ``<模块目录>/specrules``，其余一律为工作区 ``specrules``
   （非 mixed 的多模块仓共用根树，接口 §6.3）。
5. ``sourceDirs`` 以**规范化 URL** 为键（``RuleSourceCatalog.sourceDirs()`` 用 ``config.specrules_cache_dir_for_url``
   计算），因为模块显式 ``repoUrl`` 与项目 ``specrules.repoUrl`` 不按 kind 区分。
6. ``legacy`` 下 ``rule_context.modules[]`` 每个画像模块一项：``bindingStatus=legacy-diagnostic``、``kind=null``、
   ``entry`` 为根 index（不存在时为 null，``closure=[]`` 并给 advice）。
7. 链接抽取：只认 Markdown 行内链接 ``[text](target)``；跳过围栏代码块、行内代码、带 scheme 的 URL
   （``http(s)``、``mailto`` 等）与纯锚点；去掉 ``#fragment``；只跟随 ``.md`` 目标。``specrules/`` 与 ``@specrules/``
   前缀按包根解析（D-16⑦），其余相对所在文件目录；越出包根（含绝对路径）→ ``RULES_INCOMPATIBLE_SOURCE``；
   包内目标缺失 → ``missing[]`` 告警，不改变 ``bindingStatus``。
8. ``adopted`` 下绑定身份与状态的来源（D-27 / D-28 / D-30，T-04 审查修复轮）：

   =========================  ===========================================  ==========================================
   情形                        身份（kind / repoUrl / commit / 版本 / 协议）  状态与闭包
   =========================  ===========================================  ==========================================
   explicit，画像非 confirmed   全部 null（D-23 ① 门槛）                       ``binding-mismatch``，闭包空，exit 1
   explicit，其余               ``select``（配置 + 缓存校验）；回执同源时       ``select`` 结论；回执源≠配置源 →
                               commit / 版本 / 协议取回执                    ``binding-mismatch``；回执同源、受管树完整、
                                                                            失败只来自缓存校验 → ``bound`` + advice
                                                                            （D-30 ②）；树未挂载 → ``not-mounted``
                                                                            （exit 1，``RULES_BINDING_STALE``）
   implicit，回执可读            回执（保持路径）；当前识别 / catalog 只作 advice  按现有受管树：未挂载 → ``not-mounted``，
                                                                            树损坏或 kind≠回执 → ``incompatible-source``，
                                                                            否则 ``bound``；恒 exit 0
   implicit，回执不可读          现有树入口 index frontmatter（宽松读取），       树入口可读 → ``bound`` + advice（闭包按树
                               repoUrl / commit 为 null（D-30 ④）             严格计算）；入口缺失 / 不可读 →
                                                                            ``incomplete-source``；恒 exit 0
   =========================  ===========================================  ==========================================

   不变式（``_rs_enforce_status_invariants``）：``bound`` 的闭包恒来自实际受管树；``adopted`` 下非 ``bound`` /
   ``unbound-no-kind`` 状态闭包、``excluded``、``missing`` 恒为空；``unbound-no-kind`` 的绑定元数据恒为 null。
   回执未记录的模块在保持路径下（D-30 ③）：当前选源为 ``unbound-no-kind`` 时照旧；否则非 mixed 项目共享回执根条目
   的绑定，mixed 项目为 ``not-mounted`` + advice 执行 setup。``specrules.enabled=false`` 时不读取 ``ruleSources``
   （D-30 ⑤）。``ManagedTreeMigrator.plan`` 对 submodule 分发返回 ``notApplicable``（D-30 ⑥）。
9. 非 mixed 项目只有根挂载点：``select`` 中同一挂载点解析出不同 ``repoUrl`` 的模块一律 ``binding-mismatch``（D-27）。
10. 严格解析拒绝顶层 flow 写法（受限解析器把 ``{specrulesPackage: ...}`` 解析为以 ``{`` 开头的键）；入口 index 的
    ``appliesTo`` 与其他规则文件同样校验，入口不适用 → ``RULES_INCOMPATIBLE_SOURCE``。legacy 宽松读取遇未闭合
    frontmatter 按全文提取引用，入口或目标不可读只追加诊断 advice。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

from devkit_flow.version import VERSION
from devkit_flow.constants import CONFIG_PATH, SPECRULES_INVALID_LAYER, SPECRULES_INVALID_URL_MESSAGE, SPECRULES_REPO_URL
from devkit_flow.errors import DevkitFlowError
from devkit_flow.textio import atomic_write_text, emit
from devkit_flow.config import (
    _config_object_layer,
    _specrules_configured_repo_url,
    load_config_file,
    merge_specrules_switch,
    specrules_cache_dir_for_url,
    validate_specrules_git_url,
)
from devkit_flow.yaml_util import ConstrainedYamlError, parse_constrained_yaml
from devkit_flow.project_detect import ModuleProfile, ProjectDetector, ProjectProfile


RULES_MISSING_SOURCE = "RULES_MISSING_SOURCE"
RULES_INCOMPLETE_SOURCE = "RULES_INCOMPLETE_SOURCE"
RULES_INCOMPATIBLE_SOURCE = "RULES_INCOMPATIBLE_SOURCE"
RULES_BINDING_MISMATCH = "RULES_BINDING_MISMATCH"
RULES_MIGRATION_CONFLICT = "RULES_MIGRATION_CONFLICT"
RULES_BINDING_STALE = "RULES_BINDING_STALE"
RULES_INVALID_REPO_URL = "SPECRULES_INVALID_REPO_URL"

RULE_BINDING_SCHEMA_VERSION = 1
RULE_MIGRATION_SCHEMA_VERSION = 1
RULE_PACKAGE_PROTOCOL = 1
RULE_PACKAGE_ENTRY = "rules/index.md"
RULE_PACKAGE_KINDS = ("frontend", "java-backend")
RULES_ROUTING_MODES = ("adopted", "legacy", "disabled")
RULES_ROUTING_SOURCES = (
    "project-config",
    "binding-receipt",
    "implicit",
    "implicit-candidate",
    "legacy-default",
    "disabled",
)
RULES_ADOPTED_BY = ("explicit", "implicit")
RULES_BINDING_STATUSES = (
    "bound",
    "unbound-no-kind",
    "missing-source",
    "incomplete-source",
    "incompatible-source",
    "binding-mismatch",
    "not-mounted",
    "legacy-diagnostic",
    "disabled",
)
RULES_STAGES = ("design", "coding")
RULES_OK_STATUSES = frozenset({"bound", "unbound-no-kind"})
RULES_STATUS_ERROR_CODES = {
    "missing-source": RULES_MISSING_SOURCE,
    "incomplete-source": RULES_INCOMPLETE_SOURCE,
    "incompatible-source": RULES_INCOMPATIBLE_SOURCE,
    "binding-mismatch": RULES_BINDING_MISMATCH,
    # D-28 ①：explicit 下规则树未挂载。复用既有码（D-23 不新增码）：RULES_BINDING_STALE 的触发
    # 「写了 routing=auto 尚未 setup」（接口 §4.2）正是本状态的成因，修复动作同为执行 setup。
    "not-mounted": RULES_BINDING_STALE,
}
RULES_RECEIPT_RELATIVE_PATH = Path(".devkit") / "specrules-binding.json"
RULES_BACKUP_RELATIVE_DIR = Path(".devkit") / "specrules-backup"
RULES_MIGRATIONS_RELATIVE_DIR = Path(".devkit") / "specrules-migrations"
RULES_TREE_NAME = "specrules"
RULES_APPLIES_TO_FIELDS = ("languages", "classifications", "contractRoles")

RULES_ADVICE_NO_KIND = "无专用规则源，未挂载，只用 DevKit 栈中性默认"
RULES_ADVICE_IMPLICIT_CANDIDATE = "下一次 setup 将隐式采用"
RULES_ADVICE_INVALID_SWITCH = "specrules 配置非法：enabled=false 与项目 repoUrl 并存"
RULES_ADVICE_EMPTY_PACKAGE = f"{RULE_PACKAGE_ENTRY} 未链接任何规则文件（空规则包）"

_RS_LINK = re.compile(r"(!?)\[(?:[^\]\\]|\\.)*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_RS_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
_RS_FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
_RS_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_RS_HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_RS_SECTION_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)\.?\s")
_RS_VERSION = re.compile(r"^\d+(?:\.\d+)*$")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _rs_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _rs_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rs_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rs_rel(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path, base)).as_posix()


def _rs_incompatible(message: str) -> DevkitFlowError:
    return DevkitFlowError(message, error_code=RULES_INCOMPATIBLE_SOURCE)


def _rs_version_tuple(text: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", text.strip())
    if match is None:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _rs_split_frontmatter(text: str) -> tuple[str | None, str, bool]:
    """返回 (frontmatter 文本或 None, 正文, 是否闭合)。只识别文件首行 ``---`` 起始的块。"""
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text, True
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :]), True
    return "".join(lines[1:]), "", False


def _rs_sha256_strict(path: Path, root: Path) -> str:
    """adopted 闭包取摘要：读取失败按 ``RULES_INCOMPATIBLE_SOURCE``（带包内路径），不抛裸 ``OSError``。"""
    try:
        return _rs_sha256(path)
    except OSError as error:
        raise _rs_incompatible(f"{_rs_rel(path, root)}: cannot read rule file: {error}") from error


def _rs_read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise _rs_incompatible(f"{label}: cannot read rule file: {error}") from error


def _rs_strict_frontmatter(path: Path, label: str) -> tuple[dict[str, Any] | None, str]:
    """严格解析：frontmatter 存在则必须闭合、可解析且为映射；否则 ``RULES_INCOMPATIBLE_SOURCE``（带文件路径）。"""
    text = _rs_read_text(path, label)
    raw, body, closed = _rs_split_frontmatter(text)
    if raw is None:
        return None, body
    if not closed:
        raise _rs_incompatible(f"{label}: frontmatter is not closed by '---'")
    try:
        data = parse_constrained_yaml(raw)
    except ConstrainedYamlError as error:
        raise _rs_incompatible(f"{label}: frontmatter is not block-style YAML: {error}") from error
    if not isinstance(data, dict):
        raise _rs_incompatible(f"{label}: frontmatter must be a mapping")
    flow_keys = [key for key in data if isinstance(key, str) and key.lstrip()[:1] in ("{", "[")]
    if flow_keys:
        # 顶层 flow 写法（``{specrulesPackage: {...}}``）被受限解析器解析为以 ``{`` 开头的键，不能当作「无元数据」
        raise _rs_incompatible(
            f"{label}: frontmatter uses flow-style YAML at top level ({flow_keys[0][:40]!r}); only block style is supported"
        )
    return data, body


# ---------------------------------------------------------------------------
# 规则包元数据（接口 §6.1）
# ---------------------------------------------------------------------------


@dataclass
class RulePackageMeta:
    protocol: int
    kind: str
    version: str | None
    entry: str
    minDevkitFlow: str | None = None

    def isLegacy(self) -> bool:
        return self.protocol == 0

    @classmethod
    def legacy(cls) -> "RulePackageMeta":
        """无 ``specrulesPackage`` 的旧包：``protocol=0``、``kind=java-backend``（接口 §6.3）。"""
        return cls(protocol=0, kind="java-backend", version=None, entry=RULE_PACKAGE_ENTRY)

    @classmethod
    def parseIndex(cls, indexFile: Path, relPath: str | None = None) -> "RulePackageMeta":
        """严格解析入口 index 的包元数据（块写法 + 类型校验）。只在 ``adopted`` 下调用；``legacy`` 不调用。

        失败一律 ``RULES_INCOMPATIBLE_SOURCE``，消息带文件相对路径与字段名；不得把解析失败当作「无元数据」。
        """
        label = relPath or RULE_PACKAGE_ENTRY
        data, _ = _rs_strict_frontmatter(indexFile, label)
        if data is None or "specrulesPackage" not in data:
            return cls.legacy()
        package = data["specrulesPackage"]
        if not isinstance(package, dict):
            raise _rs_incompatible(
                f"{label}: field specrulesPackage must be a block-style mapping, got {type(package).__name__}"
            )
        protocol = package.get("protocol")
        if "protocol" not in package or not isinstance(protocol, int) or isinstance(protocol, bool) or protocol < 1:
            raise _rs_incompatible(f"{label}: field specrulesPackage.protocol must be a positive integer")
        kind = package.get("kind")
        if not isinstance(kind, str) or kind not in RULE_PACKAGE_KINDS:
            raise _rs_incompatible(
                f"{label}: field specrulesPackage.kind must be one of {', '.join(RULE_PACKAGE_KINDS)}"
            )
        version = package.get("version")
        if not isinstance(version, str) or not version.strip():
            raise _rs_incompatible(f"{label}: field specrulesPackage.version must be a non-empty string")
        entry = package.get("entry")
        if entry != RULE_PACKAGE_ENTRY:
            raise _rs_incompatible(f"{label}: field specrulesPackage.entry must be '{RULE_PACKAGE_ENTRY}'")
        min_cli = package.get("minDevkitFlow")
        if min_cli is not None and (not isinstance(min_cli, str) or not _RS_VERSION.match(min_cli.strip())):
            raise _rs_incompatible(f"{label}: field specrulesPackage.minDevkitFlow must be a dotted version string")
        return cls(
            protocol=protocol,
            kind=kind,
            version=version.strip(),
            entry=entry,
            minDevkitFlow=min_cli.strip() if isinstance(min_cli, str) else None,
        )

    def compatibilityProblem(self, cliVersion: str = VERSION) -> str | None:
        """协议或 CLI 版本不兼容时返回原因（``incompatible-source``），否则 None。"""
        if self.protocol > RULE_PACKAGE_PROTOCOL:
            return f"rule package protocol {self.protocol} is newer than supported {RULE_PACKAGE_PROTOCOL}"
        if self.minDevkitFlow and _rs_version_tuple(cliVersion) < _rs_version_tuple(self.minDevkitFlow):
            return f"rule package requires devkit-flow >= {self.minDevkitFlow} (running {cliVersion})"
        return None


def _rs_lenient_package_fields(index_file: Path) -> dict[str, Any] | None:
    """宽松读取入口 index 的 ``specrulesPackage`` 身份字段（fail-open）。

    入口不存在或不可读（含非 UTF-8）→ None；可读时返回 ``{kind, version, protocol}``：无 frontmatter 视为旧包
    （``kind=java-backend``、``protocol=0``），frontmatter 损坏 / flow 写法 / 字段类型不符的字段一律为 None。
    """
    try:
        text = index_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    fields: dict[str, Any] = {"kind": None, "version": None, "protocol": None}
    try:
        raw, _, closed = _rs_split_frontmatter(text)
        if raw is None:
            legacy = RulePackageMeta.legacy()
            return {"kind": legacy.kind, "version": legacy.version, "protocol": legacy.protocol}
        if not closed:
            return fields
        data = parse_constrained_yaml(raw)
    except Exception:  # noqa: BLE001 - fail-open：任何解析异常都视为未声明
        return fields
    if not isinstance(data, dict):
        return fields
    package = data.get("specrulesPackage")
    if not isinstance(package, dict):
        if "specrulesPackage" not in data:
            legacy = RulePackageMeta.legacy()
            return {"kind": legacy.kind, "version": legacy.version, "protocol": legacy.protocol}
        return fields
    kind = package.get("kind")
    version = package.get("version")
    protocol = package.get("protocol")
    fields["kind"] = kind if isinstance(kind, str) else None
    fields["version"] = version.strip() if isinstance(version, str) and version.strip() else None
    fields["protocol"] = protocol if isinstance(protocol, int) and not isinstance(protocol, bool) else None
    return fields


def _rs_lenient_package_kind(index_file: Path) -> str | None:
    """宽松读取 ``specrulesPackage.kind``（fail-open）：任何读取或解析失败、类型不符、未声明都返回 None。"""
    try:
        text = index_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    try:
        raw, _, closed = _rs_split_frontmatter(text)
        if raw is None or not closed:
            return None
        data = parse_constrained_yaml(raw)
    except Exception:  # noqa: BLE001 - fail-open：任何异常都视为「未声明非 Java kind」
        return None
    if not isinstance(data, dict):
        return None
    package = data.get("specrulesPackage")
    if not isinstance(package, dict):
        return None
    kind = package.get("kind")
    return kind if isinstance(kind, str) else None


# ---------------------------------------------------------------------------
# 绑定 / 回执（接口 §3.3、§8.3）
# ---------------------------------------------------------------------------


@dataclass
class ManagedFile:
    path: str
    sha256: str

    def toJson(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass
class RuleBinding:
    module: str
    kind: str | None = None
    repoUrl: str | None = None
    bindingStatus: str = "unbound-no-kind"
    commit: str | None = None
    packageVersion: str | None = None
    protocol: int | None = None
    entry: str | None = None
    distribution: str | None = None
    offline: bool = False
    managedFiles: list[ManagedFile] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)
    # 内部记账（不序列化）：``select`` 的失败状态仅由规则缓存校验得出时记该状态，``cacheAdviceStart`` 为其 advice 起点；
    # 配置层冲突覆盖状态时清空。供 explicit 回执覆盖区分「缓存问题」与「配置问题」（D-30 ②）。
    cacheFailure: str | None = field(default=None, repr=False, compare=False)
    cacheAdviceStart: int = field(default=0, repr=False, compare=False)

    def toReceiptJson(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "kind": self.kind,
            "repoUrl": self.repoUrl,
            "commit": self.commit,
            "packageVersion": self.packageVersion,
            "protocol": self.protocol,
            "distribution": self.distribution,
            "offline": self.offline,
            "managedFiles": [item.toJson() for item in self.managedFiles],
        }

    @classmethod
    def fromReceiptJson(cls, data: dict[str, Any]) -> "RuleBinding":
        protocol = data.get("protocol")
        return cls(
            module=str(data["module"]),
            kind=data.get("kind") if isinstance(data.get("kind"), str) else None,
            repoUrl=data.get("repoUrl") if isinstance(data.get("repoUrl"), str) else None,
            bindingStatus="bound",
            commit=data.get("commit") if isinstance(data.get("commit"), str) else None,
            packageVersion=data.get("packageVersion") if isinstance(data.get("packageVersion"), str) else None,
            protocol=protocol if isinstance(protocol, int) and not isinstance(protocol, bool) else None,
            distribution=data.get("distribution") if isinstance(data.get("distribution"), str) else None,
            offline=bool(data.get("offline")),
            managedFiles=[ManagedFile(item["path"], item["sha256"]) for item in data.get("managedFiles") or []],
        )


def _rs_receipt_shape_problem(data: dict[str, Any]) -> str | None:
    """回执 ``modules[]`` 形状校验（接口 §8.3 / §9.3）：形状不符返回原因，由 ``load`` 转为 unreadable 哨兵，不抛异常。"""
    modules = data.get("modules")
    if modules is None:
        return None
    if not isinstance(modules, list):
        return f"binding receipt modules must be a list, got {type(modules).__name__}"
    for index, item in enumerate(modules):
        if not isinstance(item, dict):
            return f"binding receipt modules[{index}] must be an object, got {type(item).__name__}"
        module = item.get("module")
        if not isinstance(module, str) or not module:
            return f"binding receipt modules[{index}].module must be a non-empty string"
        managed = item.get("managedFiles")
        if managed is None:
            continue
        if not isinstance(managed, list):
            return f"binding receipt modules[{index}].managedFiles must be a list"
        for position, entry in enumerate(managed):
            if not (
                isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("sha256"), str)
            ):
                return f"binding receipt modules[{index}].managedFiles[{position}] must have string path and sha256"
    return None


@dataclass
class BindingReceipt:
    """``<workspace>/.devkit/specrules-binding.json``。

    ``load`` 区分三种情形：文件不存在 → None；存在且合法 → ``readable=True``；存在但不可读 / 非法 /
    ``schemaVersion`` 更高 → ``readable=False`` 的哨兵对象（路由第 4 步仍视为已采用，``adoptedBy=implicit``）。
    """

    schemaVersion: int
    routingMode: str
    adoptedBy: str
    profileFingerprint: str
    boundAt: str
    modules: list[RuleBinding] = field(default_factory=list)
    readable: bool = True
    problem: str | None = None

    @staticmethod
    def pathFor(workspace: Path) -> Path:
        return Path(workspace) / RULES_RECEIPT_RELATIVE_PATH

    @classmethod
    def unreadable(cls, problem: str) -> "BindingReceipt":
        return cls(
            schemaVersion=0,
            routingMode="adopted",
            adoptedBy="implicit",
            profileFingerprint="",
            boundAt="",
            modules=[],
            readable=False,
            problem=problem,
        )

    @classmethod
    def load(cls, workspace: Path) -> "BindingReceipt | None":
        path = cls.pathFor(workspace)
        if not os.path.lexists(path):
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            return cls.unreadable(f"binding receipt unreadable: {error}")
        if not isinstance(data, dict):
            return cls.unreadable("binding receipt is not a JSON object")
        version = data.get("schemaVersion")
        if not isinstance(version, int) or isinstance(version, bool):
            return cls.unreadable("binding receipt has no integer schemaVersion")
        if version > RULE_BINDING_SCHEMA_VERSION:
            return cls.unreadable(f"binding receipt schemaVersion {version} is newer than supported")
        shape_problem = _rs_receipt_shape_problem(data)
        if shape_problem is not None:
            return cls.unreadable(shape_problem)
        try:
            adopted_by = data["adoptedBy"]
            if adopted_by not in RULES_ADOPTED_BY:
                raise ValueError(f"adoptedBy {adopted_by!r}")
            modules = [RuleBinding.fromReceiptJson(item) for item in data.get("modules") or []]
            return cls(
                schemaVersion=version,
                routingMode=str(data["routingMode"]),
                adoptedBy=adopted_by,
                profileFingerprint=str(data["profileFingerprint"]),
                boundAt=str(data["boundAt"]),
                modules=modules,
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            return cls.unreadable(f"binding receipt malformed: {error}")

    def toJson(self) -> dict[str, Any]:
        return {
            "schemaVersion": RULE_BINDING_SCHEMA_VERSION,
            "routingMode": self.routingMode,
            "adoptedBy": self.adoptedBy,
            "profileFingerprint": self.profileFingerprint,
            "boundAt": self.boundAt,
            # unbound-no-kind 不入回执（D-23 ②）
            "modules": [item.toReceiptJson() for item in self.modules if item.bindingStatus != "unbound-no-kind"],
        }

    def write(self, workspace: Path) -> Path:
        path = self.pathFor(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(self.toJson(), ensure_ascii=False, indent=2) + "\n")
        return path

    def moduleEntry(self, module: str) -> RuleBinding | None:
        for item in self.modules:
            if item.module == module:
                return item
        return None


# ---------------------------------------------------------------------------
# 规则源 catalog（接口 §5）
# ---------------------------------------------------------------------------


def _rs_url(raw: Any, where: str) -> str:
    if not isinstance(raw, str):
        raise DevkitFlowError(f"{where}: {SPECRULES_INVALID_URL_MESSAGE}", error_code=RULES_INVALID_REPO_URL)
    try:
        return validate_specrules_git_url(raw).normalized
    except DevkitFlowError as error:
        raise DevkitFlowError(f"{where}: {error}", error_code=RULES_INVALID_REPO_URL) from error


def _rs_catalog_layer(layer: dict[str, Any] | None, scope: str) -> tuple[dict[str, str], dict[str, Any]]:
    if layer is None:
        return {}, {}
    catalog_raw = layer.get("catalog", {})
    if not isinstance(catalog_raw, dict):
        raise DevkitFlowError(f"Invalid ruleSources.catalog in {scope} config: expected an object")
    catalog: dict[str, str] = {}
    for kind, entry in catalog_raw.items():
        if kind not in RULE_PACKAGE_KINDS:
            raise DevkitFlowError(
                f"Invalid ruleSources.catalog.{kind} in {scope} config: kind must be one of {', '.join(RULE_PACKAGE_KINDS)}"
            )
        if not isinstance(entry, dict):
            raise DevkitFlowError(f"Invalid ruleSources.catalog.{kind} in {scope} config: expected an object")
        if "repoUrl" in entry:
            catalog[kind] = _rs_url(entry["repoUrl"], f"ruleSources.catalog.{kind}.repoUrl ({scope})")
    return catalog, layer


@dataclass
class RuleSourceCatalog:
    globalCatalog: dict[str, str] = field(default_factory=dict)
    projectCatalog: dict[str, str] = field(default_factory=dict)
    routing: str | None = None
    moduleBindings: dict[str, dict[str, str | None]] = field(default_factory=dict)
    projectRepoUrl: str | None = None
    specrulesEnabled: bool = True
    specrulesInvalid: bool = False
    hasProjectSpecrulesConfig: bool = False
    advice: list[str] = field(default_factory=list)

    @classmethod
    def fromConfig(cls, globalConfig: dict[str, Any], projectConfig: dict[str, Any]) -> "RuleSourceCatalog":
        """读 ``ruleSources.*`` 与旧 ``specrules.*``；``specrules`` 开关合并与 ``repoUrl`` 读取复用 ``config``（同一实现）。"""
        switch = merge_specrules_switch(globalConfig, projectConfig)
        project_layer = _config_object_layer(projectConfig, "specrules", SPECRULES_INVALID_LAYER)
        project_repo_url = _specrules_configured_repo_url(project_layer)
        has_project_specrules = isinstance(projectConfig, dict) and "specrules" in projectConfig
        if not switch.enabled:
            # D-30 ⑤：既有禁用语义优先——enabled=false 直接落 disabled，不读取也不校验 ruleSources.*
            return cls(
                projectRepoUrl=project_repo_url,
                specrulesEnabled=False,
                specrulesInvalid=project_repo_url is not None,
                hasProjectSpecrulesConfig=has_project_specrules,
            )
        global_rules = _config_object_layer(globalConfig, "ruleSources", "Invalid ruleSources in global config")
        project_rules = _config_object_layer(projectConfig, "ruleSources", "Invalid ruleSources in project config")
        global_catalog, _ = _rs_catalog_layer(global_rules, "global")
        project_catalog, _ = _rs_catalog_layer(project_rules, "project")
        advice: list[str] = []
        if global_rules is not None and "routing" in global_rules:
            advice.append("全局配置中的 ruleSources.routing 被忽略（只认项目级）")
        routing = None
        if project_rules is not None and "routing" in project_rules:
            routing = project_rules["routing"]
            if routing not in ("auto", "legacy"):
                raise DevkitFlowError("Invalid ruleSources.routing in project config: expected 'auto' or 'legacy'")
        module_bindings: dict[str, dict[str, str | None]] = {}
        if global_rules is not None and "modules" in global_rules:
            advice.append("全局配置中的 ruleSources.modules 被忽略（只认项目级）")
        if project_rules is not None and "modules" in project_rules:
            modules_raw = project_rules["modules"]
            if not isinstance(modules_raw, dict):
                raise DevkitFlowError("Invalid ruleSources.modules in project config: expected an object")
            for rel, entry in modules_raw.items():
                if not isinstance(entry, dict):
                    raise DevkitFlowError(f"Invalid ruleSources.modules.{rel} in project config: expected an object")
                kind = entry.get("kind")
                if kind is not None and kind not in RULE_PACKAGE_KINDS:
                    raise DevkitFlowError(
                        f"Invalid ruleSources.modules.{rel}.kind: must be one of {', '.join(RULE_PACKAGE_KINDS)}"
                    )
                repo_url = (
                    _rs_url(entry["repoUrl"], f"ruleSources.modules.{rel}.repoUrl") if "repoUrl" in entry else None
                )
                if kind is None and repo_url is None:
                    continue
                normalized = Path(str(rel)).as_posix().strip("/") or "."
                module_bindings[normalized] = {"kind": kind, "repoUrl": repo_url}
        return cls(
            globalCatalog=global_catalog,
            projectCatalog=project_catalog,
            routing=routing,
            moduleBindings=module_bindings,
            projectRepoUrl=project_repo_url,
            specrulesEnabled=True,
            specrulesInvalid=False,
            hasProjectSpecrulesConfig=has_project_specrules,
            advice=advice,
        )

    def urlFor(self, kind: str) -> str | None:
        """项目 catalog > 全局 catalog > java-backend 缺省源（缺省值不算用户写入）。"""
        if kind in self.projectCatalog:
            return self.projectCatalog[kind]
        if kind in self.globalCatalog:
            return self.globalCatalog[kind]
        if kind == "java-backend":
            return SPECRULES_REPO_URL
        return None

    def isUserConfigured(self, kind: str) -> bool:
        return kind in self.projectCatalog or kind in self.globalCatalog

    def hasUserCatalog(self) -> bool:
        return bool(self.projectCatalog or self.globalCatalog)

    def urls(self) -> list[str]:
        found: list[str] = []
        for url in (
            *self.projectCatalog.values(),
            *self.globalCatalog.values(),
            SPECRULES_REPO_URL,
            self.projectRepoUrl,
            *(entry.get("repoUrl") for entry in self.moduleBindings.values()),
        ):
            if url and url not in found:
                found.append(url)
        return found

    def sourceDirs(self) -> dict[str, Path]:
        """规范化 URL → 规则缓存目录（``config.specrules_cache_dir_for_url``；只算路径，不访问网络）。"""
        return {url: specrules_cache_dir_for_url(url) for url in self.urls()}

    @staticmethod
    def configurePatch(
        config: dict[str, Any],
        kind: str | None = None,
        repoUrl: str | None = None,
        remove: bool = False,
        *,
        routing: str | None = None,
        module: str | None = None,
    ) -> dict[str, Any]:
        """返回打过补丁的新配置（不改入参）。T-05 的 ``specrules catalog configure`` 经 ``save_devkit_config`` 落盘。"""
        patched = copy.deepcopy(config) if isinstance(config, dict) else {}
        rules = patched.setdefault("ruleSources", {})
        if not isinstance(rules, dict):
            raise DevkitFlowError("Invalid ruleSources: expected an object")
        if routing is not None:
            if routing not in ("auto", "legacy"):
                raise DevkitFlowError("ruleSources.routing must be 'auto' or 'legacy'")
            rules["routing"] = routing
        if kind is not None and kind not in RULE_PACKAGE_KINDS:
            raise DevkitFlowError(f"kind must be one of {', '.join(RULE_PACKAGE_KINDS)}")
        if module is not None:
            modules = rules.setdefault("modules", {})
            key = Path(module).as_posix().strip("/") or "."
            if remove:
                modules.pop(key, None)
            else:
                entry: dict[str, Any] = {}
                if kind is not None:
                    entry["kind"] = kind
                if repoUrl is not None:
                    entry["repoUrl"] = _rs_url(repoUrl, "--repo-url")
                modules[key] = entry
            if not modules:
                rules.pop("modules", None)
        elif kind is not None:
            catalog = rules.setdefault("catalog", {})
            if remove:
                catalog.pop(kind, None)
            elif repoUrl is not None:
                catalog[kind] = {"repoUrl": _rs_url(repoUrl, "--repo-url")}
            if not catalog:
                rules.pop("catalog", None)
        if not rules:
            patched.pop("ruleSources", None)
        return patched


# ---------------------------------------------------------------------------
# 路由判定与选源（设计 §4.7.1.4）
# ---------------------------------------------------------------------------


@dataclass
class RoutingDecision:
    mode: str
    adoptedBy: str | None
    source: str
    advice: list[str] = field(default_factory=list)


def _rs_is_auxiliary(profile: ProjectProfile, module: ModuleProfile) -> bool:
    """D-25 附属模块：项目 confirmed 且模块 status≠confirmed（不匹配 unknowns 文案；CHANGELOG manager 确认）。"""
    return profile.status == "confirmed" and module.status != "confirmed"


def _rs_validate_source(
    source_dir: Path | None, kind: str | None, *, explicit: bool
) -> tuple[str, RulePackageMeta | None, list[str]]:
    """在规则缓存目录上校验源包：返回 (bindingStatus, meta, advice)。``kind=None`` 表示接受包声明的 kind。"""
    if source_dir is None:
        return "incomplete-source", None, ["规则源缓存目录未知"]
    index_file = Path(source_dir) / RULE_PACKAGE_ENTRY
    if not Path(source_dir).is_dir():
        return "incomplete-source", None, [f"规则源缓存不存在：{source_dir}（执行 setup 拉取）"]
    if not index_file.is_file():
        return "incomplete-source", None, [f"规则源缺少统一入口 {RULE_PACKAGE_ENTRY}"]
    try:
        meta = RulePackageMeta.parseIndex(index_file, RULE_PACKAGE_ENTRY)
    except DevkitFlowError as error:
        return "incompatible-source", None, [str(error)]
    problem = meta.compatibilityProblem()
    if problem:
        return "incompatible-source", meta, [problem]
    if meta.isLegacy() and kind not in (None, "java-backend"):
        return "incompatible-source", meta, [f"无 specrulesPackage frontmatter 的旧包只能绑定 java-backend，不能绑定 {kind}"]
    if kind is not None and meta.kind != kind:
        status = "binding-mismatch" if explicit else "incompatible-source"
        return status, meta, [f"规则包声明 kind={meta.kind}，与模块所需 kind={kind} 不一致"]
    # D-35 ①：零规则链接判定下沉到共享源校验（与 project rules 的 packageClosure 同一口径，D-31 ①）
    root = Path(os.path.normpath(os.path.abspath(source_dir)))
    try:
        _, body = _rs_strict_frontmatter(root / RULE_PACKAGE_ENTRY, RULE_PACKAGE_ENTRY)
        links = _rs_index_rule_links(root, root / RULE_PACKAGE_ENTRY, body)
    except DevkitFlowError as error:
        return "incompatible-source", meta, [str(error)]
    if not links:
        return "incomplete-source", meta, [RULES_ADVICE_EMPTY_PACKAGE]
    return "bound", meta, []


def _rs_mount_rel(module: str, mixed: bool) -> str:
    if mixed and module not in ("", "."):
        return f"{module}/{RULES_TREE_NAME}"
    return RULES_TREE_NAME


class RuleSourceSelector:
    """``routingMode`` 六步判定（``setup`` / ``feature init`` / ``project rules`` / catalog status / doctor 共用）与选源。"""

    @staticmethod
    def isNonJavaTree(workspace: Path) -> bool:
        """宽松读取现有 ``specrules/rules/index.md`` 的 ``specrulesPackage.kind``（fail-open，不走 ``parseIndex``）。"""
        kind = _rs_lenient_package_kind(Path(workspace) / RULES_TREE_NAME / RULE_PACKAGE_ENTRY)
        return kind is not None and kind != "java-backend"

    def legacyTreeAtRisk(
        self, workspace: Path, catalog: RuleSourceCatalog, receipt: BindingReceipt | None
    ) -> bool:
        """短路求值：回执存在 → True；项目配置了 ``specrules.repoUrl`` → False（不读 index）；否则 ``isNonJavaTree``。"""
        if receipt is not None:
            return True
        if catalog.projectRepoUrl:
            return False
        return self.isNonJavaTree(workspace)

    def implicitAdoptionKinds(self, profile: ProjectProfile | None, catalog: RuleSourceCatalog) -> list[str]:
        """``setup`` 第 5 步前需拉取的用户写入源 kind（画像 confirmed、非附属模块的 ``ruleKind()``，经 ``isUserConfigured`` 过滤）。"""
        if profile is None or profile.status != "confirmed":
            return []
        kinds: list[str] = []
        for module in profile.modules:
            if _rs_is_auxiliary(profile, module):
                continue
            kind = module.ruleKind()
            if kind and catalog.isUserConfigured(kind) and kind not in kinds:
                kinds.append(kind)
        return kinds

    def _implicit_problem(
        self,
        workspace: Path,
        catalog: RuleSourceCatalog,
        profile: ProjectProfile | None,
        sourceDirs: dict[str, Path],
    ) -> str | None:
        """第 5 步条件 ②③；返回不成立原因，成立返回 None（条件 ① 由调用方先判）。"""
        if os.path.lexists(Path(workspace) / RULES_TREE_NAME):
            return "工作区已有 specrules/ 规则树（非新项目）"
        if catalog.hasProjectSpecrulesConfig:
            return "项目已有 specrules 配置（非新项目）"
        if profile is None:
            return "未提供项目画像"
        if profile.status != "confirmed":
            return f"项目识别为 {profile.status}，未 confirmed"
        for module in profile.modules:
            if _rs_is_auxiliary(profile, module):
                continue
            kind = module.ruleKind()
            if kind is None:
                return f"模块 {module.id} 为 {module.classification}，无专用规则源"
            if not catalog.isUserConfigured(kind):
                return f"{kind} 源未在 catalog 中配置"
            url = catalog.urlFor(kind)
            status, _, advice = _rs_validate_source(sourceDirs.get(url or ""), kind, explicit=False)
            if status != "bound":
                detail = f"：{advice[0]}" if advice else ""
                return f"{kind} 源 {status}{detail}"
        if catalog.moduleBindings:
            # 模块级显式绑定会改变 select 的结论（含 D-27 同一挂载点多源）；预览须与 setup 实际选源一致
            for binding in self.select(profile, catalog, sourceDirs):
                if binding.bindingStatus not in RULES_OK_STATUSES:
                    detail = f"：{binding.advice[-1]}" if binding.advice else ""
                    return f"模块 {binding.module} 选源为 {binding.bindingStatus}{detail}"
        return None

    def routingMode(
        self,
        workspace: Path,
        catalog: RuleSourceCatalog,
        profile: ProjectProfile | None,
        receipt: BindingReceipt | None,
        sourceDirs: dict[str, Path],
        allowImplicitAdoption: bool = False,
    ) -> RoutingDecision:
        advice = list(catalog.advice)
        # ① specrules 开关（含 invalid：enabled=false + 项目 repoUrl）
        if not catalog.specrulesEnabled:
            if catalog.specrulesInvalid:
                advice.append(RULES_ADVICE_INVALID_SWITCH)
            return RoutingDecision("disabled", None, "disabled", advice)
        # ② 显式采用
        if catalog.routing == "auto":
            return RoutingDecision("adopted", "explicit", "project-config", advice)
        # ③ 显式保持旧路径
        if catalog.routing == "legacy":
            return RoutingDecision("legacy", None, "project-config", advice)
        # ④ 已有绑定回执（不可读 / 版本更高仍视为已采用，implicit）
        if receipt is not None:
            if not receipt.readable:
                advice.append(f"{receipt.problem}；下一次 setup 重新校验并重写回执")
                return RoutingDecision("adopted", "implicit", "binding-receipt", advice)
            return RoutingDecision("adopted", receipt.adoptedBy, "binding-receipt", advice)
        # ⑤ 隐式采用（只认用户写入的 catalog；缺省 java-backend 源不计入）
        if catalog.hasUserCatalog():
            problem = self._implicit_problem(workspace, catalog, profile, sourceDirs)
            if problem is None:
                if allowImplicitAdoption:
                    return RoutingDecision("adopted", "implicit", "implicit", advice)
                advice.append(RULES_ADVICE_IMPLICIT_CANDIDATE)
                return RoutingDecision("legacy", None, "implicit-candidate", advice)
            advice.append(f"未隐式采用新路由：{problem}")
        # ⑥ 其余
        return RoutingDecision("legacy", None, "legacy-default", advice)

    def explicitGateBindings(self, profile: ProjectProfile) -> list[RuleBinding]:
        """explicit 非 confirmed 门槛（D-23 ①）：全部已发现模块 ``kind=null``、``binding-mismatch``，与显式绑定无关。"""
        if profile.status == "budget-exhausted":
            hint = "识别预算耗尽（可能由耗时触发）：重试 setup、把大目录加入忽略或收窄扫描范围"
        else:
            hint = f"项目识别为 {profile.status}：补齐识别证据（构建文件、入口）"
        advice = [
            f"routing=auto 要求画像 confirmed；{hint}；或执行 specrules catalog configure --routing legacy 后 setup",
        ]
        return [
            RuleBinding(module=module.id, kind=None, bindingStatus="binding-mismatch", advice=list(advice))
            for module in profile.modules
        ]

    def select(
        self,
        profile: ProjectProfile,
        catalog: RuleSourceCatalog,
        sourceDirs: dict[str, Path],
    ) -> list[RuleBinding]:
        """按优先级为每个模块选源并在缓存上校验：模块显式 > 项目 ``specrules.repoUrl`` > 项目 catalog > 全局 catalog
        > java-backend 缺省（仅 Java 模块）。画像非 confirmed 时不使用任何模块 ``ruleKind()``（D-22 ③）。"""
        confirmed = profile.status == "confirmed"
        mixed = profile.classification == "mixed"
        bindings: list[RuleBinding] = []
        for module in profile.modules:
            entry = f"{_rs_mount_rel(module.id, mixed)}/{RULE_PACKAGE_ENTRY}"
            explicit = catalog.moduleBindings.get(module.id)
            detected = module.ruleKind() if confirmed else None
            binding = RuleBinding(module=module.id, entry=entry)
            if explicit is not None:
                kind = explicit.get("kind") or detected
                if confirmed and explicit.get("kind") and detected and explicit["kind"] != detected:
                    binding.kind = explicit["kind"]
                    binding.bindingStatus = "binding-mismatch"
                    binding.advice.append(
                        f"模块显式绑定 kind={explicit['kind']}，与识别结果 {detected} 冲突；保留用户文件，不挂载"
                    )
                    bindings.append(binding)
                    continue
                url = explicit.get("repoUrl") or (catalog.urlFor(kind) if kind else None)
                if url is None:
                    binding.kind = kind
                    binding.bindingStatus = "missing-source"
                    binding.advice.append(f"模块显式绑定 kind={kind} 但 catalog 中无该 kind 的源")
                    bindings.append(binding)
                    continue
                status, meta, advice = _rs_validate_source(sourceDirs.get(url), kind, explicit=True)
                self._fill(binding, kind or (meta.kind if meta else None), url, status, meta, advice)
                bindings.append(binding)
                continue
            if catalog.projectRepoUrl:
                # 项目 specrules.repoUrl 视为整项目显式绑定（兼容旧语义）
                url = catalog.projectRepoUrl
                status, meta, advice = _rs_validate_source(sourceDirs.get(url), detected, explicit=True)
                self._fill(binding, detected or (meta.kind if meta else None), url, status, meta, advice)
                bindings.append(binding)
                continue
            if not confirmed:
                binding.bindingStatus = "binding-mismatch"
                binding.advice.append(f"项目识别为 {profile.status}，未 confirmed，模块无法按识别结果绑定")
                bindings.append(binding)
                continue
            if detected is None:
                binding.bindingStatus = "unbound-no-kind"
                binding.entry = None
                binding.advice.append(RULES_ADVICE_NO_KIND)
                bindings.append(binding)
                continue
            url = catalog.urlFor(detected)
            if url is None:
                binding.kind = detected
                binding.bindingStatus = "missing-source"
                binding.advice.append(f"模块需要 {detected} 规则源，但 catalog 中无条目；不回落 Java 规则")
                bindings.append(binding)
                continue
            status, meta, advice = _rs_validate_source(sourceDirs.get(url), detected, explicit=False)
            self._fill(binding, detected, url, status, meta, advice)
            bindings.append(binding)
        if not mixed:
            self._shared_mount_conflicts(bindings)
        return bindings

    @staticmethod
    def _shared_mount_conflicts(bindings: list[RuleBinding]) -> None:
        """D-27：非 mixed 项目只有根挂载点，各模块解析出的源必须相同；否则所有已解析出源的模块改为
        ``binding-mismatch``（explicit 下由 ``project rules`` / ``setup`` 失败关闭，implicit 下不采用）。"""
        by_mount: dict[str, list[RuleBinding]] = {}
        for binding in bindings:
            if binding.entry is not None and binding.repoUrl is not None:
                by_mount.setdefault(binding.entry, []).append(binding)
        for entry, group in by_mount.items():
            if len({binding.repoUrl for binding in group}) <= 1:
                continue
            listing = "; ".join(f"{binding.module} → {binding.repoUrl}" for binding in group)
            mount = entry[: -len("/" + RULE_PACKAGE_ENTRY)] if entry.endswith("/" + RULE_PACKAGE_ENTRY) else entry
            for binding in group:
                binding.bindingStatus = "binding-mismatch"
                binding.cacheFailure = None  # 配置层冲突，不是缓存问题
                binding.advice.append(
                    f"非 mixed 项目只有一个规则挂载点 {mount}/，各模块解析出的源不同（{listing}）；"
                    "统一 ruleSources.modules 中的源或删除模块级覆盖后再 setup"
                )

    @staticmethod
    def _fill(
        binding: RuleBinding,
        kind: str | None,
        url: str,
        status: str,
        meta: RulePackageMeta | None,
        advice: list[str],
    ) -> None:
        binding.kind = kind
        binding.repoUrl = url
        binding.bindingStatus = status
        if meta is not None:
            binding.packageVersion = meta.version
            binding.protocol = meta.protocol
        binding.cacheFailure = status if status != "bound" else None
        binding.cacheAdviceStart = len(binding.advice)
        binding.advice.extend(advice)


# ---------------------------------------------------------------------------
# 闭包解析（设计 §4.7.1.6，接口 §3.3 / §6.3）
# ---------------------------------------------------------------------------


@dataclass
class ClosureEntry:
    path: str
    sha256: str
    reason: str

    def toJson(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256, "reason": self.reason}


@dataclass
class MissingLink:
    path: str
    fromFile: str
    link: str

    def toJson(self) -> dict[str, str]:
        return {"path": self.path, "from": self.fromFile, "link": self.link}


@dataclass
class RuleContext:
    binding: RuleBinding
    closure: list[ClosureEntry] = field(default_factory=list)
    excluded: list[ClosureEntry] = field(default_factory=list)
    missing: list[MissingLink] = field(default_factory=list)

    def toJson(self) -> dict[str, Any]:
        binding = self.binding
        return {
            "module": binding.module,
            "kind": binding.kind,
            "bindingStatus": binding.bindingStatus,
            "repoUrl": binding.repoUrl,
            "commit": binding.commit,
            "packageVersion": binding.packageVersion,
            "protocol": binding.protocol,
            "entry": binding.entry,
            "closure": [item.toJson() for item in self.closure],
            "excluded": [item.toJson() for item in self.excluded],
            "missing": [item.toJson() for item in self.missing],
            "advice": list(binding.advice),
        }


@dataclass
class RuleContextSet:
    routingMode: str
    routingSource: str
    adoptedBy: str | None
    stage: str
    profileDigest: str
    modules: list[RuleContext] = field(default_factory=list)
    schemaVersion: int = RULE_BINDING_SCHEMA_VERSION

    def toJson(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "routingMode": self.routingMode,
            "routingSource": self.routingSource,
            "adoptedBy": self.adoptedBy,
            "stage": self.stage,
            "profileDigest": self.profileDigest,
            "modules": [item.toJson() for item in self.modules],
        }


def _rs_section_stage(title: str) -> str | None:
    if "设计阶段" in title or re.search(r"\bdesign\b", title, re.IGNORECASE):
        return "design"
    if "开发阶段" in title or "编码阶段" in title or re.search(r"\b(coding|development)\b", title, re.IGNORECASE):
        return "coding"
    return None


def _rs_extract_links(body: str) -> list[tuple[str, str | None, str | None]]:
    """返回 [(链接原文, 所在二级节标签如 '§1', 节阶段)]；跳过围栏代码块与行内代码、图片。"""
    found: list[tuple[str, str | None, str | None]] = []
    fence: str | None = None
    label: str | None = None
    stage: str | None = None
    for line in body.splitlines():
        fence_match = _RS_FENCE.match(line)
        if fence is not None:
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= len(fence):
                fence = None
            continue
        if fence_match:
            fence = fence_match.group(1)
            continue
        heading = _RS_HEADING.match(line)
        if heading and not line.startswith("###"):
            title = heading.group(1)
            number = _RS_SECTION_NUMBER.match(title)
            label = f"§{number.group(1)}" if number else title
            stage = _rs_section_stage(title)
        stripped = _RS_INLINE_CODE.sub("", line)
        for match in _RS_LINK.finditer(stripped):
            if match.group(1):  # 图片
                continue
            target = match.group(2)
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            found.append((target, label, stage))
    return found


def _rs_index_rule_links(
    root: Path, index_file: Path, body: str
) -> list[tuple[Path, str, str | None, str | None]]:
    """入口 index 解析出的规则链接 ``[(目标, 链接原文, 节标签, 节阶段)]``：跳过非 ``.md``、URL、纯锚点与指向 index
    自身的链接；越出包根抛 ``RULES_INCOMPATIBLE_SOURCE``。零链接（空规则包）判定的唯一口径（D-31 ①、D-35 ①）。"""
    found: list[tuple[Path, str, str | None, str | None]] = []
    for link, label, stage in _rs_extract_links(body):
        target = RuleClosureResolver.resolveLink(root, index_file, link)
        if target is None or target == index_file:
            continue
        found.append((target, link, label, stage))
    return found


def _rs_excluded_digest(path: Path, why: str) -> tuple[str, str]:
    """被排除文件的摘要（D-31 ⑤）：读取失败只在 ``reason`` 中诊断，摘要置空，不抛异常。"""
    if not path.is_file():
        return "", why
    try:
        return _rs_sha256(path), why
    except OSError as error:
        return "", f"{why} (unreadable: {error.strerror or error})"


@dataclass
class _RsKindScope:
    """implicit 保持路径的 ``appliesTo`` 过滤口径（D-31 ②）：只按回执 kind 推出的语言过滤，
    ``classifications`` / ``contractRoles`` 不过滤（回执不记录这两项，按 D-29「只会多不会漏」）。"""

    id: str
    languages: list[str]
    classification: str | None = None
    contractRoles: list[str] | None = None


RULES_KIND_LANGUAGES = {
    "java-backend": ["java", "kotlin"],
    "frontend": ["typescript", "javascript"],
}


def _rs_hold_scope(binding: RuleBinding) -> "_RsKindScope | None":
    """implicit 保持路径的 ``appliesTo`` 口径（D-31 ②、D-28 ②）：恒以回执记录的 kind 推出的语言为准，不用当前画像
    （当前画像的分类 / 语言 / 契约角色变化只进 advice，不改变闭包）。kind 未知时不过滤。"""
    if binding.kind is None:
        return None
    return _RsKindScope(id=binding.module, languages=list(RULES_KIND_LANGUAGES.get(binding.kind, [])))


class RuleClosureResolver:
    """从入口按 Markdown 链接遍历包内文件，输出 ``closure`` / ``excluded`` / ``missing``。"""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)

    @staticmethod
    def resolveLink(packageRoot: Path, sourceFile: Path, link: str) -> Path | None:
        """``specrules/`` / ``@specrules/`` 前缀按包根，其余相对所在文件目录；非 ``.md``、URL、纯锚点返回 None；
        越出包根抛 ``RULES_INCOMPATIBLE_SOURCE``。返回词法规范化后的路径（不要求存在）。"""
        target = link.strip()
        if not target or target.startswith("#") or _RS_SCHEME.match(target):
            return None
        target = unquote(target.split("#", 1)[0].split("?", 1)[0])
        if not target.lower().endswith(".md"):
            return None
        root = Path(os.path.normpath(os.path.abspath(packageRoot)))
        if target.startswith("/"):
            raise _rs_incompatible(
                f"{_rs_rel(sourceFile, root)}: link '{link}' is absolute and leaves the rule package"
            )
        for prefix in ("@specrules/", "specrules/"):
            if target.startswith(prefix):
                candidate = root / target[len(prefix) :]
                break
        else:
            candidate = Path(os.path.abspath(sourceFile)).parent / target
        resolved = Path(os.path.normpath(candidate))
        if os.path.commonpath([str(root), str(resolved)]) != str(root):
            raise _rs_incompatible(
                f"{_rs_rel(sourceFile, root)}: link '{link}' leaves the rule package (cross-package link)"
            )
        if resolved.exists():
            real_root = os.path.realpath(root)
            real = os.path.realpath(resolved)
            if os.path.commonpath([real_root, real]) != real_root:
                raise _rs_incompatible(
                    f"{_rs_rel(sourceFile, root)}: link '{link}' resolves outside the rule package"
                )
        return resolved

    @staticmethod
    def legacyClosure(
        packageRoot: Path, displayRoot: Path, problems: list[str] | None = None
    ) -> list[ClosureEntry]:
        """``legacy``：入口 index 直接引用、存在于包内的 ``.md`` 全集（不过滤、不递归、不报错）。

        宽松读取：frontmatter 未闭合时按全文提取引用（不丢弃正文）；入口或链接目标不可读时跳过该文件，
        原因追加到 ``problems``（调用方转为 ``advice`` 诊断），不抛异常（接口 §9.2，legacy 只诊断不阻塞）。
        """
        root = Path(os.path.normpath(os.path.abspath(packageRoot)))
        index_file = root / RULE_PACKAGE_ENTRY
        try:
            text = index_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            if problems is not None:
                problems.append(f"legacy：无法读取 {_rs_rel(index_file, displayRoot)}，未加载规则（{error}）")
            return []
        raw, body, closed = _rs_split_frontmatter(text)
        if raw is not None and not closed:
            body = raw  # 未闭合：首行 '---' 之后的全文都按正文提取引用
        closure: list[ClosureEntry] = []
        seen: set[Path] = set()
        for link, label, _ in _rs_extract_links(body):
            try:
                target = RuleClosureResolver.resolveLink(root, index_file, link)
            except DevkitFlowError:
                continue
            if target is None or target in seen or target == index_file or not target.is_file():
                continue
            seen.add(target)
            try:
                digest = _rs_sha256(target)
            except OSError as error:
                if problems is not None:
                    problems.append(f"legacy：无法读取 {_rs_rel(target, displayRoot)}，已跳过（{error}）")
                continue
            closure.append(
                ClosureEntry(_rs_rel(target, displayRoot), digest, f"index {label}" if label else "index")
            )
        return closure

    @staticmethod
    def packageClosure(
        packageRoot: Path,
        displayRoot: Path,
        module: ModuleProfile | None,
        stage: str,
    ) -> tuple[list[ClosureEntry], list[ClosureEntry], list[MissingLink]]:
        """``adopted``：严格校验 frontmatter（块写法 + 类型），传递遍历并按阶段 / ``appliesTo`` 过滤。"""
        root = Path(os.path.normpath(os.path.abspath(packageRoot)))
        index_file = root / RULE_PACKAGE_ENTRY
        index_data, index_body = _rs_strict_frontmatter(index_file, RULE_PACKAGE_ENTRY)
        entry_problem = _rs_applies_problem(index_data, RULE_PACKAGE_ENTRY, module)
        if entry_problem is not None:
            # 入口不适用 = 整包不适用于该模块（接口 §6.2 对入口同样生效）
            raise _rs_incompatible(f"{RULE_PACKAGE_ENTRY}: entry does not apply to this module: {entry_problem}")
        included: dict[Path, str] = {}
        excluded: dict[Path, str] = {}
        missing: list[MissingLink] = []
        missing_seen: set[tuple[str, str, str]] = set()
        queue: list[tuple[Path, str, Path, str]] = []
        index_links = 0
        for target, link, label, section_stage in _rs_index_rule_links(root, index_file, index_body):
            index_links += 1
            reason = f"index {label}" if label else "index"
            if section_stage is not None and section_stage != stage:
                excluded.setdefault(target, f"stage: {section_stage} only")
                continue
            queue.append((target, reason, index_file, link))
        if index_links == 0:
            # D-31 ①：adopted 下入口 index 未解析出任何规则链接 = 空规则集，不得以 bound + 空闭包静默通过
            raise DevkitFlowError(
                f"{RULE_PACKAGE_ENTRY}: entry index links no rule files (empty rule package)",
                error_code=RULES_INCOMPLETE_SOURCE,
            )
        while queue:
            target, reason, source, link = queue.pop(0)
            if target in included or target == index_file:
                continue
            if not target.is_file():
                key = (_rs_rel(target, displayRoot), _rs_rel(source, displayRoot), link)
                if key not in missing_seen:
                    missing_seen.add(key)
                    missing.append(MissingLink(*key))
                continue
            label = _rs_rel(target, root)
            data, body = _rs_strict_frontmatter(target, label)
            not_applicable = _rs_applies_problem(data, label, module)
            if not_applicable is not None:
                excluded[target] = not_applicable
                continue
            excluded.pop(target, None)
            included[target] = reason
            for child_link, _, _ in _rs_extract_links(body):
                child = RuleClosureResolver.resolveLink(root, target, child_link)
                if child is None or child in included:
                    continue
                queue.append((child, f"linked from {_rs_rel(target, displayRoot)}", target, child_link))
        closure = [
            ClosureEntry(_rs_rel(path, displayRoot), _rs_sha256_strict(path, root), why)
            for path, why in included.items()
        ]
        dropped = [
            ClosureEntry(_rs_rel(path, displayRoot), *_rs_excluded_digest(path, why))
            for path, why in excluded.items()
            if path not in included
        ]
        return closure, dropped, missing

    def resolve(
        self,
        decision: RoutingDecision,
        bindings: list[RuleBinding],
        profile: ProjectProfile,
        stage: str = "coding",
    ) -> RuleContextSet:
        if stage not in RULES_STAGES:
            raise DevkitFlowError(f"stage must be one of {', '.join(RULES_STAGES)}")
        modules_by_id = {module.id: module for module in profile.modules}
        contexts: list[RuleContext] = []
        for binding in bindings:
            context = RuleContext(binding=binding)
            contexts.append(context)
            if decision.mode == "disabled" or binding.bindingStatus in ("disabled", "unbound-no-kind"):
                continue
            entry = binding.entry
            if entry is None:
                continue
            package_root = self.workspace / Path(entry).parent.parent
            if decision.mode == "legacy":
                problems: list[str] = []
                context.closure = self.legacyClosure(package_root, self.workspace, problems)
                binding.advice.extend(problems)
                continue
            if binding.bindingStatus not in RULES_OK_STATUSES:
                continue  # D-28 ③：非 bound 状态闭包恒为空（explicit 与 implicit 一致）
            if not (package_root / RULE_PACKAGE_ENTRY).is_file():
                # D-28 ①：未挂载不得冒充 bound；explicit 下 project rules 据此 exit 1，implicit 只诊断
                binding.bindingStatus = "not-mounted"
                binding.advice.append(f"规则树未挂载：{entry} 不存在（执行 setup 挂载）")
                continue
            try:
                meta = RulePackageMeta.parseIndex(package_root / RULE_PACKAGE_ENTRY, entry)
                tree_problem = meta.compatibilityProblem()
                if tree_problem is None and binding.kind is not None and meta.kind != binding.kind:
                    tree_problem = f"{entry}: 已挂载规则包 kind={meta.kind}，与绑定 kind={binding.kind} 不一致"
                if tree_problem is None and meta.isLegacy() and binding.kind == "frontend":
                    tree_problem = f"{entry}: 无 specrulesPackage frontmatter 的旧包不能用于 frontend 模块"
                if tree_problem is not None:
                    raise _rs_incompatible(tree_problem)
                scope: Any = modules_by_id.get(binding.module)
                if decision.adoptedBy == "implicit":
                    scope = _rs_hold_scope(binding)
                closure, dropped, missing = self.packageClosure(package_root, self.workspace, scope, stage)
            except DevkitFlowError as error:
                if error.error_code == RULES_INCOMPLETE_SOURCE:
                    binding.bindingStatus = "incomplete-source"
                    binding.advice.append(str(error))
                    continue
                if error.error_code != RULES_INCOMPATIBLE_SOURCE:
                    raise
                binding.bindingStatus = "incompatible-source"
                binding.advice.append(str(error))
                continue
            if binding.kind is None:
                binding.kind = meta.kind
            context.closure, context.excluded, context.missing = closure, dropped, missing
            if missing:
                binding.advice.append(f"规则包内 {len(missing)} 处链接目标缺失，详见 missing[]")
        for context in contexts:
            _rs_enforce_status_invariants(context, decision)
        return RuleContextSet(
            routingMode=decision.mode,
            routingSource=decision.source,
            adoptedBy=decision.adoptedBy,
            stage=stage,
            profileDigest=profile.fingerprint,
            modules=contexts,
        )


def _rs_enforce_status_invariants(context: RuleContext, decision: RoutingDecision) -> None:
    """接口 §3.3 / D-28 ③ 的输出不变式：``unbound-no-kind`` 绑定元数据全为 null；adopted 下非 bound 闭包恒为空。"""
    binding = context.binding
    if binding.bindingStatus == "unbound-no-kind":
        binding.kind = binding.repoUrl = binding.commit = binding.packageVersion = binding.entry = None
        binding.protocol = None
    if binding.bindingStatus == "unbound-no-kind" or (
        decision.mode == "adopted" and binding.bindingStatus not in RULES_OK_STATUSES
    ):
        context.closure, context.excluded, context.missing = [], [], []


def _rs_applies_problem(data: dict[str, Any] | None, label: str, module: "ModuleProfile | _RsKindScope | None") -> str | None:
    """校验 ``appliesTo``（必须为映射、字段为字符串列表），不适用时返回排除原因。"""
    if data is None or "appliesTo" not in data:
        return None
    applies = data["appliesTo"]
    if not isinstance(applies, dict):
        raise _rs_incompatible(
            f"{label}: field appliesTo must be a block-style mapping, got {type(applies).__name__}"
        )
    for name in RULES_APPLIES_TO_FIELDS:
        if name in applies:
            value = applies[name]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise _rs_incompatible(f"{label}: field appliesTo.{name} must be a block-style list of strings")
    if module is None:
        return None
    languages = applies.get("languages")
    if languages is not None and not set(languages) & set(module.languages):
        return f"appliesTo.languages={languages} does not match module languages {module.languages}"
    classifications = applies.get("classifications")
    if classifications is not None and module.classification is not None and module.classification not in classifications:
        return f"appliesTo.classifications={classifications} does not match {module.classification}"
    roles = applies.get("contractRoles")
    if roles is not None and module.contractRoles is not None and not set(roles) & set(module.contractRoles):
        return f"appliesTo.contractRoles={roles} does not match module contractRoles {module.contractRoles}"
    return None


# ---------------------------------------------------------------------------
# 受管树迁移（设计 §4.7.1.5，接口 §8.4）
# ---------------------------------------------------------------------------


@dataclass
class MigrationConflict:
    path: str
    reason: str

    def toJson(self) -> dict[str, str]:
        return {"path": self.path, "reason": self.reason}


@dataclass
class MigrationPlan:
    replacements: list[str] = field(default_factory=list)
    conflicts: list[MigrationConflict] = field(default_factory=list)
    applicable: bool = True
    module: str = "."
    mountPoint: str = RULES_TREE_NAME
    fromSource: dict[str, Any] | None = None
    # D-30 ⑥：submodule 分发不做受管树迁移。非 None 时 ``applicable=False``、``conflicts=[]``，值为原因
    # （``submodule``）；由 setup 既有 submodule 切换逻辑负责，``commit`` 拒绝此类 plan。
    notApplicable: str | None = None
    # D-31 ③：回执清单非空而整棵受管树缺失（冲突为清单内全部文件的 ``modified``）；供调用方给出「受管树被删除」advice。
    treeDeleted: bool = False


@dataclass
class MigrationReceipt:
    fromSource: dict[str, Any] | None
    toSource: dict[str, Any] | None
    result: str
    conflicts: list[MigrationConflict] = field(default_factory=list)
    backupPath: str | None = None
    at: str = ""
    schemaVersion: int = RULE_MIGRATION_SCHEMA_VERSION

    def toJson(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "from": self.fromSource,
            "to": self.toSource,
            "result": self.result,
            "conflicts": [item.toJson() for item in self.conflicts],
            "backupPath": self.backupPath,
            "at": self.at,
        }

    def write(self, workspace: Path, stamp: str) -> Path:
        path = Path(workspace) / RULES_MIGRATIONS_RELATIVE_DIR / f"{stamp}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(self.toJson(), ensure_ascii=False, indent=2) + "\n")
        return path


def _rs_source_summary(binding: RuleBinding | None) -> dict[str, Any] | None:
    if binding is None:
        return None
    return {"kind": binding.kind, "repoUrl": binding.repoUrl, "commit": binding.commit}


def _rs_tree_files(tree: Path) -> list[Path]:
    """规则树内全部条目：普通文件、文件符号链接，以及**目录符号链接本身**（``os.walk`` 不进入目录链接，
    但它们是用户内容，必须参与受管比对，Codex P2-11）。跳过 ``.git``。"""
    files: list[Path] = []
    for current, dirs, names in os.walk(tree):
        descend: list[str] = []
        for name in sorted(dirs):
            if name == ".git":
                continue
            if (Path(current) / name).is_symlink():
                files.append(Path(current) / name)
            else:
                descend.append(name)
        dirs[:] = descend
        for name in sorted(names):
            if name == ".git":
                continue
            files.append(Path(current) / name)
    return files


def _rs_entry_digest(path: Path) -> str:
    """受管清单摘要：符号链接按链接目标文本记 ``symlink:<sha256>``，永不等于普通文件摘要，也不跟随链接。"""
    if path.is_symlink():
        return "symlink:" + hashlib.sha256(os.readlink(path).encode("utf-8", "surrogateescape")).hexdigest()
    return _rs_sha256(path)


class ManagedTreeMigrator:
    """计算与文件级事务：``plan`` 计算待替换文件与冲突，``backup`` / ``restore`` / ``commit`` / ``archiveReceipt`` 负责
    备份、改名换树、写回执、归档回执。不 import ``setup``；实际拉取与挂载由 ``setup.RuleMountExecutor`` 执行。

    submodule 边界（arch P2-6② → D-30 ⑥）：迁移仅适用 copy 模式。目标或回执记录的 ``distribution=submodule``、
    或现有挂载点树根有 ``.git``（目录或 gitlink 文件）时，``plan`` 返回 ``notApplicable="submodule"`` 的计划
    （``applicable=False``、无冲突、不比对文件），由 ``setup`` 既有 submodule 切换备份 / 回滚处理（设计 §4.7.1.5）；
    ``commit`` 收到此类计划直接报错，不动树、不写任何回执。
    """

    def __init__(self, workspace: Path, stamp: Callable[[], str] = _rs_now_stamp):
        self.workspace = Path(workspace)
        self._stamp = stamp
        self._backups: dict[Path, Path] = {}
        # 最近一次 ``commit`` 写出的 applied 迁移回执路径（多挂载点回滚时改写为 rolled-back，arch P2-9）
        self.lastMigrationPath: Path | None = None

    def _mount_point(self, target: RuleBinding) -> str:
        if target.entry and target.entry.endswith("/" + RULE_PACKAGE_ENTRY):
            return target.entry[: -len("/" + RULE_PACKAGE_ENTRY)]
        return _rs_mount_rel(target.module, target.module not in ("", "."))

    def plan(
        self,
        workspace: Path,
        receipt: BindingReceipt | None,
        target: RuleBinding,
        *,
        legacySourceDir: Path | None = None,
    ) -> MigrationPlan:
        """依据回执受管清单（sha256）与当前树计算替换清单与冲突。

        无可用回执条目（存量 copy 工作区首次采用）时，与 ``legacySourceDir``（旧源缓存）逐字节比对：相同者视为受管，
        其余为 ``unknown-owner``；未提供 ``legacySourceDir`` 时现有文件全部为 ``unknown-owner``（保守，失败关闭）。
        """
        workspace = Path(workspace)
        mount = self._mount_point(target)
        tree = workspace / mount
        previous = receipt.moduleEntry(target.module) if receipt is not None and receipt.readable else None
        plan = MigrationPlan(module=target.module, mountPoint=mount, fromSource=_rs_source_summary(previous))
        if (
            target.distribution == "submodule"
            or (previous is not None and previous.distribution == "submodule")
            or os.path.lexists(tree / ".git")
        ):
            plan.applicable = False
            plan.notApplicable = "submodule"
            return plan
        managed = {item.path: item.sha256 for item in previous.managedFiles} if previous is not None else None
        if not os.path.lexists(tree):
            prefix = mount.rstrip("/") + "/"
            deleted = sorted(rel for rel in (managed or {}) if rel.startswith(prefix))
            if not deleted:
                return plan  # 未挂载 / 首次挂载（无回执清单）不算冲突
            # D-31 ③：回执清单非空而整棵受管树被删除 = 受管文件全部被删，记 modified 冲突、不覆盖
            plan.treeDeleted = True
            plan.conflicts.extend(MigrationConflict(rel, "modified") for rel in deleted)
            plan.applicable = False
            return plan
        seen: set[str] = set()
        for path in _rs_tree_files(tree):
            rel = _rs_rel(path, workspace)
            seen.add(rel)
            if managed is not None:
                expected = managed.get(rel)
                if expected is None:
                    plan.conflicts.append(MigrationConflict(rel, "unknown-owner"))
                elif _rs_entry_digest(path) != expected:
                    plan.conflicts.append(MigrationConflict(rel, "modified"))
                else:
                    plan.replacements.append(rel)
                continue
            original = Path(legacySourceDir) / _rs_rel(path, tree) if legacySourceDir is not None else None
            if (
                original is not None
                and not path.is_symlink()
                and original.is_file()
                and not original.is_symlink()
                and original.read_bytes() == path.read_bytes()
            ):
                plan.replacements.append(rel)
            else:
                plan.conflicts.append(MigrationConflict(rel, "unknown-owner"))
        if managed is not None:
            # Codex P2-10：清单中记录、但树中已消失的受管文件 = 用户删除，也是受管文件变更（接口 §8.4 记 modified）
            prefix = mount.rstrip("/") + "/"
            for rel in managed:
                if rel.startswith(prefix) and rel not in seen:
                    plan.conflicts.append(MigrationConflict(rel, "modified"))
        plan.applicable = not plan.conflicts
        return plan

    def _unique_dir(self, parent: Path, stamp: str, suffix: str = "") -> Path:
        candidate = parent / f"{stamp}{suffix}"
        counter = 2
        while os.path.lexists(candidate):
            candidate = parent / f"{stamp}-{counter}{suffix}"
            counter += 1
        return candidate

    def backup(self, tree: Path, stamp: str | None = None) -> Path:
        """把现有规则树改名到 ``.devkit/specrules-backup/<utc>/``，返回备份目录。"""
        tree = Path(tree)
        parent = self.workspace / RULES_BACKUP_RELATIVE_DIR
        parent.mkdir(parents=True, exist_ok=True)
        destination = self._unique_dir(parent, stamp or self._stamp())
        os.replace(tree, destination)
        self._backups[destination] = tree
        return destination

    def restore(self, backup: Path, tree: Path | None = None) -> None:
        """把备份目录改名回原位置（目标已存在时先删除半成品）。"""
        backup = Path(backup)
        target = Path(tree) if tree is not None else self._backups.get(backup)
        if target is None:
            raise DevkitFlowError(f"unknown backup origin: {backup}")
        if os.path.lexists(target):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup, target)
        self._backups.pop(backup, None)

    def commit(self, plan: MigrationPlan, stagedTree: Path, receipt: BindingReceipt) -> MigrationReceipt:
        """原树改名备份 → 临时树改名为挂载点 → 写回执（含本模块受管清单）与迁移回执；任一步失败按备份还原。

        ``plan`` 不可应用时不动原树，写 ``result=not-applied`` 的迁移回执并抛 ``RULES_MIGRATION_CONFLICT``
        （异常的 ``migration_receipt`` 属性带回执对象）。
        """
        if plan.notApplicable is not None:
            raise DevkitFlowError(
                f"rule tree migration not applicable to {plan.mountPoint} ({plan.notApplicable} distribution); "
                "handled by setup submodule switching"
            )
        if not plan.applicable:
            not_applied = self.recordNotApplied(plan, receipt)
            listing = ", ".join(f"{item.path} ({item.reason})" for item in plan.conflicts)
            error = DevkitFlowError(f"rule tree migration conflict: {listing}", error_code=RULES_MIGRATION_CONFLICT)
            error.migration_receipt = not_applied  # type: ignore[attr-defined]
            raise error
        stamp = self._stamp()
        target = receipt.moduleEntry(plan.module)
        to_source = _rs_source_summary(target)
        self.lastMigrationPath = None
        tree = self.workspace / plan.mountPoint
        backup_dir: Path | None = None
        swapped = False
        receipt_path = BindingReceipt.pathFor(self.workspace)
        previous_receipt = receipt_path.read_bytes() if receipt_path.is_file() else None
        try:
            if os.path.lexists(tree):
                backup_dir = self.backup(tree, stamp)
            tree.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stagedTree, tree)
            swapped = True
            if target is not None:
                target.managedFiles = [
                    ManagedFile(_rs_rel(path, self.workspace), _rs_entry_digest(path)) for path in _rs_tree_files(tree)
                ]
            receipt.write(self.workspace)
            applied = MigrationReceipt(
                fromSource=plan.fromSource,
                toSource=to_source,
                result="applied",
                backupPath=(_rs_rel(backup_dir, self.workspace) + "/") if backup_dir is not None else None,
                at=_rs_now_iso(),
            )
            # Codex P1-2：迁移回执写入也在事务内，失败同样按备份还原
            self.lastMigrationPath = applied.write(self.workspace, stamp)
        except Exception:
            if backup_dir is not None:
                self.restore(backup_dir, tree)
            elif swapped and os.path.lexists(tree):
                shutil.rmtree(tree, ignore_errors=True)
            if previous_receipt is not None:
                tmp_receipt = receipt_path.with_name(receipt_path.name + ".rollback")
                tmp_receipt.write_bytes(previous_receipt)
                os.replace(tmp_receipt, receipt_path)
            elif os.path.lexists(receipt_path):
                receipt_path.unlink()
            try:
                MigrationReceipt(
                    fromSource=plan.fromSource,
                    toSource=to_source,
                    result="rolled-back",
                    at=_rs_now_iso(),
                ).write(self.workspace, stamp)
            except OSError:
                pass  # 记录失败不掩盖原始异常；树与回执已还原
            raise
        return applied

    def recordNotApplied(self, plan: MigrationPlan, receipt: BindingReceipt) -> MigrationReceipt:
        """有冲突的计划：不动树，只写 ``result=not-applied`` 的迁移回执（附冲突清单）并返回（codex P2-7）。"""
        not_applied = MigrationReceipt(
            fromSource=plan.fromSource,
            toSource=_rs_source_summary(receipt.moduleEntry(plan.module)),
            result="not-applied",
            conflicts=list(plan.conflicts),
            at=_rs_now_iso(),
        )
        not_applied.write(self.workspace, self._stamp())
        return not_applied

    def archiveReceipt(self, workspace: Path, stamp: str) -> Path | None:
        """显式 legacy 回退：把绑定回执原样移到 ``.devkit/specrules-migrations/<stamp>.binding.json``；无回执返回 None。"""
        source = BindingReceipt.pathFor(workspace)
        if not os.path.lexists(source):
            return None
        destination = Path(workspace) / RULES_MIGRATIONS_RELATIVE_DIR / f"{stamp}.binding.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        return destination


# ---------------------------------------------------------------------------
# 命令（ProjectCommands.command_project_rules，接口 §2.3）
# ---------------------------------------------------------------------------


def _rs_legacy_bindings(workspace: Path, profile: ProjectProfile, decision: RoutingDecision) -> list[RuleBinding]:
    index_exists = (Path(workspace) / RULES_TREE_NAME / RULE_PACKAGE_ENTRY).is_file()
    entry = f"{RULES_TREE_NAME}/{RULE_PACKAGE_ENTRY}" if index_exists else None
    bindings = []
    for module in profile.modules:
        advice = list(decision.advice)
        if not index_exists:
            advice.append("工作区无 specrules/rules/index.md，未加载规则")
        bindings.append(
            RuleBinding(module=module.id, kind=None, bindingStatus="legacy-diagnostic", entry=entry, advice=advice)
        )
    return bindings


def _rs_receipt_entry_path(recorded: RuleBinding, fallback: str | None) -> str | None:
    """回执条目对应的入口：取受管清单中的 ``.../specrules/rules/index.md``；清单缺失时用当前挂载点推算。"""
    suffix = f"{RULES_TREE_NAME}/{RULE_PACKAGE_ENTRY}"
    for item in recorded.managedFiles:
        if item.path == suffix or item.path.endswith("/" + suffix):
            return item.path
    return fallback


def _rs_preview_advice(selected: RuleBinding, recorded: RuleBinding | None) -> list[str]:
    """implicit 保持路径：当前识别 / catalog 的选源结果只作 advice（下一次 setup 才生效，D-28 ②）。"""
    advice: list[str] = []
    if recorded is not None:
        if selected.bindingStatus == "unbound-no-kind":
            advice.append(
                f"当前识别该模块无专用规则源；仍沿用回执绑定 {recorded.kind or '-'} {recorded.repoUrl or '-'}，"
                "下一次 setup 才生效"
            )
        elif selected.kind is not None and (
            selected.kind != recorded.kind or (selected.repoUrl is not None and selected.repoUrl != recorded.repoUrl)
        ):
            advice.append(
                f"当前识别 / 配置选出 {selected.kind or '-'} 源 {selected.repoUrl or '-'}（{selected.bindingStatus}），"
                f"与回执记录的 {recorded.kind or '-'} 源 {recorded.repoUrl or '-'} 不同；下一次 setup 才生效，不静默换源"
            )
    for item in selected.advice:
        if item != RULES_ADVICE_NO_KIND or recorded is None:
            advice.append(f"下一次 setup 预检：{item}")
    return advice


def _rs_receipt_root_entry(receipt: BindingReceipt) -> RuleBinding | None:
    """非 mixed 项目的回执根条目（D-30 ③，D-31 ④ 收紧）：只认 ``module='.'``；没有 → None（按无根条目，
    未记录模块为 ``not-mounted``）。``setup`` 写回执时非 mixed 项目恒写 ``module='.'`` 的根挂载条目，
    submodule 条目的受管清单也记录入口（T-05）。"""
    return receipt.moduleEntry(".")


def _rs_hold_bindings(
    selected: list[RuleBinding], receipt: BindingReceipt, profile: ProjectProfile
) -> list[RuleBinding]:
    """implicit「已采用保持」（D-28 ②）：绑定身份（kind / URL / commit / 版本 / 协议）一律取自回执，状态与闭包由
    ``resolve`` 按现有受管树判定（未挂载 → not-mounted，树与回执 kind 不符或损坏 → incompatible-source）。

    回执未记录的模块（D-30 ③）：当前选源为 ``unbound-no-kind`` 时照旧（setup 本就不为其挂载、不入回执，D-23 ②）；
    否则非 mixed 项目所有模块共享根绑定，按回执根条目判定（无根条目 → ``not-mounted``）；mixed 项目为
    ``not-mounted`` + advice 执行 setup。"""
    mixed = profile.classification == "mixed"
    root_recorded = None if mixed else _rs_receipt_root_entry(receipt)
    held: list[RuleBinding] = []
    for current in selected:
        recorded = receipt.moduleEntry(current.module)
        shared_note: str | None = None
        if recorded is None and current.bindingStatus != "unbound-no-kind" and root_recorded is not None:
            recorded = root_recorded
            shared_note = (
                f"回执未单独记录模块 {current.module}：非 mixed 项目共用根挂载点，沿用回执根条目 {root_recorded.module} 的绑定"
            )
        if recorded is None:
            if current.bindingStatus == "unbound-no-kind":
                held.append(current)
                continue
            binding = RuleBinding(
                module=current.module,
                kind=current.kind,
                repoUrl=current.repoUrl,
                bindingStatus="not-mounted",
                entry=current.entry,
                advice=[f"回执未记录模块 {current.module}：尚未挂载，执行 setup 挂载（下一次 setup 才生效）"],
            )
            binding.advice.extend(_rs_preview_advice(current, None))
            held.append(binding)
            continue
        fallback = current.entry or f"{_rs_mount_rel(current.module, mixed)}/{RULE_PACKAGE_ENTRY}"
        if shared_note is not None:
            fallback = f"{RULES_TREE_NAME}/{RULE_PACKAGE_ENTRY}"
        binding = RuleBinding(
            module=current.module,
            kind=recorded.kind,
            repoUrl=recorded.repoUrl,
            bindingStatus="bound",
            commit=recorded.commit,
            packageVersion=recorded.packageVersion,
            protocol=recorded.protocol,
            entry=_rs_receipt_entry_path(recorded, fallback),
            distribution=recorded.distribution,
            offline=recorded.offline,
        )
        if shared_note is not None:
            binding.advice.append(shared_note)
        binding.advice.extend(_rs_preview_advice(current, recorded))
        held.append(binding)
    return held


RULES_ADVICE_RECEIPT_UNREADABLE = "回执不可读，执行 setup 重写"


def _rs_unreadable_receipt_bindings(
    workspace: Path, selected: list[RuleBinding], receipt: BindingReceipt, profile: ProjectProfile
) -> list[RuleBinding]:
    """implicit 且回执不可读（D-30 ④）：仍是「已采用保持」，身份取现有受管树入口 index 的 frontmatter（宽松读取），
    ``repoUrl`` / ``commit`` 无可信来源恒为 null，状态 ``bound`` + advice（闭包由 ``resolve`` 按现有树严格计算，
    树损坏仍为 ``incompatible-source``）；入口 index 不存在或不可读 → ``incomplete-source``、闭包空。
    当前选源为 ``unbound-no-kind`` 且挂载点无可读 index 的模块照旧（无树可保持）。"""
    mixed = profile.classification == "mixed"
    problem = receipt.problem or "binding receipt unreadable"
    held: list[RuleBinding] = []
    for current in selected:
        entry = f"{_rs_mount_rel(current.module, mixed)}/{RULE_PACKAGE_ENTRY}"
        fields = _rs_lenient_package_fields(Path(workspace) / entry)
        if fields is None and current.bindingStatus == "unbound-no-kind":
            held.append(current)
            continue
        binding = RuleBinding(module=current.module, entry=entry, distribution=None)
        if fields is None:
            binding.bindingStatus = "incomplete-source"
            binding.advice.append(
                f"{RULES_ADVICE_RECEIPT_UNREADABLE}（{problem}）；现有规则树入口 {entry} 不存在或不可读，无法按现有树保持"
            )
        else:
            binding.bindingStatus = "bound"
            binding.kind = fields["kind"]
            binding.packageVersion = fields["version"]
            binding.protocol = fields["protocol"]
            binding.advice.append(
                f"{RULES_ADVICE_RECEIPT_UNREADABLE}（{problem}）；绑定身份取自现有规则树 {entry}，repoUrl / commit 未知"
            )
        binding.advice.extend(_rs_preview_advice(current, None))
        held.append(binding)
    return held


def _rs_cache_missing(url: str | None, sourceDirs: dict[str, Path]) -> bool:
    directory = sourceDirs.get(url or "")
    return directory is None or not Path(directory).is_dir()


def _rs_tree_intact(workspace: Path, entry: str | None, recorded: RuleBinding) -> bool:
    """D-30 ②「受管树完整」：入口 index 存在，且回执受管清单中位于该挂载点下的文件全部仍在（内容改动不在此判定，
    闭包按现有树严格计算；文件被删除说明树已不完整，需重新挂载）。"""
    if entry is None or not (Path(workspace) / entry).is_file():
        return False
    mount = entry[: -len(RULE_PACKAGE_ENTRY)]
    for item in recorded.managedFiles:
        if item.path.startswith(mount) and not os.path.lexists(Path(workspace) / item.path):
            return False
    return True


def _rs_explicit_overlay(
    bindings: list[RuleBinding],
    receipt: BindingReceipt | None,
    sourceDirs: dict[str, Path],
    workspace: Path | None = None,
) -> None:
    """explicit：配置是权威（选哪个源由 ``select`` 决定），回执只用于两处（arch P2-2、Codex P2-5 / P2-6、D-30 ②）：

    - 回执记录的源与当前选源不同（换源未 setup）→ ``binding-mismatch``，失败关闭，不输出混合身份；
    - 同源时 ``commit`` / ``packageVersion`` / ``protocol`` 取回执（已挂载的版本）；若 ``select`` 的失败**只**来自规则
      缓存校验（缓存缺失、缓存包不完整 / 协议更高 / kind 不符等，``cacheFailure``），且受管树完整（``_rs_tree_intact``），
      则按回执与现有树判定为 ``bound``、缓存问题只给 advice（缓存只在重新挂载时需要）；树随后仍由 ``resolve`` 严格校验。
      配置层冲突（显式 kind 与识别冲突、D-27 同挂载点多源、catalog 无源）不在此列，照旧失败关闭。
    回执不可读等同无回执（首次绑定仍需缓存校验）。``unbound-no-kind`` 模块不合并回执（元数据保持全 null）。
    """
    if receipt is None or not receipt.readable:
        return
    for binding in bindings:
        if binding.bindingStatus == "unbound-no-kind":
            continue
        recorded = receipt.moduleEntry(binding.module)
        if recorded is None or binding.repoUrl is None:
            continue
        if recorded.repoUrl != binding.repoUrl or (recorded.kind and binding.kind and recorded.kind != binding.kind):
            if binding.bindingStatus != "binding-mismatch":
                binding.bindingStatus = "binding-mismatch"
                binding.advice.append(
                    f"回执记录的源 {recorded.kind or '-'} {recorded.repoUrl or '-'} 与当前配置 "
                    f"{binding.kind or '-'} {binding.repoUrl} 不一致：执行 setup 切源（explicit 失败关闭，不混合规则身份）"
                )
            continue
        cache_version = binding.packageVersion if binding.bindingStatus == "bound" else None
        cache_failure = binding.cacheFailure
        if (
            cache_failure is not None
            and binding.bindingStatus == cache_failure
            and workspace is not None
            and _rs_tree_intact(workspace, binding.entry, recorded)
        ):
            reasons = "；".join(binding.advice[binding.cacheAdviceStart :]) or cache_failure
            del binding.advice[binding.cacheAdviceStart :]
            binding.bindingStatus = "bound"
            binding.cacheFailure = None
            if binding.kind is None:
                binding.kind = recorded.kind
            if _rs_cache_missing(binding.repoUrl, sourceDirs):
                binding.advice.append("规则源缓存缺失：按回执与已挂载规则树判定；缓存只在重新挂载（setup）时需要")
            else:
                binding.advice.append(
                    f"规则源缓存校验未通过（{cache_failure}：{reasons}）：按回执与已挂载规则树判定；"
                    "缓存只在重新挂载（setup）时需要"
                )
        if binding.bindingStatus != "bound":
            continue
        binding.commit = recorded.commit
        binding.packageVersion = recorded.packageVersion
        binding.protocol = recorded.protocol
        if cache_version and recorded.packageVersion and cache_version != recorded.packageVersion:
            binding.advice.append(
                f"规则源缓存版本 {cache_version} 与已挂载版本 {recorded.packageVersion} 不同；下一次 setup 更新"
            )


RULES_REFRESH_SKIP_ADOPTED = "新路由下规则更新经 setup（refresh 不刷新已采用的规则树）"
RULES_REFRESH_SKIP_PENDING_FALLBACK = (
    "已写入 routing=legacy，回退尚未完成：先执行 setup（自动备份或记录现有树并归档回执）"
)
RULES_REFRESH_SKIP_NON_JAVA = (
    "现有 specrules/ 声明了非 Java 规则包且绑定回执缺失，未刷新：执行 specrules catalog configure --routing auto "
    "后 setup 恢复采用；或 --routing legacy 后 setup 显式回到 Java 规则（自动备份或记录现有树）"
)


def legacy_refresh_guard(workspace: Path) -> dict[str, str] | None:
    """``refresh_specrules``（``specrules refresh`` / ``feature init``）在 resolve 之后、同步之前的路由判定（设计 §4.7.1.4
    「legacy 路径对非 Java 树的处理」）：返回既有 skipped 形状 ``{status, action, message}`` 表示不刷新，None 表示按
    2.45.0 原路径刷新。``profile=None``、``allowImplicitAdoption=False``：不调用识别器、不拉取、不起子进程。

    可能抛异常（配置非法等）；调用方整段捕获并按原方式刷新（Never raises 由调用方保证）。"""
    workspace = Path(workspace)
    global_config, _ = load_config_file(Path.home() / ".config" / "devkit" / "config.json")
    project_config, _ = load_config_file(workspace / CONFIG_PATH)
    catalog = RuleSourceCatalog.fromConfig(global_config, project_config)
    receipt = BindingReceipt.load(workspace)
    selector = RuleSourceSelector()
    decision = selector.routingMode(workspace, catalog, None, receipt, catalog.sourceDirs(), allowImplicitAdoption=False)
    if decision.mode == "adopted":
        return {"status": "skipped", "action": "skipped-adopted", "message": RULES_REFRESH_SKIP_ADOPTED}
    if decision.mode != "legacy" or decision.source not in ("project-config", "legacy-default"):
        return None
    if not selector.legacyTreeAtRisk(workspace, catalog, receipt):
        return None
    if decision.source == "project-config":
        return {
            "status": "skipped",
            "action": "skipped-pending-legacy-fallback",
            "message": RULES_REFRESH_SKIP_PENDING_FALLBACK,
        }
    return {"status": "skipped", "action": "skipped-non-java-tree", "message": RULES_REFRESH_SKIP_NON_JAVA}


def hold_bindings(
    workspace: Path, selected: list[RuleBinding], receipt: BindingReceipt, profile: ProjectProfile
) -> list[RuleBinding]:
    """implicit「已采用保持」的绑定口径（D-28 ②、D-30 ③④）：``project rules`` / catalog status 预览与 ``setup``
    保持路径共用。回执可读 → 身份取回执；不可读 → 身份取现有树入口 frontmatter（宽松读取）。"""
    if receipt.readable:
        return _rs_hold_bindings(selected, receipt, profile)
    return _rs_unreadable_receipt_bindings(workspace, selected, receipt, profile)


def compute_rule_context(
    workspace: Path, stage: str = "coding", profile: ProjectProfile | None = None
) -> tuple[RuleContextSet, RoutingDecision]:
    """``project rules`` 的只读计算（不挂载、不切源、不联网、不写缓存）；供命令与 T-05 / doctor 复用。

    ``profile``：调用方已对同一工作区做过的内存识别结果（doctor 复用 project-detection 的画像，D-73 ①）；缺省时自行识别。
    """
    workspace = Path(workspace).expanduser().resolve()
    global_config, _ = load_config_file(Path.home() / ".config" / "devkit" / "config.json")
    project_config, _ = load_config_file(workspace / CONFIG_PATH)
    catalog = RuleSourceCatalog.fromConfig(global_config, project_config)
    if profile is None:
        profile = ProjectDetector().detect(workspace)
    receipt = BindingReceipt.load(workspace)
    selector = RuleSourceSelector()
    source_dirs = catalog.sourceDirs()
    decision = selector.routingMode(workspace, catalog, profile, receipt, source_dirs, allowImplicitAdoption=False)
    if decision.mode == "disabled":
        bindings = [
            RuleBinding(module=module.id, kind=None, bindingStatus="disabled", advice=list(decision.advice))
            for module in profile.modules
        ]
    elif decision.mode == "legacy":
        bindings = _rs_legacy_bindings(workspace, profile, decision)
    elif decision.adoptedBy == "explicit" and profile.status != "confirmed":
        bindings = selector.explicitGateBindings(profile)
    else:
        selected = selector.select(profile, catalog, source_dirs)
        if decision.adoptedBy == "implicit" and receipt is not None:
            # D-28 ② / D-30 ④：已采用保持（回执不可读时身份取现有受管树 index frontmatter）
            bindings = hold_bindings(workspace, selected, receipt, profile)
        else:
            # explicit：配置权威 + 回执覆盖（D-30 ②）；回执不可读时等同无回执，按缓存校验
            bindings = selected
            _rs_explicit_overlay(bindings, receipt, source_dirs, workspace)
        for binding in bindings:
            binding.advice[:0] = list(decision.advice)
    context = RuleClosureResolver(workspace).resolve(decision, bindings, profile, stage)
    return context, decision


def _rs_text_summary(context: RuleContextSet) -> str:
    adopted = f", {context.adoptedBy}" if context.adoptedBy else ""
    lines = [f"routing: {context.routingMode} ({context.routingSource}{adopted}) stage={context.stage}"]
    for item in context.modules:
        binding = item.binding
        lines.append(
            f"  - {binding.module} [{binding.kind or '-'}] {binding.bindingStatus}: "
            f"{len(item.closure)} files, {len(item.excluded)} excluded, {len(item.missing)} missing"
        )
        for advice in binding.advice:
            lines.append(f"    advice: {advice}")
    return "\n".join(lines)


def command_project_rules(args: argparse.Namespace) -> int:
    """输出 ``rule_context``（接口 §3.3）。``adopted/explicit`` 下存在 ``bound`` / ``unbound-no-kind`` 以外的模块时
    在 ``rule_context`` 上追加 ``status=error`` / ``error_code`` / ``message`` 并 exit 1；其余情形恒 exit 0。"""
    workspace = Path(getattr(args, "path", None) or ".")
    stage = getattr(args, "stage", None) or "coding"
    context, decision = compute_rule_context(workspace, stage)
    module_filter = getattr(args, "module", None)
    if module_filter is not None:
        wanted = Path(module_filter).as_posix().strip("/") or "."
        selected = [item for item in context.modules if item.binding.module == wanted]
        if not selected:
            known = ", ".join(item.binding.module for item in context.modules)
            raise DevkitFlowError(f"unknown module '{module_filter}' (known: {known})")
        context.modules = selected
    data = context.toJson()
    failing = [item for item in context.modules if item.binding.bindingStatus not in RULES_OK_STATUSES]
    exit_code = 0
    if decision.mode == "adopted" and decision.adoptedBy == "explicit" and failing:
        first = failing[0].binding
        data["status"] = "error"
        data["error_code"] = RULES_STATUS_ERROR_CODES.get(first.bindingStatus, RULES_BINDING_MISMATCH)
        data["message"] = f"module {first.module}: {first.bindingStatus}" + (
            f" ({first.advice[-1]})" if first.advice else ""
        )
        exit_code = 1
    if getattr(args, "format", "text") == "json":
        emit(data, "json")
    else:
        message = _rs_text_summary(context)
        if exit_code:
            message += f"\nerror: {data['error_code']}: {data['message']}"
        emit({"message": message}, "text")
    return exit_code
