"""项目识别器（feature 016 T-03；设计 §4.7.1.2，接口 §2.2 / §3.2 / §8.2，类图 §3）。

只读、确定性：只读文本清单与配置、按文件名判断源码布局，``*Application.java`` 候选另按受预算约束的内容判定
（D-102 / F-101）；不执行仓库脚本、不 import 用户代码、不联网、不安装依赖。

- ``ProjectDetector.detect(root)`` 是纯函数：只返回画像，不读写缓存（类图 M-1）。
- ``ProjectDetector.fingerprint(root)`` 是完整口径指纹，即 ``detect(root).fingerprint``（同一次扫描）。
- ``ProfileCache`` 负责缓存读写与快速口径 ``quickState``；写入只由调用方决定
  （``setup`` / ``feature init`` / ``project detect --write-cache``）。

扫描方式（D-22）：
- 有清单的模块（package.json / pom.xml / build.gradle(.kts)）不遍历源码树，只列模块根并做定向探针：
  浏览器入口文件 / 目录、``src/main/resources``、``src/test/java``、``src/main/java`` 首个 ``.java``
  （及必要时的 ``*Application.java``，按内容确认）；JS 模块另从模块根做上限 ``_PD_SOURCE_SAMPLE_MAX_ENTRIES``
  的有序抽样，只用于 languages / testLayout。
- 无清单的模块（判 docs-only / 未声明子工程）做深度受限的广度遍历。
- 每次列目录都边消费边计费，超限即停，不物化剩余条目。

预算（P5）口径：
- 目录项 ≤ ``maxEntries``（只计实际访问的条目）、总耗时 ≤ ``maxElapsedMs``：超出即 ``budget-exhausted``
  （项目 ``classification=unknown``，仍列出已发现模块，模块状态降为 ``unknown``）；耗时在遍历、分类、
  指纹哈希各阶段都检查，耗尽即停。
- 深度 ≤ ``maxDepth``：只是无清单模块广度遍历与 ``**`` 展开的边界，超出的目录不再下探，不算耗尽
  （定向探针用固定安全深度 ``_PD_PROBE_MAX_DEPTH``）。
- 单文件 ≤ ``maxFileBytes``：超出的清单不解析（记入 ``unknowns``），参与指纹时只取
  size / mtime，不算耗尽。

指纹：sha256(detectorVersion, 清单类文件 (path, size, mtime_ns, 内容 sha256), 结构签名, 是否耗尽)。
结构签名为各模块分类结论与证据路径（不含源码内容），使「新增浏览器入口 / Java 源码」这类不改清单的变化
也能让完整口径失效；耗尽标志保证部分扫描的指纹不会等于完整扫描的指纹。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from devkit_flow.constants import PROJECT_PROFILE_CACHE_RELATIVE_PATH
from devkit_flow.errors import DevkitFlowError
from devkit_flow.textio import atomic_write_text, emit


PROJECT_PROFILE_SCHEMA_VERSION = 1
PROJECT_DETECTOR_VERSION = "2"  # D-101 ①：java-service 证据收紧（F-019），规则变化递增（D-24 ②）
PROJECT_DETECTION_FAILED = "PROJECT_DETECTION_FAILED"

PROJECT_CLASSIFICATIONS = (
    "frontend-app",
    "java-service",
    "java-library",
    "node-service",
    "library",
    "tooling",
    "mixed",
    "docs-only",
    "unknown",
)
PROJECT_STATUSES = ("confirmed", "ambiguous", "unknown", "budget-exhausted")
PROJECT_COMMAND_KEYS = ("lint", "typecheck", "build", "test", "e2e")
PROFILE_QUICK_STATES = ("cached", "stale", "not-detected")

# 遍历忽略：目录名（任意层级）与相对遍历起点的路径模式（设计 §4.7.1.2；D-22 补齐构建 / 缓存 / 虚拟环境目录）。
_PD_IGNORED_DIR_NAMES = frozenset(
    {
        "node_modules",
        "dist",
        "build",
        "target",
        "out",
        "coverage",
        ".git",
        "vendor",
        "fixtures",
        "__fixtures__",
        "examples",
        "test-data",
        ".worktrees",
        # D-22①：前端框架构建产物与缓存
        ".next",
        ".nuxt",
        ".output",
        ".svelte-kit",
        ".turbo",
        ".cache",
        ".parcel-cache",
        ".angular",
        ".yarn",
        ".pnpm-store",
        # D-22①：JVM / IDE / Python 环境
        ".gradle",
        ".idea",
        ".venv",
        "venv",
        "__pycache__",
    }
)
_PD_IGNORED_REL_PATTERNS = ("spec/*/worktree",)

_PD_UI_FRAMEWORKS = {
    "react": "react",
    "react-dom": "react",
    "vue": "vue",
    "@angular/core": "angular",
    "svelte": "svelte",
    "solid-js": "solid",
}
_PD_BUILD_TOOLS = {
    "vite": "vite",
    "next": "next",
    "nuxt": "nuxt",
    "webpack": "webpack",
    "@vue/cli-service": "vue-cli",
    "react-scripts": "react-scripts",
}
_PD_BUILD_CONFIG_PREFIXES = {
    "vite.config.": "vite",
    "next.config.": "next",
    "nuxt.config.": "nuxt",
    "webpack.config.": "webpack",
    "vue.config.": "vue-cli",
}
_PD_WEB_COMMANDS = frozenset(
    {"vite", "next", "nuxt", "webpack", "webpack-dev-server", "vue-cli-service", "react-scripts", "ng"}
)
_PD_SSR_PACKAGES = ("next", "nuxt", "@remix-run/react", "@remix-run/node")
_PD_SERVER_FRAMEWORKS = {
    "express": "express",
    "koa": "koa",
    "fastify": "fastify",
    "@nestjs/core": "nestjs",
    "@hapi/hapi": "hapi",
    "hapi": "hapi",
}
_PD_HTTP_CLIENTS = (
    "axios",
    "ky",
    "ofetch",
    "swr",
    "@tanstack/react-query",
    "@tanstack/vue-query",
    "@apollo/client",
    "graphql-request",
    "umi-request",
)
_PD_NON_WEB_COMMANDS = frozenset({"python", "python3", "bash", "sh", "zsh", "node"})
_PD_BROWSER_ENTRY_FILES = (
    "index.html",
    "public/index.html",
    "src/main.ts",
    "src/main.tsx",
    "src/main.js",
    "src/main.jsx",
)
_PD_BROWSER_ENTRY_DIRS = ("pages", "app", "src/pages", "src/app")
_PD_LIBRARY_FIELDS = ("exports", "main", "module", "types", "typings")
_PD_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
)
_PD_SCRIPT_KEYS = {
    "lint": ("lint",),
    "typecheck": ("typecheck", "type-check", "tsc"),
    "build": ("build",),
    "test": ("test",),
    "e2e": ("e2e", "test:e2e"),
}
_PD_LANGUAGE_BY_SUFFIX = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".java": "java",
    ".kt": "kotlin",
    ".py": "python",
}
_PD_DOC_SUFFIXES = frozenset({".md", ".markdown", ".mdx", ".rst", ".adoc", ".txt"})
_PD_SOURCE_SUFFIXES = frozenset(_PD_LANGUAGE_BY_SUFFIX) | frozenset({".vue", ".svelte", ".go", ".rs", ".rb", ".php", ".sh"})
_PD_MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "pyproject.toml",
        "setup.py",
        "requirements.txt",
        "go.mod",
        "Cargo.toml",
    }
)
# 参与指纹的文件：清单 / lockfile / workspace 声明 / 构建与测试配置。
_PD_FINGERPRINT_NAMES = frozenset(
    {
        "package.json",
        "pnpm-workspace.yaml",
        "lerna.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock.json",
        "bun.lockb",
        "bun.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "tsconfig.json",
        "angular.json",
        "pyproject.toml",
        "setup.py",
        "requirements.txt",
        "pytest.ini",
        "go.mod",
        "Cargo.toml",
    }
)
_PD_FINGERPRINT_PREFIXES = (
    "vite.config.",
    "next.config.",
    "nuxt.config.",
    "webpack.config.",
    "vue.config.",
    "vitest.config.",
    "jest.config.",
    ".mocharc",
)
_PD_APPLICATION_CONFIG = re.compile(r"^src/main/resources/application[^/]*\.(ya?ml|properties)$")
_PD_EXECUTABLE_CLASSIFICATIONS = frozenset(
    {"frontend-app", "java-service", "java-library", "node-service", "library", "tooling"}
)
_PD_JAVA_CLASSIFICATIONS = frozenset({"java-service", "java-library"})
# 聚合器（声明了成员的目录，含根）只在自身有执行证据时保留为模块：应用 / 服务 / Java 源码；
# tooling 只在有 package.json ``bin`` 字段证据（根 CLI）时保留，见 ``_pd_keep_aggregator``。
# 不含 library、不按「多数脚本为 node/bash 编排」判出的 tooling：monorepo 根 package.json 常带样板 main 字段
# 与编排脚本，不能据此制造根模块（否则单栈 monorepo 被聚合成 mixed）。
_PD_AGGREGATOR_KEEP_CLASSIFICATIONS = frozenset({"frontend-app", "node-service", "java-service", "java-library"})
_PD_PROBE_MAX_DEPTH = 32  # 定向探测的安全深度（防病态目录），不是 P5 遍历深度
_PD_SOURCE_SAMPLE_MAX_ENTRIES = 1000  # JS 模块有序抽样上限（languages / testLayout），不触发耗尽
_PD_STATUS_SEVERITY = {"confirmed": 0, "unknown": 1, "ambiguous": 2, "budget-exhausted": 3}


# ---------------------------------------------------------------------------
# 领域对象（类图 §3）
# ---------------------------------------------------------------------------


@dataclass
class DetectionBudget:
    """P5 预算。``fresh()`` 返回同限额、计数清零的新实例，``ProjectDetector`` 每次运行取一份。"""

    maxEntries: int = 5000
    maxDepth: int = 6
    maxFileBytes: int = 256 * 1024
    maxElapsedMs: int = 2000
    clock: Callable[[], float] = time.monotonic
    entriesScanned: int = 0
    _started: float | None = None
    _exhausted: bool = False

    def fresh(self) -> "DetectionBudget":
        budget = DetectionBudget(
            maxEntries=self.maxEntries,
            maxDepth=self.maxDepth,
            maxFileBytes=self.maxFileBytes,
            maxElapsedMs=self.maxElapsedMs,
            clock=self.clock,
        )
        budget._started = budget.clock()
        return budget

    def elapsed_ms(self) -> int:
        if self._started is None:
            return 0
        return max(0, int(round((self.clock() - self._started) * 1000)))

    def charge(self, count: int = 1) -> bool:
        """计入目录项；返回本次之后是否仍在预算内。"""
        self.entriesScanned += count
        if self.entriesScanned > self.maxEntries:
            self._exhausted = True
        return not self.exhausted()

    def exhausted(self) -> bool:
        if not self._exhausted and self.elapsed_ms() > self.maxElapsedMs:
            self._exhausted = True
        return self._exhausted


@dataclass
class BudgetUsage:
    entriesScanned: int
    elapsedMs: int
    exhausted: bool

    def toJson(self) -> dict[str, Any]:
        return {"entriesScanned": self.entriesScanned, "elapsedMs": self.elapsedMs, "exhausted": self.exhausted}


@dataclass
class FingerprintInput:
    path: str
    size: int
    mtimeNs: int

    def toJson(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "mtimeNs": self.mtimeNs}


@dataclass
class DetectionEvidence:
    signal: str
    path: str
    detail: str

    def toJson(self) -> dict[str, str]:
        return {"signal": self.signal, "path": self.path, "detail": self.detail}


@dataclass
class ModuleProfile:
    id: str
    classification: str = "unknown"
    status: str = "unknown"
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    executionEnvironments: list[str] = field(default_factory=list)
    packageManager: str | None = None
    testRunner: str = "unknown"
    testLayout: list[str] = field(default_factory=list)
    commands: dict[str, dict[str, str] | None] = field(
        default_factory=lambda: {key: None for key in PROJECT_COMMAND_KEYS}
    )
    contractRoles: list[str] = field(default_factory=list)
    publishForm: str = "unknown"
    componentLibrary: bool = False
    evidence: list[DetectionEvidence] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)

    def ruleKind(self) -> str | None:
        """模块 kind 映射（设计 §4.7.1.4）：前端应用 / 组件库 → frontend；Java 服务 / Java 库 → java-backend；其余无专用源。

        非 ``confirmed`` 一律 None。项目 ``budget-exhausted`` 时 ``detect`` 已把全部模块降为 ``unknown``，
        因此耗尽画像中的模块 kind 不可用于绑定（T-04 / T-05 契约）。
        """
        if self.status != "confirmed":
            return None
        if self.classification == "frontend-app":
            return "frontend"
        if self.classification == "library" and self.componentLibrary:
            return "frontend"
        if self.classification in _PD_JAVA_CLASSIFICATIONS:
            return "java-backend"
        return None

    def toJson(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "classification": self.classification,
            "status": self.status,
            "languages": list(self.languages),
            "frameworks": list(self.frameworks),
            "executionEnvironments": list(self.executionEnvironments),
            "packageManager": self.packageManager,
            "testRunner": self.testRunner,
            "testLayout": list(self.testLayout),
            "commands": {key: (dict(value) if value else None) for key, value in self.commands.items()},
            "contractRoles": list(self.contractRoles),
            "publishForm": self.publishForm,
            "componentLibrary": self.componentLibrary,
            "evidence": [item.toJson() for item in self.evidence],
            "unknowns": list(self.unknowns),
        }

    @classmethod
    def fromJson(cls, data: dict[str, Any]) -> "ModuleProfile":
        commands_raw = data.get("commands") or {}
        commands: dict[str, dict[str, str] | None] = {}
        for key in PROJECT_COMMAND_KEYS:
            value = commands_raw.get(key)
            commands[key] = {"source": str(value["source"])} if isinstance(value, dict) and "source" in value else None
        return cls(
            id=str(data["id"]),
            classification=str(data["classification"]),
            status=str(data["status"]),
            languages=[str(item) for item in data.get("languages") or []],
            frameworks=[str(item) for item in data.get("frameworks") or []],
            executionEnvironments=[str(item) for item in data.get("executionEnvironments") or []],
            packageManager=data.get("packageManager"),
            testRunner=str(data.get("testRunner") or "unknown"),
            testLayout=[str(item) for item in data.get("testLayout") or []],
            commands=commands,
            contractRoles=[str(item) for item in data.get("contractRoles") or []],
            publishForm=str(data.get("publishForm") or "unknown"),
            componentLibrary=bool(data.get("componentLibrary")),
            evidence=[
                DetectionEvidence(str(item["signal"]), str(item["path"]), str(item["detail"]))
                for item in data.get("evidence") or []
            ],
            unknowns=[str(item) for item in data.get("unknowns") or []],
        )


@dataclass
class ProjectProfile:
    root: str
    classification: str
    status: str
    fingerprint: str
    detectedAt: str
    budget: BudgetUsage
    modules: list[ModuleProfile]
    fingerprintInputs: list[FingerprintInput] = field(default_factory=list)
    schemaVersion: int = PROJECT_PROFILE_SCHEMA_VERSION
    detectorVersion: str = PROJECT_DETECTOR_VERSION

    def toJson(self, *, include_fingerprint_inputs: bool = False) -> dict[str, Any]:
        """``project detect`` 输出不含 ``fingerprintInputs``；只有缓存文件带（接口 §3.2 / §8.2）。"""
        data: dict[str, Any] = {
            "schemaVersion": self.schemaVersion,
            "detectorVersion": self.detectorVersion,
            "root": self.root,
            "classification": self.classification,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "detectedAt": self.detectedAt,
            "budget": self.budget.toJson(),
            "modules": [module.toJson() for module in self.modules],
        }
        if include_fingerprint_inputs:
            data["fingerprintInputs"] = [item.toJson() for item in self.fingerprintInputs]
        return data

    @classmethod
    def fromJson(cls, data: dict[str, Any]) -> "ProjectProfile":
        budget = data.get("budget") or {}
        return cls(
            root=str(data["root"]),
            classification=str(data["classification"]),
            status=str(data["status"]),
            fingerprint=str(data["fingerprint"]),
            detectedAt=str(data["detectedAt"]),
            budget=BudgetUsage(
                entriesScanned=int(budget.get("entriesScanned", 0)),
                elapsedMs=int(budget.get("elapsedMs", 0)),
                exhausted=bool(budget.get("exhausted", False)),
            ),
            modules=[ModuleProfile.fromJson(item) for item in data.get("modules") or []],
            fingerprintInputs=[
                FingerprintInput(str(item["path"]), int(item["size"]), int(item["mtimeNs"]))
                for item in data.get("fingerprintInputs") or []
            ],
            schemaVersion=int(data["schemaVersion"]),
            detectorVersion=str(data["detectorVersion"]),
        )


# ---------------------------------------------------------------------------
# 目录访问（只列目录、stat 文件，不读内容）
# ---------------------------------------------------------------------------


@dataclass
class _PdTree:
    """一个模块的可见文件与目录：均为相对模块根的 posix 路径。

    有清单的模块（package.json / pom.xml / build.gradle(.kts)）只含模块根列表与定向探针命中项；
    无清单的模块含深度受限的广度遍历结果（判 docs-only / 未声明子工程需要全貌）。
    """

    files: list[str] = field(default_factory=list)
    dirs: set[str] = field(default_factory=set)


def _pd_is_ignored_dir(name: str, rel: str) -> bool:
    if name in _PD_IGNORED_DIR_NAMES:
        return True
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in _PD_IGNORED_REL_PATTERNS)


def _pd_is_nested_repo(path: Path) -> bool:
    return os.path.lexists(path / ".git")


class _PdProbeOutcome:
    """定向探针的完整性记录：区分「确认不存在」与「探测失败 / 中断」（D-25 只适用前者）。

    ``failed``：目录不可读（含部分读取）、预算耗尽（含截止时间）或安全深度截断，未能看全；
    ``blocked``：路径某级是符号链接、嵌套仓、忽略目录或 ``prune`` 成员，按既有口径不进入，但也不能断言其中无源码。
    """

    def __init__(self) -> None:
        self.failed = False
        self.blocked = False

    def clean(self) -> bool:
        return not self.failed and not self.blocked


class _PdOverflow(Exception):
    """``_pd_list_dir(limit=...)``：目录条目数超过调用方上限（已停止消费，结果整体不可用）。"""


def _pd_list_dir(
    path: Path,
    budget: DetectionBudget,
    *,
    limit: int | None = None,
    outcome: _PdProbeOutcome | None = None,
) -> list[os.DirEntry] | None:
    """列一个目录：边消费迭代器边计入目录项预算，超限立即停止（不物化剩余条目），再对已计费条目按名排序。

    返回 None 表示目录不可读。预算未耗尽时结果与完整排序一致（确定性）；耗尽时结果本就是部分的。
    ``limit``：条目数超过该值即停止并抛 ``_PdOverflow``（调用方整体跳过该目录，保持确定性）。
    ``outcome``：不可读、部分读取或预算中断时置 ``failed``（结果不能作为「目录内不存在」的依据）。
    """
    if budget.exhausted():
        if outcome is not None:
            outcome.failed = True
        return None
    entries: list[os.DirEntry] = []
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                if not budget.charge():
                    if outcome is not None:
                        outcome.failed = True
                    break
                if limit is not None and len(entries) >= limit:
                    raise _PdOverflow()
                entries.append(entry)
    except OSError:
        if outcome is not None:
            outcome.failed = True
        if not entries:
            return None
    entries.sort(key=lambda entry: entry.name)
    return entries


def _pd_entry_kind(entry: os.DirEntry) -> str | None:
    """``dir`` / ``file`` / None（符号链接、不可 stat 或其他类型）；不跟随符号链接。"""
    try:
        if entry.is_symlink():
            return None
        if entry.is_dir(follow_symlinks=False):
            return "dir"
        if entry.is_file(follow_symlinks=False):
            return "file"
    except OSError:
        return None
    return None


def _pd_child_rel(rel: str, name: str) -> str:
    return f"{rel}/{name}" if rel else name


def _pd_walk(
    base: Path,
    budget: DetectionBudget,
    *,
    prune: frozenset[str] = frozenset(),
    base_entries: list[os.DirEntry] | None = None,
) -> _PdTree:
    """广度优先、按名排序遍历 ``base``（只用于无清单模块）；忽略目录、嵌套仓与 ``prune`` 不下探，不跟随符号链接。

    ``base_entries`` 为调用方已列出（已计费）的 ``base`` 条目，避免重复列目录。
    """
    tree = _PdTree()
    queue: deque[tuple[Path, str, int]] = deque([(base, "", 0)])
    while queue and not budget.exhausted():
        current, rel, depth = queue.popleft()
        if rel == "" and base_entries is not None:
            entries: list[os.DirEntry] | None = base_entries
        else:
            entries = _pd_list_dir(current, budget)
        if entries is None:
            continue
        for entry in entries:
            child_rel = _pd_child_rel(rel, entry.name)
            kind = _pd_entry_kind(entry)
            if kind == "dir":
                if _pd_is_ignored_dir(entry.name, child_rel) or child_rel in prune:
                    continue
                child = Path(entry.path)
                if _pd_is_nested_repo(child):
                    continue
                tree.dirs.add(child_rel)
                if depth + 1 < budget.maxDepth:
                    queue.append((child, child_rel, depth + 1))
            elif kind == "file":
                tree.files.append(child_rel)
    tree.files.sort()
    return tree


def _pd_safe_subdir(
    base: Path,
    rel: str,
    budget: DetectionBudget,
    *,
    prune: frozenset[str] = frozenset(),
    outcome: _PdProbeOutcome | None = None,
) -> Path | None:
    """定向探针的目录前置校验：``rel`` 的每一级都必须是真实目录，且不是符号链接、忽略目录、嵌套仓或 ``prune`` 成员。

    计 1 个目录项（一次定向访问）；预算已耗尽（含截止时间）时不访问，返回 None。
    ``outcome``：预算中断 / stat 失败置 ``failed``，符号链接 / 嵌套仓 / 忽略 / prune 置 ``blocked``；
    某级不存在或不是目录时两者都不置（确认不存在）。
    """
    if not budget.charge():
        if outcome is not None:
            outcome.failed = True
        return None
    current = base
    walked = ""
    for part in rel.split("/"):
        walked = _pd_child_rel(walked, part)
        current = current / part
        if _pd_is_ignored_dir(part, walked) or walked in prune:
            if outcome is not None:
                outcome.blocked = True
            return None
        try:
            if current.is_symlink():
                if outcome is not None:
                    outcome.blocked = True
                return None
            if not current.is_dir():
                return None
        except OSError:
            if outcome is not None:
                outcome.failed = True
            return None
        if _pd_is_nested_repo(current):
            if outcome is not None:
                outcome.blocked = True
            return None
    return current


def _pd_probe_file(
    base: Path, rel: str, budget: DetectionBudget, *, prune: frozenset[str] = frozenset()
) -> bool:
    """定向探针：``rel`` 是否为普通文件（父目录须通过 ``_pd_safe_subdir``）；计 1 个目录项。"""
    parent, _, name = rel.rpartition("/")
    directory = _pd_safe_subdir(base, parent, budget, prune=prune) if parent else base
    if directory is None:
        return False
    if not parent and not budget.charge():
        return False
    target = directory / name
    try:
        return not target.is_symlink() and target.is_file()
    except OSError:
        return False


def _pd_find_first(
    base: Path,
    predicate: Callable[[str], bool],
    budget: DetectionBudget,
    *,
    ignore_names: bool = True,
    skip_rels: frozenset[str] = frozenset(),
    outcome: _PdProbeOutcome | None = None,
    accept: Callable[[Path], bool] | None = None,
) -> str | None:
    """定向探针：深度优先、按名排序，先文件后子目录，返回首个文件名命中 ``predicate`` 的相对路径。

    ``accept``：文件名命中后的二次确认（如读内容）；返回 False 时继续搜索，不会被同名的非目标文件挡住。

    不受 ``maxDepth`` 约束（Java 包路径常深于 6 层），只受目录项 / 耗时预算与固定安全深度约束；
    嵌套仓、``.git`` 与符号链接一律不进入。``ignore_names=False`` 时不套用按名忽略列表（D-24③：
    ``src/main/java`` 下的 build / examples / fixtures 等是 Java 包名，不是构建产物目录）。
    ``skip_rels``：相对 ``base`` 的目录路径，整棵跳过（视为调用方明确排除的范围，不算未看全）。
    ``outcome``：返回 None 时记录是「看全后确认没有」还是「不可读 / 预算中断 / 深度截断 / 符号链接 / 嵌套仓」。
    """
    stack: list[tuple[Path, str, int]] = [(base, "", 0)]
    while stack:
        if budget.exhausted():
            if outcome is not None:
                outcome.failed = True
            return None
        current, rel, depth = stack.pop()
        entries = _pd_list_dir(current, budget, outcome=outcome)
        if entries is None:
            continue
        subdirs: list[tuple[Path, str, int]] = []
        for entry in entries:
            child_rel = _pd_child_rel(rel, entry.name)
            kind = _pd_entry_kind(entry)
            if kind == "file":
                if predicate(entry.name) and (accept is None or accept(Path(entry.path))):
                    return child_rel
            elif kind == "dir":
                if child_rel in skip_rels:
                    continue
                if ignore_names and _pd_is_ignored_dir(entry.name, child_rel):
                    continue
                if entry.name == ".git" or _pd_is_nested_repo(Path(entry.path)):
                    if outcome is not None:
                        outcome.blocked = True
                    continue
                if depth + 1 >= _PD_PROBE_MAX_DEPTH:
                    if outcome is not None:
                        outcome.failed = True
                    continue
                subdirs.append((Path(entry.path), child_rel, depth + 1))
            elif outcome is not None:
                outcome.blocked = True  # 符号链接 / 不可 stat / 特殊文件：不进入，也不能断言其中无源码
        stack.extend(reversed(subdirs))
    if budget.exhausted() and outcome is not None:
        outcome.failed = True
    return None


def _pd_sample_tree(
    base: Path,
    budget: DetectionBudget,
    *,
    prune: frozenset[str],
    cap: int,
    base_entries: list[os.DirEntry],
) -> tuple[list[str], set[str], bool]:
    """有清单 JS 模块的有序抽样：从模块根起广度优先、按名排序，最多处理 ``cap`` 个条目（只用于 languages / testLayout）。

    返回 (相对模块根的文件, 目录, 是否截断)。处理条目数达到 ``cap`` 即停止；子目录条目数超过剩余额度时
    只计费不超过「剩余额度 + 1」个条目即停止抽样（不读完大目录，也不按枚举顺序截取，结果确定）。
    忽略目录、嵌套仓、符号链接与 ``prune`` 不下探。``base_entries`` 为已列出（已计费）的模块根条目。
    """
    files: list[str] = []
    dirs: set[str] = set()
    processed = 0
    queue: deque[tuple[Path, str, int]] = deque([(base, "", 0)])
    while queue and not budget.exhausted():
        current, rel, depth = queue.popleft()
        if rel == "":
            entries: list[os.DirEntry] | None = base_entries
        else:
            try:
                entries = _pd_list_dir(current, budget, limit=cap - processed)
            except _PdOverflow:
                return files, dirs, True
        if entries is None:
            continue
        for entry in entries:
            if processed >= cap:
                return files, dirs, True
            processed += 1
            child_rel = _pd_child_rel(rel, entry.name)
            kind = _pd_entry_kind(entry)
            if kind == "dir":
                if _pd_is_ignored_dir(entry.name, child_rel) or child_rel in prune:
                    continue
                child = Path(entry.path)
                if _pd_is_nested_repo(child):
                    continue
                dirs.add(child_rel)
                if depth + 1 < _PD_PROBE_MAX_DEPTH:
                    queue.append((child, child_rel, depth + 1))
            elif kind == "file":
                files.append(child_rel)
    return files, dirs, False


def _pd_join(module_id: str, rel: str) -> str:
    if module_id == ".":
        return rel
    return f"{module_id}/{rel}" if rel else module_id


def _pd_read_text(path: Path, budget: DetectionBudget) -> tuple[str | None, str | None]:
    """读取小文件文本；返回 (text, problem)。超过单文件预算、不可读或预算已耗尽（含截止时间）时 text 为 None。

    所有清单 / 配置读取都经过这里，读取前检查共享截止时间，耗尽后不再 stat、不再读。
    """
    if budget.exhausted():
        return None, "budget-exhausted"
    try:
        size = path.stat().st_size
    except OSError:
        return None, "unreadable"
    if size > budget.maxFileBytes:
        return None, "too-large"
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except OSError:
        return None, "unreadable"


def _pd_load_json(path: Path, budget: DetectionBudget) -> tuple[dict[str, Any] | None, str | None]:
    text, problem = _pd_read_text(path, budget)
    if text is None:
        return None, problem
    try:
        data = json.loads(text)
    except ValueError:
        return None, "invalid-json"
    if not isinstance(data, dict):
        return None, "invalid-json"
    return data, None


def _pd_problem_note(rel: str, problem: str | None, budget: DetectionBudget) -> str:
    if problem == "too-large":
        return f"{rel} exceeds {budget.maxFileBytes // 1024} KiB; not read"
    if problem == "invalid-json":
        return f"{rel} is not a valid JSON object"
    if problem == "budget-exhausted":
        return f"{rel} not read: detection budget exhausted"
    return f"{rel} is unreadable"


# ---------------------------------------------------------------------------
# 模块发现（workspace 声明）
# ---------------------------------------------------------------------------


@dataclass
class _PdDeclaration:
    source: str
    pattern: str


# 声明来源 → 成员目录必须含有的清单（任一）：npm/yarn/pnpm/lerna 成员是含 package.json 的目录，
# Maven <module> 是含 pom.xml 的目录，Gradle include 要求含 build.gradle(.kts)。
_PD_DECLARATION_MANIFESTS = {
    "package.json": ("package.json",),
    "pnpm-workspace.yaml": ("package.json",),
    "lerna.json": ("package.json",),
    "settings.gradle": ("build.gradle", "build.gradle.kts"),
    "settings.gradle.kts": ("build.gradle", "build.gradle.kts"),
    "pom.xml": ("pom.xml",),
}
_PD_MODULE_MANIFESTS = ("package.json", "pom.xml", "build.gradle", "build.gradle.kts")
_PD_MAVEN_NESTING_LIMIT = 6


def _pd_strip_delimited(text: str, opener: str, closer: str, *, drop_unclosed: bool) -> str:
    """线性删除 ``opener … closer`` 区段（F-107：替代 ``.*?`` 正则，未闭合输入下不再是平方级）。

    ``drop_unclosed``：未闭合的 ``opener`` 是否删除到文本末尾。XML 注释未闭合即 pom 已损坏，删到末尾（失败方向是
    「少证据」）；C 风格 ``/*`` 常出现在字符串里（Gradle ``'**/*.yml'``、Java ``"classpath*:spring/*.xml"``），
    未闭合时保留其后文本（与原非贪婪块注释正则的语义一致）。
    """
    parts: list[str] = []
    pos = 0
    while True:
        start = text.find(opener, pos)
        if start < 0:
            parts.append(text[pos:])
            break
        parts.append(text[pos:start])
        end = text.find(closer, start + len(opener))
        if end < 0:
            if not drop_unclosed:
                parts.append(text[start:])
            break
        pos = end + len(closer)
    return "".join(parts)


def _pd_strip_xml_comments(text: str) -> str:
    return _pd_strip_delimited(text, "<!--", "-->", drop_unclosed=True)


# ---------------------------------------------------------------------------
# Spring Boot 可部署证据（D-101 ①，F-019）
#
# java-service / publishForm=app-deploy 只认四类证据之一：构建文件声明 Spring Boot 打包插件、直接声明非 test
# （及非 provided / compileOnly / optional）的 ``spring-boot-starter*`` 依赖、``src/main/resources/application*``
# 配置、``*Application.java`` 主类（后两类在 ``classify_java`` 里判定；主类须读内容确认含 ``@SpringBootApplication``
# 或 ``public static void main(``，同名 POJO 不算，D-102 / F-101）。``<parent>`` 为
# spring-boot-starter-parent、只有 test scope 的 starter、``spring-boot-autoconfigure`` 等非 starter 依赖、
# ``dependencyManagement`` / ``pluginManagement`` 声明都**不算**：它们在 ``*-api`` 等纯库模块里同样常见，
# D-102：Java 模块的 Maven 版本门不读这里推断的 publishForm，误判为 app-deploy 不再跳过升版检查；
# 本段只关乎分类准确性。判不出时保持 java-library、publishForm=unknown。
# ---------------------------------------------------------------------------

_PD_SPRING_BOOT_PLUGIN = "spring-boot-maven-plugin"
_PD_SPRING_BOOT_STARTER_PREFIX = "spring-boot-starter"
_PD_SPRING_BOOT_TEST_STARTER = "spring-boot-starter-test"
# 不计入可部署证据的 Maven scope：test 为测试依赖；provided 由运行容器提供（库模块常见）。
_PD_MAVEN_NON_DEPLOY_SCOPES = frozenset({"test", "provided"})
# 只描述声明、不代表本模块实际使用的块；profiles 为条件激活，按不确定处理（失败方向是 unknown → 按 Maven）。
_PD_MAVEN_DECLARATION_BLOCKS = ("parent", "dependencyManagement", "pluginManagement", "profiles", "reporting")
# Gradle 中视为运行期依赖的配置；testXxx、compileOnly、annotationProcessor 等不算。
_PD_GRADLE_DEPLOY_CONFIGURATIONS = frozenset({"implementation", "api", "compile", "runtime", "runtimeOnly"})
# F-102：``id 'org.springframework.boot' … apply false`` 只声明不应用，不算证据（见 ``_pd_gradle_boot_plugin``）。
# 正则均避免相邻可重叠量词（F-107：最坏情况线性）。
_PD_GRADLE_BOOT_PLUGIN_ID = re.compile(r"""\bid\s*(?:\(\s*)?["']org\.springframework\.boot["']""")
_PD_GRADLE_BOOT_PLUGIN_APPLY = re.compile(r"""\bapply\s+plugin\s*:\s*["']org\.springframework\.boot["']""")
# 同一插件声明的边界：换行（下一行以 ``.`` 续接的 KTS 链式调用除外）、``}``、``;`` 或下一个 ``id``。
_PD_GRADLE_DECLARATION_END = re.compile(r"[\n};]|\bid\b")
_PD_GRADLE_APPLY_FALSE = re.compile(r"\bapply\s*(?:\(\s*)?false\b")
# F-103：配置名可位于行首或 ``{`` / ``;`` 之后（单行 ``dependencies { implementation '…' }``）；
# 坐标支持字符串写法与 map 写法（Groovy ``group: '…', name: '…'`` / KTS ``(group = "…", name = "…")``）。
_PD_GRADLE_STARTER = re.compile(
    r"""(?:^|[{;])[ \t]*([A-Za-z_]\w*)\s*(?:\(\s*)?["']org\.springframework\.boot:(spring-boot-starter[\w.\-]*)""",
    re.MULTILINE,
)
_PD_GRADLE_STARTER_MAP = re.compile(
    r"""(?:^|[{;])[ \t]*([A-Za-z_]\w*)\s*(?:\(\s*)?group\s*[:=]\s*["']org\.springframework\.boot["']\s*,\s*"""
    r"""name\s*[:=]\s*["'](spring-boot-starter[\w.\-]*)["']""",
    re.MULTILINE,
)
# XML 标签记号：名称后用否定前瞻截断、属性区不跨 ``<``，每个起点只扫描到下一个 ``<`` / ``>``（F-107）。
_PD_XML_TAG = re.compile(r"<(/?)([A-Za-z_][\w.\-:]*)(?![\w.\-:])([^<>]*)>")
# Java 服务入口（F-101）：去注释后含 ``@SpringBootApplication``（可全限定）或 ``public static void main(``。
_PD_JAVA_ENTRY = re.compile(
    r"@(?:[\w.]+\.)?SpringBootApplication\b|\bpublic\s+static\s+void\s+main\s*\(|\bstatic\s+public\s+void\s+main\s*\("
)


def _pd_xml_tags(text: str):
    """按出现顺序产出 ``(match, closing, name, self_closing)``；``<?…?>`` / ``<!…>`` 不是元素标签，自然跳过。"""
    for match in _PD_XML_TAG.finditer(text):
        yield match, match.group(1) == "/", match.group(2), match.group(3).endswith("/")


def _pd_xml_remove_blocks(text: str, tags: tuple[str, ...]) -> str:
    """单遍删除 ``tags`` 中任一元素的完整块（含同名嵌套计数）；线性复杂度（F-107）。

    未闭合的块删除到文本末尾（失败关闭：声明块里的内容不得因结构损坏而重新暴露为证据）。
    """
    targets = frozenset(tags)
    parts: list[str] = []
    pos = 0
    current: str | None = None
    depth = 0
    for match, closing, name, self_closing in _pd_xml_tags(text):
        if current is None:
            if closing or name not in targets:
                continue
            parts.append(text[pos : match.start()])
            if self_closing:
                pos = match.end()
            else:
                current, depth = name, 1
        elif name == current and not self_closing:
            depth += -1 if closing else 1
            if depth == 0:
                current = None
                pos = match.end()
    if current is None:
        parts.append(text[pos:])
    return "".join(parts)


def _pd_xml_blocks(text: str, tag: str) -> list[str]:
    """最外层、已闭合的 ``<tag>`` 块内文本（按出现顺序）；未闭合的块不返回。线性复杂度（F-107）。"""
    blocks: list[str] = []
    depth = 0
    inner_start = 0
    for match, closing, name, self_closing in _pd_xml_tags(text):
        if name != tag or self_closing:
            continue
        if closing:
            if depth:
                depth -= 1
                if depth == 0:
                    blocks.append(text[inner_start : match.start()])
        else:
            if depth == 0:
                inner_start = match.end()
            depth += 1
    return blocks


def _pd_xml_child_text(block: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}(?![\w.\-:])[^<>]*>([^<]*)</{tag}\s*>", block)
    return match.group(1).strip() if match else None


def _pd_gradle_boot_plugin(text: str) -> bool:
    """Gradle 是否应用 Spring Boot 插件（已去注释）：``apply plugin:`` 计入；``id '…'`` 声明在同一声明内
    出现 ``apply false`` / ``apply(false)`` 时不计（F-102，Groovy 与 KTS，带不带 ``version`` 都一样）。"""
    if _PD_GRADLE_BOOT_PLUGIN_APPLY.search(text):
        return True
    for match in _PD_GRADLE_BOOT_PLUGIN_ID.finditer(text):
        pos = match.end()
        while True:
            end = _PD_GRADLE_DECLARATION_END.search(text, pos)
            if end is None:
                stop = len(text)
                break
            stop = end.start()
            if end.group() == "\n":
                rest = text[end.end() : end.end() + 256].lstrip()
                if rest.startswith("."):
                    pos = end.end()
                    continue
            break
        if not _PD_GRADLE_APPLY_FALSE.search(text, match.end(), stop):
            return True
    return False


def _pd_strip_gradle_comments(text: str) -> str:
    return re.sub(r"//[^\n]*", "", _pd_strip_delimited(text, "/*", "*/", drop_unclosed=False))


def _pd_spring_boot_build_evidence(build_file: str, build_text: str) -> tuple[str | None, str | None]:
    """返回 ``(plugin_detail, starter_artifact)``：构建文件中的 Spring Boot 打包插件与首个可部署 starter（均可为 None）。

    Maven（``build_text`` 已去注释）：先删除 ``_PD_MAVEN_DECLARATION_BLOCKS``，插件只看 ``build > plugins > plugin``
    的自身 artifactId（插件内的 dependencies / configuration / executions 不算）；依赖只看 ``project > dependencies``
    下的直接 ``<dependency>``（删除 ``<build>`` 与 ``<exclusions>`` 后），artifactId 以 ``spring-boot-starter`` 开头、
    不是 ``spring-boot-starter-test``、scope 不在 test / provided、未标 ``<optional>true</optional>``。
    Gradle：``id 'org.springframework.boot'``（同一声明含 ``apply false`` 不算）/ ``apply plugin: 'org.springframework.boot'``；
    ``implementation|api|compile|runtime|runtimeOnly 'org.springframework.boot:spring-boot-starter*'``（行首或
    ``{`` / ``;`` 之后，含 map 写法 ``group: 'org.springframework.boot', name: 'spring-boot-starter*'``）。
    所有解析均为线性（F-107）：标签块用单遍记号扫描，未闭合的声明块删除到末尾。
    """
    if not build_text:
        return None, None
    if build_file == "pom.xml":
        text = _pd_xml_remove_blocks(build_text, _PD_MAVEN_DECLARATION_BLOCKS)
        plugin = None
        for block in _pd_xml_blocks(text, "plugin"):
            own = _pd_xml_remove_blocks(block, ("dependencies", "configuration", "executions"))
            if _pd_xml_child_text(own, "artifactId") == _PD_SPRING_BOOT_PLUGIN:
                plugin = _PD_SPRING_BOOT_PLUGIN
                break
        starter = None
        deps_text = _pd_xml_remove_blocks(text, ("build",))
        for block in _pd_xml_blocks(deps_text, "dependency"):
            own = _pd_xml_remove_blocks(block, ("exclusions",))
            artifact = _pd_xml_child_text(own, "artifactId") or ""
            scope = (_pd_xml_child_text(own, "scope") or "compile").strip().lower()
            optional = (_pd_xml_child_text(own, "optional") or "").strip().lower() == "true"
            if (
                artifact.startswith(_PD_SPRING_BOOT_STARTER_PREFIX)
                and artifact != _PD_SPRING_BOOT_TEST_STARTER
                and scope not in _PD_MAVEN_NON_DEPLOY_SCOPES
                and not optional
            ):
                starter = artifact
                break
        return plugin, starter
    text = _pd_strip_gradle_comments(build_text)
    plugin = "org.springframework.boot" if _pd_gradle_boot_plugin(text) else None
    starter = None
    candidates = sorted(
        (match.start(), match.group(1), match.group(2))
        for pattern in (_PD_GRADLE_STARTER, _PD_GRADLE_STARTER_MAP)
        for match in pattern.finditer(text)
    )
    for _, configuration, artifact in candidates:
        if configuration in _PD_GRADLE_DEPLOY_CONFIGURATIONS and artifact != _PD_SPRING_BOOT_TEST_STARTER:
            starter = artifact
            break
    return plugin, starter


def _pd_yaml_scalar(raw: str) -> str:
    return raw.strip().strip("'\"").strip()


def _pd_pnpm_workspace_patterns(text: str) -> list[str]:
    """``pnpm-workspace.yaml`` 的 ``packages``：支持块序列与 flow 序列（``packages: ['a', "b"]``，可跨行）。"""
    lines = [raw.split("#", 1)[0].rstrip() for raw in text.splitlines()]
    patterns: list[str] = []
    in_packages = False
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip():
            continue
        if not line.startswith((" ", "\t", "-")):
            key, _, rest = line.partition(":")
            in_packages = key.strip() == "packages"
            rest = rest.strip()
            if in_packages and rest.startswith("["):
                flow = rest
                while "]" not in flow and index < len(lines):
                    flow += " " + lines[index].strip()
                    index += 1
                body = flow[1 : flow.index("]")] if "]" in flow else flow[1:]
                patterns.extend(value for value in (_pd_yaml_scalar(item) for item in body.split(",")) if value)
                in_packages = False
            continue
        stripped = line.strip()
        if in_packages and stripped.startswith("-"):
            value = _pd_yaml_scalar(stripped[1:])
            if value:
                patterns.append(value)
    return patterns


def _pd_gradle_includes(text: str) -> list[str]:
    """``settings.gradle(.kts)`` 的 ``include``：支持 ``include(...)`` 跨行与 Groovy 逗号续行；只取字面量，不执行 Gradle。"""
    body = _pd_strip_delimited(text, "/*", "*/", drop_unclosed=False)
    body = re.sub(r"(?m)(^|\s)//.*$", r"\1", body)
    patterns: list[str] = []
    # F-107：每条 include 的参数范围截止到下一条 include 起点，不再对剩余全文切片 / 分行（避免平方级）。
    matches = list(re.finditer(r"(?m)^[ \t]*include\b", body))
    limits = [match.start() for match in matches[1:]] + [len(body)]
    for start, limit in zip((match.end() for match in matches), limits):
        rest = body[start:limit]
        stripped = rest.lstrip()
        if stripped.startswith("("):
            end = stripped.find(")")
            arguments = stripped[1:end] if end >= 0 else stripped[1:]
        else:
            collected: list[str] = []
            for line in rest.splitlines():
                collected.append(line)
                if not line.rstrip().endswith(","):
                    break
            arguments = "\n".join(collected)
        for name in re.findall(r"['\"]([^'\"]+)['\"]", arguments):
            path = name.strip().lstrip(":").replace(":", "/")
            if path:
                patterns.append(path)
    return patterns


def _pd_pom_modules(text: str) -> list[str]:
    blocks = _pd_xml_blocks(_pd_strip_xml_comments(text), "modules")
    if not blocks:
        return []
    return [item.strip() for item in re.findall(r"<module>([^<]+)</module>", blocks[0]) if item.strip()]


def _pd_is_file(path: Path, budget: DetectionBudget) -> bool:
    """探针前检查共享截止时间；耗尽后不再 stat。"""
    if budget.exhausted():
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def _pd_workspace_declarations(root: Path, budget: DetectionBudget, notes: list[str]) -> list[_PdDeclaration]:
    declarations: list[_PdDeclaration] = []
    package_path = root / "package.json"
    if _pd_is_file(package_path, budget):
        data, _ = _pd_load_json(package_path, budget)
        workspaces = data.get("workspaces") if data else None
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages")
        if isinstance(workspaces, list):
            declarations.extend(
                _PdDeclaration("package.json", str(item)) for item in workspaces if isinstance(item, str)
            )
    pnpm_path = root / "pnpm-workspace.yaml"
    if _pd_is_file(pnpm_path, budget):
        text, problem = _pd_read_text(pnpm_path, budget)
        if text is None:
            notes.append(_pd_problem_note("pnpm-workspace.yaml", problem, budget))
        else:
            declarations.extend(
                _PdDeclaration("pnpm-workspace.yaml", item) for item in _pd_pnpm_workspace_patterns(text)
            )
    lerna_path = root / "lerna.json"
    if _pd_is_file(lerna_path, budget):
        data, _ = _pd_load_json(lerna_path, budget)
        packages = data.get("packages") if data else None
        if isinstance(packages, list):
            declarations.extend(_PdDeclaration("lerna.json", str(item)) for item in packages if isinstance(item, str))
    for gradle_name in ("settings.gradle", "settings.gradle.kts"):
        gradle_path = root / gradle_name
        if _pd_is_file(gradle_path, budget):
            text, _ = _pd_read_text(gradle_path, budget)
            if text is not None:
                declarations.extend(_PdDeclaration(gradle_name, item) for item in _pd_gradle_includes(text))
    pom_path = root / "pom.xml"
    if _pd_is_file(pom_path, budget):
        text, _ = _pd_read_text(pom_path, budget)
        if text is not None:
            declarations.extend(_PdDeclaration("pom.xml", item) for item in _pd_pom_modules(text))
    return declarations


def _pd_has_manifest(root: Path, rel: str, manifests: tuple[str, ...], budget: DetectionBudget) -> bool:
    if not budget.charge():
        return False
    base = root / rel if rel else root
    for name in manifests:
        target = base / name
        try:
            if not target.is_symlink() and target.is_file():
                return True
        except OSError:
            continue
    return False


def _pd_list_child_dirs(root: Path, rel: str, budget: DetectionBudget, *, skip_ignored: bool) -> list[str]:
    entries = _pd_list_dir(root / rel if rel else root, budget)
    children: list[str] = []
    for entry in entries or []:
        if _pd_entry_kind(entry) != "dir":
            continue
        if skip_ignored and (entry.name in _PD_IGNORED_DIR_NAMES or entry.name.startswith(".")):
            continue
        if _pd_is_nested_repo(Path(entry.path)):
            continue
        children.append(_pd_child_rel(rel, entry.name))
    return children


def _pd_expand_pattern(
    root: Path, base_rel: str, pattern: str, manifests: tuple[str, ...], budget: DetectionBudget
) -> list[str]:
    """把 workspace 声明展开为成员目录（相对 root）：只保留含 ``manifests`` 之一的目录。

    声明目录本身即使命中忽略规则也纳入（防漏真实工程）；``**`` 不下探进已是成员的目录（包根），
    也不进入点目录与忽略目录。``..``、绝对路径与符号链接目录不展开。
    """
    cleaned = pattern.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.rstrip("/")
    if not cleaned or cleaned == ".":
        return []
    segments = cleaned.split("/")
    if any(segment in ("..", "") for segment in segments) or cleaned.startswith("/"):
        return []
    current: list[str] = [base_rel]
    for index, segment in enumerate(segments):
        following: list[str] = []
        if segment == "**":
            frontier = list(current)
            following.extend(frontier)
            depth = 0
            while frontier and depth < budget.maxDepth and not budget.exhausted():
                next_frontier: list[str] = []
                for rel in frontier:
                    if rel != base_rel and _pd_has_manifest(root, rel, manifests, budget):
                        continue  # 包根：成员本身，不再下探其源码目录
                    next_frontier.extend(_pd_list_child_dirs(root, rel, budget, skip_ignored=True))
                following.extend(next_frontier)
                frontier = next_frontier
                depth += 1
        elif any(char in segment for char in "*?["):
            for rel in current:
                for child in _pd_list_child_dirs(root, rel, budget, skip_ignored=index < len(segments) - 1):
                    if fnmatch.fnmatchcase(child.rsplit("/", 1)[-1], segment):
                        following.append(child)
        else:
            for rel in current:
                if budget.exhausted():
                    break
                candidate = _pd_child_rel(rel, segment)
                path = root / candidate
                try:
                    if not path.is_symlink() and path.is_dir():
                        following.append(candidate)
                except OSError:
                    continue
        current = sorted(set(following))
        if not current:
            break
    return [rel for rel in current if rel and rel != base_rel and _pd_has_manifest(root, rel, manifests, budget)]


@dataclass
class _PdDiscovery:
    members: dict[str, list[_PdDeclaration]]
    aggregators: set[str]


def _pd_discover_modules(root: Path, budget: DetectionBudget, notes: list[str]) -> _PdDiscovery:
    """按 workspace 声明划分模块；无声明（或声明未命中任何成员）时根目录为单模块。

    - 成员按声明来源过滤（``_PD_DECLARATION_MANIFESTS``），``!`` 前缀为排除。
    - Maven 嵌套聚合：成员的 pom.xml 若再声明 ``<modules>``，相对该 pom 递归展开；声明了成员的目录（含根）
      记为聚合器，是否作为模块保留由分类结果决定（``_pd_keep_aggregator``）。
    """
    declarations = _pd_workspace_declarations(root, budget, notes)
    members: dict[str, list[_PdDeclaration]] = {}
    excluded: set[str] = set()
    for declaration in declarations:
        negate = declaration.pattern.startswith("!")
        pattern = declaration.pattern[1:] if negate else declaration.pattern
        manifests = _PD_DECLARATION_MANIFESTS[declaration.source]
        matches = _pd_expand_pattern(root, "", pattern, manifests, budget)
        if negate:
            excluded.update(matches)
            continue
        if not matches and not any(char in pattern for char in "*?["):
            notes.append(f"{declaration.source} declares {pattern} but it has no {' / '.join(manifests)}")
        for rel in matches:
            members.setdefault(rel, []).append(declaration)
    for rel in excluded:
        members.pop(rel, None)
    aggregators: set[str] = {"."} if members else set()

    queue = [(rel, 1) for rel in sorted(members) if any(item.source == "pom.xml" for item in members[rel])]
    seen = {rel for rel, _ in queue}
    while queue and not budget.exhausted():
        parent, level = queue.pop(0)
        text, _ = _pd_read_text(root / parent / "pom.xml", budget)
        children = _pd_pom_modules(text) if text is not None else []
        if not children or level > _PD_MAVEN_NESTING_LIMIT:
            continue
        source = f"{parent}/pom.xml"
        for child in children:
            matches = _pd_expand_pattern(root, parent, child, ("pom.xml",), budget)
            if not matches:
                notes.append(f"{source} declares {child} but it has no pom.xml")
            if matches:
                aggregators.add(parent)  # 至少匹配到一个子模块才是聚合器，否则作为普通成员分类
            for rel in matches:
                members.setdefault(rel, []).append(_PdDeclaration(source, child))
                if rel not in seen:
                    seen.add(rel)
                    queue.append((rel, level + 1))

    if declarations and not members:
        notes.append("workspace declaration matched no directories; root treated as single module")
    return _PdDiscovery(members, aggregators)


# ---------------------------------------------------------------------------
# 扫描（发现 + 模块根列表 + 定向探针）
# ---------------------------------------------------------------------------


@dataclass
class _PdModuleScan:
    id: str
    path: Path
    tree: _PdTree
    declarations: list[_PdDeclaration]
    prune: frozenset[str] = frozenset()
    notes: list[str] = field(default_factory=list)
    scanned: bool = True  # False：预算耗尽后列出的已发现成员，未扫描、不分类


@dataclass
class _PdScan:
    root: Path
    budget: DetectionBudget
    modules: list[_PdModuleScan]
    root_files: list[str]
    notes: list[str]
    aggregators: set[str]


def _pd_is_fingerprint_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    if name in _PD_FINGERPRINT_NAMES:
        return True
    if any(name.startswith(prefix) for prefix in _PD_FINGERPRINT_PREFIXES):
        return True
    return False


def _pd_scan_module(
    module_id: str,
    path: Path,
    budget: DetectionBudget,
    declarations: list[_PdDeclaration],
    prune: frozenset[str],
    entries: list[os.DirEntry] | None = None,
) -> _PdModuleScan:
    """有清单的模块：列模块根 + 定向探针（不遍历源码树）；无清单的模块：深度受限的广度遍历。"""
    scan = _PdModuleScan(module_id, path, _PdTree(), declarations, prune)
    if entries is None:
        entries = _pd_list_dir(path, budget)
    if entries is None:
        return scan
    root_files = sorted(entry.name for entry in entries if _pd_entry_kind(entry) == "file")
    if not any(name in root_files for name in _PD_MODULE_MANIFESTS):
        scan.tree = _pd_walk(path, budget, prune=prune, base_entries=entries)
        return scan

    tree = scan.tree
    tree.files.extend(root_files)
    for entry in entries:
        if _pd_entry_kind(entry) != "dir" or _pd_is_ignored_dir(entry.name, entry.name) or entry.name in prune:
            continue
        if not _pd_is_nested_repo(Path(entry.path)):
            tree.dirs.add(entry.name)
    for rel in _PD_BROWSER_ENTRY_FILES:
        if "/" in rel and _pd_probe_file(path, rel, budget, prune=prune):
            tree.files.append(rel)
    for rel in _PD_BROWSER_ENTRY_DIRS + ("src/test/java",):
        if "/" in rel and _pd_safe_subdir(path, rel, budget, prune=prune) is not None:
            tree.dirs.add(rel)
    resources = _pd_safe_subdir(path, "src/main/resources", budget, prune=prune)
    if resources is not None:
        for entry in _pd_list_dir(resources, budget) or []:
            if _pd_entry_kind(entry) == "file":
                tree.files.append(f"src/main/resources/{entry.name}")
    if "package.json" in root_files:
        files, dirs, truncated = _pd_sample_tree(
            path, budget, prune=prune, cap=_PD_SOURCE_SAMPLE_MAX_ENTRIES, base_entries=entries
        )
        tree.files.extend(files)
        tree.dirs.update(dirs)
        if truncated:
            scan.notes.append(
                f"source sample stopped after {_PD_SOURCE_SAMPLE_MAX_ENTRIES} entries in "
                f"{module_id}; languages / testLayout may be incomplete"
            )
    tree.files = sorted(set(tree.files))
    return scan


def _pd_scan(root: Path, budget: DetectionBudget) -> _PdScan:
    notes: list[str] = []
    discovery = _pd_discover_modules(root, budget, notes)
    modules: list[_PdModuleScan] = []
    if not discovery.members:
        module = _pd_scan_module(".", root, budget, [], frozenset())  # 根目录在此首次列出
        return _PdScan(root, budget, [module], list(module.tree.files), notes, set())

    member_ids = sorted(discovery.members)
    root_entries = _pd_list_dir(root, budget) or []
    root_files = sorted(entry.name for entry in root_entries if _pd_entry_kind(entry) == "file")
    candidates: list[str] = []
    if any(name in root_files for name in _PD_MODULE_MANIFESTS):
        candidates.append(".")  # 根目录自身可执行（如根应用 + packages/*）时保留为模块
    candidates.extend(member_ids)
    for module_id in candidates:
        if budget.exhausted():
            # 耗尽后仍列出已发现成员（detect 中以 unknown 占位），不再列目录 / 探针
            path = root if module_id == "." else root / module_id
            declarations = discovery.members.get(module_id, [])
            modules.append(_PdModuleScan(module_id, path, _PdTree(), declarations, scanned=False))
            continue
        if module_id == ".":
            prune = frozenset(member_ids)
            path = root
        else:
            prune = frozenset(other[len(module_id) + 1 :] for other in member_ids if other.startswith(module_id + "/"))
            path = root / module_id
        listed = root_entries if module_id == "." else None
        modules.append(_pd_scan_module(module_id, path, budget, discovery.members.get(module_id, []), prune, listed))
    return _PdScan(root, budget, modules, root_files, notes, discovery.aggregators)


def _pd_fingerprint_inputs(scan: _PdScan) -> tuple[list[FingerprintInput], list[list[Any]]]:
    """参与指纹的清单 / lockfile / workspace 声明 / 构建配置 / application 配置：stat + 内容哈希（≤ 单文件上限）。

    每个文件哈希前检查耗时预算，耗尽即停（返回部分输入；耗尽标志另行计入指纹）。
    """
    budget = scan.budget
    candidates: set[str] = {rel for rel in scan.root_files if _pd_is_fingerprint_file(rel)}
    for module in scan.modules:
        for local in module.tree.files:
            if _pd_is_fingerprint_file(local) or _PD_APPLICATION_CONFIG.match(local):
                candidates.add(_pd_join(module.id, local))
    inputs: list[FingerprintInput] = []
    hashed: list[list[Any]] = []
    for rel in sorted(candidates):
        if budget.exhausted():
            break
        path = scan.root / rel
        try:
            stat_result = path.stat()
        except OSError:
            continue
        digest = ""
        if stat_result.st_size <= budget.maxFileBytes:
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                digest = ""
        inputs.append(FingerprintInput(rel, stat_result.st_size, stat_result.st_mtime_ns))
        hashed.append([rel, stat_result.st_size, stat_result.st_mtime_ns, digest])
    return inputs, hashed


def _pd_structure_signature(modules: list["ModuleProfile"]) -> list[list[Any]]:
    """结构签名：各模块的分类结论与分类相关路径（证据的 signal + path、languages、testLayout），不含源码内容。

    新增 / 删除浏览器入口、Java 源码、application 配置、未声明子工程清单等会改变结论或证据路径的文件，
    即使不在 ``fingerprintInputs`` 中也会使完整口径指纹失效。
    """
    signature: list[list[Any]] = []
    for module in modules:
        signature.append(
            [
                module.id,
                module.classification,
                module.status,
                list(module.languages),
                list(module.testLayout),
                sorted({(item.signal, item.path) for item in module.evidence}),
            ]
        )
    return signature


# ---------------------------------------------------------------------------
# 模块分类（组合证据表，设计 §4.7.1.2）
# ---------------------------------------------------------------------------


def _pd_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pd_script_commands(script: str) -> list[str]:
    """把一条 npm script 拆成各段首个命令词（跳过 ``FOO=bar`` 环境赋值与 ``cd``）。"""
    commands: list[str] = []
    for segment in re.split(r"&&|\|\||;|\|", script):
        tokens = [token for token in segment.strip().split() if token]
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        if not tokens or tokens[0] == "cd":
            continue
        if tokens[0] == "npx" and len(tokens) > 1:
            tokens.pop(0)
        commands.append(tokens[0].rsplit("/", 1)[-1])
    return commands


class _PdModuleClassifier:
    def __init__(self, scan: _PdScan, module: _PdModuleScan):
        self.scan = scan
        self.module = module
        self.budget = scan.budget
        self.files = set(module.tree.files)
        self.dirs = module.tree.dirs
        self.profile = ModuleProfile(id=module.id)
        self.evidence: list[DetectionEvidence] = []
        self.unknowns: list[str] = []
        self.java_files: list[str] = []
        # D-25 附属资格：仅当 Java 源码探针与 src/ 有界探针都「看全且确认没有任何源码文件」时为 True。
        # 探测失败 / 预算中断 / 有其他语言源码时为 False（模块照常参与项目取最差）。
        self.sourceless = False
        self._java_probe_clean = False

    # -- helpers ---------------------------------------------------------
    def rel(self, local: str) -> str:
        return _pd_join(self.module.id, local)

    def add(self, signal: str, local: str, detail: str) -> None:
        self.evidence.append(DetectionEvidence(signal, self.rel(local) if local else self.module.id, detail))

    def has_file(self, local: str) -> bool:
        return local in self.files

    def root_file_with_prefix(self, prefixes: dict[str, str]) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for local in sorted(self.files):
            if "/" in local:
                continue
            for prefix, label in prefixes.items():
                if local.startswith(prefix):
                    found.append((local, label))
        return found

    # -- main ------------------------------------------------------------
    def classify(self) -> ModuleProfile:
        for declaration in self.module.declarations:
            self.evidence.append(
                DetectionEvidence("workspace-declaration", declaration.source, declaration.pattern)
            )
        self.unknowns.extend(self.module.notes)
        package = self.load_package()
        build_file, build_text = self.load_java_build()
        js_class = self.classify_js(package) if package is not None else None
        java_class = self.classify_java(build_file, build_text) if build_file else None
        if package is None and java_class is None and self._java_probe_clean:
            self.sourceless = self.probe_no_source_files()

        profile = self.profile
        profile.languages = self.detect_languages()
        if js_class and java_class:
            profile.classification = "unknown"
            profile.status = "ambiguous"
            self.unknowns.append(f"conflicting strong evidence: {java_class} vs {js_class}")
        elif js_class == "ambiguous":
            profile.classification = "unknown"
            profile.status = "ambiguous"
            self.unknowns.append("conflicting strong evidence: frontend-app vs node-service")
        elif js_class or java_class:
            profile.classification = str(js_class or java_class)
            profile.status = "confirmed"
        elif self.is_docs_only(package is not None or build_file is not None):
            profile.classification = "docs-only"
            profile.status = "confirmed"
        else:
            profile.classification = "unknown"
            profile.status = "unknown"

        self.fill_package_manager(package, build_file)
        self.fill_test_runner(package, build_text)
        self.fill_test_layout()
        if profile.status == "confirmed" and profile.classification in _PD_EXECUTABLE_CLASSIFICATIONS:
            self.fill_commands(package, build_file)
            self.fill_environments(package)
            self.fill_contract_and_publish(package, build_text)
        elif profile.classification == "docs-only":
            profile.publishForm = "none"
        if profile.status == "confirmed" and profile.ruleKind() is None:
            self.unknowns.append(
                f"no dedicated rule source kind for {profile.classification}; stack-neutral defaults only"
            )
        if profile.status == "unknown" and len(self.unknowns) == len(self.module.notes):
            visible = [local for local in self.module.tree.files if not local.rsplit("/", 1)[-1].startswith(".")]
            if package is None and build_file is None and not visible:
                self.unknowns.append("no execution or documentation evidence")
            else:
                self.unknowns.append("no classification rule matched")

        unique: dict[tuple[str, str, str], DetectionEvidence] = {}
        for item in self.evidence:
            unique.setdefault((item.path, item.signal, item.detail), item)
        profile.evidence = [unique[key] for key in sorted(unique)]
        seen: set[str] = set()
        profile.unknowns = [note for note in self.unknowns if not (note in seen or seen.add(note))]
        return profile

    # -- package.json ----------------------------------------------------
    def load_package(self) -> dict[str, Any] | None:
        if not self.has_file("package.json"):
            return None
        data, problem = _pd_load_json(self.module.path / "package.json", self.budget)
        if data is None:
            self.unknowns.append(_pd_problem_note(self.rel("package.json"), problem, self.budget))
            return None
        self.add("manifest", "package.json", "package.json")
        return data

    def browser_entries(self) -> list[str]:
        entries = [local for local in _PD_BROWSER_ENTRY_FILES if self.has_file(local)]
        entries.extend(f"{local}/" for local in _PD_BROWSER_ENTRY_DIRS if local in self.dirs)
        return entries

    def classify_js(self, package: dict[str, Any]) -> str | None:
        deps = _pd_dict(package.get("dependencies"))
        dev = _pd_dict(package.get("devDependencies"))
        peer = _pd_dict(package.get("peerDependencies"))
        scripts = {str(key): str(value) for key, value in _pd_dict(package.get("scripts")).items()}

        ui_deps = sorted(name for name in _PD_UI_FRAMEWORKS if name in deps)
        ui_peer = sorted(name for name in _PD_UI_FRAMEWORKS if name in peer)
        ui_dev_only = sorted(name for name in _PD_UI_FRAMEWORKS if name in dev and name not in deps and name not in peer)
        for name in ui_deps:
            self.add("dependency", "package.json", f"dependencies.{name}")
        for name in ui_peer:
            self.add("dependency", "package.json", f"peerDependencies.{name}")
        for name in ui_dev_only:
            self.add("dependency", "package.json", f"devDependencies.{name}")

        build_labels: set[str] = set()
        for name, label in _PD_BUILD_TOOLS.items():
            for section, table in (("dependencies", deps), ("devDependencies", dev)):
                if name in table:
                    build_labels.add(label)
                    self.add("build-tool", "package.json", f"{section}.{name}")
        for local, label in self.root_file_with_prefix(_PD_BUILD_CONFIG_PREFIXES):
            build_labels.add(label)
            self.add("build-config", local, label)
        script_commands: dict[str, list[str]] = {key: _pd_script_commands(value) for key, value in scripts.items()}
        web_scripts = sorted(key for key, commands in script_commands.items() if _PD_WEB_COMMANDS & set(commands))
        for key in web_scripts:
            self.add("script", "package.json", f"scripts.{key}: {scripts[key]}")

        entries = self.browser_entries()
        server_deps = sorted(name for name in _PD_SERVER_FRAMEWORKS if name in deps)
        start_scripts = sorted(
            key
            for key, value in scripts.items()
            if re.search(r"(^|&&|;)\s*(node|ts-node|nodemon)\s+\S", value) or "nest start" in value
        )

        frameworks: set[str] = {_PD_UI_FRAMEWORKS[name] for name in ui_deps + ui_peer}
        frameworks.update(build_labels)
        frameworks.update(_PD_SERVER_FRAMEWORKS[name] for name in server_deps)
        self.profile.frameworks = sorted(frameworks)

        is_frontend = bool(ui_deps) and bool(entries) and bool(build_labels or web_scripts)
        if is_frontend:
            for local in entries:
                self.add("browser-entry", local.rstrip("/"), "directory" if local.endswith("/") else "file")
        elif ui_dev_only and not ui_deps:
            for name in ui_dev_only:
                self.unknowns.append(f"{name} only in devDependencies; not sufficient for frontend-app")

        is_node_service = bool(server_deps) and bool(start_scripts)
        if server_deps:
            for name in server_deps:
                self.add("dependency", "package.json", f"dependencies.{name}")
        if is_node_service:
            for key in start_scripts:
                self.add("script", "package.json", f"scripts.{key}: {scripts[key]}")
        if is_frontend and is_node_service:
            return "ambiguous"
        if is_frontend:
            return "frontend-app"
        if is_node_service:
            return "node-service"

        if "bin" in package:
            self.add("package-field", "package.json", "bin")
            return "tooling"
        library_fields = [name for name in _PD_LIBRARY_FIELDS if name in package]
        if library_fields and not entries:
            for name in library_fields:
                self.add("package-field", "package.json", name)
            self.profile.componentLibrary = bool(ui_peer)
            return "library"
        if scripts and not entries and not server_deps and not web_scripts:
            non_web = sorted(
                key for key, commands in script_commands.items() if commands and commands[0] in _PD_NON_WEB_COMMANDS
            )
            if len(non_web) * 2 > len(scripts):
                for key in non_web:
                    self.add("script", "package.json", f"scripts.{key}: {scripts[key]}")
                return "tooling"
        return None

    # -- Java ------------------------------------------------------------
    def load_java_build(self) -> tuple[str | None, str]:
        for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
            if self.has_file(name):
                text, problem = _pd_read_text(self.module.path / name, self.budget)
                if text is None:
                    self.unknowns.append(_pd_problem_note(self.rel(name), problem, self.budget))
                    return name, ""
                self.add("build-file", name, "maven" if name == "pom.xml" else "gradle")
                return name, _pd_strip_xml_comments(text) if name == "pom.xml" else text
        return None, ""

    def classify_java(self, build_file: str, build_text: str) -> str | None:
        """Java 组合证据（D-19 / D-20）。源码探测按 ``src/main/java`` 首个命中即停（包路径通常深于通用遍历深度）。

        构建文件 + ``src/main/java/**/*.java`` + 任一服务入口证据 → ``java-service``；无服务入口证据 → ``java-library``；
        服务入口证据只有四类（D-101 ①）：Spring Boot 打包插件、可部署 starter 依赖（``_pd_spring_boot_build_evidence``）、
        ``src/main/resources/application*`` 配置、``*Application.java`` 主类（读内容确认含 ``@SpringBootApplication`` 或
        ``public static void main(``，D-102 / F-101；同名 POJO、超出单文件预算或不可读的候选不算证据，继续搜索）；
        parent / test scope 不算。
        无源码 → None（模块 unknown）。``src``、``src/main``、``src/main/java`` 任一级是符号链接、嵌套仓或忽略目录即视为无源码；
        ``src/main/java`` 内部只排除嵌套仓与符号链接，不套用按名忽略列表（D-24③）。
        """
        outcome = _PdProbeOutcome()
        source_root = _pd_safe_subdir(
            self.module.path, "src/main/java", self.budget, prune=self.module.prune, outcome=outcome
        )
        first_java = None
        if source_root is not None:
            first_java = _pd_find_first(
                source_root, lambda name: name.endswith(".java"), self.budget, ignore_names=False, outcome=outcome
            )
        if first_java is None:
            if self.budget.exhausted():
                self.unknowns.append(
                    f"{self.rel(build_file)} present; src/main/java probe interrupted: detection budget exhausted"
                )
            elif outcome.failed:
                self.unknowns.append(
                    f"{self.rel(build_file)} present; src/main/java could not be fully read, Java source undetermined"
                )
            else:
                self.unknowns.append(f"{self.rel(build_file)} present but no src/main/java/**/*.java")
                self._java_probe_clean = not outcome.blocked
            return None
        self.java_files = [f"src/main/java/{first_java}"]
        self.add("java-source", self.java_files[0], "src/main/java/**/*.java")
        # D-101 ①（F-019）：只认打包插件 / 可部署 starter；parent、test scope、非 starter 依赖不算（见模块级说明）。
        boot_plugin, boot_starter = _pd_spring_boot_build_evidence(build_file, build_text)
        spring = bool(boot_plugin or boot_starter)
        app_configs = sorted(local for local in self.files if _PD_APPLICATION_CONFIG.match(local))
        if boot_starter:
            self.add("dependency", build_file, "spring-boot")
        if boot_plugin:
            self.add("build-plugin", build_file, boot_plugin)
        if spring:
            self.profile.frameworks = sorted(set(self.profile.frameworks) | {"spring-boot"})
        for local in app_configs:
            self.add("application-config", local, "src/main/resources/application*")
        if spring or app_configs:
            return "java-service"
        # 只有前两类证据都缺失时才搜 *Application.java，避免大模块全量遍历耗尽预算。
        # 独立的 outcome（R4-W-5 / D-73 ②）：首个 .java 命中即停，未必触达不可读目录；本次是全量搜索，须单独记录是否看全。
        app_outcome = _PdProbeOutcome()
        app_class = _pd_find_first(
            source_root,
            lambda name: name.endswith("Application.java"),
            self.budget,
            ignore_names=False,
            outcome=app_outcome,
            accept=self._is_java_entry_class,
        )
        if app_class is not None:
            self.add("application-class", f"src/main/java/{app_class}", "*Application.java")
            return "java-service"
        if self.budget.exhausted():
            return None  # *Application.java 搜索未完成，不能断言「无服务入口」
        if app_outcome.failed:
            # 子目录不可读 / 安全深度截断：服务入口可能就在未看到的部分，service 与 library 无法区分（D-23：不得 confirmed）。
            # blocked（符号链接 / 嵌套仓）按 D-24③ 不属于本模块源码，不影响结论。
            self.unknowns.append(
                f"{self.rel(build_file)} present; *Application.java probe could not fully read src/main/java, "
                "java-service vs java-library undetermined"
            )
            return None
        return "java-library"

    def _is_java_entry_class(self, path: Path) -> bool:
        """F-101：``*Application.java`` 候选须含服务入口（``_PD_JAVA_ENTRY``，去注释后）；读不出（超单文件预算 /
        不可读 / 预算耗尽）按非证据处理——预算耗尽由调用方的探针 outcome 另行记为未完成。"""
        text, _problem = _pd_read_text(path, self.budget)
        if text is None:
            return False
        return _PD_JAVA_ENTRY.search(_pd_strip_gradle_comments(text)) is not None

    def probe_no_source_files(self) -> bool:
        """D-25 附属资格的有界探针：模块 ``src/`` 下（排除 ``src/test``、``src/<set>/resources``）是否确认没有任何源码文件。

        任意非点号开头的普通文件都算源码（Kotlin / Scala / Groovy / TS 等一视同仁）；``src`` 不存在即确认没有。
        不可读、预算中断、深度截断、符号链接或嵌套仓都视为「未确认」，返回 False（失败关闭，D-23）。
        """
        outcome = _PdProbeOutcome()
        src = _pd_safe_subdir(self.module.path, "src", self.budget, prune=self.module.prune, outcome=outcome)
        if src is None:
            if self.budget.exhausted() or not outcome.clean():
                self.unknowns.append("src/ could not be fully probed; source presence undetermined")
                return False
            return True
        skip: set[str] = {"test"}
        entries = _pd_list_dir(src, self.budget, outcome=outcome) or []
        for entry in entries:
            if _pd_entry_kind(entry) == "dir":
                skip.add(f"{entry.name}/resources")
        found = _pd_find_first(
            src,
            lambda name: not name.startswith("."),
            self.budget,
            ignore_names=False,
            skip_rels=frozenset(skip),
            outcome=outcome,
        )
        if found is not None:
            self.unknowns.append(f"source files present under src/ (e.g. {self.rel('src/' + found)}); stack not recognized")
            return False
        if self.budget.exhausted() or not outcome.clean():
            self.unknowns.append("src/ could not be fully probed; source presence undetermined")
            return False
        return True

    # -- 其他字段 --------------------------------------------------------
    def detect_languages(self) -> list[str]:
        languages: set[str] = set()
        for local in self.files:
            name = local.rsplit("/", 1)[-1]
            if "/" not in local and (name.startswith(".") or ".config." in name):
                continue
            if name.endswith(".gradle.kts"):
                continue
            suffix = os.path.splitext(name)[1]
            if suffix in _PD_LANGUAGE_BY_SUFFIX:
                languages.add(_PD_LANGUAGE_BY_SUFFIX[suffix])
        if "tsconfig.json" in self.files:
            languages.add("typescript")
        if self.java_files:
            languages.add("java")
        return sorted(languages)

    def is_docs_only(self, has_manifest: bool) -> bool:
        if has_manifest:
            return False
        files = self.module.tree.files
        manifests = sorted(local for local in files if local.rsplit("/", 1)[-1] in _PD_MANIFEST_NAMES)
        if manifests:
            if self.module.id == ".":
                self.unknowns.append("undeclared nested manifests: " + ", ".join(manifests[:5]))
            return False
        sources = [local for local in files if os.path.splitext(local)[1] in _PD_SOURCE_SUFFIXES]
        if sources:
            return False
        docs = [local for local in files if os.path.splitext(local)[1].lower() in _PD_DOC_SUFFIXES]
        if not docs or len(docs) * 2 < len(files):
            return False
        self.add("docs", "", f"{len(docs)}/{len(files)} files are documentation")
        return True

    def fill_package_manager(self, package: dict[str, Any] | None, build_file: str | None) -> None:
        profile = self.profile
        if package is not None:
            declared = package.get("packageManager")
            if isinstance(declared, str) and declared.strip():
                profile.packageManager = declared.split("@", 1)[0].strip() or None
                self.add("package-manager", "package.json", f"packageManager: {declared}")
                return
            for lockfile, manager in _PD_LOCKFILES:
                if self.has_file(lockfile):
                    profile.packageManager = manager
                    self.add("package-manager", lockfile, manager)
                    return
                if self.module.id != "." and lockfile in self.scan.root_files:
                    profile.packageManager = manager
                    self.evidence.append(DetectionEvidence("package-manager", lockfile, manager))
                    return
            return
        if build_file == "pom.xml":
            profile.packageManager = "maven"
        elif build_file:
            profile.packageManager = "gradle"

    def fill_test_runner(self, package: dict[str, Any] | None, build_text: str) -> None:
        profile = self.profile
        if package is not None:
            all_deps = {**_pd_dict(package.get("dependencies")), **_pd_dict(package.get("devDependencies"))}
            root_names = [local for local in self.files if "/" not in local]
            for runner, config_prefix in (("vitest", "vitest.config."), ("jest", "jest.config."), ("mocha", ".mocharc")):
                configs = sorted(name for name in root_names if name.startswith(config_prefix))
                if runner in all_deps or configs or (runner == "jest" and "jest" in package):
                    profile.testRunner = runner
                    if runner in all_deps:
                        self.add("test-runner", "package.json", runner)
                    for name in configs:
                        self.add("test-runner", name, runner)
                    return
        if build_text:
            has_junit5 = "junit-jupiter" in build_text or "org.junit.jupiter" in build_text
            has_junit4 = bool(
                re.search(r"<artifactId>\s*junit\s*</artifactId>", build_text) or "junit:junit" in build_text
            )
            if has_junit5:
                profile.testRunner = "junit5"
                if has_junit4:
                    self.unknowns.append("both junit4 and junit5 declared; testRunner reported as junit5")
                return
            if has_junit4:
                profile.testRunner = "junit4"
                return
        for name in ("pyproject.toml", "requirements.txt"):
            if self.has_file(name):
                text, _ = _pd_read_text(self.module.path / name, self.budget)
                if text and "pytest" in text:
                    profile.testRunner = "pytest"
                    return
        if self.has_file("pytest.ini") or self.has_file("conftest.py"):
            profile.testRunner = "pytest"

    def fill_test_layout(self) -> None:
        layouts: set[str] = set()
        if "src/test/java" in self.dirs:
            layouts.add("src/test/java")
        for local in self.files:
            name = local.rsplit("/", 1)[-1]
            match = re.match(r"^.+\.(test|spec)\.(tsx?|jsx?|mjs|cjs|vue)$", name)
            if match:
                prefix = "src/**/" if local.startswith("src/") else "**/"
                layouts.add(f"{prefix}*.{match.group(1)}.{match.group(2)}")
            if "/__tests__/" in f"/{local}":
                layouts.add("**/__tests__/**")
        for directory in ("test", "tests"):
            if directory in self.dirs:
                layouts.add(f"{directory}/")
        self.profile.testLayout = sorted(layouts)

    def fill_commands(self, package: dict[str, Any] | None, build_file: str | None) -> None:
        profile = self.profile
        commands: dict[str, dict[str, str] | None] = {key: None for key in PROJECT_COMMAND_KEYS}
        if package is not None:
            scripts = _pd_dict(package.get("scripts"))
            for key, names in _PD_SCRIPT_KEYS.items():
                for name in names:
                    if name in scripts:
                        commands[key] = {"source": f"{self.rel('package.json')}#scripts.{name}"}
                        break
        elif build_file == "pom.xml":
            commands["build"] = {"source": f"{self.rel('pom.xml')}#lifecycle.package"}
            commands["test"] = {"source": f"{self.rel('pom.xml')}#lifecycle.test"}
        elif build_file:
            commands["build"] = {"source": f"{self.rel(build_file)}#task.build"}
            commands["test"] = {"source": f"{self.rel(build_file)}#task.test"}
        profile.commands = commands
        for key in PROJECT_COMMAND_KEYS:
            if commands[key] is None:
                self.unknowns.append(f"{key} command not declared")

    def fill_environments(self, package: dict[str, Any] | None) -> None:
        profile = self.profile
        environments: list[str] = []
        if profile.classification == "frontend-app":
            environments.append("browser")
            deps = _pd_dict(package.get("dependencies")) if package else {}
            if any(name in deps for name in _PD_SSR_PACKAGES):
                environments.append("node")
        elif profile.classification == "node-service":
            environments.append("node")
        elif profile.classification in _PD_JAVA_CLASSIFICATIONS:
            environments.append("jvm")
        elif profile.classification == "library":
            if profile.componentLibrary:
                environments.append("browser")
            elif package and "node" in _pd_dict(package.get("engines")):
                environments.append("node")
        elif profile.classification == "tooling" and package and "bin" in package:
            environments.append("node")
        profile.executionEnvironments = environments

    def fill_contract_and_publish(self, package: dict[str, Any] | None, build_text: str) -> None:
        profile = self.profile
        roles: set[str] = set()
        classification = profile.classification
        if classification == "frontend-app":
            deps = _pd_dict(package.get("dependencies")) if package else {}
            clients = [name for name in _PD_HTTP_CLIENTS if name in deps]
            if clients:
                roles.add("http-consumer")
                for name in clients:
                    self.add("dependency", "package.json", f"dependencies.{name}")
            profile.publishForm = "app-deploy"
        elif classification == "node-service":
            roles.add("http-provider")
            profile.publishForm = "app-deploy"
        elif classification == "java-service":
            if re.search(r"spring-boot-starter-web(flux)?\b|spring-webmvc", build_text):
                roles.add("http-provider")
            profile.publishForm = "app-deploy"
        elif classification == "java-library":
            # 设计 §4.7.1.2：同仓 api / domain 模块的构建文件无法确定性区分是否对外发布，不由识别器推断
            profile.publishForm = "unknown"
        elif classification in ("library", "tooling"):
            publishable = bool(package) and isinstance(package.get("name"), str) and package.get("private") is not True
            if publishable:
                roles.add("npm-public-package")
                profile.publishForm = "npm-package"
            else:
                profile.publishForm = "none"
        profile.contractRoles = sorted(roles)


def _pd_aggregate(modules: list[ModuleProfile], exhausted: bool) -> tuple[str, str]:
    """项目聚合（设计 §4.7.1.2，按序取第一个命中项）：

    1. 超预算 → unknown / budget-exhausted；空列表 → unknown / unknown；
    2. status 取各模块最差（ambiguous > unknown > confirmed），非 confirmed → unknown；
    3. 去掉 docs-only 后 ⊆ {java-service, java-library} 且至少一个 java-service → java-service；
    4. 去掉 docs-only 后全部为 java-library → java-library；
    5. 其余：java-service / java-library 按同一栈计，≥ 2 种 → mixed，1 种 → 该分类，空 → docs-only。
    """
    if exhausted:
        return "unknown", "budget-exhausted"
    if not modules:
        return "unknown", "unknown"  # 防线：正常情况下 detect 已保证 modules 非空
    status = "confirmed"
    for module in modules:
        if _PD_STATUS_SEVERITY[module.status] > _PD_STATUS_SEVERITY[status]:
            status = module.status
    if status != "confirmed":
        return "unknown", status
    kinds = {module.classification for module in modules if module.classification != "docs-only"}
    if kinds and kinds <= _PD_JAVA_CLASSIFICATIONS:
        return ("java-service" if "java-service" in kinds else "java-library"), status
    stacks = {"java" if kind in _PD_JAVA_CLASSIFICATIONS else kind for kind in kinds}
    if len(stacks) >= 2:
        return "mixed", status
    if len(stacks) == 1:
        return next(iter(kinds)), status
    return "docs-only", status


_PD_JAVA_BUILD_FILES = frozenset({"pom.xml", "build.gradle", "build.gradle.kts"})
_PD_JAVA_DECLARATION_SOURCES = ("pom.xml", "settings.gradle", "settings.gradle.kts")
_PD_AUXILIARY_NOTE = (
    "Java reactor member with a build file and no source files under src/ (test and resources excluded); "
    "auxiliary module, excluded from project aggregation (D-25)"
)


def _pd_auxiliary_java_members(modules: list[ModuleProfile], sourceless: set[str]) -> set[str]:
    """D-25：Java reactor 内无源码的成员模块（附属模块）不参与项目取最差聚合。

    ``sourceless``：分类阶段经有界探针**确认**没有任何源码文件的模块 id（探测失败 / 预算中断 / 有其他语言源码都不在其中）。
    另需证据信号：由 Maven / Gradle 声明引入（``workspace-declaration`` 来自 pom.xml / settings.gradle(.kts)）、
    有 ``build-file``、无 ``java-source``、无 ``manifest``（package.json），且 ``status=unknown``；
    前提是同仓另有 ``confirmed`` 的 java-service / java-library 模块。单模块仓或全仓无源码时返回空集（仍按 D-20）。
    """
    if not any(module.status == "confirmed" and module.classification in _PD_JAVA_CLASSIFICATIONS for module in modules):
        return set()
    auxiliary: set[str] = set()
    for module in modules:
        if module.id not in sourceless or module.status != "unknown" or module.classification != "unknown":
            continue
        signals = {item.signal for item in module.evidence}
        if "java-source" in signals or "manifest" in signals:
            continue
        if not any(
            item.signal == "build-file" and item.path.rsplit("/", 1)[-1] in _PD_JAVA_BUILD_FILES
            for item in module.evidence
        ):
            continue
        if any(
            item.signal == "workspace-declaration" and item.path.rsplit("/", 1)[-1] in _PD_JAVA_DECLARATION_SOURCES
            for item in module.evidence
        ):
            auxiliary.add(module.id)
    return auxiliary


def _pd_keep_aggregator(profile: ModuleProfile) -> bool:
    """声明了成员的目录（根或嵌套 Maven 聚合器）只在自身有运行时 / 源码证据时保留为模块；否则由成员代表。

    tooling 按证据而非分类保留：只有 package.json 声明了 ``bin``（真正的根 CLI）才保留。
    """
    if profile.status == "ambiguous":
        return True
    if profile.status != "confirmed":
        return False
    if profile.classification == "tooling":
        return any(item.signal == "package-field" and item.detail == "bin" for item in profile.evidence)
    return profile.classification in _PD_AGGREGATOR_KEEP_CLASSIFICATIONS


# ---------------------------------------------------------------------------
# ProjectDetector / ProfileCache
# ---------------------------------------------------------------------------


class ProjectDetector:
    """只读识别器。``detect`` 与 ``fingerprint`` 都不写任何文件。"""

    def __init__(self, budget: DetectionBudget | None = None, detectorVersion: str = PROJECT_DETECTOR_VERSION):
        self.budget = budget or DetectionBudget()
        self.detectorVersion = detectorVersion

    @staticmethod
    def _resolve_root(root: Path | str) -> Path:
        path = Path(root).expanduser()
        try:
            resolved = path.resolve()
        except OSError as error:
            raise DevkitFlowError(f"cannot read project root: {path}", error_code=PROJECT_DETECTION_FAILED) from error
        if not resolved.is_dir() or not os.access(resolved, os.R_OK | os.X_OK):
            raise DevkitFlowError(f"cannot read project root: {path}", error_code=PROJECT_DETECTION_FAILED)
        return resolved

    def fingerprint(self, root: Path | str) -> str:
        """完整口径指纹：与 ``detect(root).fingerprint`` 同一次扫描（含分类相关的结构签名）。"""
        return self.detect(root).fingerprint

    def detect(self, root: Path | str) -> ProjectProfile:
        resolved = self._resolve_root(root)
        budget = self.budget.fresh()
        scan = _pd_scan(resolved, budget)
        modules: list[ModuleProfile] = []
        dropped_root: ModuleProfile | None = None
        sourceless: set[str] = set()
        for module_scan in scan.modules:
            if budget.exhausted() or not module_scan.scanned:
                skipped = ModuleProfile(id=module_scan.id)
                skipped.unknowns = ["detection budget exhausted before classification"]
                if module_scan.id not in scan.aggregators:
                    modules.append(skipped)
                continue
            classifier = _PdModuleClassifier(scan, module_scan)
            profile = classifier.classify()
            if classifier.sourceless:
                sourceless.add(module_scan.id)
            if module_scan.id in scan.aggregators and not _pd_keep_aggregator(profile):
                if module_scan.id == ".":
                    dropped_root = profile
                continue
            modules.append(profile)
        if not modules:
            # schema modules.minItems=1：没有模块可保留时以根 `.` 作 unknown 占位（D-20 / D-23 失败关闭）
            placeholder = dropped_root or ModuleProfile(id=".")
            placeholder.status = "unknown"
            placeholder.classification = "unknown"
            placeholder.unknowns.append("no module could be retained; root kept as unknown placeholder")
            modules.append(placeholder)
        inputs, hashed = _pd_fingerprint_inputs(scan)
        exhausted = budget.exhausted()
        if scan.notes:
            for module in modules:
                module.unknowns = list(scan.notes) + module.unknowns
        if exhausted:
            for module in modules:
                if module.status == "confirmed":
                    module.status = "unknown"  # 证据不完整：模块 kind 不可用于绑定（D-22③）
                module.unknowns.append("detection budget exhausted; results are partial")
        auxiliary = set() if exhausted else _pd_auxiliary_java_members(modules, sourceless)
        for module in modules:
            if module.id in auxiliary:
                module.unknowns.append(_PD_AUXILIARY_NOTE)
        classification, status = _pd_aggregate(
            [module for module in modules if module.id not in auxiliary], exhausted
        )
        payload = json.dumps(
            # 附属模块集合入指纹：附属资格取决于 src/ 下是否有源码文件，而这些文件不一定进入证据路径
            [self.detectorVersion, hashed, _pd_structure_signature(modules), exhausted, sorted(auxiliary)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return ProjectProfile(
            root=str(resolved),
            classification=classification,
            status=status,
            fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            detectedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            budget=BudgetUsage(budget.entriesScanned, budget.elapsed_ms(), exhausted),
            modules=modules,
            fingerprintInputs=inputs,
            detectorVersion=self.detectorVersion,
        )


_PD_CACHE_MODULE_LIST_KEYS = (
    "languages",
    "frameworks",
    "executionEnvironments",
    "testLayout",
    "contractRoles",
    "evidence",
    "unknowns",
)


def _pd_cache_shape_ok(data: dict[str, Any]) -> bool:
    """缓存文件的结构校验：容器类型与枚举不合法即视为无缓存（接口 §9.3 不抛异常）；耗尽画像不作为缓存结果（D-22③）。"""
    for key in ("root", "classification", "status", "fingerprint", "detectedAt"):
        if not isinstance(data.get(key), str):
            return False
    if data["classification"] not in PROJECT_CLASSIFICATIONS or data["status"] not in PROJECT_STATUSES:
        return False
    if data["status"] == "budget-exhausted":
        return False
    budget = data.get("budget")
    if not isinstance(budget, dict) or budget.get("exhausted") is not False:
        return False
    modules = data.get("modules")
    if not isinstance(modules, list) or not modules:  # schema modules.minItems=1
        return False
    for module in modules:
        if not isinstance(module, dict):
            return False
        commands = module.get("commands")
        if commands is not None:
            if not isinstance(commands, dict):
                return False
            if any(value is not None and not isinstance(value, dict) for value in commands.values()):
                return False
        for key in _PD_CACHE_MODULE_LIST_KEYS:
            value = module.get(key)
            if value is not None and not isinstance(value, list):
                return False
        if any(not isinstance(item, dict) for item in module.get("evidence") or []):
            return False
    inputs = data.get("fingerprintInputs")
    if inputs is not None and (not isinstance(inputs, list) or any(not isinstance(item, dict) for item in inputs)):
        return False
    return True


class ProfileCache:
    """画像缓存 ``~/.config/devkit/cache/project-profiles/<sha256(realpath)[:16]>.json``（接口 §8.2）。

    ``read`` / ``quickState`` 对缺失、不可读、非 JSON、版本不符、结构不合法、耗尽画像一律按「无缓存」处理，
    从不抛出（接口 §9.3）。
    """

    def __init__(self, cache_dir: Path | None = None, detectorVersion: str = PROJECT_DETECTOR_VERSION):
        self.cache_dir = cache_dir if cache_dir is not None else Path.home() / PROJECT_PROFILE_CACHE_RELATIVE_PATH
        self.detectorVersion = detectorVersion

    @staticmethod
    def _realpath(root: Path | str) -> str:
        return os.path.realpath(os.path.expanduser(str(root)))

    def pathFor(self, root: Path | str) -> Path:
        digest = hashlib.sha256(self._realpath(root).encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.json"

    def _load_raw(self, root: Path | str) -> dict[str, Any] | None:
        try:
            data = json.loads(self.pathFor(root).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("schemaVersion") != PROJECT_PROFILE_SCHEMA_VERSION:
            return None
        if data.get("detectorVersion") != self.detectorVersion:
            return None
        if data.get("root") != self._realpath(root):
            return None
        if not _pd_cache_shape_ok(data):
            return None
        return data

    def read(self, root: Path | str) -> ProjectProfile | None:
        data = self._load_raw(root)
        if data is None:
            return None
        try:
            return ProjectProfile.fromJson(data)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

    def write(self, profile: ProjectProfile) -> Path:
        """写入画像（含 ``fingerprintInputs``）。耗尽画像即使被写入，``read`` / ``quickState`` / ``isFresh`` 也不采用。"""
        target = self.pathFor(profile.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(profile.toJson(include_fingerprint_inputs=True), ensure_ascii=False, indent=2)
        atomic_write_text(target, content + "\n")
        return target

    def isFresh(self, profile: ProjectProfile, fingerprint: str) -> bool:
        """指纹一致且不是耗尽画像（D-22③：部分结果不能作为已完成的识别结果复用）。"""
        if profile.budget.exhausted or profile.status == "budget-exhausted":
            return False
        return (
            profile.schemaVersion == PROJECT_PROFILE_SCHEMA_VERSION
            and profile.detectorVersion == self.detectorVersion
            and profile.fingerprint == fingerprint
        )

    def quickState(self, root: Path | str) -> str:
        """快速口径：只 stat 缓存记录的 ``fingerprintInputs``，不遍历、不读内容。"""
        data = self._load_raw(root)
        if data is None:
            return "not-detected"
        inputs = data.get("fingerprintInputs")
        if not isinstance(inputs, list):
            return "not-detected"
        base = Path(self._realpath(root))
        for item in inputs:
            if not isinstance(item, dict):
                return "not-detected"
            try:
                stat_result = (base / str(item["path"])).stat()
            except (OSError, KeyError):
                return "stale"
            if stat_result.st_size != item.get("size") or stat_result.st_mtime_ns != item.get("mtimeNs"):
                return "stale"
        return "cached"


# ---------------------------------------------------------------------------
# 命令（ProjectCommands.command_project_detect，接口 §2.2）
# ---------------------------------------------------------------------------


def _pd_text_summary(profile: ProjectProfile) -> str:
    lines = [f"project: {profile.classification} ({profile.status}) at {profile.root}"]
    for module in profile.modules:
        lines.append(f"  - {module.id}: {module.classification} ({module.status})")
    if profile.budget.exhausted:
        lines.append("  detection budget exhausted; results are partial")
    return "\n".join(lines)


def command_project_detect(args: argparse.Namespace) -> int:
    """识别一次；缓存指纹一致时返回缓存画像（保留其 ``detectedAt``）。``--write-cache`` 不写耗尽画像（D-22③）。"""
    root = Path(getattr(args, "path", None) or ".")
    cache = ProfileCache()
    profile = ProjectDetector().detect(root)
    if not getattr(args, "no_cache", False):
        cached = cache.read(root)
        if cached is not None and cache.isFresh(cached, profile.fingerprint):
            profile = cached
    if getattr(args, "write_cache", False) and not profile.budget.exhausted:
        cache.write(profile)
    output_format = getattr(args, "format", "text")
    if output_format == "json":
        emit(profile.toJson(), "json")
    else:
        emit({"message": _pd_text_summary(profile)}, "text")
    return 0
