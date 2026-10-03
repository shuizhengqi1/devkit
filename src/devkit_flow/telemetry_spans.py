"""usage-telemetry T-06：per-runtime agent span 采集器 + 会话 token 汇总。

契约依据：
- spec/011-usage-telemetry/[设计文档]usage-telemetry.md §4.4（L1/L2/L3 分层、cursor 去重、D-9 降级）
- spec/011-usage-telemetry/[设计文档]usage-telemetry.md §4.10 / [类图] §3（SessionTokenSummarizer、
  session_started_keys 200 键最旧淘汰）
- DECISIONS D-2（不采 description 正文）/ D-9（codex/grok 宿主文件事后解析，失败 tokens=null）

硬约束：本模块不得 import devkit_flow.telemetry（顶层或函数内均不得；类图 §2 注记）。
MODULE_ORDER：session_registry → telemetry_spans → telemetry；本模块顶层依赖
constants + session_registry.locate_session_file + textio.exclusive_file_lock
（textio 在 MODULE_ORDER 中早于本模块），与 bundle 拓扑校验同口径。
所有文件 IO fail-safe：异常不抛出，降级 failed / None / 空列表。
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from devkit_flow.constants import TELEMETRY_SPAN_CURSOR_RELATIVE_PATH
from devkit_flow.session_registry import locate_session_file, normalize_workspace
from devkit_flow.textio import exclusive_file_lock

# §4.4 扫描预算：Claude 仍 ≤50 文件；Codex 子 rollout 见 D-21（匹配条数 / 累计字节）
MAX_SUBAGENT_FILES_PER_FLUSH = 50
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_MATCHED_CHILD_ROLLOUTS_PER_FLUSH = 200
MAX_MATCHED_CHILD_READ_BYTES_PER_FLUSH = MAX_FILE_BYTES
MAX_SESSION_META_HEADER_BYTES = 1024 * 1024
# D-25：cumulative_tokens / 子 rollout 末条只扫尾窗，不读 20MB 前缀
MAX_TAIL_WINDOW_BYTES = 2 * 1024 * 1024
TAIL_SCAN_CHUNK_BYTES = 256 * 1024
# D-33：--usage-json stdin 分块读取；与 _read_capped_text 同 20MB 口径
USAGE_JSON_STREAM_CHUNK_BYTES = 64 * 1024
# 类图 §3 注记：session_started 去重键上限，最旧淘汰
SESSION_STARTED_KEYS_LIMIT = 200

TOKEN_SOURCE_CLAUDE = "claude-transcript"
TOKEN_SOURCE_CODEX = "codex-transcript"
TOKEN_SOURCE_GROK = "grok-transcript"
TOKEN_SOURCE_GROK_JSON = "grok-json"
TOKEN_SOURCE_DISPATCH_JSON = "dispatch-json"
TOKEN_SOURCE_NONE = "none"
# 014 R18：kimi wire.jsonl / pi session jsonl 采集来源（schema token_source 枚举随 schema 侧同步扩展）
TOKEN_SOURCE_KIMI = "kimi-transcript"
TOKEN_SOURCE_PI = "pi-transcript"

# 六键 tokens（设计 §4.1 agent_span.tokens / schema $defs.tokenCounts）
TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "thinking_tokens",
    "total_tokens",
)

_PAYLOAD_KEYS = (
    "agent_id",
    "agent_type",
    "model",
    "started_at",
    "ended_at",
    "duration_ms",
    "message_count",
    "spawn_depth",
    "tool_use_id",
    "tokens",
    "token_source",
    "collection_status",
    "step_id",
    "task_id",
    "worker_role",
    "dispatch",
    "result",
    "parent_session_id",
    "attribution_mode",
    "dispatch_seq",
    "ones_requirement_id",
    "ones_task_id",
    "cost_usd",
)

_USAGE_JSON_HINT_KEYS = ("modelUsage", "total_token_usage", "usage", "tokens", "total_tokens")
_CODEX_NAMED_KEYS = ("cached_input_tokens", "reasoning_output_tokens")
_FORM_GROK = "grok_model_usage"
_FORM_CODEX = "codex_token_count"
_FORM_ANTHROPIC = "anthropic_usage"
_FORM_DIRECT = "direct_tokens"

_CURSOR_SPANS = "spans"
_CURSOR_SESSION_KEYS = "session_started_keys"
_CURSOR_LAST_FLUSH = "last_successful_flush_at"
_CURSOR_DISPATCH_SEQ = "dispatch_seq"
# 水位子的 size/mtime 快照：无变化则跳过全量读且不计 D-21 预算
_CURSOR_SPAN_STATS = "span_file_stats"
# KI-5：codex 子 rollout 发现游标，按父 watermark key 记住上次扫到的候选路径
_CURSOR_CODEX_DISCOVERY = "codex_discovery_offsets"
# 目录宿主与指纹共用的可读后缀（须与 `_collect_records_from_host_path` 一致）
_HOST_DIR_RECORD_SUFFIXES = frozenset({".jsonl", ".json"})
# `_build_rollout_span`：预算耗尽导致读取不完整，调用方不得推进水位/指纹
_INCOMPLETE_READ = object()
# D-42③ / D-47：`_read_usage_records_from_offset` 的 complete 位；stat/open
# 失败及 open 后中途 seek/read/close I/O 失败，与预算截断 `complete=False`
# 区分，供 `_claude_span_from_offset_file` 发 failed span。
_USAGE_READ_IO_FAILED = object()

_USAGE_HINT_KEYS = frozenset(TOKEN_KEYS) | {"output_tokens_details"}
_PARSE_FAILED = object()
# D-32 / D-34：前向顶层扫描 `{` + raw_decode；成功跳到 end（嵌套不耗预算，
# 成功解码不计数——数量受 D-33 20MB 读取上限约束）；失败前进 1 字符。
# 失败尝试上限 256（防日志里的 `{` 拖时间）；取最后一个含 usage hint 的顶层 object。
_LAST_JSON_FAILED_DECODE_ATTEMPTS = 256


@dataclass
class AgentSpan:
    """agent_span payload 载体（设计 §4.1 事件 8 字段表；无 description 字段，D-2）。"""

    agent_id: str
    agent_type: Optional[str] = None
    model: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: Optional[int] = None
    message_count: Optional[int] = None
    spawn_depth: Optional[int] = None
    tool_use_id: Optional[str] = None
    tokens: Optional[dict] = None
    token_source: str = TOKEN_SOURCE_NONE
    collection_status: str = "ok"
    step_id: Optional[str] = None
    task_id: Optional[str] = None
    worker_role: Optional[str] = None
    dispatch: Optional[str] = None
    result: Optional[str] = None
    parent_session_id: Optional[str] = None
    attribution_mode: Optional[str] = None
    dispatch_seq: Optional[int] = None
    ones_requirement_id: Optional[str] = None
    ones_task_id: Optional[str] = None
    cost_usd: Optional[float] = None
    # D-21 ⑥：对账 warning 等元信息；不进 to_payload / outbox 正文
    notes: list[str] = field(default_factory=list)
    # D-26 ①：本轮尾读累计六键，仅用于水位持久化，不进 payload
    cursor_total: Optional[dict] = field(default=None, repr=False)

    def to_payload(self) -> dict:
        return {key: getattr(self, key) for key in _PAYLOAD_KEYS}


ENV_EVENT_PATTERNS: dict[str, str] = {
    "provider_capacity": r"model is at capacity|overloaded_error|\"type\":\s*\"overloaded\"|529 Overloaded|Service (?:Unavailable|Overloaded)|capacity (?:limit|exceeded)",
    "rate_limit": r"rate_limit_error|Rate limit (?:reached|exceeded)|Too Many Requests|Weekly limit left|usage limit (?:reached|exceeded)|You've hit your|quota exceeded|insufficient_quota",
    "network": r"ECONNRESET|ETIMEDOUT|socket hang up|fetch failed|Request timed out|stream disconnected|Connection (?:reset|refused)|network error",
    "self_update": r"Updating to version|New version available|Upgraded to v\d|Update installed|自动更新到",
}


class EnvEventScanner:
    """环境异常事件受控正则扫描器（D-5, R7）：从文本日志中抽取限流、容量、超时等异常计数，脱敏输出。"""

    def __init__(self, patterns: Optional[dict[str, str]] = None):
        raw_patterns = patterns or ENV_EVENT_PATTERNS
        self._compiled = {
            kind: re.compile(pat, re.IGNORECASE)
            for kind, pat in raw_patterns.items()
        }

    def scan(self, text: str) -> list[dict[str, Any]]:
        if not text or not text.strip():
            return []
        events: list[dict[str, Any]] = []
        for kind, rx in self._compiled.items():
            matches = rx.findall(text)
            if matches:
                events.append({"kind": kind, "count": len(matches)})
        return events


_GLOBAL_ENV_SCANNER = EnvEventScanner()


def scan_env_events(text: str) -> list[dict[str, Any]]:
    """便捷函数：使用全局单例 EnvEventScanner 扫描文本中的环境事件。"""
    return _GLOBAL_ENV_SCANNER.scan(text)


@dataclass
class RolloutMeta:
    """codex rollout 首行 session_meta 载体（类图 §2；不进 outbox）。"""

    rollout_id: str
    path: Path
    parent_thread_id: Optional[str] = None
    role: Optional[str] = None
    originator: Optional[str] = None
    model: Optional[str] = None
    timestamp: Optional[str] = None

    def is_child(self) -> bool:
        return bool(self.parent_thread_id)


class CumulativeTokenResult(dict):
    """六键 tokens；`.truncated` 仅当 size > MAX_FILE_BYTES（前缀读非全文件）。

    D-25 尾窗未命中时退回前缀读；2MB<size≤20MB 前缀已是全文件、值精确，不得标
    truncated，否则 D-26 ③ 会把精确值置 null。
    仍是 dict：runtime._collect_session_ended 的 `tokens is not None` 与 JSON
    序列化无需改签名。查询：`getattr(tokens, "truncated", False)`。
    """

    truncated: bool

    def __init__(self, mapping: Optional[dict] = None, *, truncated: bool = False):
        super().__init__(mapping or {})
        self.truncated = bool(truncated)


@dataclass
class UsageSummary:
    """--usage-json 解析结果（类图 §2；不进 outbox）。"""

    token_source: str
    collection_status: str
    tokens: Optional[dict] = None
    model: Optional[str] = None
    cost_usd: Optional[float] = None
    turns: Optional[int] = None
    session_id: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class CollectionBudget:
    """Flush 级采集预算（D-26 ② / D-24）：deadline + remaining_bytes。

    budget=None 表示不限（保持 collect 既有调用兼容）。
    clock 可注入（telemetry 传入模块内 time.monotonic，单测可 patch）。
    """

    deadline: float
    remaining_bytes: int
    clock: Any = None

    def exhausted(self) -> bool:
        getter = self.clock
        if getter is None:
            getter = time.monotonic
        try:
            if getter() >= self.deadline:
                return True
        except Exception:
            pass
        try:
            return int(self.remaining_bytes) <= 0
        except (TypeError, ValueError):
            return True

    def charge(self, nbytes: Any) -> None:
        if isinstance(nbytes, bool) or nbytes is None:
            return
        try:
            n = int(nbytes)
        except (TypeError, ValueError):
            return
        if n > 0:
            self.remaining_bytes -= n


class SpanCursorStore:
    """span 水位 / session_started 去重键存储（~/.devkit/telemetry-span-cursor.json，§4.4）。

    path 可注入（测试用 tempdir），默认 ~/.devkit/telemetry-span-cursor.json。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            path = Path.home() / TELEMETRY_SPAN_CURSOR_RELATIVE_PATH
        self.path = Path(path)

    def _read_cursor_unlocked(self) -> dict:
        try:
            if not self.path.is_file():
                return _blank_cursor()
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _blank_cursor()
            if not isinstance(data.get(_CURSOR_SPANS), dict):
                data[_CURSOR_SPANS] = {}
            if not isinstance(data.get(_CURSOR_SESSION_KEYS), list):
                data[_CURSOR_SESSION_KEYS] = []
            if _CURSOR_LAST_FLUSH not in data:
                data[_CURSOR_LAST_FLUSH] = None
            return data
        except Exception:
            return _blank_cursor()

    def load(self) -> dict:
        try:
            data = self._read_cursor_unlocked()
            _migrate_legacy_watermarks_in_place(data)
            return data
        except Exception:
            return _blank_cursor()

    def update(self, mutator: Callable[[dict], Any]) -> Any:
        """锁内读改写：mutator 收到当前 cursor，可就地修改或返回新 dict。失败返回 None。"""
        try:
            with exclusive_file_lock(self.path):
                data = self._read_cursor_unlocked()
                if not isinstance(data, dict):
                    data = _blank_cursor()
                _migrate_legacy_watermarks_in_place(data)
                try:
                    result = mutator(data)
                except Exception:
                    return None
                payload = result if isinstance(result, dict) else data
                self._write_cursor(payload)
                return result if result is not None else payload
        except Exception:
            return None

    def save(self, cursor: dict) -> None:
        incoming = cursor if isinstance(cursor, dict) else _blank_cursor()

        def overlay(current: dict) -> dict:
            merged = dict(current) if isinstance(current, dict) else _blank_cursor()
            for key, value in incoming.items():
                if key in (
                    _CURSOR_DISPATCH_SEQ,
                    _CURSOR_SESSION_KEYS,
                    _CURSOR_LAST_FLUSH,
                ):
                    continue
                merged[key] = value
            seq = _merge_dispatch_seq_tables(
                current.get(_CURSOR_DISPATCH_SEQ) if isinstance(current, dict) else None,
                incoming.get(_CURSOR_DISPATCH_SEQ),
            )
            if seq is None:
                merged.pop(_CURSOR_DISPATCH_SEQ, None)
            else:
                merged[_CURSOR_DISPATCH_SEQ] = seq
            merged[_CURSOR_SESSION_KEYS] = _merge_session_started_keys(
                current.get(_CURSOR_SESSION_KEYS) if isinstance(current, dict) else None,
                incoming.get(_CURSOR_SESSION_KEYS),
            )
            merged[_CURSOR_LAST_FLUSH] = _later_flush_at(
                current.get(_CURSOR_LAST_FLUSH) if isinstance(current, dict) else None,
                incoming.get(_CURSOR_LAST_FLUSH),
            )
            return merged

        self.update(overlay)

    def _write_cursor(self, cursor: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = cursor if isinstance(cursor, dict) else _blank_cursor()
        text = json.dumps(payload, ensure_ascii=False)
        tmp_path = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            tmp_path.write_text(text, encoding="utf-8")
            tmp_path.replace(self.path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def seen_session_started(self, key: str) -> bool:
        try:
            keys = self.load().get(_CURSOR_SESSION_KEYS)
            return isinstance(keys, list) and key in keys
        except Exception:
            return False

    def mark_session_started(self, key: str) -> None:
        def add(data: dict) -> dict:
            keys = [
                item
                for item in data.get(_CURSOR_SESSION_KEYS, [])
                if isinstance(item, str)
            ]
            if key not in keys:
                keys.append(key)
                overflow = len(keys) - SESSION_STARTED_KEYS_LIMIT
                if overflow > 0:
                    keys = keys[overflow:]
            data[_CURSOR_SESSION_KEYS] = keys
            return data

        self.update(add)

    def next_dispatch_seq(self, workspace: str, feature: Optional[str]) -> Optional[int]:
        holder: list[int] = []

        def bump(data: dict) -> dict:
            table = data.get(_CURSOR_DISPATCH_SEQ)
            if not isinstance(table, dict):
                table = {}
            key = _dispatch_seq_key(workspace, feature)
            current = table.get(key)
            if isinstance(current, bool) or not isinstance(current, int) or current < 1:
                nxt = 1
            else:
                nxt = current + 1
            table[key] = nxt
            data[_CURSOR_DISPATCH_SEQ] = table
            holder.append(nxt)
            return data

        written = self.update(bump)
        if written is None:
            return None
        return holder[0] if holder else None

    def reserve_dispatch_seq(
        self, workspace: str, feature: Optional[str], seq: int
    ) -> Optional[int]:
        """显式 seq 将 cursor 保留为 max(current, seq)（D-29）；失败返回 None。"""
        holder: list[int] = []

        def reserve(data: dict) -> dict:
            if isinstance(seq, bool) or not isinstance(seq, int):
                return data
            table = data.get(_CURSOR_DISPATCH_SEQ)
            if not isinstance(table, dict):
                table = {}
            key = _dispatch_seq_key(workspace, feature)
            current = table.get(key)
            if isinstance(current, bool) or not isinstance(current, int) or current < 1:
                current_n = 0
            else:
                current_n = current
            reserved = max(current_n, seq)
            table[key] = reserved
            data[_CURSOR_DISPATCH_SEQ] = table
            holder.append(reserved)
            return data

        written = self.update(reserve)
        if written is None:
            return None
        return holder[0] if holder else None

    def migrate_legacy_watermarks(self) -> None:
        """一次性把 spans / span_file_stats 的三元旧键归并为 (workspace, agent_id)（D-35）。

        在 update 锁内执行；失败 warning `span cursor migration failed` 且不抛。
        """
        try:
            self.update(lambda data: data)
        except Exception:
            _warn_span_cursor_migration_failed()


class AgentSpanCollector:
    """抽象采集器（类图 §3）。cursor 水位键 = workspace+agent_id（D-35）。"""

    token_source = TOKEN_SOURCE_NONE

    def __init__(self, workspace: str, feature: Optional[str] = None) -> None:
        self.workspace = workspace
        self.feature = feature

    def collect(
        self,
        marker: dict[str, Any],
        cursor: "SpanCursorStore",
        budget: Optional[CollectionBudget] = None,
    ) -> list[AgentSpan]:
        return []


class ClaudeSpanCollector(AgentSpanCollector):
    """L1 精准：transcript_path 同目录 <session>/subagents/agent-<id>.jsonl + agent-<id>.meta.json。"""

    token_source = TOKEN_SOURCE_CLAUDE

    def collect(
        self,
        marker: dict[str, Any],
        cursor: "SpanCursorStore",
        budget: Optional[CollectionBudget] = None,
    ) -> list[AgentSpan]:
        try:
            spans: list[AgentSpan] = []
            raw_session = marker.get("session_id")
            parent_session_id = (
                raw_session.strip()
                if isinstance(raw_session, str) and raw_session.strip()
                else None
            )
            subagents = _resolve_subagents_dir(marker)
            jsonl_files = (
                _list_subagent_jsonl(subagents) if subagents is not None else []
            )
            stopped = False
            if jsonl_files:
                over_budget = len(jsonl_files) > MAX_SUBAGENT_FILES_PER_FLUSH
                to_parse = jsonl_files[:MAX_SUBAGENT_FILES_PER_FLUSH]
                data = cursor.load()
                watermarks = data.setdefault(_CURSOR_SPANS, {})
                if not isinstance(watermarks, dict):
                    watermarks = {}
                    data[_CURSOR_SPANS] = watermarks
                stats_table = _cursor_stats_table(data)
                for path in to_parse:
                    if not _budget_allow(budget):
                        stopped = True
                        break
                    agent_id = path.stem
                    physical_ws = _watermark_workspace(marker, self.workspace)
                    key, aliased = _alias_pre_d51_consumer_watermark(
                        data, physical_ws, str(self.workspace or ""), agent_id
                    )
                    if aliased:
                        try:
                            cursor.save(data)
                        except Exception:
                            pass
                    last_seen = watermarks.get(key)
                    identity = _file_stat_identity(path)
                    if _claude_skip_unchanged(
                        last_seen, stats_table.get(key), identity
                    ):
                        continue
                    meta_path = path.with_name(path.stem + ".meta.json")
                    meta = _read_meta(path)
                    span, checkpoint, incomplete = _claude_span_from_offset_file(
                        self._read_usage_from_offset,
                        path,
                        agent_id=agent_id,
                        last_seen=last_seen,
                        budget=budget,
                        token_source=self.token_source,
                        meta=meta,
                        dispatch="subagent",
                        parent_session_id=parent_session_id,
                    )
                    _budget_charge_file(budget, meta_path, cap=MAX_FILE_BYTES)
                    if checkpoint is not None:
                        watermarks[key] = checkpoint
                        _claude_store_span_file_stat_after_checkpoint(
                            stats_table, key, identity, checkpoint, span
                        )
                    if span is not None:
                        if (over_budget or stopped or incomplete) and (
                            span.collection_status == "ok"
                        ):
                            span.collection_status = "partial"
                        spans.append(span)
                    if incomplete and budget is not None and not _budget_allow(budget):
                        stopped = True
                        break
                    if not _budget_allow(budget):
                        stopped = True
                        break
                data[_CURSOR_SPAN_STATS] = stats_table
                cursor.save(data)
            if stopped:
                _mark_spans_partial(spans)
            if _budget_allow(budget):
                host = self.collect_host_span(marker, cursor, budget=budget)
                if host is not None:
                    if stopped and host.collection_status == "ok":
                        host.collection_status = "partial"
                    if not _budget_allow(budget) and host.collection_status == "ok":
                        host.collection_status = "partial"
                    spans.append(host)
            elif spans:
                _mark_spans_partial(spans)
            return spans
        except Exception:
            return []

    def parse_subagent_file(self, path: Path) -> AgentSpan:
        agent_id = path.stem
        meta = _read_meta(path)
        try:
            records, truncated, unreadable = _extract_usage_records(path, require_assistant=True)
            if unreadable or not records:
                return _failed_span(
                    agent_id,
                    token_source=self.token_source,
                    meta=meta,
                )
            return _span_from_records(
                agent_id=agent_id,
                records=records,
                token_source=self.token_source,
                meta=meta,
                truncated=truncated,
            )
        except Exception:
            return _failed_span(agent_id, token_source=self.token_source, meta=meta)

    def _read_usage_from_offset(
        self,
        path: Path | str,
        offset: Any,
        budget: Optional[CollectionBudget] = None,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """从水位 offset 流式读取 usage（D-37）。返回 (records, new_offset, complete)。"""
        return _read_usage_records_from_offset(
            path,
            offset,
            budget,
            require_assistant=True,
            max_bytes=MAX_FILE_BYTES,
        )

    def collect_host_span(
        self,
        marker: dict[str, Any],
        cursor: "SpanCursorStore",
        budget: Optional[CollectionBudget] = None,
    ) -> Optional[AgentSpan]:
        """主会话 transcript 水位增量 host span（设计 §4.4.7 / 类图 §3 / D-37）。"""
        try:
            if not _budget_allow(budget):
                return None
            session_id = marker.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                return None
            session_id = session_id.strip()
            path = _host_path(marker, locate_grok=False)
            if path is None:
                return None
            try:
                if not path.is_file():
                    return None
            except Exception:
                return None
            data = cursor.load()
            watermarks = data.setdefault(_CURSOR_SPANS, {})
            if not isinstance(watermarks, dict):
                watermarks = {}
                data[_CURSOR_SPANS] = watermarks
            stats_table = _cursor_stats_table(data)
            physical_ws = _watermark_workspace(marker, self.workspace)
            key, aliased = _alias_pre_d51_consumer_watermark(
                data, physical_ws, str(self.workspace or ""), session_id
            )
            if aliased:
                try:
                    cursor.save(data)
                except Exception:
                    pass
            last_seen = watermarks.get(key)
            identity = _file_stat_identity(path)
            if _claude_skip_unchanged(last_seen, stats_table.get(key), identity):
                return None
            if not _budget_allow(budget):
                return None
            span, checkpoint, _incomplete = _claude_span_from_offset_file(
                self._read_usage_from_offset,
                path,
                agent_id=session_id,
                last_seen=last_seen,
                budget=budget,
                token_source=self.token_source,
                meta={},
                dispatch="host",
                parent_session_id=None,
            )
            if checkpoint is not None:
                watermarks[key] = checkpoint
                _claude_store_span_file_stat_after_checkpoint(
                    stats_table, key, identity, checkpoint, span
                )
                data[_CURSOR_SPANS] = watermarks
                data[_CURSOR_SPAN_STATS] = stats_table
                cursor.save(data)
            return span
        except Exception:
            return None


class CodexSpanCollector(AgentSpanCollector):
    """L2 best-effort：codex rollout jsonl 事后解析；失败 tokens=null + failed（D-9）。"""

    token_source = TOKEN_SOURCE_CODEX

    def collect(
        self,
        marker: dict[str, Any],
        cursor: "SpanCursorStore",
        budget: Optional[CollectionBudget] = None,
    ) -> list[AgentSpan]:
        path = _host_path(marker, locate_grok=False)
        if (path is None or not path.exists()) and self.workspace:
            sess_id = marker.get("session_id")
            fallback_rollout = _find_codex_rollout_by_cwd(self.workspace, sess_id)
            if fallback_rollout is not None:
                path = fallback_rollout
        agent_id = _derived_agent_id(marker, path)
        try:
            if not _budget_allow(budget):
                return []
            data = cursor.load()
            watermarks = data.get(_CURSOR_SPANS)
            if not isinstance(watermarks, dict):
                watermarks = {}
            physical_ws = _watermark_workspace(marker, self.workspace)
            parent_key, aliased = _alias_pre_d51_consumer_watermark(
                data, physical_ws, str(self.workspace or ""), agent_id
            )
            if aliased:
                try:
                    cursor.save(data)
                except Exception:
                    pass
            stats_table = data.get(_CURSOR_SPAN_STATS)
            if not isinstance(stats_table, dict):
                stats_table = {}
            else:
                stats_table = dict(stats_table)
            parent_identity = _file_stat_identity(path) if path is not None else None
            skip_parent = bool(
                path is not None
                and _skip_unchanged_span_file(
                    watermarks.get(parent_key),
                    stats_table.get(parent_key),
                    parent_identity,
                    require_total=True,
                )
            )
            reader = CodexRolloutReader()
            if path is None:
                meta = None
            elif skip_parent:
                meta = RolloutMeta(rollout_id=agent_id, path=path)
            elif budget is None:
                meta = reader.read_meta(path)
            else:
                meta = reader.read_meta(path, budget=budget)
            if meta is None:
                if budget is None:
                    return _collect_host_transcript(self, marker, cursor)
                return _collect_host_transcript(self, marker, cursor, budget=budget)
            spans: list[AgentSpan] = []
            parent_incomplete = False
            if not skip_parent:
                parent_span = self._span_for_rollout(
                    meta, cursor, None, budget=budget, workspace=physical_ws
                )
                if parent_span is _INCOMPLETE_READ:
                    parent_incomplete = True
                else:
                    if parent_span is not None:
                        spans.append(parent_span)
                    # 仅完整读取后才记指纹；不完整读取不得推进 cursor
                    _store_span_file_stat(stats_table, parent_key, parent_identity)
            # budget=0：发现阶段不截断，由本方法按匹配 200 / 20MB 与水位计数（D-21）
            if budget is None:
                children = reader.find_children(
                    meta, 0, cursor=cursor, parent_key=parent_key
                )
            else:
                children = reader.find_children(
                    meta,
                    0,
                    collection_budget=budget,
                    cursor=cursor,
                    parent_key=parent_key,
                )
            data = cursor.load()
            watermarks = data.get(_CURSOR_SPANS)
            if not isinstance(watermarks, dict):
                watermarks = {}
            bytes_used = 0
            matched_full = 0
            truncated = bool(
                parent_incomplete
                or (budget is not None and not _budget_allow(budget))
            )
            for child in children:
                if not _budget_allow(budget):
                    truncated = True
                    break
                key, aliased = _alias_pre_d51_consumer_watermark(
                    data, physical_ws, str(self.workspace or ""), child.rollout_id
                )
                if aliased:
                    try:
                        cursor.save(data)
                    except Exception:
                        pass
                    src_stats = data.get(_CURSOR_SPAN_STATS)
                    if isinstance(src_stats, dict) and key in src_stats:
                        stats_table[key] = src_stats[key]
                has_wm = _has_span_watermark(watermarks.get(key))
                identity = _file_stat_identity(child.path)
                # 已有水位且（文件消失 / size+mtime 未变）→ 跳过全量读且不计预算
                if has_wm and (
                    identity is None or _file_stat_unchanged(stats_table.get(key), identity)
                ):
                    continue
                if matched_full >= MAX_MATCHED_CHILD_ROLLOUTS_PER_FLUSH:
                    truncated = True
                    break
                take = min(max(identity[0] if identity is not None else 0, 0), MAX_FILE_BYTES)
                if (
                    matched_full > 0
                    and bytes_used + take > MAX_MATCHED_CHILD_READ_BYTES_PER_FLUSH
                ):
                    truncated = True
                    break
                bytes_used += take
                matched_full += 1
                if child.model is None:
                    if not _budget_allow(budget):
                        truncated = True
                        break
                    # model=None 不视为失败；扫描按实际字节 charge，耗尽即停
                    child.model = _first_turn_context_model(child.path, budget=budget)
                if not _budget_allow(budget):
                    truncated = True
                    break
                child_span = self._span_for_rollout(
                    child,
                    cursor,
                    meta.rollout_id,
                    budget=budget,
                    workspace=physical_ws,
                )
                if child_span is _INCOMPLETE_READ:
                    truncated = True
                else:
                    if child_span is not None:
                        spans.append(child_span)
                    if identity is not None:
                        stats_table[key] = {"size": identity[0], "mtime_ns": identity[1]}
                if not _budget_allow(budget):
                    truncated = True
                    break
            if truncated:
                _mark_spans_partial(spans)
            try:
                persisted = cursor.load()
                if not isinstance(persisted, dict):
                    persisted = _blank_cursor()
                persisted[_CURSOR_SPAN_STATS] = stats_table
                cursor.save(persisted)
            except Exception:
                pass
            return spans
        except Exception:
            return [_failed_span(agent_id, token_source=self.token_source)]

    def _span_for_rollout(
        self,
        meta: RolloutMeta,
        cursor: "SpanCursorStore",
        parent_id: Optional[str],
        budget: Optional[CollectionBudget] = None,
        workspace: Optional[str] = None,
    ) -> Any:
        data = cursor.load()
        watermarks = data.setdefault(_CURSOR_SPANS, {})
        if not isinstance(watermarks, dict):
            watermarks = {}
            data[_CURSOR_SPANS] = watermarks
        physical_ws = workspace if workspace else self.workspace
        key, aliased = _alias_pre_d51_consumer_watermark(
            data, physical_ws, str(self.workspace or ""), meta.rollout_id
        )
        if aliased:
            try:
                cursor.save(data)
            except Exception:
                pass
        last_seen = watermarks.get(key)
        span = self._build_rollout_span(meta, last_seen, parent_id, budget=budget)
        if span is _INCOMPLETE_READ:
            return _INCOMPLETE_READ
        if span is None:
            return None
        if span.collection_status == "failed":
            watermarks[key] = ""
        else:
            watermarks[key] = _codex_watermark_value(span)
        data[_CURSOR_SPANS] = watermarks
        cursor.save(data)
        return span

    def _build_rollout_span(
        self,
        meta: RolloutMeta,
        watermark: Any,
        parent_id: Optional[str],
        budget: Optional[CollectionBudget] = None,
    ) -> Any:
        dispatch = "subagent" if parent_id else "host"
        agent_type = meta.role if parent_id else None
        path = meta.path
        model = meta.model
        has_wm = _has_span_watermark(watermark)
        try:
            exists = path.is_file()
        except Exception:
            exists = False
        if not exists:
            if has_wm:
                return None
            span = _failed_span(meta.rollout_id, token_source=self.token_source)
            span.dispatch = dispatch
            span.parent_session_id = parent_id
            span.agent_type = agent_type
            return span
        if not _budget_allow(budget):
            return _INCOMPLETE_READ
        reader = CodexRolloutReader()
        tail_record = None
        tail_miss = False
        tailed = False
        if _budget_allow(budget):
            tailed = True
            try:
                tail_record, tail_miss = _last_token_count_from_tail(path)
            except Exception:
                tail_record, tail_miss = None, True
            _budget_charge_file(budget, path, cap=MAX_TAIL_WINDOW_BYTES)
        records: list[dict] = []
        prefix_skipped = not _budget_allow(budget)
        if not prefix_skipped:
            records = reader.token_count_records(path)
            _budget_charge_file(budget, path, cap=MAX_FILE_BYTES)
        if tail_record is not None:
            records = _merge_tail_last_record(records, tail_record)
        # 尾读 miss / 未做尾读 且前缀因预算跳过：records 为空是读取不完整，不是真零。
        # 不得发零值 partial span，也不得推进水位/指纹，否则下轮会永久跳过。
        if prefix_skipped and tail_record is None and (tail_miss or not tailed):
            return _INCOMPLETE_READ
        # D-25 / D-26：truncated 仅当 size > MAX_FILE_BYTES（前缀读非全文件）。
        # 2MB<size≤20MB 且末条早于末 2MB 时前缀读已是全文件，值精确，不得标
        # truncated，否则 D-26 ③ 会把精确值置 null。
        truncated = False
        try:
            truncated = path.stat().st_size > MAX_FILE_BYTES
        except Exception:
            truncated = False
        if budget is not None and not _budget_allow(budget):
            truncated = True
        status = "partial" if truncated else "ok"
        if model is None and _budget_allow(budget):
            model = _first_turn_context_model(path, budget=budget)
        if not records:
            if has_wm:
                return None
            stamps: list[str] = []
            if _budget_allow(budget):
                stamps = _jsonl_timestamps(path)
                _budget_charge_file(budget, path, cap=MAX_FILE_BYTES)
            started_at = stamps[0] if stamps else None
            ended_at = stamps[-1] if stamps else None
            zero = TokenMapper.zero()
            return AgentSpan(
                agent_id=meta.rollout_id,
                agent_type=agent_type,
                model=model,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=_duration_ms(started_at, ended_at),
                message_count=0,
                tokens=zero,
                token_source=self.token_source,
                collection_status=status,
                dispatch=dispatch,
                parent_session_id=parent_id,
                cursor_total=dict(zero),
            )
        current_record = tail_record if tail_record is not None else records[-1]
        current_total = TokenMapper.map_codex(
            current_record.get("total_token_usage") if isinstance(current_record, dict) else None
        )
        previous_total = _watermark_total(watermark)
        tokens = reader.increment_since(current_total, previous_total)
        if tokens is None:
            return None
        if previous_total is not None and all(
            _coerce_token(tokens.get(key)) == 0 for key in TOKEN_KEYS
        ):
            return None
        wm_ts = _watermark_ended_at(watermark)
        if wm_ts:
            active = [
                item
                for item in records
                if item.get("timestamp") and _timestamp_gt(str(item["timestamp"]), wm_ts)
            ]
        else:
            active = records
        timestamps = [str(item["timestamp"]) for item in active if item.get("timestamp")]
        if not timestamps:
            timestamps = [str(item["timestamp"]) for item in records if item.get("timestamp")]
        started_at = timestamps[0] if timestamps else None
        ended_at = timestamps[-1] if timestamps else None
        return AgentSpan(
            agent_id=meta.rollout_id,
            agent_type=agent_type,
            model=model,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=_duration_ms(started_at, ended_at),
            message_count=len(active) if has_wm else len(records),
            tokens=tokens,
            token_source=self.token_source,
            collection_status=status,
            dispatch=dispatch,
            parent_session_id=parent_id,
            cursor_total=dict(current_total),
        )


class GrokSpanCollector(AgentSpanCollector):
    """L2 best-effort：grok 会话文件事后解析；失败 tokens=null + failed（D-9）。

    014 R18：宿主路径命中 grok 会话目录（含 updates.jsonl）时走
    `tools/eval/sddl_eval/evidence.py` 已校准实现——updates.jsonl 末条累计
    `params.update.usage` + 主会话 `subagents/` 列出的兄弟子代理会话目录；
    否则保持既有平铺 transcript 解析（`telemetry span --usage-json` 夹具等）。
    """

    token_source = TOKEN_SOURCE_GROK

    def collect(
        self,
        marker: dict[str, Any],
        cursor: "SpanCursorStore",
        budget: Optional[CollectionBudget] = None,
    ) -> list[AgentSpan]:
        try:
            host = _host_path(marker, locate_grok=True)
        except Exception:
            host = None
        if host is not None and _is_grok_session_dir(host):
            return _collect_grok_session_tree(
                self, marker, cursor, host, budget=budget
            )
        return _collect_host_transcript(
            self, marker, cursor, locate_grok=True, budget=budget
        )


class KimiSpanCollector(AgentSpanCollector):
    """L2 best-effort：kimi-code `<sessionDir>/agents/*/wire.jsonl` 事后解析（014 R18）。

    搬 `tools/eval/sddl_eval/evidence.py` kimi_usage 已校准实现：`usage.record`
    为 per-turn 增量（usageScope=turn），六键映射 inputOther→input、output→output、
    inputCacheRead→cache_read、inputCacheCreation→cache_creation（inputOther 不含
    缓存，口径同 claude）；`time` 为 epoch millis。只采 token 计数与时间戳，
    不采对话内容（D-2 同口径）。会话定位：marker 显式路径 → session_index.jsonl
    按 sessionId 反查 → 按 workDir 兜底（evidence.py 口径）。失败 tokens=null +
    failed（D-9）；主代理 dispatch=host、子代理目录 dispatch=subagent。
    """

    token_source = TOKEN_SOURCE_KIMI

    def collect(
        self,
        marker: dict[str, Any],
        cursor: "SpanCursorStore",
        budget: Optional[CollectionBudget] = None,
    ) -> list[AgentSpan]:
        fallback_id = _derived_agent_id(marker, None)
        try:
            if not _budget_allow(budget):
                return []
            session_path = _kimi_session_path(marker, self.workspace)
            try:
                exists = session_path is not None and session_path.exists()
            except Exception:
                exists = False
            if session_path is None or not exists:
                return [_failed_span(fallback_id, token_source=self.token_source)]
            wire_files = _list_kimi_wire_files(session_path)
            if not wire_files:
                return [_failed_span(fallback_id, token_source=self.token_source)]
            data = cursor.load()
            watermarks = data.setdefault(_CURSOR_SPANS, {})
            if not isinstance(watermarks, dict):
                watermarks = {}
                data[_CURSOR_SPANS] = watermarks
            stats_table = _cursor_stats_table(data)
            physical_ws = _watermark_workspace(marker, self.workspace)
            session_name = (
                session_path.stem if session_path.is_file() else session_path.name
            )
            over_budget = len(wire_files) > MAX_SUBAGENT_FILES_PER_FLUSH
            to_parse = wire_files[:MAX_SUBAGENT_FILES_PER_FLUSH]
            spans: list[AgentSpan] = []
            stopped = False
            for path in to_parse:
                if not _budget_allow(budget):
                    stopped = True
                    break
                span = self._collect_wire_file(
                    path,
                    session_path=session_path,
                    session_name=session_name,
                    marker=marker,
                    data=data,
                    watermarks=watermarks,
                    stats_table=stats_table,
                    physical_ws=physical_ws,
                    cursor=cursor,
                    budget=budget,
                )
                if span is not None:
                    spans.append(span)
            data[_CURSOR_SPANS] = watermarks
            data[_CURSOR_SPAN_STATS] = stats_table
            cursor.save(data)
            if over_budget or stopped:
                _mark_spans_partial(spans)
            return spans
        except Exception:
            return [_failed_span(fallback_id, token_source=self.token_source)]

    def _collect_wire_file(
        self,
        path: Path,
        *,
        session_path: Path,
        session_name: str,
        marker: dict[str, Any],
        data: dict,
        watermarks: dict,
        stats_table: dict,
        physical_ws: str,
        cursor: "SpanCursorStore",
        budget: Optional[CollectionBudget],
    ) -> Optional[AgentSpan]:
        """单份 wire.jsonl：ended_at 水位增量 + size/mtime 指纹跳过（同 _collect_host_transcript 语义）。"""
        try:
            is_main = session_path.is_file() or path.parent.name == "main"
            agent_id = session_name if is_main else f"{session_name}/{path.parent.name}"
            key, aliased = _alias_pre_d51_consumer_watermark(
                data, physical_ws, str(self.workspace or ""), agent_id
            )
            if aliased:
                try:
                    cursor.save(data)
                except Exception:
                    pass
            last_seen = watermarks.get(key)
            has_wm = _has_span_watermark(last_seen)
            wm_ts = _watermark_ended_at(last_seen)
            identity = _file_stat_identity(path)
            if _skip_unchanged_span_file(last_seen, stats_table.get(key), identity):
                return None
            records, truncated, unreadable = _extract_usage_records(
                path,
                require_assistant=False,
                budget=budget,
                record_extractor=_kimi_wire_record_from_obj,
            )
            _budget_charge_file(budget, path, cap=MAX_FILE_BYTES)
            if unreadable or not records:
                _store_span_file_stat(stats_table, key, identity)
                if has_wm:
                    return None
                watermarks[key] = ""
                span = _failed_span(agent_id, token_source=self.token_source)
                span.dispatch = "host" if is_main else "subagent"
                if not is_main:
                    span.parent_session_id = session_name
                return span
            if wm_ts:
                records = [
                    item
                    for item in records
                    if item.get("timestamp")
                    and _timestamp_gt(str(item["timestamp"]), wm_ts)
                ]
                if not records:
                    _store_span_file_stat(stats_table, key, identity)
                    return None
            span = _span_from_records(
                agent_id=agent_id,
                records=records,
                token_source=self.token_source,
                meta={},
                truncated=truncated,
            )
            span.dispatch = "host" if is_main else "subagent"
            if not is_main:
                span.parent_session_id = session_name
            _store_span_file_stat(stats_table, key, identity)
            watermarks[key] = span.ended_at if span.ended_at else ""
            return span
        except Exception:
            return None


class PiSpanCollector(AgentSpanCollector):
    """L2 best-effort：pi 会话 jsonl 事后解析（014 R18）。

    marker.session_file 直指会话文件（pi extension `getSessionFile()`；
    `--no-session` 时为 None → failed 降级，D-9 同口径）。记录形态（session v3）：
    `type=message` 且 `message.role=assistant` 携带 `message.usage`
    {input, output, cacheRead, cacheWrite, reasoning, totalTokens} → 六键；
    `timestamp` 为 ISO 字符串。只采 token 计数与时间戳，不采对话内容（D-2 同口径）。
    """

    token_source = TOKEN_SOURCE_PI

    def collect(
        self,
        marker: dict[str, Any],
        cursor: "SpanCursorStore",
        budget: Optional[CollectionBudget] = None,
    ) -> list[AgentSpan]:
        return _collect_host_transcript(
            self,
            marker,
            cursor,
            budget=budget,
            record_extractor=_pi_session_record_from_obj,
        )


class NullSpanCollector(AgentSpanCollector):
    """L3 降级兜底：未匹配到真实 collector 的 runtime（unknown / unattributed 等）不发
    agent_span（空列表）。kimi-code / pi 自 014 T14（R18）起由 KimiSpanCollector /
    PiSpanCollector 真实采集，不再走本降级。"""

    token_source = TOKEN_SOURCE_NONE

    def collect(
        self,
        marker: dict[str, Any],
        cursor: "SpanCursorStore",
        budget: Optional[CollectionBudget] = None,
    ) -> list[AgentSpan]:
        return []


class SessionTokenSummarizer:
    """claude-code 主会话 transcript 逐条 usage → 六键 tokens；失败/缺文件返回 None（§4.10）。"""

    def summarize(
        self,
        transcript_path: str | Path | None,
        budget: Optional[CollectionBudget] = None,
    ) -> Optional[dict]:
        if transcript_path is None:
            return None
        try:
            path = Path(transcript_path)
            if not path.is_file():
                return None
            records, _new_offset, complete = _read_usage_records_from_offset(
                path,
                0,
                budget,
                require_assistant=True,
                max_bytes=None,
            )
            if complete is _USAGE_READ_IO_FAILED:
                return None
            # D-47：budget=None 的 session-end 全量扫描同样不得把半写末行 /
            # 未读完前缀当完整总量上报（接口 §147：扫描截断 → tokens=null）。
            if not complete:
                return None
            if not records:
                return None
            return _aggregate_tokens(records)
        except Exception:
            return None


class GrokTokenSummarizer:
    """grok updates.jsonl 会话累计 usage → 六键 tokens；失败/缺文件返回 None。"""

    def summarize(
        self,
        transcript_path: str | Path | None,
        budget: Optional[CollectionBudget] = None,
    ) -> Optional[dict]:
        if transcript_path is None:
            return None
        try:
            path = Path(transcript_path)
            if path.is_dir():
                path = path / "updates.jsonl"
            if not path.is_file():
                return None
            usage, _first, _last, truncated, unreadable = _grok_session_usage_from_updates(
                path, budget
            )
            if unreadable or usage is None:
                return None
            return _map_grok_session_usage(usage)
        except Exception:
            return None


class KimiTokenSummarizer:
    """kimi-code wire.jsonl per-turn 增量汇总 → 六键 tokens；失败/缺文件返回 None。"""

    def summarize(
        self,
        transcript_path: str | Path | None,
        budget: Optional[CollectionBudget] = None,
        *,
        workspace: Optional[str] = None,
        marker: Optional[dict[str, Any]] = None,
    ) -> Optional[dict]:
        try:
            target_path: Optional[Path] = None
            if transcript_path is not None:
                cand = Path(transcript_path)
                if cand.exists():
                    target_path = cand
            if target_path is None and marker is not None and workspace is not None:
                target_path = _kimi_session_path(marker, workspace)
            if target_path is None:
                return None
            if target_path.is_file():
                wire_files = [target_path]
            elif target_path.is_dir():
                wire_files = _list_kimi_wire_files(target_path)
            else:
                return None
            if not wire_files:
                return None

            all_records: list[dict[str, Any]] = []
            for wf in wire_files:
                if not _budget_allow(budget):
                    break
                records, _truncated, unreadable = _extract_usage_records(
                    wf,
                    require_assistant=False,
                    budget=budget,
                    record_extractor=_kimi_wire_record_from_obj,
                )
                if unreadable:
                    continue
                if records:
                    all_records.extend(records)
            if not all_records:
                return None
            return _aggregate_tokens(all_records)
        except Exception:
            return None


class PiTokenSummarizer:
    """pi 会话 jsonl assistant message.usage 汇总 → 六键 tokens；失败/缺文件返回 None。"""

    def summarize(
        self,
        transcript_path: str | Path | None,
        budget: Optional[CollectionBudget] = None,
    ) -> Optional[dict]:
        if transcript_path is None:
            return None
        try:
            path = Path(transcript_path)
            if not path.is_file():
                return None
            records, _truncated, unreadable = _extract_usage_records(
                path,
                require_assistant=True,
                budget=budget,
                record_extractor=_pi_session_record_from_obj,
            )
            if unreadable or not records:
                return None
            return _aggregate_tokens(records)
        except Exception:
            return None


class TokenMapper:
    """codex / grok token 六键映射（设计 §4.4.3 / 类图 §3）。"""

    @staticmethod
    def map_codex(info: Any) -> dict:
        src = info if isinstance(info, dict) else {}
        nested = src.get("total_token_usage")
        if isinstance(nested, dict) and "cached_input_tokens" not in src and "input_tokens" not in src:
            src = nested
        cached = _coerce_token(src.get("cached_input_tokens"))
        reasoning = _coerce_token(src.get("reasoning_output_tokens"))
        mapped = {
            "input_tokens": max(_coerce_token(src.get("input_tokens")) - cached, 0),
            "output_tokens": max(_coerce_token(src.get("output_tokens")) - reasoning, 0),
            "cache_creation_input_tokens": _coerce_token(src.get("cache_write_input_tokens")),
            "cache_read_input_tokens": cached,
            "thinking_tokens": reasoning,
            "total_tokens": 0,
        }
        if "total_tokens" in src and src.get("total_tokens") is not None:
            mapped["total_tokens"] = _coerce_token(src.get("total_tokens"))
        else:
            mapped["total_tokens"] = _five_key_sum(mapped)
        return mapped

    @staticmethod
    def map_grok(model_usage: Any) -> dict:
        mapped = TokenMapper.zero()
        if not isinstance(model_usage, dict):
            return mapped
        for usage in model_usage.values():
            if not isinstance(usage, dict):
                continue
            mapped["input_tokens"] += _coerce_token(usage.get("inputTokens"))
            mapped["output_tokens"] += _coerce_token(usage.get("outputTokens"))
            mapped["cache_read_input_tokens"] += _coerce_token(usage.get("cacheReadInputTokens"))
            mapped["cache_creation_input_tokens"] += _coerce_token(usage.get("cacheCreationInputTokens"))
        mapped["thinking_tokens"] = 0
        mapped["total_tokens"] = _five_key_sum(mapped)
        return mapped

    @staticmethod
    def subtract(current: dict, previous: dict) -> dict:
        cur = current if isinstance(current, dict) else {}
        prev = previous if isinstance(previous, dict) else {}
        return {
            key: max(_coerce_token(cur.get(key)) - _coerce_token(prev.get(key)), 0)
            for key in TOKEN_KEYS
        }

    @staticmethod
    def zero() -> dict:
        return {key: 0 for key in TOKEN_KEYS}


class UsageJsonParser:
    """--usage-json 四形态识别（接口 §4.1 / 设计 §4.4.4 / 类图 §3）。"""

    def parse(self, text: str, runtime: str) -> UsageSummary:
        try:
            raw = text if isinstance(text, str) else ""
            obj: Optional[dict] = None
            try:
                parsed = json.loads(raw) if raw.strip() else None
            except (TypeError, ValueError):
                parsed = _PARSE_FAILED
            if parsed is _PARSE_FAILED:
                obj = self._last_json_object(raw)
            elif isinstance(parsed, dict):
                obj = parsed
            else:
                return _failed_usage_summary()
            if not isinstance(obj, dict):
                return _failed_usage_summary()
            form = self._detect_form(obj)
            if form is None:
                return _failed_usage_summary()
            return self._summary_for_form(obj, form, runtime)
        except Exception:
            return _failed_usage_summary()

    def to_span(self, text: str, runtime: str, agent_id: str) -> AgentSpan:
        """生产路径：parse → AgentSpan；对账 warnings 写入 notes（不进 payload）。"""
        return span_from_usage_summary(agent_id, self.parse(text, runtime))

    def _last_json_object(self, text: str) -> Optional[dict]:
        if not isinstance(text, str) or not text.strip():
            return None
        decoder = json.JSONDecoder()
        pos = 0
        failed_attempts = 0
        found: Optional[dict] = None
        while failed_attempts < _LAST_JSON_FAILED_DECODE_ATTEMPTS:
            index = text.find("{", pos)
            if index < 0:
                break
            try:
                obj, parsed_end = decoder.raw_decode(text, index)
            except (TypeError, ValueError):
                failed_attempts += 1
                pos = index + 1
                continue
            if isinstance(obj, dict) and _has_usage_json_hint(obj):
                found = obj
            pos = parsed_end
        return found

    def _detect_form(self, obj: dict) -> Optional[str]:
        if not isinstance(obj, dict):
            return None
        if isinstance(obj.get("modelUsage"), dict):
            return _FORM_GROK
        if _extract_codex_usage(obj) is not None:
            return _FORM_CODEX
        if isinstance(obj.get("usage"), dict):
            return _FORM_ANTHROPIC
        if isinstance(obj.get("tokens"), dict):
            return _FORM_DIRECT
        return None

    def _primary_model(self, model_usage: dict) -> Optional[str]:
        return _primary_model_from_model_usage(model_usage)

    def _summary_for_form(self, obj: dict, form: str, runtime: str) -> UsageSummary:
        grok_source = runtime == "grok-build" and form == _FORM_GROK
        token_source = TOKEN_SOURCE_GROK_JSON if grok_source else TOKEN_SOURCE_DISPATCH_JSON
        if form == _FORM_GROK:
            model_usage = obj.get("modelUsage") if isinstance(obj.get("modelUsage"), dict) else {}
            tokens = TokenMapper.map_grok(model_usage)
            cost, cost_warnings = _grok_cost_usd(obj, model_usage)
            session_id = obj.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                session_id = obj.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                session_id = None
            warnings = _total_mismatch_warnings(obj.get("total_tokens"), tokens)
            warnings.extend(cost_warnings)
            return UsageSummary(
                token_source=token_source,
                collection_status="ok",
                tokens=tokens,
                model=self._primary_model(model_usage),
                cost_usd=cost,
                turns=_optional_turns(obj.get("num_turns")),
                session_id=session_id,
                warnings=warnings,
            )
        if form == _FORM_CODEX:
            usage = _extract_codex_usage(obj) or {}
            model = obj.get("model")
            if not isinstance(model, str) or not model:
                model = None
            return UsageSummary(
                token_source=token_source,
                collection_status="ok",
                tokens=TokenMapper.map_codex(usage),
                model=model,
            )
        if form == _FORM_ANTHROPIC:
            usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
            return UsageSummary(
                token_source=token_source,
                collection_status="ok",
                tokens=_direct_six_key(usage),
                model=_optional_str(obj.get("model")),
            )
        usage = obj.get("tokens") if isinstance(obj.get("tokens"), dict) else {}
        tokens = _direct_six_key(usage)
        return UsageSummary(
            token_source=token_source,
            collection_status="ok",
            tokens=tokens,
            model=_optional_str(obj.get("model")),
            warnings=_total_mismatch_warnings(usage.get("total_tokens"), tokens),
        )


class CodexRolloutReader:
    """真实 codex rollout jsonl 解析（设计 §4.4.2 / 类图 §3）。"""

    def read_meta(
        self, path: Path | str, budget: Optional[CollectionBudget] = None
    ) -> Optional[RolloutMeta]:
        try:
            if not _budget_allow(budget):
                return None
            file_path = Path(path)
            if not file_path.is_file():
                return None
            if budget is None:
                header = _read_first_jsonl_object(file_path)
            else:
                header = _read_first_jsonl_object(file_path, budget=budget)
            meta = _rollout_meta_from_session_obj(header, file_path)
            if meta is None:
                return None
            if _budget_allow(budget):
                # 无早期 turn_context 时可扫到 MAX_FILE_BYTES；必须带 budget，
                # 按实际读取字节 charge。model=None 不视为失败。
                meta.model = _first_turn_context_model(file_path, budget=budget)
            return meta
        except Exception:
            return None

    def find_children(
        self,
        parent_meta: RolloutMeta,
        budget: int,
        *,
        collection_budget: Optional[CollectionBudget] = None,
        cursor: Optional["SpanCursorStore"] = None,
        parent_key: Optional[str] = None,
    ) -> list[RolloutMeta]:
        try:
            cap = (
                budget
                if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0
                else None
            )
            candidates = _candidate_paths_for_parent(parent_meta)
            resume = _load_discovery_resume(cursor, parent_key)
            start = 0
            if resume:
                for index, path in enumerate(candidates):
                    if str(path) == resume:
                        start = index + 1
                        break
            matched: list[RolloutMeta] = []
            last_scanned: Optional[str] = None
            exhausted = False
            for candidate in candidates[start:]:
                if not _budget_allow(collection_budget):
                    exhausted = True
                    break
                last_scanned = str(candidate)
                if collection_budget is None:
                    header = _read_first_jsonl_object(candidate)
                else:
                    header = _read_first_jsonl_object(
                        candidate, budget=collection_budget
                    )
                child = _rollout_meta_from_session_obj(header, candidate)
                if child is None:
                    continue
                if child.parent_thread_id == parent_meta.rollout_id:
                    matched.append(child)
            if exhausted:
                _store_discovery_resume(cursor, parent_key, last_scanned, done=False)
            else:
                _store_discovery_resume(cursor, parent_key, None, done=True)
            matched.sort(key=lambda item: _child_sort_key(item, parent_meta))
            if cap is not None:
                return matched[:cap]
            return matched
        except Exception:
            return []

    def token_count_records(self, path: Path | str) -> list[dict]:
        records: list[dict] = []
        try:
            file_path = Path(path)
            text, _truncated = _read_capped_text(file_path)
        except Exception:
            return records
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except (TypeError, ValueError):
                continue
            record = _token_count_record_from_obj(obj)
            if record is not None:
                records.append(record)
        return records

    def cumulative_tokens(self, path: Path | str) -> Optional[dict]:
        """末条 total_token_usage 映射六键（D-25 尾读）。

        从文件末尾按块（256KB）向前扫描，尾窗 ≤2MB；命中最后一条完整
        `event_msg.payload.type==token_count`。尾窗无完整记录 → 退回前缀读。
        `.truncated=True` 仅当 size > MAX_FILE_BYTES（前缀读非全文件；D-25 /
        D-26：2MB<size≤20MB 前缀已是全文件，值精确，不得标 truncated，否则
        D-26 ③ 会把精确值置 null）。文件不可读 → None。签名保持 Optional[dict]。
        """
        try:
            file_path = Path(path)
            if not file_path.is_file():
                return None
            record = None
            truncated = False
            try:
                tail_record, tail_miss = _last_token_count_from_tail(file_path)
            except Exception:
                tail_record, tail_miss = None, True
            if tail_record is not None:
                record = tail_record
            elif tail_miss:
                records = self.token_count_records(file_path)
                if not records:
                    return None
                record = records[-1]
                # D-25 / D-26：前缀回退仅在 size > MAX_FILE_BYTES 时才是截断读
                try:
                    truncated = file_path.stat().st_size > MAX_FILE_BYTES
                except Exception:
                    truncated = False
            else:
                return None
            mapped = TokenMapper.map_codex(
                record.get("total_token_usage") if isinstance(record, dict) else None
            )
            return CumulativeTokenResult(mapped, truncated=truncated)
        except Exception:
            return None

    def increment_since(self, current: Any, previous: Any = None) -> Optional[dict]:
        """D-26 ①：delta = 当前累计总量 − 水位累计总量（各键 clamp ≥0）。

        current / previous 为已映射六键或 raw total_token_usage。
        previous 缺省或无法解析 → 按首轮返回 current。
        current 无法解析 → None。不再用 token_count 记录列表按 timestamp 做差。
        """
        mapped_current = _as_token_total(current)
        if mapped_current is None:
            return None
        mapped_previous = _as_token_total(previous)
        if mapped_previous is None:
            return mapped_current
        return TokenMapper.subtract(mapped_current, mapped_previous)


# ---------------------------------------------------------------------------
# 内部：cursor / 路径 / jsonl / tokens
# ---------------------------------------------------------------------------

def _blank_cursor() -> dict:
    return {
        _CURSOR_SPANS: {},
        _CURSOR_SESSION_KEYS: [],
        _CURSOR_LAST_FLUSH: None,
    }


def _merge_dispatch_seq_tables(left: Any, right: Any) -> Optional[dict]:
    """合并 dispatch_seq 表：合法正整数取 max；两侧都不是 dict 时返回 None（不补键）。"""
    result: dict = {}
    saw_table = False
    for table in (left, right):
        if not isinstance(table, dict):
            continue
        saw_table = True
        for key, value in table.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                continue
            existing = result.get(key)
            if isinstance(existing, bool) or not isinstance(existing, int) or existing < 1:
                result[key] = value
            else:
                result[key] = max(existing, value)
    if not saw_table:
        return None
    return result


def _merge_session_started_keys(left: Any, right: Any) -> list:
    seen: list[str] = []
    for table in (left, right):
        if not isinstance(table, list):
            continue
        for item in table:
            if isinstance(item, str) and item not in seen:
                seen.append(item)
    overflow = len(seen) - SESSION_STARTED_KEYS_LIMIT
    if overflow > 0:
        seen = seen[overflow:]
    return seen


def _later_flush_at(left: Any, right: Any) -> Any:
    candidates = [
        item for item in (left, right) if isinstance(item, str) and item.strip()
    ]
    if not candidates:
        return None
    return max(candidates)


def _budget_allow(budget: Optional[CollectionBudget]) -> bool:
    return budget is None or not budget.exhausted()


def _budget_charge_file(
    budget: Optional[CollectionBudget],
    path: Path | str | None,
    *,
    cap: Optional[int] = None,
) -> None:
    """读取后按实际可得字节累计；文件不存在/不可 stat 则跳过。"""
    if budget is None or path is None:
        return
    try:
        size = int(Path(path).stat().st_size)
    except Exception:
        return
    if size <= 0:
        return
    if cap is not None:
        try:
            size = min(size, int(cap))
        except (TypeError, ValueError):
            pass
    budget.charge(size)


def _has_span_checkpoint(value: Any) -> bool:
    """进度水位是否存在（读到哪），与是否已发 span 无关。"""
    return isinstance(value, str) or isinstance(value, dict)


def _has_span_watermark(value: Any) -> bool:
    """D-48：该 agent 曾发出过 span（ok / partial / failed）。

    纯进度 checkpoint 可在未发 span 时存在。旧字符串水位、缺 ``span_emitted``
    的 dict（D-35/D-37 Claude ``{ended_at, offset}``、codex ``{ended_at, total}``）
    视为已发，避免升级后重放。
    """
    if isinstance(value, str):
        return True
    if isinstance(value, dict):
        if "span_emitted" in value:
            return bool(value.get("span_emitted"))
        return True
    return False


def _as_token_total(value: Any) -> Optional[dict]:
    """把六键 dict 或 raw total_token_usage 归一成六键；无法解析 → None。"""
    if not isinstance(value, dict):
        return None
    if any(
        key in value
        for key in (
            "cache_read_input_tokens",
            "thinking_tokens",
            "cache_creation_input_tokens",
        )
    ):
        return {key: _coerce_token(value.get(key)) for key in TOKEN_KEYS}
    if any(
        key in value
        for key in (
            "cached_input_tokens",
            "cache_write_input_tokens",
            "reasoning_output_tokens",
        )
    ):
        return TokenMapper.map_codex(value)
    if any(key in value for key in TOKEN_KEYS):
        return {key: _coerce_token(value.get(key)) for key in TOKEN_KEYS}
    nested = value.get("total_token_usage")
    if isinstance(nested, dict):
        return TokenMapper.map_codex(nested)
    return None


def _watermark_total(watermark: Any) -> Optional[dict]:
    """新水位 dict.total；旧 str / 缺 total → None（D-26 按首轮）。"""
    if not isinstance(watermark, dict):
        return None
    return _as_token_total(watermark.get("total"))


def _watermark_ended_at(watermark: Any) -> Optional[str]:
    if isinstance(watermark, str) and watermark:
        return watermark
    if isinstance(watermark, dict):
        ended = watermark.get("ended_at")
        if isinstance(ended, str) and ended:
            return ended
    return None


def _watermark_offset(watermark: Any) -> int:
    """Claude 水位 offset；旧字符串 / 缺字段 / 非法值 → 0（D-37）。"""
    if isinstance(watermark, dict):
        raw = watermark.get("offset")
        if isinstance(raw, bool) or raw is None:
            return 0
        if isinstance(raw, int):
            return max(raw, 0)
        if isinstance(raw, float) and raw.is_integer():
            return max(int(raw), 0)
        try:
            return max(int(raw), 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _claude_watermark_value(
    ended_at: Any, offset: Any, span_emitted: Any = False
) -> dict:
    ended = ended_at if isinstance(ended_at, str) else ""
    try:
        off = int(offset)
    except (TypeError, ValueError):
        off = 0
    if isinstance(offset, bool) or off < 0:
        off = 0
    return {
        "ended_at": ended,
        "offset": off,
        "span_emitted": bool(span_emitted),
    }


def _tokens_all_zero(tokens: Any) -> bool:
    if not isinstance(tokens, dict):
        return True
    return all(_coerce_token(tokens.get(key)) == 0 for key in TOKEN_KEYS)


def _filter_records_after_legacy_ended_at(
    records: list[dict[str, Any]], watermark: Any
) -> list[dict[str, Any]]:
    """旧字符串水位从 offset=0 重扫时，按 ended_at 滤已上报行（T-07 兼容）。"""
    if _watermark_offset(watermark) > 0:
        return records
    ended = _watermark_ended_at(watermark)
    if not ended:
        return records
    return [
        item
        for item in records
        if item.get("timestamp") and _timestamp_gt(str(item["timestamp"]), ended)
    ]


def _claude_skip_unchanged(
    watermark: Any,
    stored: Any,
    identity: Optional[tuple[int, ...]],
) -> bool:
    """未读到 EOF（offset < size）时不得走 size/mtime 快路径，否则 >20MB 会冻在前缀。

    D-48：未发过 span 时即使 offset 已达 size 也不得 skip——预算切片可能在 EOF
    停下却标 complete=False，下一趟需进函数发 failed（start==size → complete）。
    """
    if not _has_span_watermark(watermark):
        return False
    if identity is not None:
        try:
            size = int(identity[0])
        except (TypeError, ValueError, IndexError):
            size = -1
        if size >= 0 and _watermark_offset(watermark) < size:
            return False
    return _skip_unchanged_span_file(watermark, stored, identity)


def _claude_span_from_offset_file(
    reader: Callable[..., tuple[list[dict[str, Any]], int, bool]],
    path: Path,
    *,
    agent_id: str,
    last_seen: Any,
    budget: Optional[CollectionBudget],
    token_source: str,
    meta: dict[str, Any],
    dispatch: str,
    parent_session_id: Optional[str],
) -> tuple[Optional[AgentSpan], Optional[dict], bool]:
    """按 offset 续读一个 Claude jsonl。

    返回 (span, checkpoint, incomplete)：
    - checkpoint 非 None 才允许写水位/指纹（D-27）
    - incomplete=True 表示未到 EOF（单次 20MB 帽或 deadline）
    - 无完整行（new_offset 未前进）→ 不 checkpoint
    """
    offset = _watermark_offset(last_seen)
    emitted = _has_span_watermark(last_seen)
    try:
        records, new_offset, complete = reader(path, offset, budget)
    except Exception:
        return None, None, False
    if complete is _USAGE_READ_IO_FAILED:
        # D-42③ / D-47：stat/open/中途 I/O 读不到。host 保持 T-07 不发 span；
        # subagent 从未发过 span 才发 failed（D-48：纯进度 checkpoint 不算已发）。
        # D-49：发 failed 时写 span_emitted=True 水位，offset / ended_at 保持
        # 不变（不推进）。collect 保存该水位时 identity=None 不写指纹；identity
        # 存在时 _claude_skip_unchanged 因 offset < size 仍重试。下轮
        # emitted=True 抑制重复 failed。预算截断是 complete=False，不走本分支。
        if dispatch != "host" and not emitted:
            span = _failed_span(agent_id, token_source=token_source, meta=meta)
            span.dispatch = dispatch
            span.parent_session_id = parent_session_id
            checkpoint = _claude_watermark_value(
                _watermark_ended_at(last_seen) or "",
                offset,
                span_emitted=True,
            )
            return span, checkpoint, False
        return None, None, False
    records = _filter_records_after_legacy_ended_at(records, last_seen)
    checkpoint: Optional[dict] = None
    if complete or new_offset > offset:
        ended = ""
        if records:
            last_ts = records[-1].get("timestamp")
            ended = last_ts if isinstance(last_ts, str) else ""
        if not ended:
            ended = _watermark_ended_at(last_seen) or ""
        checkpoint = _claude_watermark_value(
            ended, new_offset, span_emitted=emitted
        )
    if not records:
        # D-48：末趟 complete 且全程未发 span 的 subagent，不因中途进度水位抑制 failed。
        if complete and not emitted and dispatch != "host":
            span = _failed_span(agent_id, token_source=token_source, meta=meta)
            span.dispatch = dispatch
            span.parent_session_id = parent_session_id
            if checkpoint is None:
                checkpoint = _claude_watermark_value(
                    "", new_offset, span_emitted=True
                )
            else:
                checkpoint = _claude_watermark_value(
                    checkpoint.get("ended_at"),
                    checkpoint.get("offset"),
                    span_emitted=True,
                )
            return span, checkpoint, False
        return None, checkpoint, not complete
    span = _span_from_records(
        agent_id=agent_id,
        records=records,
        token_source=token_source,
        meta=meta,
        truncated=not complete,
    )
    span.dispatch = dispatch
    span.parent_session_id = parent_session_id
    if not complete:
        span.collection_status = "partial"
    if emitted and _tokens_all_zero(span.tokens):
        return None, checkpoint, not complete
    if checkpoint is not None:
        checkpoint = _claude_watermark_value(
            checkpoint.get("ended_at"),
            checkpoint.get("offset"),
            span_emitted=True,
        )
    return span, checkpoint, not complete


def _codex_watermark_value(span: AgentSpan) -> dict:
    total = span.cursor_total
    if not isinstance(total, dict):
        total = span.tokens if isinstance(span.tokens, dict) else TokenMapper.zero()
    return {
        "ended_at": span.ended_at or "",
        "total": {key: _coerce_token(total.get(key)) for key in TOKEN_KEYS},
    }


def _mark_spans_partial(spans: list[AgentSpan]) -> None:
    for span in spans:
        if span.collection_status == "ok":
            span.collection_status = "partial"


def _watermark_workspace(marker: dict[str, Any], fallback: str) -> str:
    """D-51：水位键 workspace 分量取 marker 物理 workspace，不取消费方 feature workspace。

    真实 session-registry marker 恒有 workspace。测试夹具若未写该字段，回落 collector workspace。
    """
    raw = marker.get("workspace") if isinstance(marker, dict) else None
    if isinstance(raw, str) and raw.strip():
        path = Path(raw.strip()).expanduser()
        try:
            return str(path.resolve())
        except OSError:
            return os.path.normpath(os.path.abspath(str(path)))
    return fallback


def _watermark_key(workspace: str, agent_id: str) -> str:
    return json.dumps([workspace, agent_id], ensure_ascii=False, separators=(",", ":"))


def _alias_pre_d51_consumer_watermark(
    data: dict,
    physical_workspace: str,
    consumer_workspace: str,
    agent_id: str,
) -> tuple[str, bool]:
    """D-51 升级：物理键缺失且旧 (self.workspace, agent) 存在时别名水位 + file-stat。

    与 D-35 三元→二元正交：只搬二元键的 workspace 分量。不覆盖已有物理键。
    返回 (physical_key, aliased)。
    """
    physical_ws = (
        physical_workspace
        if isinstance(physical_workspace, str)
        else str(physical_workspace or "")
    )
    consumer_ws = (
        consumer_workspace
        if isinstance(consumer_workspace, str)
        else str(consumer_workspace or "")
    )
    agent = agent_id.strip() if isinstance(agent_id, str) else ""
    physical_key = _watermark_key(physical_ws, agent)
    if not isinstance(data, dict) or not agent:
        return physical_key, False
    spans = data.get(_CURSOR_SPANS)
    if not isinstance(spans, dict):
        return physical_key, False
    if physical_key in spans:
        return physical_key, False
    if not consumer_ws:
        return physical_key, False
    candidates: list[str] = []
    seen_ws: set[str] = set()
    for workspace in (consumer_ws,):
        if workspace and workspace not in seen_ws:
            seen_ws.add(workspace)
            candidates.append(_watermark_key(workspace, agent))
    try:
        normalized = normalize_workspace(consumer_ws)
    except Exception:
        normalized = None
    if isinstance(normalized, str) and normalized and normalized not in seen_ws:
        candidates.append(_watermark_key(normalized, agent))
    stats = data.get(_CURSOR_SPAN_STATS)
    has_stats = isinstance(stats, dict)
    for legacy_key in candidates:
        if legacy_key == physical_key or legacy_key not in spans:
            continue
        try:
            spans[physical_key] = copy.deepcopy(spans[legacy_key])
        except Exception:
            spans[physical_key] = spans[legacy_key]
        if has_stats and legacy_key in stats and physical_key not in stats:
            try:
                stats[physical_key] = copy.deepcopy(stats[legacy_key])
            except Exception:
                stats[physical_key] = stats[legacy_key]
        return physical_key, True
    return physical_key, False


def _warn_span_cursor_migration_failed() -> None:
    try:
        sys.stderr.write("warning: span cursor migration failed\n")
    except Exception:
        return


def _parse_span_cursor_key(key: str) -> Optional[tuple[str, str, int]]:
    """解析 spans 键：二元 (workspace, agent_id, 2) 或三元旧键 (workspace, agent_id, 3)。"""
    if not isinstance(key, str):
        return None
    try:
        parsed = json.loads(key)
    except Exception:
        return None
    if not isinstance(parsed, list):
        return None
    if len(parsed) == 2:
        workspace, agent_id = parsed
        if isinstance(workspace, str) and isinstance(agent_id, str):
            return (workspace, agent_id, 2)
        return None
    if len(parsed) == 3:
        workspace, _feature, agent_id = parsed
        if isinstance(workspace, str) and isinstance(agent_id, str):
            return (workspace, agent_id, 3)
        return None
    return None


def _legacy_ended_at_better(left: Any, right: Any) -> bool:
    left_at = _watermark_ended_at(left) or ""
    right_at = _watermark_ended_at(right) or ""
    if not left_at:
        return False
    if not right_at:
        return True
    return _timestamp_gt(left_at, right_at)


def _pick_legacy_watermark(items: list[tuple[str, Any]]) -> tuple[str, Any]:
    """同 (workspace, agent_id)：codex 取 total.total_tokens 最大者，否则取 ended_at 最大者。"""
    best_key, best_val = items[0]
    best_total = _watermark_total(best_val)
    # 单条也探测 ended_at，使该 agent 迁移失败可 warning 而非静默
    _watermark_ended_at(best_val)
    for key, value in items[1:]:
        cand_total = _watermark_total(value)
        _watermark_ended_at(value)
        if cand_total is not None or best_total is not None:
            if cand_total is None:
                continue
            if best_total is None:
                best_key, best_val, best_total = key, value, cand_total
                continue
            cand_n = _coerce_token(cand_total.get("total_tokens"))
            best_n = _coerce_token(best_total.get("total_tokens"))
            if cand_n > best_n or (
                cand_n == best_n and _legacy_ended_at_better(value, best_val)
            ):
                best_key, best_val, best_total = key, value, cand_total
            continue
        if _legacy_ended_at_better(value, best_val):
            best_key, best_val = key, value
    return best_key, best_val


def _migrate_legacy_watermarks_in_place(data: dict) -> None:
    """把 json[workspace, feature, agent_id] 归并为 json[workspace, agent_id]；旧键不保留。"""
    try:
        if not isinstance(data, dict):
            return
        spans = data.get(_CURSOR_SPANS)
        if not isinstance(spans, dict):
            return
        stats = data.get(_CURSOR_SPAN_STATS)
        has_stats = isinstance(stats, dict)
        if not has_stats:
            stats = {}
        groups: dict[tuple[str, str], list[tuple[str, Any, int]]] = {}
        for key, value in list(spans.items()):
            parsed = _parse_span_cursor_key(key)
            if parsed is None:
                continue
            workspace, agent_id, arity = parsed
            groups.setdefault((workspace, agent_id), []).append((key, value, arity))
        changed_stats = False
        for (workspace, agent_id), items in groups.items():
            if not any(arity == 3 for _key, _value, arity in items):
                continue
            try:
                winner_key, winner_val = _pick_legacy_watermark(
                    [(key, value) for key, value, _arity in items]
                )
                new_key = _watermark_key(workspace, agent_id)
                spans[new_key] = winner_val
                if winner_key in stats:
                    stats[new_key] = stats[winner_key]
                    changed_stats = True
                elif new_key not in stats:
                    for key, _value, _arity in items:
                        if key in stats:
                            stats[new_key] = stats[key]
                            changed_stats = True
                            break
                for key, _value, _arity in items:
                    if key != new_key:
                        spans.pop(key, None)
                        if key in stats:
                            stats.pop(key, None)
                            changed_stats = True
            except Exception:
                _warn_span_cursor_migration_failed()
                had_binary = any(arity == 2 for _key, _value, arity in items)
                for key, _value, arity in items:
                    if arity == 3:
                        spans.pop(key, None)
                        if key in stats:
                            stats.pop(key, None)
                            changed_stats = True
                if not had_binary:
                    new_key = _watermark_key(workspace, agent_id)
                    spans.pop(new_key, None)
                    if new_key in stats:
                        stats.pop(new_key, None)
                        changed_stats = True
        if changed_stats:
            data[_CURSOR_SPAN_STATS] = stats
    except Exception:
        _warn_span_cursor_migration_failed()


def _file_stat_identity(path: Path) -> Optional[tuple[int, int]]:
    """返回 (size, mtime_ns)；不可 stat 时 None。"""
    try:
        st = path.stat()
        mtime_ns = getattr(st, "st_mtime_ns", None)
        if not isinstance(mtime_ns, int):
            mtime_ns = int(st.st_mtime * 1_000_000_000)
        return (int(st.st_size), int(mtime_ns))
    except Exception:
        return None


def _iter_host_dir_record_files(path: Path) -> list[Path]:
    """`_collect_records_from_host_path` 与目录指纹共用的文件枚举，避免类型漂移。

    listing / is_file 异常向上抛，由调用方按各自 fail-safe 处理。
    """
    files = [
        item
        for item in path.rglob("*")
        if item.is_file() and item.suffix in _HOST_DIR_RECORD_SUFFIXES
    ]
    files.sort(key=lambda item: str(item))
    return files


def _dir_jsonl_stat_identity(path: Path) -> Optional[tuple[int, int, int]]:
    """目录源：递归 jsonl+json 的 (count, Σsize, max mtime_ns)；listing 失败则 None。

    文件集合与 `_collect_records_from_host_path` 共用 `_iter_host_dir_record_files`。
    """
    try:
        count = 0
        total_size = 0
        max_mtime_ns = 0
        for item in _iter_host_dir_record_files(path):
            ident = _file_stat_identity(item)
            if ident is None:
                continue
            count += 1
            total_size += ident[0]
            if ident[1] > max_mtime_ns:
                max_mtime_ns = ident[1]
        return (count, total_size, max_mtime_ns)
    except Exception:
        return None


def _span_source_stat_identity(
    path: Path,
) -> Optional[tuple[int, int] | tuple[int, int, int]]:
    """文件：(size, mtime_ns)；目录：递归 jsonl+json 的 (count, Σsize, max mtime_ns)。"""
    try:
        if path.is_dir():
            return _dir_jsonl_stat_identity(path)
        return _file_stat_identity(path)
    except Exception:
        return None


def _file_stat_unchanged(stored: Any, identity: Optional[tuple[int, ...]]) -> bool:
    if identity is None or not isinstance(stored, dict):
        return False
    if len(identity) >= 3:
        return (
            stored.get("count") == identity[0]
            and stored.get("size") == identity[1]
            and stored.get("mtime_ns") == identity[2]
        )
    return stored.get("size") == identity[0] and stored.get("mtime_ns") == identity[1]


def _skip_unchanged_span_file(
    watermark: Any,
    stored: Any,
    identity: Optional[tuple[int, ...]],
    *,
    require_total: bool = False,
) -> bool:
    """有水位且 size+mtime 未变（或文件已消失）则跳过读取且不计预算。

    目录源比较 `_iter_host_dir_record_files` 的 (count, Σsize, max mtime)，不看目录 inode。
    require_total=True 用于 codex 父 rollout：旧 str 水位仍需再读（D-26 ① 首轮）。
    """
    if require_total:
        if not isinstance(watermark, dict) or _watermark_total(watermark) is None:
            return False
    elif not _has_span_checkpoint(watermark):
        # D-48：size/mtime 快路径看进度水位是否存在，不要求已发 span。
        return False
    if identity is None:
        return True
    return _file_stat_unchanged(stored, identity)


def _claude_store_span_file_stat_after_checkpoint(
    stats_table: dict,
    key: str,
    identity: Optional[tuple[int, ...]],
    checkpoint: Any,
    span: Optional[AgentSpan],
) -> None:
    """写 span_file_stats。D-49：I/O failed 仅标记 emitted、offset 未达 size 时不写指纹。"""
    if identity is None:
        return
    if span is not None and span.collection_status == "failed":
        try:
            size = int(identity[0])
        except (TypeError, ValueError, IndexError):
            size = -1
        if size >= 0 and _watermark_offset(checkpoint) < size:
            return
    _store_span_file_stat(stats_table, key, identity)


def _store_span_file_stat(
    stats_table: dict, key: str, identity: Optional[tuple[int, ...]]
) -> None:
    if identity is None:
        return
    if len(identity) >= 3:
        stats_table[key] = {
            "count": identity[0],
            "size": identity[1],
            "mtime_ns": identity[2],
        }
        return
    stats_table[key] = {"size": identity[0], "mtime_ns": identity[1]}


def _cursor_stats_table(data: dict) -> dict:
    stats = data.get(_CURSOR_SPAN_STATS)
    if not isinstance(stats, dict):
        stats = {}
        data[_CURSOR_SPAN_STATS] = stats
    return stats


def _dispatch_seq_key(workspace: str, feature: Optional[str]) -> str:
    return json.dumps([workspace, feature], ensure_ascii=False, separators=(",", ":"))


def _failed_usage_summary() -> UsageSummary:
    return UsageSummary(token_source=TOKEN_SOURCE_NONE, collection_status="failed")


def _five_key_sum(tokens: dict) -> int:
    return (
        _coerce_token(tokens.get("input_tokens"))
        + _coerce_token(tokens.get("output_tokens"))
        + _coerce_token(tokens.get("cache_creation_input_tokens"))
        + _coerce_token(tokens.get("cache_read_input_tokens"))
        + _coerce_token(tokens.get("thinking_tokens"))
    )


def _direct_six_key(usage: dict) -> dict:
    src = usage if isinstance(usage, dict) else {}
    mapped = {
        "input_tokens": _coerce_token(src.get("input_tokens")),
        "output_tokens": _coerce_token(src.get("output_tokens")),
        "cache_creation_input_tokens": _coerce_token(src.get("cache_creation_input_tokens")),
        "cache_read_input_tokens": _coerce_token(src.get("cache_read_input_tokens")),
        "thinking_tokens": _thinking_tokens(src),
        "total_tokens": 0,
    }
    mapped["total_tokens"] = _five_key_sum(mapped)
    return mapped


def _optional_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _optional_turns(value: Any) -> Optional[int]:
    turns = _optional_int(value)
    if turns is None:
        return None
    return max(turns, 0)


def _coerce_cost(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    parsed: Optional[float] = None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed is None or not math.isfinite(parsed):
        return None
    return max(parsed, 0.0)


def _total_mismatch_warnings(source_total: Any, tokens: Optional[dict]) -> list[str]:
    if source_total is None:
        return []
    computed = _five_key_sum(tokens) if isinstance(tokens, dict) else 0
    src = _coerce_token(source_total)
    if src != computed:
        return [f"total_tokens mismatch: source={src} computed={computed}"]
    return []


def attach_usage_warnings(span: AgentSpan, summary: UsageSummary) -> AgentSpan:
    """把 UsageSummary.warnings 写入 AgentSpan.notes（不进 payload）。"""
    try:
        raw = summary.warnings if isinstance(getattr(summary, "warnings", None), list) else []
        span.notes = [item for item in raw if isinstance(item, str) and item]
    except Exception:
        span.notes = []
    return span


def span_from_usage_summary(agent_id: str, summary: UsageSummary) -> AgentSpan:
    """UsageJsonParser → AgentSpan 生产路径：对账 warnings 写入 notes（不进 payload）。"""
    try:
        span = AgentSpan(
            agent_id=agent_id,
            model=summary.model,
            message_count=summary.turns,
            tokens=summary.tokens,
            token_source=summary.token_source,
            collection_status=summary.collection_status,
            cost_usd=summary.cost_usd,
        )
        return attach_usage_warnings(span, summary)
    except Exception:
        fallback_id = agent_id if isinstance(agent_id, str) and agent_id else "unknown"
        fallback = _failed_span(fallback_id, token_source=TOKEN_SOURCE_NONE)
        try:
            return attach_usage_warnings(fallback, summary)
        except Exception:
            return fallback


def _grok_cost_usd(obj: dict, model_usage: dict) -> tuple[Optional[float], list[str]]:
    """cost_usd：仅顶层缺失才 Σ costUSD；存在但非法 → null + warning（D-21 ⑤）。"""
    raw_total = obj.get("total_cost_usd") if isinstance(obj, dict) else None
    if raw_total is not None:
        cost = _coerce_cost(raw_total)
        if cost is None:
            return None, ["cost_usd: invalid total_cost_usd"]
        return cost, []
    return _sum_model_cost_usd_with_warnings(model_usage)


def _sum_model_cost_usd(model_usage: dict) -> Optional[float]:
    cost, _warnings = _sum_model_cost_usd_with_warnings(model_usage)
    return cost


def _sum_model_cost_usd_with_warnings(model_usage: dict) -> tuple[Optional[float], list[str]]:
    if not isinstance(model_usage, dict):
        return None, []
    total = 0.0
    saw = False
    for usage in model_usage.values():
        if not isinstance(usage, dict):
            continue
        raw = usage.get("costUSD")
        if raw is None:
            continue
        cost = _coerce_cost(raw)
        if cost is None:
            return None, ["cost_usd: invalid costUSD"]
        total += cost
        saw = True
    if not saw or not math.isfinite(total):
        return None, []
    return total, []


def _has_usage_json_hint(obj: dict) -> bool:
    if any(key in obj for key in _USAGE_JSON_HINT_KEYS):
        return True
    info = obj.get("info")
    if isinstance(info, dict) and "total_token_usage" in info:
        return True
    payload = obj.get("payload")
    if isinstance(payload, dict):
        if any(key in payload for key in _USAGE_JSON_HINT_KEYS):
            return True
        nested = payload.get("info")
        if isinstance(nested, dict) and "total_token_usage" in nested:
            return True
    return False


def _extract_codex_usage(obj: dict) -> Optional[dict]:
    if not isinstance(obj, dict):
        return None
    top = obj.get("total_token_usage")
    if isinstance(top, dict):
        return top
    info = obj.get("info")
    if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
        return info["total_token_usage"]
    payload = obj.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("info")
        if isinstance(nested, dict) and isinstance(nested.get("total_token_usage"), dict):
            return nested["total_token_usage"]
        usage = payload.get("usage")
        if isinstance(usage, dict) and any(key in usage for key in _CODEX_NAMED_KEYS):
            return usage
    usage = obj.get("usage")
    if isinstance(usage, dict) and any(key in usage for key in _CODEX_NAMED_KEYS):
        return usage
    return None


def _role_from_source(source: Any, thread_source: Any) -> Optional[str]:
    if isinstance(source, dict):
        subagent = source.get("subagent")
        if isinstance(subagent, str) and subagent:
            return subagent
        if isinstance(subagent, dict):
            spawn = subagent.get("thread_spawn")
            if isinstance(spawn, dict):
                role = spawn.get("agent_role")
                if isinstance(role, str) and role:
                    return role
            if isinstance(thread_source, str) and thread_source:
                return thread_source
        return None
    if isinstance(thread_source, str) and thread_source and thread_source != "user":
        return thread_source
    return None


def _read_first_jsonl_object(
    path: Path, budget: Optional[CollectionBudget] = None
) -> Optional[dict[str, Any]]:
    """发现阶段只读首个非空 JSONL 对象（session_meta），不扫全文件。"""
    if not _budget_allow(budget):
        return None
    try:
        with path.open("rb") as handle:
            consumed = 0
            while consumed < MAX_SESSION_META_HEADER_BYTES:
                remaining = MAX_SESSION_META_HEADER_BYTES - consumed
                line = handle.readline(remaining)
                if not line:
                    break
                consumed += len(line)
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped.decode("utf-8", errors="replace"))
                except (TypeError, ValueError):
                    if budget is not None and consumed:
                        budget.charge(consumed)
                    return None
                if budget is not None and consumed:
                    budget.charge(consumed)
                return obj if isinstance(obj, dict) else None
        if budget is not None and consumed:
            budget.charge(consumed)
        return None
    except Exception:
        return None


def _rollout_meta_from_session_obj(obj: Any, path: Path) -> Optional[RolloutMeta]:
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    rollout_id = payload.get("id") or payload.get("session_id")
    if not isinstance(rollout_id, str) or not rollout_id:
        return None
    parent_thread_id = payload.get("parent_thread_id")
    if not isinstance(parent_thread_id, str) or not parent_thread_id:
        parent_thread_id = None
    originator = payload.get("originator")
    if not isinstance(originator, str) or not originator:
        originator = None
    timestamp = obj.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        timestamp = None
    return RolloutMeta(
        rollout_id=rollout_id,
        path=path,
        parent_thread_id=parent_thread_id,
        role=_role_from_source(payload.get("source"), payload.get("thread_source")),
        originator=originator,
        model=None,
        timestamp=timestamp,
    )


def _first_turn_context_model(
    path: Path, budget: Optional[CollectionBudget] = None
) -> Optional[str]:
    """只读到首条 turn_context（不扫满 20MB 前缀）。

    读取前 `_budget_allow`；按实际读取字节 `charge`。无早期 turn_context 时
    可扫到 MAX_FILE_BYTES，但不得绕过 CollectionBudget（耗尽即停，
    model=None 不视为失败）。
    """
    if not _budget_allow(budget):
        return None
    try:
        consumed = 0
        with path.open("rb") as handle:
            while consumed < MAX_FILE_BYTES:
                if not _budget_allow(budget):
                    break
                if budget is not None:
                    try:
                        remaining_budget = int(budget.remaining_bytes)
                    except (TypeError, ValueError):
                        remaining_budget = 0
                    if remaining_budget <= 0:
                        break
                    line = handle.readline(remaining_budget)
                else:
                    line = handle.readline()
                if not line:
                    break
                n = len(line)
                consumed += n
                if budget is not None:
                    budget.charge(n)
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped.decode("utf-8", errors="replace"))
                except (TypeError, ValueError):
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "turn_context":
                    continue
                payload = obj.get("payload")
                if isinstance(payload, dict):
                    raw_model = payload.get("model")
                    if isinstance(raw_model, str) and raw_model:
                        return raw_model
        return None
    except Exception:
        return None


def _datetime_from_rollout_path(path: Path) -> Optional[datetime]:
    try:
        name = path.name
    except Exception:
        return None
    prefix = "rollout-"
    suffix = ".jsonl"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    stamp = name[len(prefix) : -len(suffix)]
    if "T" not in stamp:
        return None
    date_part, rest = stamp.split("T", 1)
    parts = rest.split("-")
    if len(parts) < 3:
        return None
    hour, minute, second = parts[0], parts[1], parts[2]
    if not (hour.isdigit() and minute.isdigit() and second.isdigit()):
        return None
    return _parse_dt(f"{date_part}T{hour}:{minute}:{second}Z")


def _child_sort_key(child: RolloutMeta, parent_meta: RolloutMeta) -> tuple:
    try:
        same_day = 0 if child.path.parent == parent_meta.path.parent else 1
    except Exception:
        same_day = 1
    parent_dt = _parse_dt(parent_meta.timestamp) or _datetime_from_rollout_path(parent_meta.path)
    child_dt = _parse_dt(child.timestamp) or _datetime_from_rollout_path(child.path)
    dist = float("inf")
    if parent_dt is not None and child_dt is not None:
        try:
            dist = abs((child_dt - parent_dt).total_seconds())
        except TypeError:
            dist = float("inf")
    try:
        name = child.path.name
    except Exception:
        name = ""
    return (same_day, dist, name)


def _candidate_paths_for_parent(parent_meta: RolloutMeta) -> list[Path]:
    """列出 ±1 天候选 rollout，按与父时间戳距离排序（KI-5：先搜父附近，防文件名序饿死）。"""
    parent_path = parent_meta.path
    try:
        parent_resolved = parent_path.resolve()
    except Exception:
        parent_resolved = parent_path
    candidates: list[Path] = []
    for day_dir in _adjacent_day_dirs(parent_path):
        for candidate in _iter_rollout_jsonl(day_dir):
            try:
                resolved = candidate.resolve()
            except Exception:
                resolved = candidate
            if resolved == parent_resolved:
                continue
            candidates.append(candidate)

    def sort_key(path: Path) -> tuple:
        try:
            same_day = 0 if path.parent == parent_path.parent else 1
        except Exception:
            same_day = 1
        parent_dt = _parse_dt(parent_meta.timestamp) or _datetime_from_rollout_path(
            parent_path
        )
        child_dt = _datetime_from_rollout_path(path)
        dist = float("inf")
        if parent_dt is not None and child_dt is not None:
            try:
                dist = abs((child_dt - parent_dt).total_seconds())
            except TypeError:
                dist = float("inf")
        try:
            name = path.name
        except Exception:
            name = ""
        return (same_day, dist, name)

    candidates.sort(key=sort_key)
    return candidates


def _load_discovery_resume(
    cursor: Optional["SpanCursorStore"], parent_key: Optional[str]
) -> Optional[str]:
    if cursor is None or not parent_key:
        return None
    try:
        data = cursor.load() if callable(getattr(cursor, "load", None)) else {}
        table = data.get(_CURSOR_CODEX_DISCOVERY) if isinstance(data, dict) else None
        if not isinstance(table, dict):
            return None
        value = table.get(parent_key)
        return value if isinstance(value, str) and value else None
    except Exception:
        return None


def _store_discovery_resume(
    cursor: Optional["SpanCursorStore"],
    parent_key: Optional[str],
    last_path: Optional[str],
    *,
    done: bool,
) -> None:
    if cursor is None or not parent_key:
        return

    def mutate(data: dict) -> dict:
        table = data.get(_CURSOR_CODEX_DISCOVERY)
        if not isinstance(table, dict):
            table = {}
        if done or not last_path:
            table.pop(parent_key, None)
        else:
            table[parent_key] = last_path
        data[_CURSOR_CODEX_DISCOVERY] = table
        return data

    try:
        updater = getattr(cursor, "update", None)
        if callable(updater):
            updater(mutate)
            return
        data = cursor.load() if callable(getattr(cursor, "load", None)) else {}
        if not isinstance(data, dict):
            data = {}
        mutate(data)
        saver = getattr(cursor, "save", None)
        if callable(saver):
            saver(data)
    except Exception:
        return


def _adjacent_day_dirs(path: Path) -> list[Path]:
    day_dir = path.parent
    parts = day_dir.parts
    if len(parts) < 3:
        return [day_dir]
    try:
        year = int(parts[-3])
        month = int(parts[-2])
        day = int(parts[-1])
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return [day_dir]
        base = datetime(year, month, day)
    except ValueError:
        return [day_dir]
    sessions_root = day_dir.parent.parent.parent
    dirs: list[Path] = []
    # D-21：父同日目录优先，再 ±1 天
    for offset in (0, -1, 1):
        current = base + timedelta(days=offset)
        dirs.append(
            sessions_root
            / f"{current.year:04d}"
            / f"{current.month:02d}"
            / f"{current.day:02d}"
        )
    return dirs


def _iter_rollout_jsonl(day_dir: Path) -> list[Path]:
    files: list[Path] = []
    try:
        if not day_dir.is_dir():
            return []
        for item in day_dir.iterdir():
            if item.is_file() and item.name.startswith("rollout-") and item.suffix == ".jsonl":
                files.append(item)
    except Exception:
        return []
    files.sort(key=lambda item: item.name)
    return files


def _jsonl_timestamps(path: Path) -> list[str]:
    stamps: list[str] = []
    try:
        text, _truncated = _read_capped_text(path)
    except Exception:
        return stamps
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        timestamp = obj.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            stamps.append(timestamp)
    return stamps


def _resolve_subagents_dir(marker: dict[str, Any]) -> Optional[Path]:
    transcript = marker.get("transcript_path")
    session_id = marker.get("session_id")
    candidates: list[Path] = []
    if isinstance(transcript, str) and transcript.strip():
        tpath = Path(transcript).expanduser()
        candidates.append(tpath.parent / tpath.stem / "subagents")
        candidates.append(tpath.parent / "subagents")
        if tpath.is_dir():
            candidates.append(tpath / "subagents")
    if isinstance(session_id, str) and session_id.strip() and isinstance(transcript, str) and transcript.strip():
        candidates.append(Path(transcript).expanduser().parent / session_id / "subagents")
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except Exception:
            continue
    return None


def _list_subagent_jsonl(subagents: Path) -> list[Path]:
    files: list[Path] = []
    try:
        for path in subagents.iterdir():
            if path.is_file() and path.suffix == ".jsonl":
                files.append(path)
    except Exception:
        return []
    files.sort(key=lambda item: item.name)
    return files


def _read_meta(jsonl_path: Path) -> dict[str, Any]:
    """只取 agentType / spawnDepth / toolUseId，绝不读 description（D-2）。"""
    meta_path = jsonl_path.with_name(jsonl_path.stem + ".meta.json")
    result: dict[str, Any] = {
        "agent_type": None,
        "spawn_depth": None,
        "tool_use_id": None,
    }
    try:
        if not meta_path.is_file():
            return result
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return result
        agent_type = data.get("agentType")
        if isinstance(agent_type, str):
            result["agent_type"] = agent_type
        spawn_depth = data.get("spawnDepth")
        if isinstance(spawn_depth, int) and not isinstance(spawn_depth, bool):
            result["spawn_depth"] = spawn_depth
        tool_use_id = data.get("toolUseId")
        if isinstance(tool_use_id, str):
            result["tool_use_id"] = tool_use_id
    except Exception:
        return result
    return result


def _token_count_record_from_obj(obj: Any) -> Optional[dict]:
    """从 jsonl 对象抽出 token_count 记录；形态不符 → None。"""
    if not isinstance(obj, dict):
        return None
    payload = obj.get("payload")
    if obj.get("type") != "event_msg" or not isinstance(payload, dict):
        return None
    if payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    usage = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(usage, dict):
        return None
    timestamp = obj.get("timestamp")
    if not isinstance(timestamp, str):
        timestamp = ""
    return {"timestamp": timestamp, "total_token_usage": usage, "info": info}


def _last_token_count_in_bytes(raw: bytes, *, skip_partial_head: bool) -> Optional[dict]:
    """解析一段尾窗字节中的最后一条完整 token_count。"""
    if skip_partial_head:
        newline = raw.find(b"\n")
        if newline < 0:
            return None
        raw = raw[newline + 1 :]
    last: Optional[dict] = None
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        record = _token_count_record_from_obj(obj)
        if record is not None:
            last = record
    return last


def _last_token_count_from_tail(path: Path) -> tuple[Optional[dict], bool]:
    """从文件末尾向前按块扫描最后一条完整 token_count（D-25）。

    返回 (record, window_exhausted_without_hit)。命中则第二项为 False；
    尾窗（≤2MB）扫尽仍无完整记录则为 (None, True)，调用方退回前缀读。
    """
    size = path.stat().st_size
    if size <= 0:
        return None, False
    limit = min(size, MAX_TAIL_WINDOW_BYTES)
    with path.open("rb") as handle:
        scanned = 0
        buf = b""
        while scanned < limit:
            take = min(TAIL_SCAN_CHUNK_BYTES, limit - scanned)
            offset = size - scanned - take
            handle.seek(offset)
            piece = handle.read(take)
            if not piece:
                break
            buf = piece + buf
            scanned += len(piece)
            record = _last_token_count_in_bytes(buf, skip_partial_head=(offset > 0))
            if record is not None:
                return record, False
    return None, size > MAX_TAIL_WINDOW_BYTES


def _merge_tail_last_record(records: list[dict], tail_record: dict) -> list[dict]:
    """把尾读末条接到 records 末尾；已是同一条则不重复。"""
    if not records:
        return [tail_record]
    last = records[-1]
    if (
        isinstance(last, dict)
        and last.get("timestamp") == tail_record.get("timestamp")
        and last.get("total_token_usage") == tail_record.get("total_token_usage")
    ):
        return records
    return list(records) + [tail_record]


def _read_capped_text(path: Path) -> tuple[str, bool]:
    """Prefix-read at most MAX_FILE_BYTES. Flush / D-25 fallback; do not change to tail."""
    size = path.stat().st_size
    truncated = size > MAX_FILE_BYTES
    with path.open("rb") as handle:
        raw = handle.read(MAX_FILE_BYTES)
    return raw.decode("utf-8", errors="replace"), truncated


def _usage_json_truncated_warning(cap: Optional[int] = None) -> str:
    n = MAX_FILE_BYTES if cap is None else cap
    return f"usage-json truncated to tail {n} bytes"


def _read_capped_tail_text(path: Path, *, cap: Optional[int] = None) -> tuple[str, bool]:
    """Read a file with a tail window of at most MAX_FILE_BYTES (D-33).

    Oversized files seek to size-cap and drop the first incomplete line so
    D-32 forward scan can still see a trailing usage object. Size <= cap
    reads the whole file. `_read_capped_text` stays prefix-read.
    """
    limit = MAX_FILE_BYTES if cap is None else cap
    size = path.stat().st_size
    truncated = size > limit
    with path.open("rb") as handle:
        if truncated:
            handle.seek(size - limit)
        raw = handle.read(limit if limit > 0 else 0)
    if truncated:
        newline = raw.find(b"\n")
        if newline >= 0:
            raw = raw[newline + 1 :]
    return raw.decode("utf-8", errors="replace"), truncated


def _read_tail_window_text(path: Path, max_bytes: int = MAX_TAIL_WINDOW_BYTES) -> tuple[str, bool]:
    """读取文件末尾 max_bytes（默认 2MB）文本；返回 (text, is_truncated)；跳过首个截断行。"""
    return _read_capped_tail_text(path, cap=max_bytes)


def _read_capped_stream_text(
    stream: Any,
    *,
    cap: Optional[int] = None,
    chunk_size: Optional[int] = None,
) -> tuple[str, bool]:
    """Read a stream until MAX_FILE_BYTES then stop (D-33 stdin `-`).

    Always passes a size to ``read`` so an unbounded stdin cannot hang or
    OOM. May consume up to cap + one chunk to detect that more data remains;
    does not drain the rest of the stream.
    """
    limit = MAX_FILE_BYTES if cap is None else cap
    step = USAGE_JSON_STREAM_CHUNK_BYTES if chunk_size is None else chunk_size
    if step <= 0:
        step = 1
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while total < limit:
        take = min(step, limit - total)
        piece = stream.read(take)
        if not piece:
            break
        if isinstance(piece, bytes):
            encoded = piece
        elif isinstance(piece, bytearray):
            encoded = bytes(piece)
        elif isinstance(piece, str):
            encoded = piece.encode("utf-8")
        else:
            encoded = bytes(piece)
        remain = limit - total
        if len(encoded) > remain:
            chunks.append(encoded[:remain])
            total = limit
            truncated = True
            extra = stream.read(step)
            if extra:
                truncated = True
            break
        chunks.append(encoded)
        total += len(encoded)
        if total >= limit:
            extra = stream.read(step)
            if extra:
                truncated = True
            break
    return b"".join(chunks).decode("utf-8", errors="replace"), truncated


def _read_usage_records_from_offset(
    path: Path | str,
    offset: Any,
    budget: Optional[CollectionBudget] = None,
    *,
    require_assistant: bool = True,
    max_bytes: Optional[int] = MAX_FILE_BYTES,
) -> tuple[list[dict[str, Any]], int, Any]:
    """从字节 offset 流式解析 Claude NDJSON usage（D-37）。

    返回 (records, new_offset, complete)。new_offset 为最后一条完整行之后的位置。
    complete=True 仅当读到 EOF。单次 max_bytes（默认 20MB）或 CollectionBudget
    耗尽 → complete=False，new_offset 停在最后完整行（不把半行算进去）。
    D-43：当前行本身已 ≥ max_bytes 且无换行时有界前向扫描；仅当该行以
    换行结尾才跳过并推进。D-44：扫到 EOF 仍无换行 → 不完整末行，恢复
    new_offset 为扫描起点（最后完整行之后），complete=False。D-42①：EOF
    残行仅 json.loads 成功（完整对象，可缺尾换行）才推进并 complete；半写
    失败不推进、complete=False。D-42②：读满 max_bytes 且已在 EOF（无残行）
    → complete=True。D-42③ / D-47：stat/open 失败及 open 后中途 I/O 失败
    complete=_USAGE_READ_IO_FAILED，与预算截断 complete=False 区分。
    json.loads 失败仍是半写行（complete=False，不推进），不是 I/O 失败。
    D-46：入口 _budget_allow 放行后首块可读到 remaining 为负；其后每块及
    超大行扫描按 exhausted() 停。
    """
    try:
        start = int(offset)
    except (TypeError, ValueError):
        start = 0
    if isinstance(offset, bool) or start < 0:
        start = 0
    if not _budget_allow(budget):
        return [], start, False
    try:
        file_path = Path(path)
        size = file_path.stat().st_size
    except Exception:
        return [], start, _USAGE_READ_IO_FAILED
    if start > size:
        start = size
    if start == size:
        return [], start, True
    records: list[dict[str, Any]] = []
    new_offset = start
    consumed = 0
    buf = b""
    chunk = USAGE_JSON_STREAM_CHUNK_BYTES if USAGE_JSON_STREAM_CHUNK_BYTES > 0 else 65536
    complete = False
    try:
        handle = file_path.open("rb")
    except Exception:
        return [], start, _USAGE_READ_IO_FAILED
    try:
        with handle:
            if start > 0:
                handle.seek(max(start - 1, 0))
                prev = handle.read(1)
                if prev != b"\n":
                    skipped = handle.readline()
                    if budget is not None:
                        budget.charge(len(skipped) + len(prev))
                    consumed += len(skipped) + len(prev)
                    start = handle.tell()
                    new_offset = start
                else:
                    handle.seek(start)
            else:
                handle.seek(0)
            abs_buf = start
            first_chunk_done = False
            while True:
                # D-24/D-26：文件一旦获准开始读，首块允许 remaining 扣成负（首会话
                # 不饿死）。D-46：首块之后每块读取前 exhausted()（含字节与 deadline）。
                if budget is not None:
                    if first_chunk_done:
                        if budget.exhausted():
                            complete = False
                            break
                    else:
                        getter = (
                            budget.clock if budget.clock is not None else time.monotonic
                        )
                        try:
                            if getter() >= budget.deadline:
                                complete = False
                                break
                        except Exception:
                            pass
                take = chunk
                if max_bytes is not None:
                    left = max_bytes - consumed
                    if left <= 0:
                        # D-43：仅当「当前行本身」已吃满 max_bytes 且仍无换行才
                        # 有界前向扫描（常数内存丢弃内容）。D-44：只有扫到换行
                        # （完整超大行）才推进 offset；EOF 仍无换行视为不完整末行，
                        # 恢复到扫描起点（最后完整行之后）。buf 为空走 D-42② EOF
                        # 探测，避免 offset 已在文件尾却标 partial 永久卡。
                        if buf and buf.find(b"\n") < 0 and len(buf) >= max_bytes:
                            scan_base = new_offset
                            abs_buf += len(buf)
                            buf = b""
                            saw_newline = False
                            while True:
                                # 进入扫描时已过主循环首块；D-46 按 exhausted() 停。
                                if budget is not None and budget.exhausted():
                                    break
                                piece2 = handle.read(chunk)
                                if not piece2:
                                    break
                                if budget is not None:
                                    budget.charge(len(piece2))
                                nl2 = piece2.find(b"\n")
                                if nl2 < 0:
                                    abs_buf += len(piece2)
                                    continue
                                abs_buf += nl2 + 1
                                new_offset = abs_buf
                                buf = piece2[nl2 + 1 :]
                                saw_newline = True
                                break
                            if not saw_newline:
                                new_offset = scan_base
                                complete = False
                            elif not buf:
                                extra = handle.read(1)
                                complete = not extra
                            else:
                                complete = False
                            break
                        if not buf:
                            extra = handle.read(1)
                            complete = not extra
                            break
                        complete = False
                        break
                    take = min(take, left)
                if take <= 0:
                    complete = False
                    break
                piece = handle.read(take)
                if not piece:
                    if buf.strip():
                        line = buf.decode("utf-8", errors="replace").strip()
                        try:
                            obj = json.loads(line)
                        except (TypeError, ValueError):
                            # D-42① / D-44：半写残行不推进 offset、不标 complete。
                            complete = False
                        else:
                            # json.loads 成功（含非 usage / JSON null）视为完整行。
                            record = _usage_record_from_object(
                                obj, require_assistant=require_assistant
                            )
                            if record is not None:
                                records.append(record)
                            abs_buf += len(buf)
                            new_offset = abs_buf
                            complete = True
                    else:
                        complete = True
                    break
                if budget is not None:
                    budget.charge(len(piece))
                first_chunk_done = True
                consumed += len(piece)
                buf += piece
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    raw_line = buf[:nl]
                    line_len = nl + 1
                    buf = buf[nl + 1 :]
                    abs_buf += line_len
                    new_offset = abs_buf
                    stripped = raw_line.decode("utf-8", errors="replace").strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except (TypeError, ValueError):
                        continue
                    record = _usage_record_from_object(
                        obj, require_assistant=require_assistant
                    )
                    if record is not None:
                        records.append(record)
    except OSError:
        # D-47：open 已成功后的 seek/read/close I/O 与 stat/open 同哨兵。
        # json.loads 只捕 TypeError/ValueError，不会落到这里。
        return [], start, _USAGE_READ_IO_FAILED
    except Exception:
        return [], start, False
    if new_offset < start:
        new_offset = start
    return records, new_offset, complete


def _extract_usage_records(
    path: Path,
    *,
    require_assistant: bool,
    budget: Optional[CollectionBudget] = None,
    record_extractor: Optional[Callable[[Any], Optional[dict[str, Any]]]] = None,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """返回 (records, truncated, unreadable)。unreadable=文件无法得到任何 JSON 对象。

    record_extractor 缺省为 `_usage_record_from_object`（claude/通用平铺形态）；
    kimi/pi 等宿主传本校准提取器，返回 {"timestamp", "usage"(六键 snake_case), "model"}。
    """
    if not _budget_allow(budget):
        return [], True, False
    try:
        text, truncated = _read_capped_text(path)
    except Exception:
        return [], False, True
    if record_extractor is None:
        def extract(obj: Any) -> Optional[dict[str, Any]]:
            return _usage_record_from_object(obj, require_assistant=require_assistant)
    else:
        extract = record_extractor
    records: list[dict[str, Any]] = []
    parsed_any = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        parsed_any = True
        record = extract(obj)
        if record is not None:
            records.append(record)
    unreadable = (not parsed_any) and bool(text.strip())
    if not text.strip():
        unreadable = True
    return records, truncated, unreadable


def _usage_record_from_object(obj: Any, *, require_assistant: bool) -> Optional[dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    if require_assistant and obj.get("type") != "assistant":
        return None
    usage: Any = None
    model: Optional[str] = None
    message = obj.get("message")
    if isinstance(message, dict):
        if isinstance(message.get("usage"), dict):
            usage = message["usage"]
        raw_model = message.get("model")
        if isinstance(raw_model, str) and raw_model:
            model = raw_model
    if usage is None and isinstance(obj.get("usage"), dict):
        usage = obj["usage"]
    if usage is None and isinstance(obj.get("token_usage"), dict):
        usage = obj["token_usage"]
    if not isinstance(usage, dict):
        return None
    if not require_assistant and not _looks_like_usage(usage):
        return None
    timestamp = obj.get("timestamp")
    if not isinstance(timestamp, str):
        timestamp = ""
    return {"timestamp": timestamp, "usage": usage, "model": model}


def _looks_like_usage(usage: dict) -> bool:
    return any(key in usage for key in _USAGE_HINT_KEYS)


# ---------------------------------------------------------------------------
# 内部：014 R18 宿主采集补齐（kimi wire.jsonl / pi session / grok 会话目录，
# 解析口径搬 tools/eval/sddl_eval/evidence.py 已校准实现）
# ---------------------------------------------------------------------------

def _iso_from_epoch(value: Any) -> Optional[str]:
    """epoch 秒/毫秒 → ISO8601 Z（evidence.py 口径：>1e11 按毫秒）；非法 → None。"""
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float)):
        return None
    seconds = value / 1000 if value > 1e11 else value
    try:
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _primary_model_from_model_usage(model_usage: Any) -> Optional[str]:
    """modelUsage 中 modelCalls 最多者为主模型（evidence.py / UsageJsonParser 同口径）。"""
    if not isinstance(model_usage, dict):
        return None
    best_name: Optional[str] = None
    best_calls: Optional[int] = None
    for name, usage in model_usage.items():
        if not isinstance(name, str) or not name:
            continue
        calls = 0
        if isinstance(usage, dict):
            calls = _coerce_token(usage.get("modelCalls"))
        if best_calls is None or calls > best_calls:
            best_name = name
            best_calls = calls
    return best_name


def _kimi_wire_record_from_obj(obj: Any) -> Optional[dict[str, Any]]:
    """kimi wire.jsonl `usage.record` → {"timestamp", "usage"(六键 snake_case), "model"}。

    per-turn 增量（usageScope=turn），求和即会话总量；inputOther 不含缓存
    （口径同 claude）。evidence.py 兼容回退键（input_tokens/prompt_tokens 等）保留。
    """
    if not isinstance(obj, dict) or obj.get("type") != "usage.record":
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    normalized = {
        "input_tokens": _coerce_token(
            usage.get("inputOther", usage.get("input_tokens", usage.get("prompt_tokens")))
        ),
        "output_tokens": _coerce_token(
            usage.get("output", usage.get("output_tokens", usage.get("completion_tokens")))
        ),
        "cache_read_input_tokens": _coerce_token(
            usage.get(
                "inputCacheRead",
                usage.get("cache_read_input_tokens", usage.get("cached_tokens")),
            )
        ),
        "cache_creation_input_tokens": _coerce_token(
            usage.get("inputCacheCreation", usage.get("cache_creation_input_tokens"))
        ),
        "thinking_tokens": _coerce_token(usage.get("thinking_tokens")),
    }
    timestamp = _iso_from_epoch(obj.get("time"))
    if timestamp is None:
        raw = obj.get("timestamp")
        timestamp = raw if isinstance(raw, str) else ""
    model = obj.get("model")
    return {
        "timestamp": timestamp,
        "usage": normalized,
        "model": model if isinstance(model, str) and model else None,
    }


def _pi_session_record_from_obj(obj: Any) -> Optional[dict[str, Any]]:
    """pi session jsonl（v3）assistant message.usage → {"timestamp", "usage", "model"}。

    usage 键 {input, output, cacheRead, cacheWrite, reasoning, totalTokens}；
    input 不含缓存读（totalTokens=input+output+cacheRead+cacheWrite 实测口径），
    reasoning 单列。timestamp 为 ISO 字符串。
    """
    if not isinstance(obj, dict) or obj.get("type") != "message":
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    normalized = {
        "input_tokens": _coerce_token(usage.get("input", usage.get("input_tokens"))),
        "output_tokens": _coerce_token(usage.get("output", usage.get("output_tokens"))),
        "cache_read_input_tokens": _coerce_token(
            usage.get("cacheRead", usage.get("cache_read_input_tokens"))
        ),
        "cache_creation_input_tokens": _coerce_token(
            usage.get("cacheWrite", usage.get("cache_creation_input_tokens"))
        ),
        "thinking_tokens": _coerce_token(
            usage.get("reasoning", usage.get("thinking_tokens"))
        ),
    }
    timestamp = obj.get("timestamp")
    if not isinstance(timestamp, str):
        timestamp = ""
    model = message.get("model")
    return {
        "timestamp": timestamp,
        "usage": normalized,
        "model": model if isinstance(model, str) and model else None,
    }


def _kimi_session_path(marker: dict[str, Any], workspace: str) -> Optional[Path]:
    """定位 kimi 会话目录：显式路径/sessionId 反查 → workDir 兜底（evidence.py 口径）。"""
    try:
        located = locate_session_file(marker)
    except Exception:
        located = None
    if isinstance(located, str) and located.strip():
        return Path(located).expanduser()
    try:
        raw_ws = marker.get("workspace")
        wanted = normalize_workspace(
            raw_ws if isinstance(raw_ws, str) and raw_ws.strip() else workspace
        )
    except Exception:
        return None
    try:
        index_path = Path.home() / ".kimi-code" / "session_index.jsonl"
        if not index_path.is_file():
            return None
        session_dir: Optional[str] = None
        for line in index_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("workDir") != wanted:
                continue
            candidate = entry.get("sessionDir")
            if isinstance(candidate, str) and candidate.strip():
                session_dir = candidate
        if session_dir is None:
            return None
        return Path(session_dir).expanduser()
    except Exception:
        return None


def _list_kimi_wire_files(session_path: Path) -> list[Path]:
    """`<sessionDir>/agents/*/wire.jsonl` 枚举；main 在前，其余按目录名排序。"""
    try:
        if session_path.is_file():
            return [session_path]
        agents = session_path / "agents"
        files: list[Path] = []
        if agents.is_dir():
            for entry in agents.iterdir():
                candidate = entry / "wire.jsonl"
                try:
                    if entry.is_dir() and candidate.is_file():
                        files.append(candidate)
                except Exception:
                    continue
        if not files:
            files = [p for p in session_path.rglob("wire.jsonl") if p.is_file()]
        files.sort(key=lambda item: (item.parent.name != "main", item.parent.name))
        return files
    except Exception:
        return []


def _is_grok_session_dir(path: Path) -> bool:
    """grok 会话目录判定：目录且含 updates.jsonl（evidence.py grok_usage 解析单元）。"""
    try:
        return path.is_dir() and (path / "updates.jsonl").is_file()
    except Exception:
        return False


def _locate_grok_child_session_dir(host_dir: Path, child_name: str) -> Optional[Path]:
    """按子会话 id 定位 grok 子代理会话目录。

    子代理可能运行在不同 cwd（如 worktree），其会话目录落在对应 project 根下，
    不一定是宿主的兄弟目录（实测校准）；故先查兄弟、再按 session_registry
    grok 分支同款口径遍历 `~/.grok/sessions/*/<child_id>`。
    """
    try:
        sibling = host_dir.parent / child_name
        if _is_grok_session_dir(sibling):
            return sibling
        sessions_root = host_dir.parent.parent
        if not sessions_root.is_dir():
            return None
        for project_dir in sessions_root.iterdir():
            candidate = project_dir / child_name
            try:
                if _is_grok_session_dir(candidate):
                    return candidate
            except Exception:
                continue
        return None
    except Exception:
        return None


def _map_grok_session_usage(usage: Any) -> dict:
    """grok updates.jsonl 累计 usage（camelCase）→ 六键；total=五键和（同 map_grok 口径）。"""
    mapped = TokenMapper.zero()
    if not isinstance(usage, dict):
        return mapped
    mapped["input_tokens"] = _coerce_token(usage.get("inputTokens"))
    mapped["output_tokens"] = _coerce_token(usage.get("outputTokens"))
    mapped["cache_read_input_tokens"] = _coerce_token(usage.get("cachedReadTokens"))
    mapped["cache_creation_input_tokens"] = _coerce_token(
        usage.get("cacheCreationTokens")
    )
    mapped["thinking_tokens"] = _coerce_token(usage.get("reasoningTokens"))
    mapped["total_tokens"] = _five_key_sum(mapped)
    return mapped


def _grok_session_usage_from_updates(
    path: Path, budget: Optional[CollectionBudget] = None
) -> tuple[Optional[dict], Optional[str], Optional[str], bool, bool]:
    """搬 evidence.py `_grok_session_usage`：updates.jsonl 末条累计 usage + 首末时间戳。

    返回 (usage, first_iso, last_iso, truncated, unreadable)。usage 为最后一条含
    modelCalls 的 `params.update.usage`（会话累计值）；timestamp 为 epoch 秒/毫秒。
    """
    if not _budget_allow(budget):
        return None, None, None, True, False
    try:
        size = path.stat().st_size
        if size > MAX_TAIL_WINDOW_BYTES:
            text, truncated = _read_tail_window_text(path, MAX_TAIL_WINDOW_BYTES)
        else:
            text, truncated = _read_capped_text(path)
    except Exception:
        return None, None, None, False, True
    usage: Optional[dict] = None
    first_iso: Optional[str] = None
    last_iso: Optional[str] = None
    for line in text.splitlines():
        if '"modelCalls"' not in line and '"timestamp"' not in line:
            continue
        try:
            obj = json.loads(line.strip())
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        iso = _iso_from_epoch(obj.get("timestamp"))
        if iso is not None:
            if first_iso is None or iso < first_iso:
                first_iso = iso
            if last_iso is None or iso > last_iso:
                last_iso = iso
        params = obj.get("params")
        update = params.get("update") if isinstance(params, dict) else None
        candidate = update.get("usage") if isinstance(update, dict) else None
        if isinstance(candidate, dict) and "modelCalls" in candidate:
            usage = candidate
    if truncated:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                first_line = fh.readline()
            if first_line.strip():
                f_obj = json.loads(first_line.strip())
                if isinstance(f_obj, dict):
                    f_iso = _iso_from_epoch(f_obj.get("timestamp"))
                    if f_iso is not None:
                        first_iso = f_iso
        except Exception:
            pass
    unreadable = not text.strip()
    return usage, first_iso, last_iso, truncated, unreadable


def _grok_session_watermark_value(
    ended_at: Optional[str],
    total: dict,
    model_calls: Any,
    cost_ticks: Any,
) -> dict:
    return {
        "ended_at": ended_at if isinstance(ended_at, str) else "",
        "total": {key: _coerce_token(total.get(key)) for key in TOKEN_KEYS},
        "model_calls": _coerce_token(model_calls),
        "cost_ticks": _coerce_token(cost_ticks),
    }


def _grok_session_span(
    collector: "GrokSpanCollector",
    session_dir: Path,
    *,
    agent_id: str,
    is_host: bool,
    host_agent_id: str,
    marker: dict[str, Any],
    data: dict,
    watermarks: dict,
    stats_table: dict,
    physical_ws: str,
    cursor: "SpanCursorStore",
    budget: Optional[CollectionBudget],
) -> Optional[AgentSpan]:
    """单个 grok 会话目录 → span；累计 usage 按水位做差（同 codex D-26 ① 口径）。"""
    try:
        if not _budget_allow(budget):
            return None
        key, aliased = _alias_pre_d51_consumer_watermark(
            data, physical_ws, str(collector.workspace or ""), agent_id
        )
        if aliased:
            try:
                cursor.save(data)
            except Exception:
                pass
        last_seen = watermarks.get(key)
        has_wm = _has_span_watermark(last_seen)
        updates = session_dir / "updates.jsonl"
        identity = _file_stat_identity(updates)
        if _skip_unchanged_span_file(last_seen, stats_table.get(key), identity):
            return None
        usage, first_iso, last_iso, truncated, unreadable = (
            _grok_session_usage_from_updates(updates, budget=budget)
        )
        _budget_charge_file(budget, updates, cap=MAX_FILE_BYTES)
        if unreadable or usage is None:
            # 无累计 usage 的子代理会话按 evidence.py `continue` 口径跳过；
            # 宿主会话缺失按 D-9 发 failed span（不发零值 span，不推进既有水位）
            if not is_host or has_wm:
                _store_span_file_stat(stats_table, key, identity)
                return None
            watermarks[key] = ""
            _store_span_file_stat(stats_table, key, identity)
            return _failed_span(agent_id, token_source=collector.token_source)
        current_total = _map_grok_session_usage(usage)
        current_calls = _coerce_token(usage.get("modelCalls"))
        current_ticks = _coerce_token(usage.get("costUsdTicks"))
        model = _primary_model_from_model_usage(usage.get("modelUsage"))
        previous_total = _watermark_total(last_seen)
        previous_calls = 0
        previous_ticks = 0
        if isinstance(last_seen, dict):
            previous_calls = _coerce_token(last_seen.get("model_calls"))
            previous_ticks = _coerce_token(last_seen.get("cost_ticks"))
        status = "partial" if truncated else "ok"
        dispatch = "host" if is_host else "subagent"
        parent_session_id = None if is_host else host_agent_id
        if previous_total is not None:
            tokens = TokenMapper.subtract(current_total, previous_total)
            if _tokens_all_zero(tokens):
                _store_span_file_stat(stats_table, key, identity)
                return None
            wm_ended = _watermark_ended_at(last_seen)
            started_at = wm_ended or first_iso
            message_count = max(current_calls - previous_calls, 0)
            cost_ticks = max(current_ticks - previous_ticks, 0)
        else:
            tokens = current_total
            started_at = first_iso
            message_count = current_calls
            cost_ticks = current_ticks
        ended_at = last_iso
        if (
            _watermark_ended_at(last_seen)
            and ended_at
            and _timestamp_lte(ended_at, str(_watermark_ended_at(last_seen)))
        ):
            _store_span_file_stat(stats_table, key, identity)
            return None
        watermarks[key] = _grok_session_watermark_value(
            ended_at, current_total, current_calls, current_ticks
        )
        _store_span_file_stat(stats_table, key, identity)
        return AgentSpan(
            agent_id=agent_id,
            model=model,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=_duration_ms(started_at, ended_at),
            message_count=message_count,
            tokens=tokens,
            token_source=collector.token_source,
            collection_status=status,
            dispatch=dispatch,
            parent_session_id=parent_session_id,
            cost_usd=round(cost_ticks / 1e10, 4) if cost_ticks > 0 else None,
            cursor_total=dict(current_total),
        )
    except Exception:
        return None


def _collect_grok_session_tree(
    collector: "GrokSpanCollector",
    marker: dict[str, Any],
    cursor: "SpanCursorStore",
    host_dir: Path,
    *,
    budget: Optional[CollectionBudget] = None,
) -> list[AgentSpan]:
    """grok 会话目录树采集：宿主会话 + 主会话 subagents/ 列出的兄弟子代理目录（evidence.py 校准实现）。"""
    host_agent_id = _derived_agent_id(marker, host_dir)
    try:
        if not _budget_allow(budget):
            return []
        data = cursor.load()
        watermarks = data.setdefault(_CURSOR_SPANS, {})
        if not isinstance(watermarks, dict):
            watermarks = {}
            data[_CURSOR_SPANS] = watermarks
        stats_table = _cursor_stats_table(data)
        physical_ws = _watermark_workspace(marker, collector.workspace)
        session_dirs: list[tuple[Path, bool]] = [(host_dir, True)]
        truncated_listing = False
        subagents = host_dir / "subagents"
        try:
            child_names = (
                sorted(entry.name for entry in subagents.iterdir())
                if subagents.is_dir()
                else []
            )
        except Exception:
            child_names = []
        if len(child_names) > MAX_SUBAGENT_FILES_PER_FLUSH:
            truncated_listing = True
            child_names = child_names[:MAX_SUBAGENT_FILES_PER_FLUSH]
        for name in child_names:
            candidate = _locate_grok_child_session_dir(host_dir, name)
            if candidate is None or candidate == host_dir:
                continue
            session_dirs.append((candidate, False))
        spans: list[AgentSpan] = []
        stopped = False
        for session_dir, is_host in session_dirs:
            if not _budget_allow(budget):
                stopped = True
                break
            agent_id = host_agent_id if is_host else session_dir.name
            span = _grok_session_span(
                collector,
                session_dir,
                agent_id=agent_id,
                is_host=is_host,
                host_agent_id=host_agent_id,
                marker=marker,
                data=data,
                watermarks=watermarks,
                stats_table=stats_table,
                physical_ws=physical_ws,
                cursor=cursor,
                budget=budget,
            )
            if span is not None:
                spans.append(span)
        data[_CURSOR_SPANS] = watermarks
        data[_CURSOR_SPAN_STATS] = stats_table
        cursor.save(data)
        if truncated_listing or stopped:
            _mark_spans_partial(spans)
        return spans
    except Exception:
        return [_failed_span(host_agent_id, token_source=collector.token_source)]


def _coerce_token(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(value.strip()), 0)
        except ValueError:
            return 0
    return 0


def _thinking_tokens(usage: dict) -> int:
    details = usage.get("output_tokens_details")
    if isinstance(details, dict):
        thinking = _coerce_token(details.get("thinking_tokens"))
        if thinking:
            return thinking
        thinking = _coerce_token(details.get("reasoning_tokens"))
        if thinking:
            return thinking
    return _coerce_token(usage.get("thinking_tokens"))


def _empty_token_counts() -> dict:
    return {key: 0 for key in TOKEN_KEYS}


def _aggregate_tokens(records: list[dict[str, Any]]) -> dict:
    acc = _empty_token_counts()
    for record in records:
        usage = record.get("usage")
        if not isinstance(usage, dict):
            continue
        acc["input_tokens"] += _coerce_token(usage.get("input_tokens"))
        acc["output_tokens"] += _coerce_token(usage.get("output_tokens"))
        acc["cache_creation_input_tokens"] += _coerce_token(usage.get("cache_creation_input_tokens"))
        acc["cache_read_input_tokens"] += _coerce_token(usage.get("cache_read_input_tokens"))
        acc["thinking_tokens"] += _thinking_tokens(usage)
    acc["total_tokens"] = (
        acc["input_tokens"]
        + acc["output_tokens"]
        + acc["cache_creation_input_tokens"]
        + acc["cache_read_input_tokens"]
        + acc["thinking_tokens"]
    )
    return acc


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _duration_ms(started_at: Optional[str], ended_at: Optional[str]) -> Optional[int]:
    start = _parse_dt(started_at)
    end = _parse_dt(ended_at)
    if start is None or end is None:
        return None
    try:
        delta = end - start
    except TypeError:
        return None
    millis = int(delta.total_seconds() * 1000)
    return max(millis, 0)


def _timestamp_lte(left: str, right: str) -> bool:
    left_dt = _parse_dt(left)
    right_dt = _parse_dt(right)
    if left_dt is not None and right_dt is not None:
        return left_dt <= right_dt
    return left <= right


def _timestamp_gt(left: str, right: str) -> bool:
    return not _timestamp_lte(left, right) if left else False


def _span_from_records(
    *,
    agent_id: str,
    records: list[dict[str, Any]],
    token_source: str,
    meta: dict[str, Any],
    truncated: bool,
) -> AgentSpan:
    timestamps = [item["timestamp"] for item in records if item.get("timestamp")]
    started_at = timestamps[0] if timestamps else None
    ended_at = timestamps[-1] if timestamps else None
    model = None
    for item in records:
        if isinstance(item.get("model"), str) and item["model"]:
            model = item["model"]
            break
    return AgentSpan(
        agent_id=agent_id,
        agent_type=meta.get("agent_type"),
        model=model,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=_duration_ms(started_at, ended_at),
        message_count=len(records),
        spawn_depth=meta.get("spawn_depth"),
        tool_use_id=meta.get("tool_use_id"),
        tokens=_aggregate_tokens(records),
        token_source=token_source,
        collection_status="partial" if truncated else "ok",
    )


def _failed_span(
    agent_id: str,
    *,
    token_source: str,
    meta: Optional[dict[str, Any]] = None,
) -> AgentSpan:
    meta = meta or {}
    return AgentSpan(
        agent_id=agent_id,
        agent_type=meta.get("agent_type"),
        spawn_depth=meta.get("spawn_depth"),
        tool_use_id=meta.get("tool_use_id"),
        tokens=None,
        token_source=token_source,
        collection_status="failed",
    )


def _span_after_watermark(span: AgentSpan, path: Path, last_seen: str) -> Optional[AgentSpan]:
    """按水位只发增量：无新消息返回 None；有新消息则只汇总 timestamp > last_seen 的行。"""
    if span.collection_status == "failed":
        return None
    if not span.ended_at or _timestamp_lte(span.ended_at, last_seen):
        return None
    try:
        records, truncated, _unreadable = _extract_usage_records(path, require_assistant=True)
    except Exception:
        return None
    incremental = [
        item
        for item in records
        if item.get("timestamp") and _timestamp_gt(str(item["timestamp"]), last_seen)
    ]
    if not incremental:
        return None
    return _span_from_records(
        agent_id=span.agent_id,
        records=incremental,
        token_source=span.token_source,
        meta={
            "agent_type": span.agent_type,
            "spawn_depth": span.spawn_depth,
            "tool_use_id": span.tool_use_id,
        },
        truncated=truncated,
    )


def _derived_agent_id(marker: dict[str, Any], path: Optional[Path]) -> str:
    session_id = marker.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    if path is not None:
        name = path.stem or path.name
        if name:
            return name
    runtime = marker.get("runtime")
    if isinstance(runtime, str) and runtime.strip():
        return runtime.strip()
    return "unknown"


def _host_path(marker: dict[str, Any], *, locate_grok: bool) -> Optional[Path]:
    for key in ("transcript_path", "session_file"):
        value = marker.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser()
    if not locate_grok:
        return None
    try:
        located = locate_session_file(marker)
    except Exception:
        return None
    if isinstance(located, str) and located.strip():
        return Path(located).expanduser()
    return None


def _find_codex_rollout_by_cwd(
    workspace: str,
    session_id: Optional[str] = None,
    home: Optional[Path] = None,
) -> Optional[Path]:
    """Codex 会话 CWD 兜底查找（R2, T-04）：当 marker 缺少 transcript_path 时，在 ~/.codex/sessions 中按 workspace 匹配。"""
    try:
        base_home = home or Path.home()
        codex_root = base_home / ".codex" / "sessions"
        if not codex_root.is_dir():
            return None
        wanted_ws = normalize_workspace(workspace)
        candidates = sorted(
            codex_root.rglob("rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        for f in candidates:
            try:
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    head = fh.readline()
                if not head.strip():
                    continue
                meta = json.loads(head)
                if not isinstance(meta, dict) or meta.get("type") != "session_meta":
                    continue
                payload = meta.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if session_id and (
                    str(payload.get("session_id")) == session_id
                    or f.stem == f"rollout-{session_id}"
                ):
                    return f.resolve()
                cwd_val = normalize_workspace(payload.get("cwd") or "")
                if cwd_val == wanted_ws:
                    return f.resolve()
            except Exception:
                continue
        return None
    except Exception:
        return None


def _collect_records_from_host_path(
    path: Path,
    budget: Optional[CollectionBudget] = None,
    record_extractor: Optional[Callable[[Any], Optional[dict[str, Any]]]] = None,
) -> tuple[list[dict[str, Any]], bool, bool]:
    try:
        if path.is_dir():
            files = _iter_host_dir_record_files(path)[:MAX_SUBAGENT_FILES_PER_FLUSH]
            merged: list[dict[str, Any]] = []
            truncated_any = False
            any_unreadable = True
            saw_file = False
            for file_path in files:
                if not _budget_allow(budget):
                    truncated_any = True
                    break
                saw_file = True
                records, truncated, unreadable = _extract_usage_records(
                    file_path,
                    require_assistant=False,
                    budget=budget,
                    record_extractor=record_extractor,
                )
                _budget_charge_file(budget, file_path, cap=MAX_FILE_BYTES)
                truncated_any = truncated_any or truncated
                if not unreadable:
                    any_unreadable = False
                merged.extend(records)
            if not saw_file:
                return [], truncated_any, True
            return merged, truncated_any, any_unreadable and not merged
        if not _budget_allow(budget):
            return [], True, False
        records, truncated, unreadable = _extract_usage_records(
            path,
            require_assistant=False,
            budget=budget,
            record_extractor=record_extractor,
        )
        _budget_charge_file(budget, path, cap=MAX_FILE_BYTES)
        return records, truncated, unreadable
    except Exception:
        return [], False, True


def _collect_host_transcript(
    collector: AgentSpanCollector,
    marker: dict[str, Any],
    cursor: SpanCursorStore,
    *,
    locate_grok: bool = False,
    budget: Optional[CollectionBudget] = None,
    record_extractor: Optional[Callable[[Any], Optional[dict[str, Any]]]] = None,
) -> list[AgentSpan]:
    path = _host_path(marker, locate_grok=locate_grok)
    agent_id = _derived_agent_id(marker, path)
    try:
        if not _budget_allow(budget):
            return []
        data = cursor.load()
        watermarks = data.setdefault(_CURSOR_SPANS, {})
        if not isinstance(watermarks, dict):
            watermarks = {}
            data[_CURSOR_SPANS] = watermarks
        stats_table = _cursor_stats_table(data)
        physical_ws = _watermark_workspace(marker, collector.workspace)
        key, aliased = _alias_pre_d51_consumer_watermark(
            data, physical_ws, str(collector.workspace or ""), agent_id
        )
        if aliased:
            try:
                cursor.save(data)
            except Exception:
                pass
        last_seen = watermarks.get(key)
        has_wm = _has_span_watermark(last_seen)
        wm_ts = _watermark_ended_at(last_seen)
        if path is not None:
            identity = _span_source_stat_identity(path)
            if _skip_unchanged_span_file(last_seen, stats_table.get(key), identity):
                return []
        span: AgentSpan
        if path is None or not path.exists():
            span = _failed_span(agent_id, token_source=collector.token_source)
        else:
            if not _budget_allow(budget):
                return []
            records, truncated, unreadable = _collect_records_from_host_path(
                path, budget=budget, record_extractor=record_extractor
            )
            dir_incomplete = bool(
                path.is_dir() and truncated and not _budget_allow(budget)
            )
            if not dir_incomplete:
                _store_span_file_stat(
                    stats_table, key, _span_source_stat_identity(path)
                )
                data[_CURSOR_SPAN_STATS] = stats_table
            if unreadable or not records:
                if dir_incomplete:
                    return []
                span = _failed_span(agent_id, token_source=collector.token_source)
            else:
                if wm_ts:
                    records = [
                        item
                        for item in records
                        if item.get("timestamp") and _timestamp_gt(item["timestamp"], wm_ts)
                    ]
                    if not records:
                        cursor.save(data)
                        return []
                span = _span_from_records(
                    agent_id=agent_id,
                    records=records,
                    token_source=collector.token_source,
                    meta={},
                    truncated=truncated,
                )
        if has_wm and span.collection_status == "failed":
            # 失败时不覆盖既有 dict 水位（保留 total）；str 水位同样保留
            cursor.save(data)
            return []
        if wm_ts and span.ended_at and _timestamp_lte(span.ended_at, wm_ts):
            cursor.save(data)
            return []
        if budget is not None and not _budget_allow(budget) and span.collection_status == "ok":
            span.collection_status = "partial"
        watermarks[key] = span.ended_at if span.ended_at else ""
        cursor.save(data)
        return [span]
    except Exception:
        return [_failed_span(agent_id, token_source=collector.token_source)]
