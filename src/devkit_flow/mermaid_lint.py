"""Heuristic classDiagram checks for Mermaid 11.16.0 portability.

The bundled preview and 文档/IDE hosts do not share one parser. These checks
flag generation patterns that explode in HTML-unescaped 11.16.0 hosts or
that the chart-generator 禁区 forbids. They are not a substitute for mermaid.run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_FENCE = re.compile(r"```mermaid\s*\n(.*?)```", re.S | re.I)
_RELATION = re.compile(
    r"^\s*\S+\s+(?:<\|\.\.|\.\.>|\.\.--|-->|<\|--|o--|o\.\.|<\|-\.|--|\*--|<\|>)\s+\S+\s*:\s*(.*)$"
)
_JAVA_METHOD = re.compile(
    r"^\s*[+\-#~](?:static\s+)?[A-Za-z_][\w.]*(?:\[\])?\s+[A-Za-z_][\w.]*\s*\("
)
_JAVA_GENERIC = re.compile(r"[A-Za-z_][\w.]*<[^>\n]+>")
_QUOTED_DEFAULT = re.compile(r"=\s*\"")
_STEREOTYPE = re.compile(r"<<([^>]*)>>")
_EMOJI = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF]"
)
_CJK = re.compile(r"[\u4e00-\u9fff]")
_VISIBILITY_MEMBER = re.compile(r"^\s*[+\-#~]")
_CLASS_OPEN = re.compile(r"^\s*class\s+\S+\s*\{")
_ASCII_STEREOTYPE = re.compile(r"^[A-Za-z0-9_ ]+$")


@dataclass(frozen=True)
class MermaidLintIssue:
    code: str
    message: str
    line: int


def extract_class_diagrams(markdown: str) -> list[str]:
    blocks: list[str] = []
    for match in _FENCE.finditer(markdown):
        body = match.group(1).strip()
        if body.lower().startswith("classdiagram"):
            blocks.append(body)
    return blocks


def lint_class_diagram(src: str) -> list[MermaidLintIssue]:
    issues: list[MermaidLintIssue] = []
    in_class = False
    for index, raw in enumerate(src.splitlines(), start=1):
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("%%"):
            continue
        if _CLASS_OPEN.search(line):
            in_class = True
            _check_stereotypes(line, index, issues)
            continue
        if in_class and stripped == "}":
            in_class = False
            continue
        if in_class:
            _lint_member_line(line, index, issues)
            continue
        _check_stereotypes(line, index, issues)
        relation = _RELATION.match(line)
        if relation:
            _lint_relation_label(relation.group(1).strip(), index, issues)
            continue
        if _JAVA_GENERIC.search(line):
            issues.append(
                MermaidLintIssue(
                    "java_generic",
                    "泛型禁止使用 <>，改写为 List~T~",
                    index,
                )
            )
    return issues


def lint_markdown(markdown: str) -> list[MermaidLintIssue]:
    issues: list[MermaidLintIssue] = []
    for block in extract_class_diagrams(markdown):
        issues.extend(lint_class_diagram(block))
    return issues


def _lint_member_line(line: str, index: int, issues: list[MermaidLintIssue]) -> None:
    stripped = line.strip()
    if not stripped:
        return
    _check_stereotypes(line, index, issues)
    if _JAVA_GENERIC.search(line):
        issues.append(
            MermaidLintIssue("java_generic", "泛型禁止使用 <>，改写为 List~T~", index)
        )
    if _QUOTED_DEFAULT.search(line):
        issues.append(
            MermaidLintIssue(
                "quoted_default",
                "字段默认值禁止带引号",
                index,
            )
        )
    if _EMOJI.search(line):
        issues.append(
            MermaidLintIssue("emoji_member", "成员行禁止 emoji / 表情符号", index)
        )
    if _JAVA_METHOD.search(line):
        issues.append(
            MermaidLintIssue(
                "java_style_method",
                "方法禁止 Java 风格 ReturnType method()；写成 method(params) ReturnType",
                index,
            )
        )
    if _CJK.search(line):
        if _VISIBILITY_MEMBER.search(line) or stripped.startswith("说明"):
            issues.append(
                MermaidLintIssue(
                    "chinese_member",
                    "成员行禁止中文备注或把说明文字当成员",
                    index,
                )
            )
        elif not stripped.startswith("<<"):
            issues.append(
                MermaidLintIssue(
                    "prose_member",
                    "不要把说明文字当作 class 成员",
                    index,
                )
            )


def _lint_relation_label(label: str, index: int, issues: list[MermaidLintIssue]) -> None:
    if not label:
        return
    quoted = (label.startswith('"') and label.endswith('"')) or (
        label.startswith("'") and label.endswith("'")
    )
    if quoted:
        return
    if any(token in label for token in ("(", ")", "/", "=", '"')):
        issues.append(
            MermaidLintIssue(
                "unquoted_relation_special",
                "关系标签含 () / = \" 时必须加引号",
                index,
            )
        )
    if _CJK.search(label):
        issues.append(
            MermaidLintIssue(
                "unquoted_relation_chinese",
                "中文关系标签必须用双引号包裹",
                index,
            )
        )


def _check_stereotypes(
    line: str, index: int, issues: list[MermaidLintIssue]
) -> None:
    for match in _STEREOTYPE.finditer(line):
        body = match.group(1).strip()
        if not body:
            continue
        if not _ASCII_STEREOTYPE.match(body):
            issues.append(
                MermaidLintIssue(
                    "stereotype_non_ascii",
                    "stereotype / 构造型只用 ASCII，如 <<new>> / <<interface>>",
                    index,
                )
            )
