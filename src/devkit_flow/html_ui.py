from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from devkit_flow.clarify import (
    clarification_batch_has_presentable_questions,
    clarification_question_record,
    clarification_response,
    clarification_state_path,
    command_flow_clarify_add_batch,
    command_flow_clarify_configure,
    load_clarification_batch,
    new_collecting_batch,
    pending_required_questions,
    reject_empty_batch,
    save_clarification_batch,
)
from devkit_flow.constants import DASHBOARD_TABLE_CARD_THRESHOLD, MERMAID_MIN_RENDERABLE_BYTES, MERMAID_STUB_MARKER, PRESENT_WAIT_SECONDS
from devkit_flow.errors import DevkitFlowError
from devkit_flow.flow import _flow_feature_trace, apply_write_side_effects, flow_emit_event, flow_select_feature, WriteSideEffects
from devkit_flow.review import _select_review_feature
from devkit_flow.switches import html_ui_summary
from devkit_flow.textio import atomic_write_text, attach_dashboard_warning, compact_error, current_cli_path, emit, exclusive_file_lock


HTML_UI_OPENER = None  # tests may replace with a fake opener


def _emit_clarify_batch(
    workspace: Path,
    feature: dict[str, Any] | None,
    batch: dict[str, Any],
    action: str,
    *,
    channel: str,
) -> dict[str, Any]:
    """点位 #10/#11：clarify 动作发 clarify_batch（仅计数，隐私 D-2 不含问答正文）。"""
    questions = batch.get("questions") or []
    items_total = len(questions)
    answered = sum(
        1 for question in questions if str(question.get("answer", "")).strip()
    )
    return flow_emit_event(
        workspace,
        feature,
        "clarify_batch",
        {
            "batch_id": str(batch.get("batchId") or ""),
            "action": action,
            "channel": channel,
            "items_total": items_total,
            "answered": answered,
            "unanswered": max(0, items_total - answered),
        },
        command="flow clarify" if channel == "cli" else "html-ui present",
        # D-23（014 T16）：v1 feature 上下文事件补 trace 注入（_flow_feature_trace 现成通道）。
        trace=_flow_feature_trace(workspace, feature),
    )


_CLARIFY_ANSWER_SOURCES = {"human", "preset", "ai", "prefill", "default"}


def _emit_clarify_answer(
    workspace: Path,
    feature: dict[str, Any] | None,
    batch: dict[str, Any],
    question: dict[str, Any],
    *,
    channel: str,
) -> dict[str, Any] | None:
    """014 R20（接口 §4.2）：单题作答发 clarify_answer，CLI answer 与 HTML present 两通道共用。

    answer_source 由写入侧在落库时按 D-19 通道判定写进 question（CLI 恒 human；present 按
    提交值与 control_initial_value 比对判 prefill/human），本函数只透传不重新判定，保证
    事件与 clarification-state.json 同源同值。topic 取题目 category（未分类 → null）。
    fail-safe：answer_source 缺省/越枚举（存量批次）→ None（丢事件不阻断作答写入）。
    """
    answer_source = question.get("answer_source")
    if answer_source not in _CLARIFY_ANSWER_SOURCES:
        return None
    return flow_emit_event(
        workspace,
        feature,
        "clarify_answer",
        {
            "batch_id": str(batch.get("batchId") or ""),
            "question_id": str(question.get("id") or ""),
            "answer_source": answer_source,
            "is_required": bool(question.get("required")),
            "topic": str(question.get("category") or "").strip() or None,
        },
        command="flow clarify" if channel == "cli" else "html-ui present",
        trace=_flow_feature_trace(workspace, feature),
    )


