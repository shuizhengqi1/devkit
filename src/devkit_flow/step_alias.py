from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NoReturn
from devkit_flow.constants import FLOW_MODES, STEP_ALIAS_ROWS
from devkit_flow.errors import DevkitFlowError


SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# 未知但形如 Sx 的输入直通（夹具/历史自定义行如 S2），禁止再走模糊以免改写存量精确查找。
_SX_LIKE_RE = re.compile(r"^S[0-9][A-Za-z0-9._-]*$", re.IGNORECASE)


def canonicalize_sx_id(text: str) -> str:
    """Sx-like 透传规范化：s2 → S2、s2-h → S2-H、s5-e2e → S5-E2E。"""
    return text.strip().upper()
MATCHED_BY_ID = "id"
MATCHED_BY_SLUG = "slug"
MATCHED_BY_ZH = "zh"
MATCHED_BY_FUZZY = "fuzzy"
MATCHED_BY_CUSTOM_SLUG = "custom-slug"
_DIFFLIB_CUTOFF = 0.6
_DIFFLIB_N = 8


@dataclass(frozen=True)
class StepAliasRow:
    step_id: str
    mode: str
    slug: str
    zh_name: str


@dataclass(frozen=True)
class StepResolution:
    step_id: str
    matched_by: str
    input_text: str

    def as_json(self) -> dict[str, str]:
        return {
            "input": self.input_text,
            "matched_by": self.matched_by,
            "step": self.step_id,
        }


ALIAS_ROWS: tuple[StepAliasRow, ...] = tuple(
    StepAliasRow(step_id=step_id, mode=mode, slug=slug, zh_name=zh_name)
    for step_id, mode, slug, zh_name in STEP_ALIAS_ROWS
)


def normalize_pipeline_mode(mode: str | None) -> str | None:
    text = (mode or "").strip()
    if text in FLOW_MODES:
        return text
    return None


def feature_pipeline_mode(feature: Mapping[str, Any] | None) -> str | None:
    """从 feature summary.模式 取值；pending / 空 / 未知 → None。"""
    if not feature:
        return None
    summary = feature.get("summary")
    if not isinstance(summary, Mapping):
        return None
    return normalize_pipeline_mode(str(summary.get("mode") or ""))


def rows_for_mode(mode: str | None) -> list[StepAliasRow]:
    normalized = normalize_pipeline_mode(mode)
    if normalized is None:
        return list(ALIAS_ROWS)
    return [row for row in ALIAS_ROWS if row.mode in ("*", normalized)]


def format_candidate(row: StepAliasRow) -> str:
    if row.mode == "*":
        return f"{row.zh_name}（{row.step_id} / {row.slug}）"
    return f"{row.zh_name}（{row.step_id} / {row.slug}，{row.mode}）"


def _sorted_rows(rows: Iterable[StepAliasRow]) -> list[StepAliasRow]:
    mode_rank = {"*": 0, "传统": 1, "TDD": 2}
    return sorted(
        rows,
        key=lambda row: (row.step_id, mode_rank.get(row.mode, 9), row.slug),
    )


def format_ambiguity_error(input_text: str, matches: Iterable[StepAliasRow]) -> str:
    lines = [f'步骤引用有歧义: "{input_text}" 匹配多个候选：']
    lines.extend(f"  {format_candidate(row)}" for row in _sorted_rows(matches))
    lines.append("请改用 Sx 精确 ID，或先执行 flow mode 确认模式。")
    return "\n".join(lines)


def attach_step_resolution(data: dict[str, Any], resolution: StepResolution) -> None:
    """非 Sx 输入回显 step_resolution；matched_by=id 不回显（接口 §2.6）。"""
    if resolution.matched_by != MATCHED_BY_ID:
        data["step_resolution"] = resolution.as_json()


def candidates_for(text: str, mode: str | None = None) -> list[StepAliasRow]:
    """返回当前模式作用域内、与 text 精确或模糊相关的候选行。"""
    rows = rows_for_mode(mode)
    stripped = (text or "").strip()
    if not stripped:
        return _sorted_rows(rows)
    exact = _exact_id_rows(stripped, rows) or _exact_slug_rows(stripped, rows) or _exact_zh_rows(
        stripped, rows
    )
    if exact:
        return _sorted_rows(exact)
    return _sorted_rows(_fuzzy_rows(stripped, rows))


