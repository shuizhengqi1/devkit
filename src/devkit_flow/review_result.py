"""016 T-11：审查机器结果文件与 `devkit-flow review-result verify`（R-01～R-03；BC-03/BC-04）。

设计 §4.7.7.1、§4.7.8.2（E1～E6）；接口文档 §2.6、§4.3、§7；类图 review_result 相关类。

- ``ReviewResultValidator``：手写结构校验（必填键、类型、闭合枚举、E2）。运行时不读取
  ``schemas/devkit-flow/review-result.schema.json``；该文件是契约与测试基准，
  ``scripts/test_review_result_schema_parity.py`` 断言 ``REQUIRED_KEYS`` / ``ENUM_SETS`` 与之一致。
  不复用 ``runtime_contracts.validate_instance``（只认 contract-set 根、强制 ``schemaVersion == "1.0"``）。
- ``ReviewVerifier``：``checkEvidence``（发现必填键、E1/E3～E5、not-applicable 原因）、``checkScore``
  （重算评分、去重、扣分对账、E6）、``checkMarkdown``（摘要行、结论行、发布前遗留项）。
- 四类问题一次全部收集进 ``problems[]``；有任一项即 ``verdict=inconsistent``、exit 1，输出仍为 verify 对象。
  统一错误通道只用于无法开始校验：feature 不存在、报告文件不存在 / 不可读 / 不是普通文件
  （``REVIEW_RESULT_REPORT_NOT_FOUND``）。报告存在而结果文件确实不存在、或结果文件 ``schemaVersion`` 为大于 1 的整数
  （接口 §9.3）→ ``legacy-unverified``、exit 0，并以 ``reason``（no-result-file / unsupported-schema-version）区分
  原因（D-64 ⑤）；结果路径不是普通文件（目录 / FIFO / 设备 / 指向它们的链接 / 坏链接）/ 不可读 / 无法解码 / 根不是对象
  → 只报 ``SCHEMA_INVALID``（D-63 ⑤、D-64 ②④）。
- 「## 发布前遗留项」条目以开头的 ``[<finding-id>]`` 标记关联发现（D-64 ①）；Markdown 扫描识别 ``` / ~~~ 围栏（D-64 ③）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devkit_flow.errors import DevkitFlowError
from devkit_flow.parse_util import find_features, select_feature
from devkit_flow.textio import emit

REVIEW_RESULT_SCHEMA_VERSION = 1
REVIEW_RESULT_THRESHOLD = 90
REVIEW_RESULT_DEFAULT_POINTS = 10

REVIEW_RESULT_SCHEMA_INVALID = "REVIEW_RESULT_SCHEMA_INVALID"
REVIEW_RESULT_MISSING_EVIDENCE = "REVIEW_RESULT_MISSING_EVIDENCE"
REVIEW_RESULT_INCONSISTENT = "REVIEW_RESULT_INCONSISTENT"
REVIEW_RESULT_REPORT_MISMATCH = "REVIEW_RESULT_REPORT_MISMATCH"
REVIEW_RESULT_REPORT_NOT_FOUND = "REVIEW_RESULT_REPORT_NOT_FOUND"
# verify 对象 problems[] 内允许出现的错误码（接口 §2.6）；REPORT_NOT_FOUND 只走统一错误通道。
REVIEW_RESULT_PROBLEM_CODES = (
    REVIEW_RESULT_SCHEMA_INVALID,
    REVIEW_RESULT_MISSING_EVIDENCE,
    REVIEW_RESULT_INCONSISTENT,
    REVIEW_RESULT_REPORT_MISMATCH,
)

API_VERSION_POLICY_RULE_PREFIX = "specrules:02_design/api_version_policy.md"
REVIEW_RESULT_RULE_ID_RE = re.compile(r"^(tpl:[^#\s]+#\S.*|specrules:[^#\s]+#\S.*|devkit:\S.*)$")
REVIEW_RESULT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# 发现必填键中缺失时按接口 §4.3 报 REVIEW_RESULT_MISSING_EVIDENCE 的部分（其余必填键缺失报 SCHEMA_INVALID）。
FINDING_EVIDENCE_KEYS = ("ruleId", "scope", "source", "applicability", "evidence", "class", "fix")
API_IMPACT_EVIDENCE_ITEMS = ("artifactRepo", "consumers", "introducedBy")

# 契约常量：与 schema 文件的 required / enum 逐项对应（INV-16，parity 测试守卫）。
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "root": (
        "schemaVersion",
        "report",
        "reviewer",
        "round",
        "previousResultSha256",
        "template",
        "rulesVersion",
        "threshold",
        "score",
        "openBlocking",
        "verdict",
        "deductions",
        "findings",
    ),
    "deduction": ("ruleId", "scope", "points", "findingId"),
    "finding": ("id", "ruleId", "scope", "source", "applicability", "class", "status", "evidence", "fix"),
    "source": ("kind", "ref", "version"),
    "apiImpactEvidence": API_IMPACT_EVIDENCE_ITEMS,
    "apiImpactEvidenceItem": ("checked", "ref"),
    "evidence": ("location",),
}
ENUM_SETS: dict[str, tuple[str, ...]] = {
    "verdict": ("pass", "fail"),
    "source.kind": ("template", "specrules", "devkit"),
    "applicability": ("applicable", "not-applicable"),
    "class": ("blocking", "release-todo", "advisory", "not-applicable"),
    "status": ("open", "closed"),
    "apiImpactClass": (
        "no-contract-change",
        "not-maven-artifact",
        "unreleased-unconsumed",
        "released-or-consumed",
        "breaking",
        "evidence-unknown",
    ),
}
VERIFY_VERDICTS = ("pass", "fail", "inconsistent", "legacy-unverified")
# legacy-unverified 的原因（D-64 ⑤；T-23 据此按「需人工处理」而非 pass 对待）。
LEGACY_REASON_NO_RESULT_FILE = "no-result-file"
LEGACY_REASON_UNSUPPORTED_SCHEMA_VERSION = "unsupported-schema-version"
LEGACY_REASONS = (LEGACY_REASON_NO_RESULT_FILE, LEGACY_REASON_UNSUPPORTED_SCHEMA_VERSION)

# 扣分表（后端 specrules 02_design/review_classification.md §4）：按序匹配 ruleId，命中取该行，否则缺省值。
# 当前无差异化扣分（全部 10）；保留匹配口径，规则仓调整时同步此表。
POINTS_TABLE: tuple[tuple[str, int], ...] = (
    (r"^tpl:[^#]+#4\.1$", 10),
    (r"^tpl:[^#]+#6\.[123]$", 10),
    (r"^specrules:constitution\.md#", 10),
    (r"^specrules:02_design/api_version_policy\.md#", 10),
)

# E6：apiImpactClass → 必须的 class（evidence-unknown 只约束 status=open 的发现，D-18）。
API_IMPACT_REQUIRED_CLASS = {
    "no-contract-change": "not-applicable",
    "not-maven-artifact": "not-applicable",
    "unreleased-unconsumed": "advisory",
}
API_IMPACT_EXCERPT_CLASSES = ("no-contract-change", "not-maven-artifact")
# 规则 §2.2 适用的来源：这些来源的发现只能是 blocking / advisory / not-applicable，不得为 release-todo。
RULE_SOURCE_KINDS = ("specrules", "devkit")

REPORT_ALIASES = {
    "design": "review-design.md",
    "apidoc": "review-apidoc.md",
    "arch": "review-arch-comprehensive.md",
}
ARCH_TASK_REPORT_RE = re.compile(r"^arch:(T-[0-9A-Za-z][0-9A-Za-z-]*)$")
RELEASE_TODO_HEADING = "## 发布前遗留项"
RESULT_FILE_LINE_RE = re.compile(r"结果文件\s*[:：]\s*`?([^\s`]+)`?\s+sha256\s*=\s*`?([0-9A-Fa-f]{64})`?")
CONCLUSION_LINE_RE = re.compile(r"结论\s*[:：]\s*[*`]*\s*([A-Za-z-]+)")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)?\s*$")
EMPTY_LIST_ITEM_RE = re.compile(r"^(?:无|暂无|N/?A|none|-)\s*[。.]?$", re.IGNORECASE)


@dataclass
class VerifyProblem:
    errorCode: str
    message: str
    findingId: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"error_code": self.errorCode, "message": self.message}
        if self.findingId is not None:
            data["finding_id"] = self.findingId
        return data


@dataclass
class ApiImpactEvidence:
    artifactRepo: dict[str, Any]
    consumers: dict[str, Any]
    introducedBy: dict[str, Any]

    def items(self) -> list[tuple[str, dict[str, Any]]]:
        return [("artifactRepo", self.artifactRepo), ("consumers", self.consumers), ("introducedBy", self.introducedBy)]


@dataclass
class ReviewFinding:
    id: str
    ruleId: Any
    scope: Any
    source: Any
    applicability: Any
    classification: Any
    status: Any
    evidence: Any
    fix: Any
    apiImpactClass: Any = None
    apiImpactEvidence: ApiImpactEvidence | None = None
    present: frozenset[str] = frozenset()
    hasApiImpactEvidence: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def identity(self) -> tuple[str, str]:
        return (str(self.ruleId), str(self.scope))

    @property
    def excerpt(self) -> str:
        if isinstance(self.evidence, dict) and isinstance(self.evidence.get("excerpt"), str):
            return self.evidence["excerpt"].strip()
        return ""

    def is_api_policy(self) -> bool:
        return isinstance(self.ruleId, str) and self.ruleId.startswith(API_VERSION_POLICY_RULE_PREFIX)

    def is_open_blocking(self) -> bool:
        return self.classification == "blocking" and self.status == "open"


@dataclass
class Deduction:
    ruleId: Any
    scope: Any
    points: Any
    findingId: Any


@dataclass
class ReviewResult:
    schemaVersion: Any = None
    report: Any = None
    reviewer: Any = None
    round: Any = None
    previousResultSha256: Any = None
    template: Any = None
    rulesVersion: Any = None
    threshold: Any = None
    score: Any = None
    openBlocking: Any = None
    verdict: Any = None
    deductions: list[Deduction] = field(default_factory=list)
    findings: list[ReviewFinding] = field(default_factory=list)
    # 结果文件字节的 sha256（load 时由同一次读取计算，checkMarkdown 不再二次读取文件）。
    sha256: str = ""

    @staticmethod
    def from_raw(raw: Any) -> "ReviewResult":
        """防御式构造：结构不合法的条目跳过（问题已由 validator 记录），保证后续检查仍可执行。"""
        result = ReviewResult()
        if not isinstance(raw, dict):
            return result
        for key in REQUIRED_KEYS["root"]:
            if key not in ("deductions", "findings"):
                setattr(result, key, raw.get(key))
        for item in raw.get("deductions") if isinstance(raw.get("deductions"), list) else []:
            if isinstance(item, dict):
                result.deductions.append(
                    Deduction(item.get("ruleId"), item.get("scope"), item.get("points"), item.get("findingId"))
                )
        for index, item in enumerate(raw.get("findings") if isinstance(raw.get("findings"), list) else []):
            if isinstance(item, dict):
                result.findings.append(_finding_from_raw(item, index))
        return result

    @staticmethod
    def load(path: Path) -> tuple["ReviewResult", Any, list[VerifyProblem]]:
        """读取并解析结果文件；读取 / 解码失败作为 SCHEMA_INVALID 问题返回（不走统一错误通道，D-63 ⑤）。

        读取前先 ``os.stat``（跟随符号链接）确认是普通文件（D-64 ②）：目录、FIFO、设备、socket、指向它们的
        符号链接、坏链接 / 循环链接都直接报 SCHEMA_INVALID，不打开、不读取（FIFO 不会挂起）。打开时另加
        ``O_NONBLOCK`` 并 ``fstat`` 复核，防止 stat 与 open 之间被替换。
        覆盖：非 UTF-8（UnicodeDecodeError）、JSON 语法错误（JSONDecodeError）、整数字面量超出有符号 64 位范围
        （``_parse_bounded_int``，与解释器的整数位数限制无关，D-101 ②）、其他解码失败（ValueError，如 NaN / Infinity
        非标准常量）、嵌套过深（RecursionError）。
        """
        data, problem = _read_regular_file(path)
        if problem is not None:
            return ReviewResult(), None, [problem]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ReviewResult(), None, [_problem(REVIEW_RESULT_SCHEMA_INVALID, "result file is not valid UTF-8")]
        try:
            raw = json.loads(text, parse_constant=_reject_non_json_constant, parse_int=_parse_bounded_int)
        except json.JSONDecodeError as error:
            return ReviewResult(), None, [
                _problem(REVIEW_RESULT_SCHEMA_INVALID, f"result file is not valid JSON: line {error.lineno} column {error.colno}")
            ]
        except _IntegerOutOfRange:
            return ReviewResult(), None, [
                _problem(REVIEW_RESULT_SCHEMA_INVALID, "result file contains an integer outside the signed 64-bit range")
            ]
        except (ValueError, RecursionError) as error:
            return ReviewResult(), None, [_problem(REVIEW_RESULT_SCHEMA_INVALID, f"result file cannot be decoded: {type(error).__name__}")]
        result = ReviewResult.from_raw(raw)
        result.sha256 = hashlib.sha256(data).hexdigest()
        return result, raw, []


# 整数字面量上限（D-101 ②，F-002）：结果文件里的整数字段都是小值（版本、轮次、分数、扣分）。Python 3.11+（及部分 3.9 / 3.10
# 补丁版）对超过 ``sys.get_int_max_str_digits()``（默认 4300，可由 PYTHONINTMAXSTRDIGITS 调到最低 640）的整数字面量抛
# ValueError，旧解释器则照常解析，同一文件会在不同解释器上得出 SCHEMA_INVALID 与 legacy-unverified 两种结论。这里先按位数、
# 再按有符号 64 位范围自行拒绝，远低于任何解释器的位数限制，所有受支持版本同输入同结论。
_INT_LITERAL_MAX_DIGITS = 19
_INT_MIN = -(2**63)
_INT_MAX = 2**63 - 1


class _IntegerOutOfRange(ValueError):
    """JSON 整数字面量超出有符号 64 位范围（含位数超限，未转换即拒绝）。"""


def _parse_bounded_int(token: str) -> int:
    digits = token[1:] if token.startswith("-") else token
    if len(digits) > _INT_LITERAL_MAX_DIGITS:
        raise _IntegerOutOfRange(f"integer literal has {len(digits)} digits")
    value = int(token)
    if not _INT_MIN <= value <= _INT_MAX:
        raise _IntegerOutOfRange("integer literal outside signed 64-bit range")
    return value


def _reject_non_json_constant(token: str) -> Any:
    """严格 JSON（RFC 8259）：``NaN`` / ``Infinity`` / ``-Infinity`` 不是合法值，按解码失败处理。"""
    raise ValueError(f"non-standard JSON constant {token}")


class _NotRegularFile(Exception):
    """路径（跟随符号链接）不是普通文件：FIFO / 目录 / 设备等。"""


def _read_regular_bytes(path: Path) -> bytes:
    """只读取普通文件（跟随符号链接判断）：stat 预检 + O_NONBLOCK 打开 + fstat 复核，FIFO 等不阻塞（D-64 ②）。

    非普通文件抛 ``_NotRegularFile``；stat / open / read 失败抛 ``OSError``。结果文件与审查报告共用（R3 P1-N2）。
    """
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise _NotRegularFile()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _NotRegularFile()
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(fd)


def _read_regular_file(path: Path) -> tuple[bytes, VerifyProblem | None]:
    """结果文件读取：非普通文件或 stat / open 失败返回 SCHEMA_INVALID 问题（D-64 ②）。"""
    try:
        return _read_regular_bytes(path), None
    except _NotRegularFile:
        return b"", _problem(REVIEW_RESULT_SCHEMA_INVALID, "result file is not a regular file")
    except OSError as error:
        return b"", _problem(REVIEW_RESULT_SCHEMA_INVALID, f"result file unreadable: {type(error).__name__}")


def _read_report_text(report_file: Path) -> str:
    """审查报告读取：与结果文件同一普通文件口径；不存在 / 不可读 / 非普通文件 / 非 UTF-8 → 统一错误通道（R3 P1-N2）。"""
    try:
        return _read_regular_bytes(report_file).decode("utf-8")
    except _NotRegularFile:
        raise DevkitFlowError(
            f"review report is not a regular file: {report_file.name}", error_code=REVIEW_RESULT_REPORT_NOT_FOUND
        ) from None
    except (OSError, UnicodeDecodeError) as error:
        raise DevkitFlowError(
            f"review report not found or unreadable: {report_file.name} ({type(error).__name__})",
            error_code=REVIEW_RESULT_REPORT_NOT_FOUND,
        ) from None


def _problem(code: str, message: str, finding_id: Any = None) -> VerifyProblem:
    return VerifyProblem(code, message, finding_id if isinstance(finding_id, str) and finding_id else None)


def _finding_from_raw(item: dict[str, Any], index: int) -> ReviewFinding:
    raw_evidence = item.get("apiImpactEvidence")
    evidence: ApiImpactEvidence | None = None
    if isinstance(raw_evidence, dict):
        parts = [raw_evidence.get(name) if isinstance(raw_evidence.get(name), dict) else {} for name in API_IMPACT_EVIDENCE_ITEMS]
        evidence = ApiImpactEvidence(*parts)
    finding_id = item.get("id") if isinstance(item.get("id"), str) and item.get("id") else f"findings[{index}]"
    return ReviewFinding(
        id=finding_id,
        ruleId=item.get("ruleId"),
        scope=item.get("scope"),
        source=item.get("source"),
        applicability=item.get("applicability"),
        classification=item.get("class"),
        status=item.get("status"),
        evidence=item.get("evidence"),
        fix=item.get("fix"),
        apiImpactClass=item.get("apiImpactClass"),
        apiImpactEvidence=evidence,
        present=frozenset(item.keys()),
        hasApiImpactEvidence="apiImpactEvidence" in item,
        raw=item,
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _path_present(path: Path) -> bool:
    """路径存在（含目录、坏符号链接）；只有 FileNotFoundError 视为「确实不存在」。其他 stat 错误视为存在，交给 load 报问题。"""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


class ReviewResultValidator:
    """手写结构校验（必填键、类型、闭合枚举、条件必填 E2）。只报 REVIEW_RESULT_SCHEMA_INVALID。

    发现中 ``FINDING_EVIDENCE_KEYS`` 的缺失 / 空值不在此报告，留给 ``ReviewVerifier.checkEvidence``
    统一报 ``REVIEW_RESULT_MISSING_EVIDENCE``（接口 §4.3），同一缺口只报一次。
    """

    requiredKeys = REQUIRED_KEYS
    enumSets = ENUM_SETS

    def validate(self, raw: Any) -> list[VerifyProblem]:
        if not isinstance(raw, dict):
            return [_problem(REVIEW_RESULT_SCHEMA_INVALID, "result file root must be a JSON object")]
        problems: list[VerifyProblem] = []

        def bad(message: str, finding_id: Any = None) -> None:
            problems.append(_problem(REVIEW_RESULT_SCHEMA_INVALID, message, finding_id))

        for key in self.requiredKeys["root"]:
            if key not in raw:
                bad(f"missing required key: {key}")
        if "schemaVersion" in raw and (not _is_int(raw["schemaVersion"]) or raw["schemaVersion"] != REVIEW_RESULT_SCHEMA_VERSION):
            bad(f"schemaVersion must be integer {REVIEW_RESULT_SCHEMA_VERSION}")
        for key in ("report", "reviewer"):
            if key in raw and not _non_empty_str(raw[key]):
                bad(f"{key} must be a non-empty string")
        if "round" in raw and (not _is_int(raw["round"]) or raw["round"] < 1):
            bad("round must be an integer >= 1")
        previous = raw.get("previousResultSha256")
        if previous is not None and not (isinstance(previous, str) and REVIEW_RESULT_SHA256_RE.match(previous)):
            bad("previousResultSha256 must be null or a lowercase sha256 hex string")
        if "template" in raw and raw["template"] is not None and not isinstance(raw["template"], dict):
            bad("template must be an object or null")
        if "rulesVersion" in raw and not isinstance(raw["rulesVersion"], dict):
            bad("rulesVersion must be an object")
        if "threshold" in raw and (not _is_int(raw["threshold"]) or raw["threshold"] != REVIEW_RESULT_THRESHOLD):
            bad(f"threshold must be integer {REVIEW_RESULT_THRESHOLD}")
        if "score" in raw and (not _is_int(raw["score"]) or raw["score"] > 100):
            bad("score must be an integer <= 100")
        if "openBlocking" in raw and (not _is_int(raw["openBlocking"]) or raw["openBlocking"] < 0):
            bad("openBlocking must be an integer >= 0")
        if "verdict" in raw and raw["verdict"] not in self.enumSets["verdict"]:
            bad(f"verdict must be one of {list(self.enumSets['verdict'])}")
        deductions = raw.get("deductions")
        if "deductions" in raw and not isinstance(deductions, list):
            bad("deductions must be an array")
        for index, item in enumerate(deductions if isinstance(deductions, list) else []):
            self._validate_deduction(item, index, bad)
        findings = raw.get("findings")
        if "findings" in raw and not isinstance(findings, list):
            bad("findings must be an array")
        for index, item in enumerate(findings if isinstance(findings, list) else []):
            self._validate_finding(item, index, bad)
        return problems

    @staticmethod
    def missing_fields(item: dict[str, Any]) -> list[str]:
        """发现的证据类必填项缺失 / 为空（接口 §4.3 → REVIEW_RESULT_MISSING_EVIDENCE）。

        只判「缺失或空白字符串」；类型错误由 ``validate`` 报 SCHEMA_INVALID，避免同一缺口重复报告。
        """
        messages: list[str] = []
        for key in FINDING_EVIDENCE_KEYS:
            value = item.get(key)
            if key not in item:
                messages.append(f"missing required field {key}")
            elif isinstance(value, str) and not value.strip():
                messages.append(f"{key} must be non-empty")
            elif key == "evidence" and isinstance(value, dict):
                location = value.get("location")
                if "location" not in value or (isinstance(location, str) and not location.strip()):
                    messages.append("evidence.location must be non-empty")
        return messages

    def _validate_deduction(self, item: Any, index: int, bad: Any) -> None:
        where = f"deductions[{index}]"
        if not isinstance(item, dict):
            bad(f"{where} must be an object")
            return
        for key in self.requiredKeys["deduction"]:
            if key not in item:
                bad(f"{where} missing required key: {key}")
        for key in ("ruleId", "scope", "findingId"):
            if key in item and not _non_empty_str(item[key]):
                bad(f"{where}.{key} must be a non-empty string")
        if "points" in item and (not _is_int(item["points"]) or item["points"] < 1):
            bad(f"{where}.points must be an integer >= 1")

    def _validate_finding(self, item: Any, index: int, bad: Any) -> None:
        where = f"findings[{index}]"
        if not isinstance(item, dict):
            bad(f"{where} must be an object")
            return
        finding_id = item.get("id")
        for key in self.requiredKeys["finding"]:
            if key not in item and key not in FINDING_EVIDENCE_KEYS:
                bad(f"{where} missing required key: {key}", finding_id)
        if "id" in item and not _non_empty_str(finding_id):
            bad(f"{where}.id must be a non-empty string")
        rule_id = item.get("ruleId")
        if _non_empty_str(rule_id) and not REVIEW_RESULT_RULE_ID_RE.match(rule_id):
            bad(f"{where}.ruleId must match tpl:<id>#<section> | specrules:<path>#<item> | devkit:<check>", finding_id)
        elif "ruleId" in item and not isinstance(rule_id, str):
            bad(f"{where}.ruleId must be a string", finding_id)
        if "scope" in item and not isinstance(item["scope"], str):
            bad(f"{where}.scope must be a string", finding_id)
        if "fix" in item and not isinstance(item["fix"], str):
            bad(f"{where}.fix must be a string", finding_id)
        source = item.get("source")
        if isinstance(source, dict):
            for key in self.requiredKeys["source"]:
                if key not in source:
                    bad(f"{where}.source missing required key: {key}", finding_id)
            if "kind" in source and source["kind"] not in self.enumSets["source.kind"]:
                bad(f"{where}.source.kind must be one of {list(self.enumSets['source.kind'])}", finding_id)
            for key in ("ref", "version"):
                if key in source and not isinstance(source[key], str):
                    bad(f"{where}.source.{key} must be a string", finding_id)
        elif "source" in item:
            bad(f"{where}.source must be an object", finding_id)
        for key, enum_name in (("applicability", "applicability"), ("class", "class"), ("status", "status")):
            if key in item and item[key] not in self.enumSets[enum_name]:
                bad(f"{where}.{key} must be one of {list(self.enumSets[enum_name])}", finding_id)
        evidence = item.get("evidence")
        if isinstance(evidence, dict):
            if "location" in evidence and not isinstance(evidence["location"], str):
                bad(f"{where}.evidence.location must be a string", finding_id)
            if evidence.get("excerpt") is not None and not isinstance(evidence["excerpt"], str):
                bad(f"{where}.evidence.excerpt must be a string or null", finding_id)
        elif "evidence" in item:
            bad(f"{where}.evidence must be an object", finding_id)
        self._validate_api_impact(item, where, finding_id, bad)

    def _validate_api_impact(self, item: dict[str, Any], where: str, finding_id: Any, bad: Any) -> None:
        rule_id = item.get("ruleId")
        is_policy = isinstance(rule_id, str) and rule_id.startswith(API_VERSION_POLICY_RULE_PREFIX)
        if not is_policy:
            # E2：非 API 版本策略规则的发现不得出现这两个字段。
            for key in ("apiImpactClass", "apiImpactEvidence"):
                if key in item:
                    bad(f"{where}.{key} is only allowed for {API_VERSION_POLICY_RULE_PREFIX} findings (E2)", finding_id)
            return
        if "apiImpactClass" in item and item["apiImpactClass"] not in self.enumSets["apiImpactClass"]:
            bad(f"{where}.apiImpactClass must be one of {list(self.enumSets['apiImpactClass'])}", finding_id)
        if "apiImpactEvidence" not in item:
            return
        evidence = item["apiImpactEvidence"]
        if not isinstance(evidence, dict):
            bad(f"{where}.apiImpactEvidence must be an object", finding_id)
            return
        allowed = set(self.requiredKeys["apiImpactEvidence"])
        for key in self.requiredKeys["apiImpactEvidence"]:
            if key not in evidence:
                bad(f"{where}.apiImpactEvidence missing required key: {key}", finding_id)
        for key in sorted(set(evidence) - allowed):
            bad(f"{where}.apiImpactEvidence has unexpected key: {key}", finding_id)
        for key in self.requiredKeys["apiImpactEvidence"]:
            entry = evidence.get(key)
            if key not in evidence:
                continue
            if not isinstance(entry, dict):
                bad(f"{where}.apiImpactEvidence.{key} must be an object", finding_id)
                continue
            for sub in self.requiredKeys["apiImpactEvidenceItem"]:
                if sub not in entry:
                    bad(f"{where}.apiImpactEvidence.{key} missing required key: {sub}", finding_id)
            for sub in sorted(set(entry) - set(self.requiredKeys["apiImpactEvidenceItem"])):
                bad(f"{where}.apiImpactEvidence.{key} has unexpected key: {sub}", finding_id)
            if "checked" in entry and not isinstance(entry["checked"], bool):
                bad(f"{where}.apiImpactEvidence.{key}.checked must be a boolean", finding_id)
            if "ref" in entry and entry["ref"] is not None and not isinstance(entry["ref"], str):
                bad(f"{where}.apiImpactEvidence.{key}.ref must be a string or null", finding_id)


class ScoreCalculator:
    """``score = 100 − Σ points``：只计 class=blocking ∧ status=open 的发现，同一 (ruleId, scope) 只扣一次。"""

    @staticmethod
    def points_for(rule_id: str, points_table: tuple[tuple[str, int], ...] = POINTS_TABLE) -> int:
        for pattern, points in points_table:
            if re.search(pattern, rule_id):
                return points
        return REVIEW_RESULT_DEFAULT_POINTS

    def compute(self, findings: list[ReviewFinding], points_table: tuple[tuple[str, int], ...] = POINTS_TABLE) -> int:
        seen: set[tuple[str, str]] = set()
        total = 0
        for finding in findings:
            if not finding.is_open_blocking() or finding.identity in seen:
                continue
            seen.add(finding.identity)
            total += self.points_for(str(finding.ruleId), points_table)
        return 100 - total

    @staticmethod
    def open_blocking(findings: list[ReviewFinding]) -> int:
        return sum(1 for finding in findings if finding.is_open_blocking())

    @staticmethod
    def verdict(score: int, open_blocking: int, threshold: int = REVIEW_RESULT_THRESHOLD) -> str:
        return "pass" if open_blocking == 0 and score >= threshold else "fail"


@dataclass
class VerifyReport:
    status: str
    report: str
    resultFile: str | None
    verdict: str
    score: int | None = None
    openBlocking: int | None = None
    releaseTodoCount: int | None = None
    problems: list[VerifyProblem] = field(default_factory=list)
    # 仅 verdict=legacy-unverified 时输出（D-64 ⑤）：no-result-file / unsupported-schema-version。
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": self.status,
            "report": self.report,
            "result_file": self.resultFile,
            "verdict": self.verdict,
            "score": self.score,
            "open_blocking": self.openBlocking,
            "release_todo_count": self.releaseTodoCount,
            "problems": [problem.to_dict() for problem in self.problems],
        }
        if self.verdict == "legacy-unverified":
            data["reason"] = self.reason
        return data


def result_file_for_report(report_file: Path) -> Path:
    """结果文件 = 报告同名去掉 `.md` 加 `.result.json`（接口 §2.6）。"""
    name = report_file.name[:-3] if report_file.name.endswith(".md") else report_file.name
    return report_file.with_name(f"{name}.result.json")


FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HEADER_END_RE = re.compile(r"^ {0,3}##\s")
RELEASE_TODO_HEADING_RE = re.compile(r"^ {0,3}##\s+发布前遗留项\s*$")
ANY_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s")
TABLE_SEPARATOR_CELL_RE = re.compile(r":?-{2,}:?")
# 遗留项条目的发现身份（D-64 ①）：条目开头的一个或多个 `[<finding-id>]`；只有方括号内的 token 是发现 id。
LEADING_ID_MARKERS_RE = re.compile(r"^(?:\[[^\[\]]*\]\s*)+")
ID_MARKER_RE = re.compile(r"\[([^\[\]]*)\]")


def _fence_flags(lines: list[str]) -> list[bool]:
    """逐行标记是否属于代码围栏（含开合标记行），D-64 ③。

    CommonMark 口径：行首 0～3 空格后跟 ≥3 个 ``` ` ``` 或 ``~`` 开启（反引号围栏的信息串不得含反引号）；
    闭合标记须缩进 ≤3 列（tab 按 4 列制表位展开）、同字符、长度不少于开启标记、其后只有空白；未闭合的围栏延续到文末（围栏内行一律不参与扫描）。
    """
    flags: list[bool] = []
    fence: str | None = None
    for line in lines:
        if fence is None:
            match = FENCE_OPEN_RE.match(line)
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence = match.group(1)
                flags.append(True)
                continue
            flags.append(False)
            continue
        flags.append(True)
        # 缩进按 CommonMark 计 tab 为到下一 4 列制表位：tab 缩进的标记行（≥4 列）是围栏内容，不闭合（R3 P2-N7）。
        expanded = line.expandtabs(4)
        stripped = expanded.strip()
        if (
            len(expanded) - len(expanded.lstrip(" ")) <= 3
            and stripped
            and set(stripped) == {fence[0]}
            and len(stripped) >= len(fence)
        ):
            fence = None
    return flags


def _markdown_header(text: str) -> str:
    """首个（围栏外）``##`` 标题之前的内容；围栏内的行不计入头部，也不结束头部。"""
    lines = text.splitlines()
    kept: list[str] = []
    for line, fenced in zip(lines, _fence_flags(lines)):
        if fenced:
            continue
        if HEADER_END_RE.match(line):
            break
        kept.append(line)
    return "\n".join(kept)


def _release_todo_entries(text: str) -> list[str] | None:
    """返回**最后一个**「## 发布前遗留项」小节内的条目文本；小节缺失返回 None（D-63 ②）。

    - 多轮追加格式中最新一轮在末尾，只取最后一节；小节在下一个任意级标题（``#``～``######``）处结束。
    - 代码围栏（``` / ~~~）内的行不是小节起点、不截断小节、不产生条目（D-64 ③）。
    - 条目 = 顶层列表项或表格数据行；表头行（紧跟分隔行 ``|---|`` 的行）、分隔行、状态列为 closed 的行、
      「无 / N/A」等空标记不算条目。表格行以首个单元格起的文本作为条目文本。
    - 缩进更深的列表项与缩进续行归属上一条列表条目（拼接到其文本），不作独立条目。
    """
    lines = text.splitlines()
    fenced = _fence_flags(lines)
    starts = [index for index, line in enumerate(lines) if not fenced[index] and RELEASE_TODO_HEADING_RE.match(line)]
    if not starts:
        return None
    entries: list[str] = []
    last_kind: str | None = None  # 上一个非空行的种类：list / table-* / other
    base_indent: int | None = None
    for index in range(starts[-1] + 1, len(lines)):
        line = lines[index]
        if fenced[index]:
            # 围栏行对条目解析透明：不产生条目、不截断，也不打断父条目与其后续子项的归属。
            continue
        if ANY_HEADING_RE.match(line):
            break
        if not line.strip():
            continue
        expanded = line.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip())
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if any(cells) and all(TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells if cell):
                if last_kind == "table-row" and entries:
                    entries.pop()  # 分隔行之前的一行是表头
                last_kind = "table-separator"
                continue
            if not any(cells) or any(cell.lower() == "closed" for cell in cells):
                last_kind = "table-skip"
                continue
            entries.append(" | ".join(cells))
            last_kind = "table-row"
            continue
        match = LIST_ITEM_RE.match(line)
        if last_kind == "list" and base_indent is not None and indent > base_indent:
            # 子项 / 续行：归属父条目（拼在父条目正文之后，不影响开头的发现 id 标记）。
            entries[-1] = f"{entries[-1]} {match.group(1).strip() if match and match.group(1) else stripped}"
            continue
        if match and match.group(1):
            content = match.group(1).strip()
            if EMPTY_LIST_ITEM_RE.match(content):
                last_kind = "other"
                continue
            if base_indent is None:
                base_indent = indent
            entries.append(content)
            last_kind = "list"
            continue
        last_kind = "other"
    return entries


def _entry_finding_ids(entry: str) -> list[str]:
    """条目开头的显式发现 id 标记（D-64 ①）：``[F-001] [F-002] 正文`` → ``["F-001", "F-002"]``；无标记返回空列表。

    方括号外的别名（PR-xx）、路径、编号一律视为正文；开头之后出现的方括号也是正文。
    """
    match = LEADING_ID_MARKERS_RE.match(entry)
    if match is None:
        return []
    return [token.strip() for token in ID_MARKER_RE.findall(match.group(0))]


class ReviewVerifier:
    def __init__(self) -> None:
        self.validator = ReviewResultValidator()
        self.calculator = ScoreCalculator()

    def verify(self, report_file: Path, result_file: Path, report_label: str | None = None) -> VerifyReport:
        report_text = _read_report_text(report_file)
        label = report_label or report_file.name
        if not _path_present(result_file):
            # 只有结果文件确实不存在才走兼容分支（AC-45）；目录 / FIFO / 坏链接 / 不可读 → load 报 SCHEMA_INVALID（D-63 ⑤、D-64 ②）。
            return VerifyReport(
                status="ok", report=label, resultFile=None, verdict="legacy-unverified", reason=LEGACY_REASON_NO_RESULT_FILE
            )
        result, raw, problems = ReviewResult.load(result_file)
        if isinstance(raw, dict) and _is_int(raw.get("schemaVersion")) and raw["schemaVersion"] > REVIEW_RESULT_SCHEMA_VERSION:
            # 接口 §9.3：高于自身支持版本的结果文件按 legacy-unverified 处理，不校验、不阻断（D-63 ①）。
            return VerifyReport(
                status="ok",
                report=label,
                resultFile=result_file.name,
                verdict="legacy-unverified",
                reason=LEGACY_REASON_UNSUPPORTED_SCHEMA_VERSION,
            )
        if not problems:
            # 已成功解码（含 JSON null / 数组等非对象根）才做结构校验；加载失败的问题已在 problems 中。
            problems.extend(self.validator.validate(raw))
        if not isinstance(raw, dict):
            # 加载失败（读取 / 解码失败）或根不是对象：只报 SCHEMA_INVALID，不再附带由空结果派生的
            # INCONSISTENT / REPORT_MISMATCH（D-64 ④）。
            return VerifyReport(
                status="inconsistent", report=label, resultFile=result_file.name, verdict="inconsistent", problems=problems
            )
        problems.extend(self.checkEvidence(result))
        problems.extend(self.checkScore(result))
        problems.extend(self.checkMarkdown(report_file, report_text, result_file, result))
        score = self.calculator.compute(result.findings)
        open_blocking = self.calculator.open_blocking(result.findings)
        release_todo = sum(1 for f in result.findings if f.classification == "release-todo" and f.status == "open")
        if problems:
            verdict, status = "inconsistent", "inconsistent"
        else:
            verdict, status = self.calculator.verdict(score, open_blocking, REVIEW_RESULT_THRESHOLD), "ok"
        return VerifyReport(
            status=status,
            report=label,
            resultFile=result_file.name,
            verdict=verdict,
            score=score,
            openBlocking=open_blocking,
            releaseTodoCount=release_todo,
            problems=problems,
        )

    def checkEvidence(self, result: ReviewResult) -> list[VerifyProblem]:
        problems: list[VerifyProblem] = []

        def missing(finding: ReviewFinding, message: str) -> None:
            problems.append(_problem(REVIEW_RESULT_MISSING_EVIDENCE, f"{finding.id}: {message}", finding.id))

        for finding in result.findings:
            for message in self.validator.missing_fields(finding.raw):
                missing(finding, message)
            e5_reported = False
            if finding.is_api_policy():
                # E1：两字段必须同时存在。
                if "apiImpactClass" not in finding.present or not finding.hasApiImpactEvidence:
                    missing(finding, "apiImpactClass and apiImpactEvidence are required for API version policy findings (E1)")
                evidence = finding.apiImpactEvidence
                if evidence is not None:
                    for name, item in evidence.items():
                        # E3：checked=true 时 ref 必须为非空字符串。
                        if item.get("checked") is True and not _non_empty_str(item.get("ref")):
                            missing(finding, f"apiImpactEvidence.{name}.checked=true requires a non-empty ref (E3)")
                    checked = [item.get("checked") is True for _, item in evidence.items()]
                    # E4：unreleased-unconsumed 三项证据都必须 checked=true。
                    if finding.apiImpactClass == "unreleased-unconsumed" and not all(checked):
                        missing(finding, "unreleased-unconsumed requires all three evidence items checked=true (E4)")
                    # E5：no-contract-change / not-maven-artifact 三项均未查证时须写 evidence.excerpt。
                    if finding.apiImpactClass in API_IMPACT_EXCERPT_CLASSES and not any(checked) and not finding.excerpt:
                        missing(finding, f"{finding.apiImpactClass} without checked evidence requires evidence.excerpt (E5)")
                        e5_reported = True
            if finding.applicability == "not-applicable" and not finding.excerpt and not e5_reported:
                missing(finding, "applicability=not-applicable requires evidence.excerpt stating the reason")
        return problems

    def checkScore(self, result: ReviewResult) -> list[VerifyProblem]:
        problems: list[VerifyProblem] = []

        def inconsistent(message: str, finding_id: Any = None) -> None:
            problems.append(_problem(REVIEW_RESULT_INCONSISTENT, message, finding_id))

        by_id: dict[str, ReviewFinding] = {}
        by_identity: dict[tuple[str, str], ReviewFinding] = {}
        for finding in result.findings:
            if finding.id in by_id:
                inconsistent(f"{finding.id}: duplicate finding id", finding.id)
            by_id.setdefault(finding.id, finding)
            if _non_empty_str(finding.ruleId) and _non_empty_str(finding.scope):
                if finding.identity in by_identity:
                    first = by_identity[finding.identity]
                    inconsistent(
                        f"{finding.id}: duplicate finding for (ruleId, scope)=({finding.ruleId}, {finding.scope}); first is {first.id}",
                        finding.id,
                    )
                by_identity.setdefault(finding.identity, finding)
            if (finding.applicability == "not-applicable") != (finding.classification == "not-applicable") and (
                finding.applicability in ENUM_SETS["applicability"] and finding.classification in ENUM_SETS["class"]
            ):
                inconsistent(
                    f"{finding.id}: applicability={finding.applicability} contradicts class={finding.classification}",
                    finding.id,
                )
            # E6：apiImpactClass 与 class 一致（evidence-unknown 只约束 open 的发现，已处置须 closed，D-18）。
            if finding.is_api_policy() and finding.apiImpactClass in ENUM_SETS["apiImpactClass"]:
                required = API_IMPACT_REQUIRED_CLASS.get(finding.apiImpactClass)
                if finding.apiImpactClass == "evidence-unknown" and finding.status == "open":
                    required = "blocking"
                if required is not None and finding.classification != required:
                    inconsistent(
                        f"{finding.id}: apiImpactClass={finding.apiImpactClass} requires class={required}, got {finding.classification} (E6)",
                        finding.id,
                    )
            # 规则 §2.2：规则仓 / DevKit 来源（source.kind=specrules|devkit）的发现不产生 release-todo（D-63 ④）。
            source_kind = finding.source.get("kind") if isinstance(finding.source, dict) else None
            if finding.classification == "release-todo" and source_kind in RULE_SOURCE_KINDS:
                inconsistent(
                    f"{finding.id}: source.kind={source_kind} findings cannot be class=release-todo "
                    "(release-todo only comes from template sections; use blocking / advisory / not-applicable)",
                    finding.id,
                )

        deducted: dict[tuple[str, str], Deduction] = {}
        for index, deduction in enumerate(result.deductions):
            where = f"deductions[{index}]"
            target = by_id.get(deduction.findingId) if isinstance(deduction.findingId, str) else None
            if target is None:
                inconsistent(f"{where}: deduction references no finding (findingId={deduction.findingId})")
                continue
            if not target.is_open_blocking():
                inconsistent(
                    f"{where}: deduction on {target.id} which is class={target.classification} status={target.status}; "
                    "only open blocking findings may be deducted",
                    target.id,
                )
            if (deduction.ruleId, deduction.scope) != (target.ruleId, target.scope):
                inconsistent(f"{where}: ruleId/scope differ from finding {target.id}", target.id)
            key = (str(deduction.ruleId), str(deduction.scope))
            if key in deducted:
                inconsistent(f"{where}: duplicate deduction for (ruleId, scope)=({deduction.ruleId}, {deduction.scope})", target.id)
                continue
            deducted[key] = deduction
            expected_points = self.calculator.points_for(str(deduction.ruleId))
            if _is_int(deduction.points) and deduction.points != expected_points:
                inconsistent(f"{where}: points={deduction.points} differs from points table value {expected_points}", target.id)
        for identity, finding in by_identity.items():
            if finding.is_open_blocking() and identity not in deducted:
                inconsistent(f"{finding.id}: open blocking finding has no deduction", finding.id)

        score = self.calculator.compute(result.findings)
        open_blocking = self.calculator.open_blocking(result.findings)
        if _is_int(result.score) and result.score != score:
            inconsistent(f"declared score {result.score} != recomputed {score}")
        if _is_int(result.openBlocking) and result.openBlocking != open_blocking:
            inconsistent(f"declared openBlocking {result.openBlocking} != recomputed {open_blocking}")
        if _is_int(result.openBlocking) and result.verdict in ENUM_SETS["verdict"]:
            if result.openBlocking == 0 and result.verdict == "fail":
                inconsistent("openBlocking == 0 cannot coexist with verdict=fail (score is only deducted by open blocking findings)")
            elif result.openBlocking > 0 and result.verdict == "pass":
                inconsistent("verdict=pass while openBlocking > 0")
            elif result.verdict != self.calculator.verdict(score, open_blocking):
                inconsistent(f"declared verdict {result.verdict} != recomputed {self.calculator.verdict(score, open_blocking)}")
        return problems

    def checkMarkdown(self, report_file: Path, report_text: str, result_file: Path, result: ReviewResult) -> list[VerifyProblem]:
        problems: list[VerifyProblem] = []

        def mismatch(message: str, finding_id: Any = None) -> None:
            problems.append(_problem(REVIEW_RESULT_REPORT_MISMATCH, message, finding_id))

        if isinstance(result.report, str) and result.report and result.report != report_file.name:
            mismatch(f"result file report={result.report} but verified report is {report_file.name}")
        header = _markdown_header(report_text)
        summary = RESULT_FILE_LINE_RE.search(header)
        if summary is None:
            mismatch(f"report header lacks '结果文件: {result_file.name} sha256=<digest>' line")
        else:
            if summary.group(1) != result_file.name:
                mismatch(f"report header names result file {summary.group(1)}, expected {result_file.name}")
            if summary.group(2).lower() != result.sha256:
                mismatch("report header sha256 does not match the result file content")
        conclusion = CONCLUSION_LINE_RE.search(header)
        if conclusion is None:
            mismatch("report header lacks '结论: <verdict>' line")
        elif conclusion.group(1) != result.verdict:
            mismatch(f"report header 结论 {conclusion.group(1)} != result verdict {result.verdict}")
        expected = [f for f in result.findings if f.classification == "release-todo" and f.status == "open"]
        entries = _release_todo_entries(report_text)
        if entries is None:
            if expected:
                mismatch(f"report lacks section '{RELEASE_TODO_HEADING}' but {len(expected)} open release-todo finding(s) exist")
            return problems
        # 条目集合须与 open release-todo 发现集合严格相等（接口 §7.3；D-63 ③）；身份只取条目开头的 `[id]`（D-64 ①）。
        by_id = {f.id: f for f in result.findings}
        expected_ids = {f.id for f in expected}
        listed: set[str] = set()
        for entry in entries:
            ids = _entry_finding_ids(entry)
            if not ids:
                mismatch(f"release-todo entry lacks a leading [finding-id] marker: {entry[:80]}")
                continue
            for fid in ids:
                target = by_id.get(fid)
                if target is None:
                    mismatch(f"release-todo entry references unknown finding id [{fid}]: {entry[:80]}")
                elif fid not in expected_ids:
                    mismatch(
                        f"{fid}: listed in '{RELEASE_TODO_HEADING}' but is class={target.classification} status={target.status}; "
                        "only open release-todo findings may be listed",
                        fid,
                    )
                else:
                    listed.add(fid)
        for finding in expected:
            if finding.id not in listed:
                mismatch(f"{finding.id}: open release-todo finding missing from '{RELEASE_TODO_HEADING}'", finding.id)
        return problems


def resolve_review_report(workspace: Path, feature: dict[str, Any], report: str) -> tuple[Path, str]:
    """报告映射按文件名确定，不看 mtime（接口 §2.6）；显式路径相对 cwd（工作区根）解析。"""
    feature_dir = Path(feature["feature_dir"])
    if not feature_dir.is_absolute():
        feature_dir = workspace / feature_dir
    if report in REPORT_ALIASES:
        return feature_dir / REPORT_ALIASES[report], report
    match = ARCH_TASK_REPORT_RE.match(report)
    if match:
        return feature_dir / f"review-arch-{match.group(1)}.md", report
    path = Path(report).expanduser()
    return (path if path.is_absolute() else workspace / path), report


def format_review_result_verify_text(body: dict[str, Any]) -> str:
    reason = f"reason={body['reason']}, " if body.get("reason") else ""
    lines = [
        f"review-result: {body['verdict']} ({reason}report={body['report']}, result_file={body['result_file'] or '-'}, "
        f"score={body['score']}, open_blocking={body['open_blocking']}, release_todo={body['release_todo_count']})"
    ]
    for problem in body["problems"]:
        suffix = f" [{problem['finding_id']}]" if problem.get("finding_id") else ""
        lines.append(f"- {problem['error_code']}{suffix}: {problem['message']}")
    return "\n".join(lines)


def command_review_result_verify(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    selected = select_feature(find_features(workspace), args.feature)
    if selected.get("status") != "ok":
        raise DevkitFlowError(selected.get("message") or "feature not found")
    report_file, label = resolve_review_report(workspace, selected, args.report)
    result = ReviewVerifier().verify(report_file, result_file_for_report(report_file), label).to_dict()
    if args.format == "json":
        emit(result, "json")
    else:
        print(format_review_result_verify_text(result))
    return 1 if result["status"] == "inconsistent" else 0