def command_flow_clarify(args: argparse.Namespace, *, dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None, side_effects: WriteSideEffects | None = None) -> int:
    if args.flow_clarify_command == "configure":
        return command_flow_clarify_configure(args)
    workspace = Path.cwd()
    feature = flow_select_feature(workspace, args)
    if feature is None:
        return 1
    path = clarification_state_path(workspace, feature)
    command = args.flow_clarify_command

    if command == "present":
        return command_html_ui_present(
            args, dashboard_refresh=dashboard_refresh, side_effects=side_effects
        )

    if command == "add-batch":
        return command_flow_clarify_add_batch(
            args, dashboard_refresh=dashboard_refresh, side_effects=side_effects
        )

    if command == "status":
        batch = load_clarification_batch(path)
        message = "No clarification batch found." if batch is None else f"Clarification batch {batch['batchId']}: {batch['status']}"
        emit(clarification_response(workspace, feature, batch, message), args.format)
        return 0

    events: list[dict[str, Any]] = []
    with exclusive_file_lock(path):
        batch = load_clarification_batch(path)
        if command == "open":
            if batch is not None and batch["status"] != "closed":
                raise DevkitFlowError(
                    f"An active clarification batch already exists: {batch['batchId']} ({batch['status']})"
                )
            batch_id = (args.batch_id or f"CB-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}").strip()
            if not batch_id:
                raise DevkitFlowError("--batch-id cannot be empty")
            batch = new_collecting_batch(batch_id)
            save_clarification_batch(path, batch)
            events.append(_emit_clarify_batch(workspace, feature, batch, "open", channel="cli"))
            data = clarification_response(workspace, feature, batch, f"Opened clarification batch {batch_id}.")
        else:
            if batch is None:
                raise DevkitFlowError("No clarification batch found; run `flow clarify open` first")
            if batch["status"] == "closed":
                raise DevkitFlowError(f"Clarification batch {batch['batchId']} is closed")

            if command == "add":
                if batch["status"] != "collecting":
                    raise DevkitFlowError("Questions can only be added while the batch is collecting")
                question_id = args.question_id.strip()
                if any(str(item.get("id")) == question_id for item in batch["questions"]):
                    raise DevkitFlowError(f"Question id already exists: {question_id}")
                question_text = args.question.strip()
                if not question_id or not question_text:
                    raise DevkitFlowError("Question id and text cannot be empty")
                question_type = (getattr(args, "question_type", None) or "text").strip() or "text"
                options = [value.strip() for value in (getattr(args, "option", None) or []) if value.strip()]
                if question_type in {"choice", "multi"} and len(options) < 2:
                    raise DevkitFlowError("choice/multi questions require at least 2 --option values")
                if question_type in {"text", "confirm"} and options:
                    raise DevkitFlowError("text/confirm questions cannot include --option")
                batch["questions"].append(
                    clarification_question_record(
                        question_id=question_id,
                        question=question_text,
                        category=args.category,
                        required=bool(args.required),
                        depends_on=args.depends_on,
                        recommendation=args.recommendation,
                        source=args.source,
                        question_type=question_type,
                        options=options,
                        context=getattr(args, "context", None) or "",
                    )
                )
                save_clarification_batch(path, batch)
                events.append(_emit_clarify_batch(workspace, feature, batch, "add", channel="cli"))
                data = clarification_response(workspace, feature, batch, f"Added clarification question {question_id}.")
            elif command == "answer":
                if batch["status"] != "collecting":
                    raise DevkitFlowError("Answers can only be recorded while the batch is collecting")
                question_id = args.question_id.strip()
                question = next(
                    (item for item in batch["questions"] if str(item.get("id")) == question_id),
                    None,
                )
                if question is None:
                    raise DevkitFlowError(f"Question not found: {question_id}")
                answer = args.answer.strip()
                if not answer:
                    raise DevkitFlowError("Answer cannot be empty")
                question["answer"] = answer
                # 014 R20（D-19）：CLI 作答通道恒记 human；preset/ai/default 保留枚举、当前无通道可达。
                question["answer_source"] = "human"
                save_clarification_batch(path, batch)
                events.append(_emit_clarify_batch(workspace, feature, batch, "answer", channel="cli"))
                # R20：单题作答随 clarify_batch 同点发 clarify_answer（answer_source 同源透传）。
                answer_event = _emit_clarify_answer(workspace, feature, batch, question, channel="cli")
                if answer_event is not None:
                    events.append(answer_event)
                data = clarification_response(workspace, feature, batch, f"Recorded answer for {question_id}; no documents were applied.")
            elif command == "apply":
                if batch["status"] != "collecting":
                    raise DevkitFlowError("Only a collecting batch can become ready-to-apply")
                pending = pending_required_questions(batch)
                if pending:
                    raise DevkitFlowError(
                        "Required clarification questions are unanswered: " + ", ".join(pending)
                    )
                batch["status"] = "ready-to-apply"
                save_clarification_batch(path, batch)
                events.append(_emit_clarify_batch(workspace, feature, batch, "apply", channel="cli"))
                data = clarification_response(workspace, feature, batch, "Clarification batch is ready for one impact-analysis cycle.")
            elif command == "close":
                if batch["status"] != "ready-to-apply":
                    raise DevkitFlowError("Clarification batch must be ready-to-apply before close")
                change_log_id = args.change_log_id.strip()
                if not change_log_id:
                    raise DevkitFlowError("--change-log-id cannot be empty")
                batch["status"] = "closed"
                batch["changeLogId"] = change_log_id
                save_clarification_batch(path, batch)
                events.append(_emit_clarify_batch(workspace, feature, batch, "close", channel="cli"))
                data = clarification_response(workspace, feature, batch, f"Closed clarification batch with {change_log_id}.")
            else:
                raise AssertionError(f"unhandled flow clarify command: {command}")

    data["_events"] = events
    data = apply_write_side_effects(data, feature, side_effects)
    attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return 0


def open_local_html_report(path: Path | str) -> tuple[bool, str | None]:
    target = os.fspath(path)
    if sys.platform == "darwin":
        command = ["open", target]
    elif os.name == "nt":
        command = ["cmd", "/c", "start", "", target]
    else:
        command = ["xdg-open", target]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, compact_error(error)
    if result.returncode == 0:
        return True, None
    detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    return False, detail[:300]