def resolve_step(
    text: str,
    *,
    mode: str | None = None,
    allowed: Iterable[str] | None = None,
    extra_rows: Iterable[StepAliasRow] | None = None,
) -> StepResolution:
    """四层解析：Sx casefold → slug → 中文名精确 → 模糊；同层多命中不猜测。

    extra_rows（014 R15，接口 §3.5）：调用方从 STATUS.md 主表+历史区提取的存量自定义
    行（X-* 等未登记进静态别名表的步骤）。custom-slug 精确层位于 Sx 直通之后、静态
    slug 层之前，四层相对顺序与 Sx 直通不变式（R9）不变；自定义行不参与模糊候选，
    不污染静态别名表。
    """
    input_text = text if isinstance(text, str) else str(text)
    stripped = input_text.strip()
    if not stripped:
        raise DevkitFlowError("步骤引用不能为空。")
    # Sx 是全局机器 ID，第 1 层不按模式过滤，保证存量 Sx 输入路径不变（R9）。
    all_rows = list(ALIAS_ROWS)
    scoped_rows = rows_for_mode(mode)
    exact_id = _unique_resolution(input_text, _exact_id_rows(stripped, all_rows), MATCHED_BY_ID)
    if exact_id is None and _SX_LIKE_RE.fullmatch(stripped):
        exact_id = StepResolution(
            step_id=canonicalize_sx_id(stripped),
            matched_by=MATCHED_BY_ID,
            input_text=input_text,
        )
    resolution = (
        exact_id
        or _unique_resolution(
            input_text, _custom_slug_rows(stripped, extra_rows), MATCHED_BY_CUSTOM_SLUG
        )
        or _unique_resolution(input_text, _exact_slug_rows(stripped, scoped_rows), MATCHED_BY_SLUG)
        or _unique_resolution(input_text, _exact_zh_rows(stripped, scoped_rows), MATCHED_BY_ZH)
        or _unique_resolution(input_text, _hyphen_slug_rows(stripped, scoped_rows), MATCHED_BY_FUZZY)
        or _unique_resolution(input_text, _prefix_rows(stripped, scoped_rows), MATCHED_BY_FUZZY)
        or _unique_resolution(input_text, _difflib_rows(stripped, scoped_rows), MATCHED_BY_FUZZY)
    )
    if resolution is None:
        _raise_unmatched(input_text, stripped, scoped_rows)
    if allowed is not None:
        allowed_ids = frozenset(allowed)
        if resolution.step_id not in allowed_ids:
            _raise_not_allowed(input_text, resolution.step_id, allowed_ids, mode)
    return resolution


def _unique_resolution(
    input_text: str, hits: list[StepAliasRow], matched_by: str
) -> StepResolution | None:
    if not hits:
        return None
    step_ids = {row.step_id for row in hits}
    if len(step_ids) > 1:
        raise DevkitFlowError(format_ambiguity_error(input_text, hits))
    return StepResolution(step_id=next(iter(step_ids)), matched_by=matched_by, input_text=input_text)


def _exact_id_rows(stripped: str, rows: list[StepAliasRow]) -> list[StepAliasRow]:
    key = stripped.casefold()
    return [row for row in rows if row.step_id.casefold() == key]


def _exact_slug_rows(stripped: str, rows: list[StepAliasRow]) -> list[StepAliasRow]:
    key = stripped.casefold()
    return [row for row in rows if row.slug.casefold() == key]


def _custom_slug_rows(
    stripped: str, extra_rows: Iterable[StepAliasRow] | None
) -> list[StepAliasRow]:
    """extra_rows 内按 step_id / slug casefold 精确命中（X-F2-QUERY 类自定义 slug）。"""
    if not extra_rows:
        return []
    key = stripped.casefold()
    return [
        row
        for row in extra_rows
        if row.step_id.casefold() == key or row.slug.casefold() == key
    ]


def _exact_zh_rows(stripped: str, rows: list[StepAliasRow]) -> list[StepAliasRow]:
    return [row for row in rows if row.zh_name == stripped]


def _hyphen_slug_rows(stripped: str, rows: list[StepAliasRow]) -> list[StepAliasRow]:
    """空格改连字符后的 slug 精确命中（如 arch review → arch-review），计为 fuzzy。"""
    key = stripped.casefold()
    hyphenated = key.replace(" ", "-")
    if hyphenated == key:
        return []
    return [row for row in rows if row.slug.casefold() == hyphenated]


def _prefix_rows(stripped: str, rows: list[StepAliasRow]) -> list[StepAliasRow]:
    key = stripped.casefold()
    hyphenated = key.replace(" ", "-")
    hits: list[StepAliasRow] = []
    for row in rows:
        slug = row.slug.casefold()
        if key and slug.startswith(key):
            hits.append(row)
            continue
        if hyphenated != key and slug.startswith(hyphenated):
            hits.append(row)
            continue
        if stripped and row.zh_name.startswith(stripped):
            hits.append(row)
    return hits


def _difflib_rows(stripped: str, rows: list[StepAliasRow]) -> list[StepAliasRow]:
    key = stripped.casefold()
    hyphenated = key.replace(" ", "-")
    slug_map = _group_by(rows, lambda row: row.slug.casefold())
    zh_map = _group_by(rows, lambda row: row.zh_name)
    close_slugs = list(
        difflib.get_close_matches(key, list(slug_map), n=_DIFFLIB_N, cutoff=_DIFFLIB_CUTOFF)
    )
    if hyphenated != key:
        close_slugs.extend(
            difflib.get_close_matches(
                hyphenated, list(slug_map), n=_DIFFLIB_N, cutoff=_DIFFLIB_CUTOFF
            )
        )
    hits: list[StepAliasRow] = []
    seen: set[tuple[str, str, str]] = set()

    def add(row: StepAliasRow) -> None:
        marker = (row.step_id, row.mode, row.slug)
        if marker not in seen:
            seen.add(marker)
            hits.append(row)

    for slug in close_slugs:
        for row in slug_map.get(slug, ()):
            add(row)
    for name in difflib.get_close_matches(
        stripped, list(zh_map), n=_DIFFLIB_N, cutoff=_DIFFLIB_CUTOFF
    ):
        for row in zh_map.get(name, ()):
            add(row)
    return hits


def _fuzzy_rows(stripped: str, rows: list[StepAliasRow]) -> list[StepAliasRow]:
    hits: list[StepAliasRow] = []
    seen: set[tuple[str, str, str]] = set()
    for row in (
        *_hyphen_slug_rows(stripped, rows),
        *_prefix_rows(stripped, rows),
        *_difflib_rows(stripped, rows),
    ):
        marker = (row.step_id, row.mode, row.slug)
        if marker not in seen:
            seen.add(marker)
            hits.append(row)
    return hits


def _group_by(
    rows: list[StepAliasRow], key_fn: Callable[[StepAliasRow], str]
) -> dict[str, list[StepAliasRow]]:
    grouped: dict[str, list[StepAliasRow]] = {}
    for row in rows:
        grouped.setdefault(key_fn(row), []).append(row)
    return grouped


def _raise_unmatched(input_text: str, stripped: str, rows: list[StepAliasRow]) -> NoReturn:
    approx = _sorted_rows(_fuzzy_rows(stripped, rows))
    lines = [f'步骤引用无法匹配: "{input_text}"']
    if approx:
        lines.append("近似候选：")
        lines.extend(f"  {format_candidate(row)}" for row in approx)
    else:
        lines.append("无近似候选。")
    lines.append("请改用 Sx 精确 ID、slug 或中文名。")
    raise DevkitFlowError("\n".join(lines))


def _raise_not_allowed(
    input_text: str,
    step_id: str,
    allowed_ids: frozenset[str],
    mode: str | None,
) -> NoReturn:
    allowed_rows = [
        row for row in rows_for_mode(mode) if row.step_id in allowed_ids
    ]
    lines = [
        f'步骤引用 "{input_text}" 解析为 {step_id}，不在允许的集合内。',
        "允许的候选：",
    ]
    if allowed_rows:
        lines.extend(f"  {format_candidate(row)}" for row in _sorted_rows(allowed_rows))
    else:
        lines.append("  " + ", ".join(sorted(allowed_ids)))
    raise DevkitFlowError("\n".join(lines))