def _chromium_executable_candidates() -> list[Path]:
    candidates: list[Path] = []
    if sys.platform == "darwin":
        home_apps = Path.home() / "Applications"
        for name in (
            "Google Chrome.app/Contents/MacOS/Google Chrome",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "Brave Browser.app/Contents/MacOS/Brave Browser",
            "Chromium.app/Contents/MacOS/Chromium",
        ):
            candidates.append(Path("/Applications") / name)
            candidates.append(home_apps / name)
    elif os.name == "nt":
        extras = [os.environ.get(key) for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for root in extras:
            if not root:
                continue
            base = Path(root)
            candidates.extend(
                [
                    base / "Google" / "Chrome" / "Application" / "chrome.exe",
                    base / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    base / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
                ]
            )
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "brave-browser",
    ):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


PRESENT_BROWSER_SESSION: dict[str, Any] = {"proc": None, "profile": None}


def present_browser_launchers(url: str, *, profile_dir: str | None = None) -> list[list[str]]:
    """Isolated Chromium --app window so submit can close it without touching the user's browser."""
    app = f"--app={url}"
    extra: list[str] = []
    if profile_dir:
        extra = [
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
        ]
        if sys.platform == "darwin":
            extra.append("--use-mock-keychain")
        elif os.name != "nt":
            extra.append("--password-store=basic")
    commands: list[list[str]] = []
    seen: set[str] = set()

    def add(command: list[str]) -> None:
        key = "\0".join(command)
        if key in seen:
            return
        seen.add(key)
        commands.append(command)

    for binary in _chromium_executable_candidates():
        if binary.is_file():
            add([str(binary), *extra, app])
    if sys.platform == "darwin":
        add(["open", "-na", "Google Chrome", "--args", *extra, app])
        add(["open", "-na", "Microsoft Edge", "--args", *extra, app])
    if not any(app in " ".join(item) for item in commands):
        add(["google-chrome", *extra, app])
    return commands


def close_present_browser() -> None:
    proc = PRESENT_BROWSER_SESSION.get("proc")
    profile = PRESENT_BROWSER_SESSION.get("profile")
    PRESENT_BROWSER_SESSION["proc"] = None
    PRESENT_BROWSER_SESSION["profile"] = None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    if profile:
        shutil.rmtree(profile, ignore_errors=True)


def open_present_browser(url: str) -> tuple[bool, str | None]:
    close_present_browser()
    profile = Path(tempfile.mkdtemp(prefix="devkit-html-ui-present-"))
    last_error: str | None = None
    for command in present_browser_launchers(url, profile_dir=str(profile)):
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            last_error = compact_error(error)
            continue
        try:
            code = proc.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            PRESENT_BROWSER_SESSION["proc"] = proc
            PRESENT_BROWSER_SESSION["profile"] = profile
            return True, None
        last_error = f"exit {code}"
    shutil.rmtree(profile, ignore_errors=True)
    fallback_ok, fallback_error = open_local_html_report(url)
    if fallback_ok:
        return True, None
    return False, last_error or fallback_error


def _present_wait_seconds() -> float:
    raw = os.environ.get("DEVKIT_HTML_UI_WAIT_SECONDS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            return float(PRESENT_WAIT_SECONDS)
    return float(PRESENT_WAIT_SECONDS)


def bundled_html_ui_file(name: str) -> Path:
    cli = current_cli_path()
    candidates = (
        cli.parent.parent / "references" / "assets" / "html-ui" / name,
        cli.parent / "references" / "assets" / "html-ui" / name,
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def load_html_ui_template(name: str) -> str:
    path = bundled_html_ui_file(name)
    if not path.is_file():
        raise DevkitFlowError(f"missing html-ui template: {path}")
    return path.read_text(encoding="utf-8")


def fill_html_ui_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def present_done_html(title: str, message: str) -> str:
    return fill_html_ui_template(
        load_html_ui_template("clarification-done.html"),
        {"title": html.escape(title), "message": html.escape(message)},
    )


def _confirm_option_label(value: str) -> str:
    mapping = {"yes": "是", "no": "否"}
    return mapping.get(value, value)


def control_initial_value(question: dict[str, Any] | None) -> str:
    if not isinstance(question, dict):
        return ""
    answer = str(question.get("answer") or "").strip()
    if answer:
        return answer
    return str(question.get("recommendation") or "").strip()


def _multi_initial_values(initial: str) -> list[str]:
    text = str(initial or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [parsed.strip()]
    return [part.strip() for part in text.split(",") if part.strip()]


def _present_prefill_applied(
    questions: list[Any] | None,
    *,
    opened: bool,
    submitted: bool,
) -> bool:
    if not opened or not submitted:
        return False
    return any(
        bool(control_initial_value(question))
        for question in (questions or [])
        if isinstance(question, dict)
    )


def _present_answer_source(initial: str, value: Any, *, multi: bool = False) -> str:
    """014 R20（D-19）：HTML present 通道的 answer_source 判定。

    提交值等于预填初始值（`control_initial_value`，可写题在渲染时即 recommendation
    预填）视为「页面预填被当确认」记 `prefill`；已编辑或无预填记 `human`。
    """
    initial_text = str(initial or "").strip()
    if not initial_text:
        return "human"
    if multi:
        submitted = {str(item) for item in (value or [])}
        return "prefill" if submitted == set(_multi_initial_values(initial_text)) else "human"
    return "prefill" if str(value) == initial_text else "human"


def render_clarification_question_html(question: dict[str, Any]) -> str:
    qid_raw = str(question.get("id") or "")
    qid = html.escape(qid_raw)
    title = html.escape(str(question.get("question") or ""))
    context = html.escape(str(question.get("context") or ""))
    recommendation = html.escape(str(question.get("recommendation") or ""))
    qtype = str(question.get("type") or "text")
    options = question.get("options") or []
    required = bool(question.get("required"))
    initial = control_initial_value(question)
    req = '<span class="req">必填</span>' if required else ""
    ctx = f'<p class="ctx">{context}</p>' if context else ""
    rec = f'<p class="rec">建议：{recommendation}</p>' if recommendation else ""
    if qtype in {"choice", "confirm"}:
        if qtype == "confirm" and not options:
            options = ["yes", "no"]
        choices = []
        for option in options:
            value = html.escape(str(option))
            label = html.escape(_confirm_option_label(str(option))) if qtype == "confirm" else value
            checked = " checked" if initial and str(option) == initial else ""
            choices.append(
                f'<label class="opt"><input type="radio" name="{qid}" value="{value}"{checked}>'
                f"<span>{label}</span></label>"
            )
        control = f'<div class="opts">{"".join(choices)}</div>'
    elif qtype == "multi":
        selected = set(_multi_initial_values(initial))
        choices = []
        for option in options:
            value = html.escape(str(option))
            checked = " checked" if str(option) in selected else ""
            choices.append(
                f'<label class="opt"><input type="checkbox" name="{qid}" value="{value}"{checked}>'
                f"<span>{value}</span></label>"
            )
        control = f'<div class="opts">{"".join(choices)}</div>'
    else:
        control = f'<textarea name="{qid}" rows="3">{html.escape(initial)}</textarea>'
    return (
        f'<section class="q" data-type="{html.escape(qtype)}">'
        f'<div class="q-head"><span class="qid">{qid}</span><h2>{title}</h2>{req}</div>'
        f"{ctx}{rec}{control}</section>"
    )


def render_clarification_present_html(
    *,
    feature: dict[str, Any],
    batch: dict[str, Any],
    token: str,
    action_url: str = "/submit",
) -> str:
    questions = "".join(
        render_clarification_question_html(question)
        for question in (batch.get("questions") or [])
    )
    feature_name = html.escape(str(feature.get("feature") or ""))
    return fill_html_ui_template(
        load_html_ui_template("clarification-present.html"),
        {
            "title": f"澄清 {feature_name}",
            "feature": feature_name,
            "batch_id": html.escape(str(batch.get("batchId") or "")),
            "action_url": html.escape(action_url),
            "token": html.escape(token),
            "questions": questions,
        },
    )


def _apply_present_answers(batch: dict[str, Any], answers: dict[str, Any]) -> list[dict[str, Any]]:
    """把提交答案写入批次，返回本次实际作答的题目列表（R20 clarify_answer 逐题发射用）。"""
    answered: list[dict[str, Any]] = []
    for question in batch.get("questions") or []:
        qid = str(question.get("id") or "")
        if qid not in answers:
            continue
        if str(question.get("answer") or "").strip():
            continue
        # 014 R20（D-19）：作答通道来源按提交值与预填初始值比对写入 answer_source。
        initial = control_initial_value(question)
        qtype = str(question.get("type") or "text")
        raw = answers[qid]
        if qtype == "multi":
            values = raw if isinstance(raw, list) else [raw]
            cleaned = [str(item).strip() for item in values if str(item).strip()]
            question["answer"] = json.dumps(cleaned, ensure_ascii=False)
            question["answer_source"] = _present_answer_source(initial, cleaned, multi=True)
        elif qtype == "confirm":
            value = str(raw).strip().lower()
            if value not in {"yes", "no"}:
                continue
            question["answer"] = value
            question["answer_source"] = _present_answer_source(initial, value)
        else:
            value = str(raw).strip()
            if value:
                question["answer"] = value
                question["answer_source"] = _present_answer_source(initial, value)
            else:
                continue
        answered.append(question)
    return answered


def start_present_http(
    *,
    html_text_provider,
    token: str,
    on_submit,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    submitted = {"done": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _request_path(self) -> str:
            path = urlparse(self.path).path or "/"
            return path

        def _write_html(self, status: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            if self._request_path() not in {"/", ""}:
                self.send_error(404)
                return
            body = str(html_text_provider()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if self._request_path() != "/submit":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b""
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype == "application/json":
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                posted_token = str(payload.get("token") or "")
                answers = {
                    str(key): value
                    for key, value in payload.items()
                    if key != "token"
                }
            else:
                parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                posted_token = (parsed.get("token") or [""])[0]
                answers = {
                    key: values if len(values) > 1 else values[0]
                    for key, values in parsed.items()
                    if key != "token"
                }
            if posted_token != token:
                body = b'{"status":"forbidden"}'
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if submitted["done"]:
                self._write_html(200, present_done_html("已提交", "已经提交过，可以关闭本页。"))
                return
            on_submit(answers)
            submitted["done"] = True
            self._write_html(200, present_done_html("提交成功", "提交成功，可以关闭本页。"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _answered_question_count(batch: dict[str, Any]) -> int:
    return sum(
        1
        for question in batch.get("questions", [])
        if str(question.get("answer", "")).strip()
    )


def command_html_ui_present(args: argparse.Namespace, *, dashboard_refresh: Callable[[Path, dict[str, Any]], str | None] | None = None, side_effects: WriteSideEffects | None = None) -> int:
    workspace = Path.cwd()
    summary = html_ui_summary(workspace)
    if not summary.get("enabled"):
        emit(
            {
                "status": "skipped-disabled",
                "message": "HTML UI is disabled; fall back to the current-runtime question UI.",
                "reason": "skipped-disabled",
                "opened": False,
                "open_error": None,
                "html_path": None,
                "absolute_path": None,
                "pending_required": [],
                "batch": None,
                "html_ui": summary,
                "prefill_applied": False,
            },
            args.format,
        )
        return 0
    feature = _select_review_feature(args)
    path = clarification_state_path(workspace, feature)
    batch = load_clarification_batch(path)
    if batch is None or batch.get("status") != "collecting":
        raise DevkitFlowError("No clarification batch found")
    if not clarification_batch_has_presentable_questions(batch):
        empty_payload = reject_empty_batch(batch, html_ui=summary, feature=feature)
        empty_payload["prefill_applied"] = False
        emit(empty_payload, args.format)
        return 1
    answered_snapshot = _answered_question_count(batch)
    questions_for_prefill = list(batch.get("questions") or [])
    token = secrets.token_urlsafe(32)
    result_holder: dict[str, Any] = {"submitted": False}

    def on_submit(answers: dict[str, Any]) -> None:
        with exclusive_file_lock(path):
            current = load_clarification_batch(path)
            if current is None:
                return
            answered = _apply_present_answers(current, answers)
            save_clarification_batch(path, current)
            result_holder["submitted"] = True
            result_holder["batch"] = current
            # R20：留存本次实际作答题目（answer_source 已按 D-19 判定写入），供提交后逐题发
            # clarify_answer。迟到提交窗口（轮询超时与 server.shutdown 之间落盘）holder 可能
            # 未置位——此时只发 clarify_batch 汇总事件，per-question 事件缺失属 fail-safe 可接受方向。
            result_holder["answered_questions"] = answered

    html_holder = {"text": "<html></html>"}

    def serving_html() -> str:
        return html_holder["text"]

    server, _thread = start_present_http(
        html_text_provider=serving_html, token=token, on_submit=on_submit
    )
    port = int(server.server_address[1])
    form_url = f"http://127.0.0.1:{port}/"
    html_text = render_clarification_present_html(
        feature=feature, batch=batch, token=token, action_url="/submit"
    )
    html_holder["text"] = html_text
    if HTML_UI_OPENER is not None:
        opened, open_error = HTML_UI_OPENER(form_url)
    else:
        opened, open_error = open_present_browser(form_url)
    deadline = time.time() + _present_wait_seconds()
    try:
        while time.time() < deadline and not result_holder["submitted"]:
            if not opened:
                break
            time.sleep(0.05)
    finally:
        server.shutdown()
        server.server_close()
        if result_holder["submitted"]:
            time.sleep(0.4)
        close_present_browser()

    batch = load_clarification_batch(path) or batch
    pending = pending_required_questions(batch)
    # 迟到提交窗口加固：轮询超时瞬间与 server.shutdown 之间完成落盘的提交，
    # 内存 holder 可能尚未置位；以重读 batch 的 answered 增量（相对展示时快照）双判定。
    submitted = bool(result_holder["submitted"]) or _answered_question_count(batch) > answered_snapshot
    events: list[dict[str, Any]] = []
    if submitted:
        status = "submitted" if not pending else "submitted_partial"
        reason = None
        exit_code = 0
        message = f"Recorded HTML answers for batch {batch.get('batchId')}."
        # 点位 #11：html-ui present 提交回写后发 clarify_batch(action=answer, channel=html-present)。
        events.append(_emit_clarify_batch(workspace, feature, batch, "answer", channel="html-present"))
        # R20：本次提交实际作答的每题发 clarify_answer（answer_source 与落库值同源透传）。
        for answered_question in result_holder.get("answered_questions") or []:
            answer_event = _emit_clarify_answer(
                workspace, feature, batch, answered_question, channel="html-present"
            )
            if answer_event is not None:
                events.append(answer_event)
    elif not opened:
        status = "fallback_needed"
        reason = "open_failed"
        exit_code = 0
        message = "Failed to open HTML clarification form; fall back to the current-runtime question UI."
    else:
        status = "fallback_needed"
        reason = "timeout"
        exit_code = 0
        message = "HTML clarification timed out; fall back to the current-runtime question UI."
    data = {
        "status": status,
        "message": message,
        "feature": feature["feature"],
        "reason": reason,
        "opened": bool(opened),
        "open_error": open_error,
        "html_path": None,
        "absolute_path": form_url,
        "pending_required": pending,
        "batch": batch,
        "html_ui": summary,
        "prefill_applied": _present_prefill_applied(
            questions_for_prefill,
            opened=bool(opened),
            submitted=bool(submitted),
        ),
    }
    data["_events"] = events
    data = apply_write_side_effects(data, feature, side_effects)
    if submitted:
        # §4.3.3 挂钩：html-ui present 提交回写后重渲染（未提交则无状态变更，不刷新）
        attach_dashboard_warning(data, dashboard_refresh, workspace, feature)
    emit(data, args.format)
    return exit_code


def mermaid_source_is_renderable(text: str) -> bool:
    if MERMAID_STUB_MARKER in text:
        return False
    if len(text.encode("utf-8")) < MERMAID_MIN_RENDERABLE_BYTES:
        return False
    return "mermaid" in text.lower()


def bundled_mermaid_path() -> Path:
    cli = current_cli_path()
    candidates = (
        cli.parent.parent / "references" / "assets" / "mermaid.min.js",
        cli.parent / "references" / "assets" / "mermaid.min.js",
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def find_design_preview_docs(feature_dir: Path) -> dict[str, Path]:
    wanted = {
        "design": "[设计文档]",
        "api": "[接口文档]",
        "class": "[类图]",
    }
    found: dict[str, Path] = {}
    for key, prefix in wanted.items():
        matches = [
            path
            for path in feature_dir.iterdir()
            if path.is_file() and path.name.startswith(prefix) and path.suffix == ".md"
        ]
        if len(matches) == 1:
            found[key] = matches[0]
    return found


def markdown_heading_id(title: str) -> str:
    slug = title.strip().lower()
    slug = re.sub(r"[^\w\u4e00-\u9fff\s-]+", "", slug, flags=re.UNICODE)
    slug = re.sub(r"\s+", "-", slug)
    return slug


def markdown_inline_preview(text: str) -> str:
    pattern = re.compile(
        r"(`[^`\n]+`)"
        r"|(\*\*[^*]+?\*\*)"
        r"|(\[([^\]]+)\]\(([^)]+)\))"
    )
    pieces: list[str] = []
    last = 0
    for match in pattern.finditer(text):
        if match.start() > last:
            pieces.append(html.escape(text[last:match.start()]))
        if match.group(1):
            pieces.append(f"<code>{html.escape(match.group(1)[1:-1])}</code>")
        elif match.group(2):
            pieces.append(f"<strong>{html.escape(match.group(2)[2:-2])}</strong>")
        else:
            label, href = match.group(4), match.group(5)
            pieces.append(
                f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'
            )
        last = match.end()
    pieces.append(html.escape(text[last:]))
    return "".join(pieces)


def _markdown_table_separator(line: str) -> bool:
    stripped = line.strip().strip("|")
    if not stripped:
        return False
    cells = [cell.strip() for cell in stripped.split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _markdown_table_cells(line: str) -> list[str]:
    # 接口文档 §8.1：按未被 `\` 转义的 `|` 切分单元格；单元格内 `\|` 还原为字面 `|`。
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    cells = re.split(r"(?<!\\)\|", stripped)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def _markdown_table_is_wide(headers: list[str], rows: list[list[str]]) -> bool:
    for cell in headers:
        if len(cell) > DASHBOARD_TABLE_CARD_THRESHOLD:
            return True
    for row in rows:
        for cell in row:
            if len(cell) > DASHBOARD_TABLE_CARD_THRESHOLD:
                return True
    return False


def _markdown_table_record_cards(headers: list[str], rows: list[list[str]]) -> str:
    # 设计 §4.6.4：宽表卡片化——每行一张卡，首列值为卡头，其余列「列名: 值」竖排，
    # 空值 / `-` 字段不渲染；类名对齐 demo-dashboard-v2.html（.recs/.rec/.rec-head/.rec-field/.rec-k/.rec-v）。
    cards: list[str] = []
    for row in rows:
        padded = row + [""] * max(0, len(headers) - len(row))
        head = markdown_inline_preview(padded[0]) if headers else ""
        fields: list[str] = []
        for name, value in zip(headers[1:], padded[1 : len(headers)]):
            if not value or value == "-":
                continue
            fields.append(
                "<div class='rec-field'>"
                f"<span class='rec-k'>{markdown_inline_preview(name)}</span>"
                f"<div class='rec-v'>{markdown_inline_preview(value)}</div>"
                "</div>"
            )
        cards.append(
            f"<div class='rec'><div class='rec-head'>{head}</div>" + "".join(fields) + "</div>"
        )
    return "<div class='recs'>" + "".join(cards) + "</div>"


PREVIEW_DIAGRAM_CLASS = "dk-diagram"


def _render_mermaid_fence(body: str, *, preview_diagrams: bool) -> str:
    if not preview_diagrams:
        return f"<pre class='mermaid'>{html.escape(body, quote=False)}</pre>"
    src = html.escape(body, quote=True)
    return f"<pre class='{PREVIEW_DIAGRAM_CLASS}' data-diagram data-src='{src}'></pre>"


def render_markdown_subset(
    text: str,
    *,
    wide_table_cards: bool = False,
    preview_diagrams: bool = False,
) -> str:
    chunks: list[str] = []
    fence = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
    last = 0
    for match in fence.finditer(text):
        before = text[last:match.start()]
        if before.strip():
            chunks.append(_render_markdown_blocks(before, wide_table_cards=wide_table_cards))
        lang = (match.group(1) or "").strip().lower()
        body = match.group(2).rstrip("\n")
        if lang == "mermaid":
            chunks.append(_render_mermaid_fence(body, preview_diagrams=preview_diagrams))
        else:
            chunks.append(f"<pre><code>{html.escape(body)}</code></pre>")
        last = match.end()
    tail = text[last:]
    if tail.strip():
        chunks.append(_render_markdown_blocks(tail, wide_table_cards=wide_table_cards))
    return "\n".join(chunks)


def _render_markdown_blocks(text: str, *, wide_table_cards: bool = False) -> str:
    lines = text.splitlines()
    parts: list[str] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            parts.append("<hr>")
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = min(len(heading.group(1)), 6)
            title = heading.group(2).strip()
            heading_id = html.escape(markdown_heading_id(title), quote=True)
            parts.append(
                f'<h{level} id="{heading_id}">{markdown_inline_preview(title)}</h{level}>'
            )
            index += 1
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and _markdown_table_separator(lines[index + 1]):
            headers = _markdown_table_cells(stripped)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                if not _markdown_table_separator(lines[index]):
                    rows.append(_markdown_table_cells(lines[index]))
                index += 1
            if wide_table_cards and _markdown_table_is_wide(headers, rows):
                parts.append(_markdown_table_record_cards(headers, rows))
                continue
            head = "".join(f"<th>{markdown_inline_preview(cell)}</th>" for cell in headers)
            body_rows = []
            for row in rows:
                padded = row + [""] * max(0, len(headers) - len(row))
                cells = "".join(
                    f"<td>{markdown_inline_preview(cell)}</td>" for cell in padded[: len(headers)]
                )
                body_rows.append(f"<tr>{cells}</tr>")
            parts.append(
                "<div class='table-wrap'><table><thead><tr>"
                + head
                + "</tr></thead><tbody>"
                + "".join(body_rows)
                + "</tbody></table></div>"
            )
            continue
        unordered = re.match(r"^[-*]\s+(.*)$", stripped)
        if unordered:
            items = [f"<li>{markdown_inline_preview(unordered.group(1).strip())}</li>"]
            index += 1
            while index < len(lines):
                nxt = re.match(r"^[-*]\s+(.*)$", lines[index].strip())
                if not nxt:
                    break
                items.append(f"<li>{markdown_inline_preview(nxt.group(1).strip())}</li>")
                index += 1
            parts.append("<ul>" + "".join(items) + "</ul>")
            continue
        ordered = re.match(r"^\d+\.\s+(.*)$", stripped)
        if ordered:
            items = [f"<li>{markdown_inline_preview(ordered.group(1).strip())}</li>"]
            index += 1
            while index < len(lines):
                nxt = re.match(r"^\d+\.\s+(.*)$", lines[index].strip())
                if not nxt:
                    break
                items.append(f"<li>{markdown_inline_preview(nxt.group(1).strip())}</li>")
                index += 1
            parts.append("<ol>" + "".join(items) + "</ol>")
            continue
        if stripped.startswith(">"):
            quotes = [markdown_inline_preview(stripped.lstrip("> ").strip())]
            index += 1
            while index < len(lines) and lines[index].strip().startswith(">"):
                quotes.append(markdown_inline_preview(lines[index].strip().lstrip("> ").strip()))
                index += 1
            parts.append("<blockquote><p>" + "<br>\n".join(quotes) + "</p></blockquote>")
            continue
        paragraph: list[str] = [stripped]
        index += 1
        while index < len(lines):
            nxt = lines[index].strip()
            if not nxt:
                break
            if (
                nxt.startswith("#")
                or nxt.startswith("|")
                or nxt.startswith(">")
                or re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", nxt)
                or re.match(r"^[-*]\s+", nxt)
                or re.match(r"^\d+\.\s+", nxt)
            ):
                break
            paragraph.append(nxt)
            index += 1
        parts.append("<p>" + "<br>\n".join(markdown_inline_preview(item) for item in paragraph) + "</p>")
    return "\n".join(parts)


def render_design_preview_html(*, feature: dict[str, Any], docs: dict[str, Path], mermaid_src: str) -> str:
    labels = {"design": "设计文档", "api": "接口文档", "class": "类图"}
    nav: list[str] = []
    sections: list[str] = []
    for key in ("design", "api", "class"):
        body = render_markdown_subset(
            docs[key].read_text(encoding="utf-8"),
            preview_diagrams=True,
        )
        label = labels[key]
        selected = key == "design"
        nav.append(
            f'<button type="button" role="tab" class="tab{" is-active" if selected else ""}" '
            f'data-tab="{key}" aria-selected="{"true" if selected else "false"}">'
            f"{html.escape(label)}</button>"
        )
        hidden = "" if selected else " hidden"
        active = " is-active" if selected else ""
        sections.append(
            f'<section class="doc{active}" id="{key}" role="tabpanel"{hidden}>'
            f'<h1 class="doc-title">{html.escape(label)}</h1>'
            f'<div class="md">{body}</div>'
            "</section>"
        )
    return fill_html_ui_template(
        load_html_ui_template("design-preview.html"),
        {
            "title": html.escape(f"{feature['feature']} 设计预览"),
            "feature": html.escape(f"{feature['feature']} 设计预览"),
            "nav": "".join(nav),
            "sections": "".join(sections),
            "mermaid_src": html.escape(mermaid_src, quote=True),
        },
    )


def command_html_ui_preview(args: argparse.Namespace) -> int:
    workspace = Path.cwd()
    summary = html_ui_summary(workspace)
    if not summary.get("enabled"):
        emit(
            {
                "status": "skipped-disabled",
                "message": "HTML UI is disabled; skip design preview.",
                "opened": False,
                "open_error": None,
                "output": None,
                "absolute_path": None,
                "mermaid_rendered": False,
                "html_ui": summary,
            },
            args.format,
        )
        return 0
    feature = _select_review_feature(args)
    feature_dir = workspace / feature["feature_dir"]
    docs = find_design_preview_docs(feature_dir)
    if len(docs) != 3:
        raise DevkitFlowError("design preview requires 设计文档, 接口文档 and 类图")
    mermaid_src_file = bundled_mermaid_path()
    if not mermaid_src_file.is_file():
        raise DevkitFlowError(f"missing bundled mermaid: {mermaid_src_file}")
    mermaid_text = mermaid_src_file.read_text(encoding="utf-8")
    if not mermaid_source_is_renderable(mermaid_text):
        raise DevkitFlowError("bundled mermaid is a stub; cannot render diagrams")
    asset_dir = feature_dir / ".preview-assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    mermaid_dest = asset_dir / "mermaid.min.js"
    shutil.copyfile(mermaid_src_file, mermaid_dest)
    dest_text = mermaid_dest.read_text(encoding="utf-8")
    mermaid_rendered = mermaid_source_is_renderable(dest_text)
    if not mermaid_rendered:
        raise DevkitFlowError("bundled mermaid is a stub; cannot render diagrams")
    html_text = render_design_preview_html(
        feature=feature,
        docs=docs,
        mermaid_src=".preview-assets/mermaid.min.js",
    )
    output_path = feature_dir / "design-preview.html"
    if output_path.name == "workflow-visualization.html":
        raise DevkitFlowError("preview must not write workflow-visualization.html")
    atomic_write_text(output_path, html_text)
    opened = False
    open_error = None
    status = "generated"
    if not args.no_open:
        opener = HTML_UI_OPENER or open_local_html_report
        opened, open_error = opener(output_path)
        if opened:
            status = "opened"
    emit(
        {
            "status": status,
            "message": "Opened design preview." if opened else "Generated design preview.",
            "feature": feature["feature"],
            "output": output_path.relative_to(workspace).as_posix(),
            "absolute_path": str(output_path.resolve()),
            "opened": bool(opened),
            "open_error": open_error,
            "mermaid_rendered": mermaid_rendered,
            "html_ui": summary,
        },
        args.format,
    )
    return 0


def html_ui_doctor(workspace: Path) -> dict[str, Any]:
    try:
        summary = html_ui_summary(workspace)
    except DevkitFlowError as error:
        return {
            "status": "warn",
            "message": compact_error(error),
            "fix": "check .devkit/config.json JSON syntax",
        }
    state = "on" if summary["enabled"] else "off"
    return {"status": "ok", "message": f"{state}/{summary['source']}"}
