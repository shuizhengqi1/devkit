from __future__ import annotations

import argparse
import re
from devkit_flow.embedded import embedded_template_names
from devkit_flow.constants import CANONICAL_RUNTIME_ORDER, FEEDBACK_DEFAULT_ASSIGNEE, FEEDBACK_HELP, FLOW_MODES, FLOW_STEP_STATES, FLOW_TASK_STATES, HTML_UI_HELP, PIPELINE_SCALE_STEPS, PROGRESS_BAR_HELP, REVIEW_ROUNDS_HELP, SPEC_ARTIFACTS_HELP, SPECRULES_HELP
from devkit_flow.version import VERSION

_TELEMETRY_SPAN_WORKER_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


def _telemetry_span_worker(value: str) -> str:
    text = str(value)
    if _TELEMETRY_SPAN_WORKER_RE.fullmatch(text) is None:
        raise argparse.ArgumentTypeError(
            "--worker must match ^[A-Za-z0-9._:/-]{1,128}$"
        )
    return text


def _pr_review_older_than(value: str) -> int:
    text = str(value).strip().lower()
    number = text[:-1] if text.endswith("d") else text
    if not number.isdigit():
        raise argparse.ArgumentTypeError("--older-than must be <Nd> or a non-negative integer")
    return int(number)


def _pr_review_common(
    parser: argparse.ArgumentParser,
    *,
    repo: str = "optional",
    formats: tuple[str, ...] = ("json", "text"),
) -> None:
    """pr-review 通用参数。``--target`` 只表示工作区；目标分支用 ``--base``（DR-41）。"""
    parser.add_argument("--format", choices=formats, default="text")
    parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="忽略当前仓，按 repos.json 逐仓。",
    )
    parser.add_argument(
        "--target",
        default=None,
        metavar="<dir>",
        help="工作区目录，默认当前目录。",
    )
    if repo == "required":
        parser.add_argument("--repo", required=True, metavar="<project/repo>", help="project/repo。")
    elif repo == "optional":
        parser.add_argument(
            "--repo",
            default=None,
            metavar="<project/repo>",
            help="显式指定 project/repo，覆盖 origin 推导。",
        )


def _pr_review_watch_target(
    parser: argparse.ArgumentParser,
    *,
    formats: tuple[str, ...] = ("json", "text"),
) -> None:
    """watch 子命令共用的输出格式与工作区。不带单值 ``--repo``。"""
    parser.add_argument("--format", choices=formats, default="text")
    parser.add_argument("--target", default=None, metavar="<dir>", help="工作区目录，默认当前目录。")


def _pr_review_watch_identity(parser: argparse.ArgumentParser, *, runtime_required: bool = False) -> None:
    parser.add_argument("--runtime", required=runtime_required, default=None, metavar="<rt>")
    parser.add_argument("--session-id", dest="session_id", default=None, metavar="<sid>")


_PR_REVIEW_REF = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[1-9][0-9]*\Z")


def _pr_review_ref(value: str) -> str:
    """会话内 ``--pr`` 是 ``<project>/<repo>#<id>``，不是 017 的整数 PR 号。"""
    text = str(value).strip()
    if _PR_REVIEW_REF.fullmatch(text) is None:
        raise argparse.ArgumentTypeError("--pr must be <project>/<repo>#<id>")
    return text


def _add_pr_review_session(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """会话内执行入口。``show`` 默认只列上次已读唤醒之后的活动。"""
    show = subparsers.add_parser("show", help="读取 PR 元数据、新活动与改动文件名。")
    _pr_review_common(show)
    show.add_argument("--pr", required=True, type=_pr_review_ref, metavar="<project/repo#id>")
    scope = show.add_mutually_exclusive_group()
    scope.add_argument("--since-watch", action="store_true", help="只列本会话最近一次已读唤醒以来的新活动（默认）。")
    scope.add_argument("--all", action="store_true", help="全部活动。")

    prepared = subparsers.add_parser("prepare-review", help="认领台账并准备会话内审查任务。")
    _pr_review_common(prepared)
    prepared.add_argument("--pr", required=True, type=_pr_review_ref, metavar="<project/repo#id>")
    prepared.add_argument("--rereview", action="store_true", help="同 SHA 再开一轮，台账 round+1。")
    prepared.add_argument("--job", default=None, metavar="<jobId>", help="为该作业生成会话内 task.json。")
    prepared.add_argument(
        "--dispatch",
        default=None,
        metavar="<playbook>",
        help="为路由命中的 dispatcher 作业生成会话内 task.json。不能与 --job / --rereview 同用。",
    )

    reply = subparsers.add_parser("reply", help="回复 PR 评论。")
    _pr_review_common(reply)
    reply.add_argument("--pr", required=True, type=_pr_review_ref, metavar="<project/repo#id>")
    reply.add_argument("--body-file", required=True, metavar="<md>")
    reply.add_argument("--to", default=None, metavar="<commentId>")


def _add_pr_review_watch(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """``pr-review watch``。``converge`` 是内部子命令，不出现在帮助里。"""
    watch = subparsers.add_parser("watch", help="登记、查询和驱动 PR 监听。")
    commands = watch.add_subparsers(dest="watch_command", required=True)

    start = commands.add_parser("start", help="登记当前会话的监听。")
    _pr_review_watch_target(start)
    _pr_review_watch_identity(start)
    start.add_argument("--lane", required=True, choices=("author", "review"))
    start.add_argument("--pr", default=None, metavar="<ref>")
    start.add_argument("--repo", dest="repos", action="append", default=None, metavar="<project/repo>")
    start.add_argument("--takeover", action="store_true")

    stop = commands.add_parser("stop", help="注销监听。")
    _pr_review_watch_target(stop)
    _pr_review_watch_identity(stop)
    stop.add_argument("--id", dest="ids", action="append", default=None, metavar="<watchId>")
    stop.add_argument("--pr", default=None, metavar="<ref>")
    stop.add_argument("--session", action="store_true", help="注销当前会话的全部监听。")
    stop.add_argument("--all", action="store_true")
    stop.add_argument("--yes", action="store_true")

    listed = commands.add_parser("list", help="列出监听。只读。")
    _pr_review_watch_target(listed)
    _pr_review_watch_identity(listed)
    listed.add_argument("--session", action="store_true", help="只列当前会话。")
    listed.add_argument("--candidates", action="store_true")

    tick = commands.add_parser("tick", help="系统定时器调用的一轮轮询。")
    _pr_review_watch_target(tick)

    # 内部子命令：只由 tick / SessionEnd / CLI 超时路径派生，帮助里不列出。
    # 3.9 的 argparse 会把 SUPPRESS 打成 "==SUPPRESS==" 并留在用法括号里，所以再从帮助动作里拿掉。
    converge = commands.add_parser("converge", help=argparse.SUPPRESS)
    _pr_review_watch_target(converge)
    commands._choices_actions = [
        action for action in commands._choices_actions if getattr(action, "dest", None) != "converge"
    ]
    commands.metavar = "{start,stop,list,tick,tail,inbox}"

    tail = commands.add_parser("tail", help="会话内常驻读取本会话队列。")
    _pr_review_watch_target(tail, formats=("text", "jsonl"))
    _pr_review_watch_identity(tail, runtime_required=True)

    inbox = commands.add_parser("inbox", help="读取本会话未读事件。")
    _pr_review_watch_target(inbox)
    _pr_review_watch_identity(inbox)
    inbox.add_argument("--all", action="store_true", help="含最近已送达事件。")
    inbox.add_argument("--drain", action="store_true", help="把未送达事件视为本次送达。")
    inbox.add_argument("--undelivered", action="store_true", help="自动注入只取尚未送达的事件。")


def _telemetry_span_seq(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("--seq must be an integer >= 1") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("--seq must be an integer >= 1")
    return parsed


PIPELINE_STEP_ALIAS_HELP = (
    "Pipeline step as Sx id, slug, or Chinese name; case-insensitive Sx/slug, "
    "exact Chinese name, or unique fuzzy match. Ambiguous input lists candidates "
    "and does not guess."
)
PIPELINE_GATE_ALIAS_HELP = (
    "Human gate as Sx id, slug, or Chinese name (resolved then checked against "
    "S2-H / S5.3 / S5.5). Ambiguous input lists candidates and does not guess."
)
REVIEW_STEP_ALIAS_HELP = (
    "Review step as Sx id, slug, or Chinese name, or T-*-arch task-arch form. "
    "Ambiguous input lists candidates and does not guess."
)


def add_no_ones_sync_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-ones-sync",
        action="store_true",
        help="Skip automatic ONES follow-up after the local write. Does not affect explicit ones sync.",
    )


_UUID_TEXT_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def flow_batch_op_id(value: str) -> str:
    """016 T-10（D-51⑥ / Codex P2-11）：--op-id 必须是 8-4-4-4-12 形式的 UUID（与结果 schema 的 opId 口径一致），
    原样保留大小写（幂等键按字面比较）。"""
    text = str(value).strip()
    if not _UUID_TEXT_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"--op-id must be a UUID (8-4-4-4-12 hex), got {value!r}")
    return text


class FlowBatchMemberAction(argparse.Action):
    """016 T-10（接口 §2.1）：``--step`` / ``--task`` 共用一个有序成员列表，保留命令行出现顺序（成员按此顺序求值）。"""

    def __init__(self, option_strings, dest, member_kind: str = "step", **kwargs):  # type: ignore[no-untyped-def]
        self.member_kind = member_kind
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[no-untyped-def]
        members = list(getattr(namespace, self.dest, None) or [])
        members.append((self.member_kind, str(values)))
        setattr(namespace, self.dest, members)


def add_feedback_draft_arguments(parser: argparse.ArgumentParser, *, submit: bool) -> None:
    parser.add_argument(
        "--source",
        required=True,
        choices=("proactive", "retro"),
        help="Feedback source: proactive preview/submit or retro auto-review.",
    )
    parser.add_argument("--name", required=True, help="ONES requirement name.")
    desc_group = parser.add_mutually_exclusive_group(required=True)
    desc_group.add_argument("--desc", help="Requirement description body.")
    desc_group.add_argument("--desc-file", help="Read requirement description from a file.")
    parser.add_argument(
        "--assigned",
        default=FEEDBACK_DEFAULT_ASSIGNEE,
        help=f"Assignee MIS (default: {FEEDBACK_DEFAULT_ASSIGNEE}).",
    )
    parser.add_argument("--reporter", default=None, help="Reporter MIS; default follows identity chain.")
    parser.add_argument("--feature", default="", help="Feature name used in draft and retro idempotency.")
    parser.add_argument("--feature-dir", default="", help="Feature directory containing STATUS.md.")
    parser.add_argument("--git-repo", default="", help="Git remote shown in the draft description.")
    parser.add_argument("--branch", default="", help="Git branch shown in the draft description.")
    parser.add_argument("--step", default="", help="Override pipeline step label in the draft.")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    if submit:
        parser.add_argument(
            "--confirmed",
            action="store_true",
            help="Required for --source proactive after preview.",
        )
        parser.add_argument(
            "--agent-flow-count",
            type=int,
            default=0,
            help="agent-flow item count for --source retro; <1 skips.",
        )
        parser.add_argument("--request-id", default="", help="Idempotency key; generated when omitted.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="devkit workflow helper.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"devkit-flow {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Read current feature workflow status.")
    status.add_argument("--feature", default=None, help="Feature name or spec directory to select.")
    status.add_argument("--format", choices=("json", "text"), default="text")

    review_gate = subparsers.add_parser("review-gate", help="Configure review gate timing.")
    review_gate_subparsers = review_gate.add_subparsers(dest="review_gate_command", required=True)
    review_gate_status = review_gate_subparsers.add_parser("status")
    review_gate_status.add_argument("--format", choices=("json", "text"), default="text")
    review_gate_configure = review_gate_subparsers.add_parser("configure")
    review_gate_configure.add_argument("--scope", choices=("global", "project"), default="project")
    review_gate_action = review_gate_configure.add_mutually_exclusive_group(required=True)
    review_gate_action.add_argument("--timing", choices=("dev-once", "per-task"))
    review_gate_action.add_argument("--inherit", action="store_true")
    review_gate_action.add_argument("--clear", action="store_true")
    review_gate_configure.add_argument("--format", choices=("json", "text"), default="text")

    quality_gate = subparsers.add_parser("quality-gate", help="Configure workflow quality gates.")
    quality_subparsers = quality_gate.add_subparsers(dest="quality_command", required=True)
    quality_configure = quality_subparsers.add_parser("configure", help="Configure a quality gate.")
    quality_configure.add_argument("--name", choices=("fund-security",), required=True)
    quality_configure.add_argument("--scope", choices=("global", "project"), default="project")
    quality_action = quality_configure.add_mutually_exclusive_group(required=True)
    quality_action.add_argument("--enable", action="store_true")
    quality_action.add_argument("--disable", action="store_true")
    quality_action.add_argument("--inherit", action="store_true")
    quality_action.add_argument("--clear", action="store_true")
    quality_configure.add_argument("--format", choices=("json", "text"), default="text")

    context_source = subparsers.add_parser(
        "context-source",
        help="Configure optional external context sources.",
    )
    context_source_subparsers = context_source.add_subparsers(
        dest="context_source_command",
        required=True,
    )
    context_source_status = context_source_subparsers.add_parser(
        "status",
        help="Show the effective context source configuration.",
    )
    context_source_status.add_argument("--name", choices=("kba",), required=True)
    context_source_status.add_argument("--format", choices=("json", "text"), default="text")
    context_source_configure = context_source_subparsers.add_parser(
        "configure",
        help="Configure an external context source.",
    )
    context_source_configure.add_argument("--name", choices=("kba",), required=True)
    context_source_configure.add_argument(
        "--scope",
        choices=("global", "project"),
        default="project",
    )
    context_source_action = context_source_configure.add_mutually_exclusive_group(required=True)
    context_source_action.add_argument("--enable", action="store_true")
    context_source_action.add_argument("--disable", action="store_true")
    context_source_action.add_argument("--inherit", action="store_true")
    context_source_action.add_argument("--clear", action="store_true")
    context_source_configure.add_argument("--format", choices=("json", "text"), default="text")

    e2e = subparsers.add_parser(
        "e2e",
        help="Configure and inspect the e2e release gate (quality-gate.json).",
    )
    e2e_subparsers = e2e.add_subparsers(dest="e2e_command", required=True)
    e2e_configure = e2e_subparsers.add_parser(
        "configure",
        help="Configure the e2e source (phase 1: local) and gate file.",
    )
    e2e_configure.add_argument("--scope", choices=("global", "project"), default="project")
    # 互斥校验在 command 内经 DevkitFlowError 收口（exit 1，接口文档 §9），不走 argparse exit 2。
    e2e_configure.add_argument("--source-type", dest="source_type", default=None)
    e2e_configure.add_argument("--path", default=None, help="e2e 工程本地 checkout 路径。")
    e2e_configure.add_argument(
        "--gate-file",
        dest="gate_file",
        default=None,
        help="quality-gate.json 路径（相对 --path 或绝对）。",
    )
    e2e_configure.add_argument(
        "--inherit", action="store_true", help="仅 project：去项目层 e2e 键，跟随全局。"
    )
    e2e_configure.add_argument(
        "--clear", action="store_true", help="仅 global：去全局层 e2e 键。"
    )
    e2e_configure.add_argument("--format", choices=("json", "text"), default="text")
    e2e_status = e2e_subparsers.add_parser(
        "status",
        help="Show the effective e2e configuration and gate verdict.",
    )
    e2e_status.add_argument("--format", choices=("json", "text"), default="text")

    workflow = subparsers.add_parser(
        "workflow",
        help="Configure or query SDD/feature-manager auto-takeover; not the pipeline state machine.",
        description="Configure or query SDD/feature-manager auto-takeover; not the pipeline state machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    workflow_subparsers = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_status = workflow_subparsers.add_parser(
        "status",
        help="Show effective auto-takeover state. Omit --runtime for all five runtimes.",
        description="Show effective auto-takeover state. Omit --runtime for all five runtimes.",
    )
    workflow_status.add_argument("--runtime", default=None)
    workflow_status.add_argument("--format", choices=("json", "text"), default="text")
    workflow_configure = workflow_subparsers.add_parser(
        "configure",
        help="Enable, disable, inherit, or clear auto-takeover for one runtime.",
        description="Enable, disable, inherit, or clear auto-takeover for one runtime.",
    )
    workflow_configure.add_argument("--runtime", required=True)
    workflow_configure.add_argument(
        "--scope",
        choices=("global", "project"),
        default="project",
    )
    workflow_action = workflow_configure.add_mutually_exclusive_group(required=True)
    workflow_action.add_argument("--enable", action="store_true")
    workflow_action.add_argument("--disable", action="store_true")
    workflow_action.add_argument("--inherit", action="store_true")
    workflow_action.add_argument("--clear", action="store_true")
    workflow_configure.add_argument("--format", choices=("json", "text"), default="text")

    docs_curator = subparsers.add_parser(
        "docs-curator",
        help="Configure or query whether docs-curator runs at task/feature completion; default on.",
        description="Configure or query whether docs-curator runs at task/feature completion; default on.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    docs_curator_subparsers = docs_curator.add_subparsers(
        dest="docs_curator_command",
        required=True,
    )
    docs_curator_status = docs_curator_subparsers.add_parser(
        "status",
        help="Show the effective docs-curator switch.",
    )
    docs_curator_status.add_argument("--format", choices=("json", "text"), default="text")
    docs_curator_configure = docs_curator_subparsers.add_parser(
        "configure",
        help="Enable, disable, inherit, or clear the docs-curator switch.",
    )
    docs_curator_configure.add_argument(
        "--scope",
        choices=("global", "project"),
        default="project",
    )
    docs_curator_action = docs_curator_configure.add_mutually_exclusive_group(required=True)
    docs_curator_action.add_argument("--enable", action="store_true")
    docs_curator_action.add_argument("--disable", action="store_true")
    docs_curator_action.add_argument("--inherit", action="store_true")
    docs_curator_action.add_argument("--clear", action="store_true")
    docs_curator_configure.add_argument("--format", choices=("json", "text"), default="text")

    dashboard = subparsers.add_parser(
        "dashboard",
        help="Configure or query whether status writes render dashboard.html; default on.",
        description="Configure or query whether status writes render dashboard.html; default on.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dashboard_subparsers = dashboard.add_subparsers(
        dest="dashboard_command",
        required=True,
    )
    dashboard_status = dashboard_subparsers.add_parser(
        "status",
        help="Show the effective dashboard switch.",
    )
    dashboard_status.add_argument("--format", choices=("json", "text"), default="text")
    dashboard_configure = dashboard_subparsers.add_parser(
        "configure",
        help="Enable, disable, inherit, or clear the dashboard switch.",
    )
    dashboard_configure.add_argument(
        "--scope",
        choices=("global", "project"),
        default="project",
    )
    dashboard_action = dashboard_configure.add_mutually_exclusive_group(required=True)
    dashboard_action.add_argument("--enable", action="store_true")
    dashboard_action.add_argument("--disable", action="store_true")
    dashboard_action.add_argument("--inherit", action="store_true")
    dashboard_action.add_argument("--clear", action="store_true")
    dashboard_configure.add_argument("--format", choices=("json", "text"), default="text")

    specrules = subparsers.add_parser(
        "specrules",
        help=SPECRULES_HELP,
        description=SPECRULES_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    specrules_subparsers = specrules.add_subparsers(dest="specrules_command", required=True)
    specrules_status = specrules_subparsers.add_parser(
        "status",
        help="Show the effective specrules mount switch and project-only git URL.",
    )
    specrules_status.add_argument("--format", choices=("json", "text"), default="text")
    specrules_refresh = specrules_subparsers.add_parser(
        "refresh",
        help=(
            "If remote specrules is mounted, fetch updates and fast-forward "
            "(submodule) or sync from cache (copy). Skips when disabled, dirty, "
            "or unmounted. Never auto-commits."
        ),
    )
    specrules_refresh.add_argument("--target", default=".")
    specrules_refresh.add_argument("--format", choices=("json", "text"), default="text")
    specrules_configure = specrules_subparsers.add_parser(
        "configure",
        help="Enable, disable, follow the global switch, or set a project-only git URL.",
    )
    specrules_configure.add_argument(
        "--scope",
        choices=("global", "project"),
        default="project",
    )
    specrules_switch = specrules_configure.add_mutually_exclusive_group()
    specrules_switch.add_argument("--enable", action="store_true")
    specrules_switch.add_argument("--disable", action="store_true")
    specrules_switch.add_argument("--follow-global", action="store_true")
    specrules_url = specrules_configure.add_mutually_exclusive_group()
    specrules_url.add_argument("--repo-url", metavar="GIT-URL")
    specrules_url.add_argument("--use-default-repo", action="store_true")
    specrules_configure.add_argument("--format", choices=("json", "text"), default="text")
    # 016 T-05：规则源 catalog（接口 §2.4）。只写配置 / 只读预览，挂载在下一次 setup。
    specrules_catalog = specrules_subparsers.add_parser(
        "catalog",
        help="Show or configure rule-source catalog (frontend / java-backend) and routing; applied by the next setup.",
    )
    specrules_catalog_subparsers = specrules_catalog.add_subparsers(dest="specrules_catalog_command", required=True)
    specrules_catalog_status = specrules_catalog_subparsers.add_parser(
        "status",
        help="Show catalog layers, routing mode/source and a read-only binding preview (no mount, no pull).",
    )
    specrules_catalog_status.add_argument("--format", choices=("json", "text"), default="text")
    specrules_catalog_configure = specrules_catalog_subparsers.add_parser(
        "configure",
        help="Write ruleSources catalog / routing / module bindings into config (no mount; run setup to apply).",
    )
    specrules_catalog_configure.add_argument("--scope", choices=("global", "project"), default="project")
    specrules_catalog_configure.add_argument("--kind", choices=("frontend", "java-backend"))
    specrules_catalog_configure.add_argument("--repo-url", metavar="GIT-URL")
    specrules_catalog_configure.add_argument("--remove", action="store_true")
    specrules_catalog_configure.add_argument(
        "--routing", choices=("auto", "legacy"), help="Project only: auto = adopt new routing; legacy = keep/return to old path."
    )
    specrules_catalog_configure.add_argument("--module", metavar="RELPATH", help="Project only: explicit module binding.")
    specrules_catalog_configure.add_argument("--format", choices=("json", "text"), default="text")

    design_template = subparsers.add_parser(
        "design-template",
        help="Configure or query the single tech-design template pointer; not a template marketplace.",
        description="Configure or query the single tech-design template pointer; not a template marketplace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    design_template_subparsers = design_template.add_subparsers(
        dest="design_template_command",
        required=True,
    )
    design_template_status = design_template_subparsers.add_parser(
        "status",
        help="Show the effective tech-design template pointer.",
    )
    design_template_status.add_argument("--format", choices=("json", "text"), default="text")
    design_template_configure = design_template_subparsers.add_parser(
        "configure",
        help="Set, inherit, or clear the tech-design template pointer.",
    )
    design_template_configure.add_argument(
        "--scope",
        choices=("global", "project"),
        default="project",
    )
    design_template_action = design_template_configure.add_mutually_exclusive_group(required=True)
    design_template_action.add_argument("--pointer")
    design_template_action.add_argument("--inherit", action="store_true")
    design_template_action.add_argument("--clear", action="store_true")
    design_template_configure.add_argument("--format", choices=("json", "text"), default="text")

    html_ui = subparsers.add_parser(
        "html-ui",
        help=HTML_UI_HELP,
        description=HTML_UI_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    html_ui_subparsers = html_ui.add_subparsers(dest="html_ui_command", required=True)
    html_ui_status = html_ui_subparsers.add_parser("status", help="Show the effective HTML UI switch.")
    html_ui_status.add_argument("--format", choices=("json", "text"), default="text")
    html_ui_configure = html_ui_subparsers.add_parser(
        "configure",
        help="Enable, disable, inherit, or clear the HTML UI switch.",
    )
    html_ui_configure.add_argument("--scope", choices=("global", "project"), default="project")
    html_ui_action = html_ui_configure.add_mutually_exclusive_group(required=True)
    html_ui_action.add_argument("--enable", action="store_true")
    html_ui_action.add_argument("--disable", action="store_true")
    html_ui_action.add_argument("--inherit", action="store_true")
    html_ui_action.add_argument("--clear", action="store_true")
    html_ui_configure.add_argument("--format", choices=("json", "text"), default="text")
    html_ui_present = html_ui_subparsers.add_parser(
        "present",
        help="Open the HTML clarification form and wait for submit.",
    )
    html_ui_present.add_argument("--feature", default=None)
    html_ui_present.add_argument("--format", choices=("json", "text"), default="text")
    html_ui_preview = html_ui_subparsers.add_parser(
        "preview",
        help="Render design/interface/class docs to HTML and open the browser.",
    )
    html_ui_preview.add_argument("--feature", default=None)
    html_ui_preview.add_argument("--no-open", action="store_true")
    html_ui_preview.add_argument("--format", choices=("json", "text"), default="text")

    progress_bar = subparsers.add_parser(
        "progress-bar",
        help=PROGRESS_BAR_HELP,
        description=PROGRESS_BAR_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    progress_bar_subparsers = progress_bar.add_subparsers(dest="progress_bar_command", required=True)
    progress_bar_status = progress_bar_subparsers.add_parser(
        "status", help="Show the effective progress bar switch."
    )
    progress_bar_status.add_argument("--format", choices=("json", "text"), default="text")
    progress_bar_configure = progress_bar_subparsers.add_parser(
        "configure",
        help="Enable, disable, inherit, or clear the progress bar switch.",
    )
    progress_bar_configure.add_argument("--scope", choices=("global", "project"), default="project")
    progress_bar_action = progress_bar_configure.add_mutually_exclusive_group(required=True)
    progress_bar_action.add_argument("--enable", action="store_true")
    progress_bar_action.add_argument("--disable", action="store_true")
    progress_bar_action.add_argument("--inherit", action="store_true")
    progress_bar_action.add_argument("--clear", action="store_true")
    progress_bar_configure.add_argument("--format", choices=("json", "text"), default="text")

    spec_artifacts = subparsers.add_parser(
        "spec-artifacts",
        help=SPEC_ARTIFACTS_HELP,
        description=SPEC_ARTIFACTS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    spec_artifacts_subparsers = spec_artifacts.add_subparsers(
        dest="spec_artifacts_command",
        required=True,
    )
    spec_artifacts_status = spec_artifacts_subparsers.add_parser(
        "status",
        help="Show the effective spec-artifacts switch.",
    )
    spec_artifacts_status.add_argument("--format", choices=("json", "text"), default="text")
    spec_artifacts_configure = spec_artifacts_subparsers.add_parser(
        "configure",
        help="Enable, disable, inherit, or clear the spec-artifacts switch.",
    )
    spec_artifacts_configure.add_argument(
        "--scope",
        choices=("global", "project"),
        default="project",
    )
    spec_artifacts_action = spec_artifacts_configure.add_mutually_exclusive_group(
        required=True
    )
    spec_artifacts_action.add_argument("--enable", action="store_true")
    spec_artifacts_action.add_argument("--disable", action="store_true")
    spec_artifacts_action.add_argument("--inherit", action="store_true")
    spec_artifacts_action.add_argument("--clear", action="store_true")
    spec_artifacts_configure.add_argument(
        "--format", choices=("json", "text"), default="text"
    )

    review_rounds = subparsers.add_parser(
        "review-rounds",
        help=REVIEW_ROUNDS_HELP,
        description=REVIEW_ROUNDS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    review_rounds_subparsers = review_rounds.add_subparsers(
        dest="review_rounds_command", required=True
    )
    review_rounds_status = review_rounds_subparsers.add_parser(
        "status", help="Show the effective Codex review-round limit."
    )
    review_rounds_status.add_argument("--format", choices=("json", "text"), default="text")
    review_rounds_configure = review_rounds_subparsers.add_parser(
        "configure",
        help="Set, inherit, or clear the Codex review-round limit.",
    )
    review_rounds_configure.add_argument("--scope", choices=("global", "project"), default="project")
    review_rounds_action = review_rounds_configure.add_mutually_exclusive_group(required=True)
    review_rounds_action.add_argument("--max-rounds", dest="max_rounds", type=int)
    review_rounds_action.add_argument("--inherit", action="store_true")
    review_rounds_action.add_argument("--clear", action="store_true")
    review_rounds_configure.add_argument("--format", choices=("json", "text"), default="text")
    review_rounds_check = review_rounds_subparsers.add_parser(
        "check", help="Read whether another auto review cycle is allowed."
    )
    review_rounds_check.add_argument("--feature", default=None)
    review_rounds_check.add_argument("--step", required=True, help=REVIEW_STEP_ALIAS_HELP)
    review_rounds_check.add_argument("--format", choices=("json", "text"), default="text")
    review_rounds_tick = review_rounds_subparsers.add_parser(
        "tick", help="Record a review cycle as failed or passed."
    )
    review_rounds_tick.add_argument("--feature", default=None)
    review_rounds_tick.add_argument("--step", required=True, help=REVIEW_STEP_ALIAS_HELP)
    review_rounds_tick.add_argument("--event", required=True, choices=("failed", "passed"))
    review_rounds_tick.add_argument(
        "--finding-ids",
        dest="finding_ids",
        default=None,
        help="Comma-separated finding IDs from the review just recorded.",
    )
    review_rounds_tick.add_argument(
        "--new-class",
        dest="new_class",
        action="store_true",
        help="This failed round introduced a new finding class (counts toward new-class budget).",
    )
    review_rounds_tick.add_argument("--format", choices=("json", "text"), default="text")
    review_rounds_reset = review_rounds_subparsers.add_parser(
        "reset", help="Clear a step's review-round counter after human takeover."
    )
    review_rounds_reset.add_argument("--feature", default=None)
    review_rounds_reset.add_argument("--step", required=True, help=REVIEW_STEP_ALIAS_HELP)
    review_rounds_reset.add_argument("--format", choices=("json", "text"), default="text")

    # 016 T-11（接口 §2.6）：审查机器结果文件校验。
    review_result = subparsers.add_parser(
        "review-result",
        help="Verify a review report against its machine result file (review-<kind>.result.json).",
    )
    review_result_subparsers = review_result.add_subparsers(dest="review_result_command", required=True)
    review_result_verify = review_result_subparsers.add_parser(
        "verify",
        help="Check schema, evidence, score and Markdown consistency; legacy reports without a result file are unverified (exit 0).",
    )
    review_result_verify.add_argument("--feature", default=None)
    review_result_verify.add_argument(
        "--report",
        required=True,
        help="design | apidoc | arch (review-arch-comprehensive.md) | arch:<T-xx> (review-arch-<T-xx>.md) | <path> (relative to cwd).",
    )
    review_result_verify.add_argument("--format", choices=("json", "text"), default="text")

    feature = subparsers.add_parser("feature", help="Manage feature spec directories.")
    feature_subparsers = feature.add_subparsers(dest="feature_command", required=True)
    next_id = feature_subparsers.add_parser(
        "next-id",
        help="Compute the next numeric feature directory prefix under spec/.",
        description=(
            "Scan spec/ for directories matching NNN-name and print the next "
            "zero-padded sequence number (001 when none exist)."
        ),
    )
    next_id.add_argument("--format", choices=("json", "text"), default="text")

    feature_init = feature_subparsers.add_parser(
        "init",
        help="Create spec/{NNN}-{name}/ with STATUS.md and DECISIONS.md from embedded templates.",
        description=(
            "Create the feature directory with a scale-trimmed pipeline STATUS.md "
            "(mode rows are appended later by 'flow mode') and an empty DECISIONS.md. "
            "FEATURE.md is not created; it belongs to prd-analyzer-agent."
        ),
    )
    feature_init.add_argument("--name", required=True, help="Feature name (lowercase letters/digits/dashes).")
    feature_init.add_argument("--scale", required=True, choices=sorted(PIPELINE_SCALE_STEPS))
    feature_init.add_argument("--format", choices=("json", "text"), default="text")

    spec = subparsers.add_parser("spec", help="Manage long-lived spec knowledge and archives.")
    spec_subparsers = spec.add_subparsers(dest="spec_command", required=True)
    spec_archive = spec_subparsers.add_parser(
        "archive", help="Scan and run resumable historical spec archive transactions."
    )
    spec_archive_subparsers = spec_archive.add_subparsers(
        dest="spec_archive_command", required=True
    )

    spec_archive_scan = spec_archive_subparsers.add_parser(
        "scan", help="Classify historical spec directories without changing them."
    )
    scan_selection = spec_archive_scan.add_mutually_exclusive_group()
    scan_selection.add_argument("--all", action="store_true")
    scan_selection.add_argument("--feature", default=None)
    spec_archive_scan.add_argument("--inactive-days", type=int, default=None)
    spec_archive_scan.add_argument("--format", choices=("json", "text"), default="text")

    spec_archive_configure = spec_archive_subparsers.add_parser(
        "configure", help="Configure historical spec archive defaults."
    )
    spec_archive_configure.add_argument(
        "--scope", choices=("global", "project"), default="project"
    )
    spec_archive_configure.add_argument(
        "--content-mode", choices=("compact-copy", "in-place"), default=None
    )
    spec_archive_configure.add_argument(
        "--knowledge-base-approval", choices=("ask", "auto"), default=None
    )
    spec_archive_configure.add_argument(
        "--cleanup-policy", choices=("keep", "ask"), default=None
    )
    spec_archive_configure.add_argument(
        "--retention", choices=("keep", "compact", "git-only"), default=None
    )
    spec_archive_configure.add_argument("--inactive-days", type=int, default=None)
    spec_archive_configure.add_argument("--inherit", action="store_true")
    spec_archive_configure.add_argument("--clear", action="store_true")
    spec_archive_configure.add_argument(
        "--format", choices=("json", "text"), default="text"
    )

    spec_archive_plan = spec_archive_subparsers.add_parser(
        "plan", help="Freeze one candidate's source hashes and archive options."
    )
    spec_archive_plan.add_argument("--feature", required=True)
    spec_archive_plan.add_argument("--include-review-required", action="store_true")
    spec_archive_plan.add_argument(
        "--content-mode", choices=("compact-copy", "in-place"), default=None
    )
    spec_archive_plan.add_argument(
        "--retention", choices=("keep", "compact", "git-only"), default=None
    )
    spec_archive_plan.add_argument("--format", choices=("json", "text"), default="text")

    spec_archive_status = spec_archive_subparsers.add_parser(
        "status", help="Show one receipt or list all archive receipts."
    )
    spec_archive_status.add_argument("--archive-id", default=None)
    spec_archive_status.add_argument("--format", choices=("json", "text"), default="text")

    spec_archive_stage = spec_archive_subparsers.add_parser(
        "stage", help="Register the Skill-produced archive and knowledge drafts."
    )
    spec_archive_stage.add_argument("--archive-id", required=True)
    spec_archive_stage.add_argument("--knowledge-noop-reason", default="")
    spec_archive_stage.add_argument("--format", choices=("json", "text"), default="text")

    spec_archive_approve = spec_archive_subparsers.add_parser(
        "approve", help="Record exactly one archive transaction approval gate."
    )
    spec_archive_approve.add_argument("--archive-id", required=True)
    spec_archive_approve.add_argument(
        "--gate", choices=("knowledge-base", "cleanup", "git-only-cleanup"), required=True
    )
    spec_archive_approve.add_argument("--format", choices=("json", "text"), default="text")

    spec_archive_verify = spec_archive_subparsers.add_parser(
        "verify", help="Verify source hashes, staged data, and knowledge approval."
    )
    spec_archive_verify.add_argument("--archive-id", required=True)
    spec_archive_verify.add_argument("--format", choices=("json", "text"), default="text")

    spec_archive_apply = spec_archive_subparsers.add_parser(
        "apply", help="Apply verified knowledge and compact archive content."
    )
    spec_archive_apply.add_argument("--archive-id", required=True)
    spec_archive_apply.add_argument("--format", choices=("json", "text"), default="text")

    spec_archive_prune = spec_archive_subparsers.add_parser(
        "prune", help="Apply the approved destructive retention action."
    )
    spec_archive_prune.add_argument("--archive-id", required=True)
    spec_archive_prune.add_argument("--format", choices=("json", "text"), default="text")

    spec_archive_finalize = spec_archive_subparsers.add_parser(
        "finalize", help="Finalize an archived or cleaned transaction."
    )
    spec_archive_finalize.add_argument("--archive-id", required=True)
    spec_archive_finalize.add_argument("--format", choices=("json", "text"), default="text")

    spec_archive_restore = spec_archive_subparsers.add_parser(
        "restore", help="Restore original tracked spec files from the recorded Git commit."
    )
    spec_archive_restore.add_argument("--archive-id", required=True)
    spec_archive_restore.add_argument("--format", choices=("json", "text"), default="text")

    flow = subparsers.add_parser(
        "flow",
        help="Validate and apply feature pipeline state transitions.",
    )
    flow_subparsers = flow.add_subparsers(dest="flow_command", required=True)

    flow_next = flow_subparsers.add_parser(
        "next",
        help="Show the current breakpoint step, dependency status and suggested action.",
    )
    flow_next.add_argument("--feature", default=None)
    flow_next.add_argument("--format", choices=("json", "text"), default="text")

    flow_set = flow_subparsers.add_parser(
        "set",
        help="Set a pipeline step state with dependency and human-gate validation.",
    )
    flow_set.add_argument("--feature", default=None)
    flow_set.add_argument("--step", required=True, help=PIPELINE_STEP_ALIAS_HELP)
    flow_set.add_argument("--status", required=True, choices=sorted(FLOW_STEP_STATES))
    flow_set.add_argument("--note", default=None, help="Replace the step note.")
    flow_set.add_argument(
        "--confirmed",
        action="store_true",
        help="Required when setting a -H human-gate step to done.",
    )
    flow_set.add_argument(
        "--force",
        action="store_true",
        help="Override unmet dependencies; requires --reason.",
    )
    flow_set.add_argument("--reason", default=None, help="Reason recorded with --force.")
    flow_set.add_argument(
        "--override-clarify-guard",
        dest="override_clarify_guard",
        action="store_true",
        help="Bypass the collecting-batch guard for write-back set actions (recorded as an intervention).",
    )
    add_no_ones_sync_argument(flow_set)
    flow_set.add_argument("--format", choices=("json", "text"), default="text")

    flow_batch_set = flow_subparsers.add_parser(
        "batch-set",
        help="Set several pipeline steps and design tasks (D-xx) atomically under one STATUS.md lock.",
    )
    flow_batch_set.add_argument("--feature", default=None)
    flow_batch_set.add_argument(
        "--step",
        dest="batch_members",
        action=FlowBatchMemberAction,
        member_kind="step",
        default=None,
        metavar="STEP_ID",
        help="Pipeline step member (repeatable; -H human gates are not allowed). " + PIPELINE_STEP_ALIAS_HELP,
    )
    flow_batch_set.add_argument(
        "--status",
        default=None,
        help="Target state shared by every --step member (flow set states except waived). Required with --step.",
    )
    flow_batch_set.add_argument(
        "--task",
        dest="batch_members",
        action=FlowBatchMemberAction,
        member_kind="task",
        default=None,
        metavar="D-xx=STATE",
        help="Design task member as D-xx=<state> (repeatable; T-xx development tasks are not allowed).",
    )
    flow_batch_set.add_argument(
        "--confirm-gates",
        action="store_true",
        help="Required when any task member is set to 已完成.",
    )
    flow_batch_set.add_argument(
        "--note",
        default=None,
        help="Note for every non-noop member: replaces step notes, appends to task notes.",
    )
    flow_batch_set.add_argument(
        "--op-id", dest="op_id", default=None, type=flow_batch_op_id, help="Idempotency key (uuid); derived when omitted."
    )
    flow_batch_set.add_argument(
        "--expect-revision",
        dest="expect_revision",
        default=None,
        help="Optimistic concurrency: sha256 of STATUS.md the caller last read.",
    )
    flow_batch_set.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and evaluate in memory only; no lock, no write, no events.",
    )
    add_no_ones_sync_argument(flow_batch_set)
    flow_batch_set.add_argument("--format", choices=("json", "text"), default="text")

    flow_accept_risk = flow_subparsers.add_parser(
        "accept-risk",
        help="Accept unresolved design-review or task-architecture risks without falsifying PASS.",
    )
    flow_accept_risk.add_argument("--feature", default=None)
    flow_accept_risk.add_argument("--gate", required=True, help=PIPELINE_GATE_ALIAS_HELP)
    flow_accept_risk.add_argument(
        "--task-id",
        dest="task_id",
        default=None,
        help="Required when the resolved gate is S5.5/S5.3 (the development task whose architecture review is waived).",
    )
    flow_accept_risk.add_argument("--reason", default=None, help="Human risk-acceptance reason.")
    flow_accept_risk.add_argument(
        "--confirmed",
        action="store_true",
        help="Required proof that a human explicitly accepted the remaining risks.",
    )
    add_no_ones_sync_argument(flow_accept_risk)
    flow_accept_risk.add_argument("--format", choices=("json", "text"), default="text")

    flow_task = flow_subparsers.add_parser(
        "task",
        help="Update task.md rows or STATUS.md design task rows.",
    )
    flow_task_subparsers = flow_task.add_subparsers(dest="flow_task_command", required=True)
    flow_task_set = flow_task_subparsers.add_parser(
        "set",
        help="Set a task state with timestamps and reopen semantics.",
    )
    flow_task_set.add_argument("--feature", default=None)
    flow_task_set.add_argument("--task-id", required=True, help="D-xx selects the design task table.")
    flow_task_set.add_argument("--status", required=True, choices=FLOW_TASK_STATES)
    flow_task_set.add_argument(
        "--confirm-gates",
        action="store_true",
        help="Required when setting a task to 已完成.",
    )
    flow_task_set.add_argument("--note", default=None, help="Append a note to the task row.")
    add_no_ones_sync_argument(flow_task_set)
    flow_task_set.add_argument("--format", choices=("json", "text"), default="text")
    flow_task_ensure_small = flow_task_subparsers.add_parser(
        "ensure-small",
        help="Persist the single T-01 development task required by a small feature.",
    )
    flow_task_ensure_small.add_argument("--feature", default=None)
    flow_task_ensure_small.add_argument("--description", required=True)
    flow_task_ensure_small.add_argument("--write-scope", default="")
    flow_task_ensure_small.add_argument(
        "--acceptance",
        default="目标测试通过；受影响模块全量测试通过；资金安全与任务级架构审查闭环",
    )
    add_no_ones_sync_argument(flow_task_ensure_small)
    flow_task_ensure_small.add_argument("--format", choices=("json", "text"), default="text")

    flow_confirm = flow_subparsers.add_parser(
        "confirm",
        help="Confirm the current waiting-confirmation breakpoint step as done.",
    )
    flow_confirm.add_argument("--feature", default=None)
    add_no_ones_sync_argument(flow_confirm)
    flow_confirm.add_argument("--format", choices=("json", "text"), default="text")

    flow_rollback = flow_subparsers.add_parser(
        "rollback",
        help="Reset a step back to pending (default: affected closure; archive current cycle rows).",
    )
    flow_rollback.add_argument("--feature", default=None)
    flow_rollback.add_argument("--step", required=True, help=PIPELINE_STEP_ALIAS_HELP)
    flow_rollback.add_argument("--reason", default=None, help="Reason appended to each reset note.")
    flow_rollback_scope = flow_rollback.add_mutually_exclusive_group()
    flow_rollback_scope.add_argument(
        "--affected-only",
        dest="all_downstream",
        action="store_false",
        help="Reset only the affected closure (target step + artifact-dependent downstream); default.",
    )
    flow_rollback_scope.add_argument(
        "--all-downstream",
        dest="all_downstream",
        action="store_true",
        help="Reset every pipeline row from the target step to the end (legacy semantics).",
    )
    flow_rollback.set_defaults(all_downstream=False)
    flow_rollback.add_argument(
        "--override-clarify-guard",
        dest="override_clarify_guard",
        action="store_true",
        help="Bypass the collecting-batch guard (recorded as an intervention).",
    )
    flow_rollback.add_argument("--format", choices=("json", "text"), default="text")

    lint = subparsers.add_parser(
        "lint",
        help="适用于 Java 模块：Run heuristic static checks (naming/layers/di) against a target Java repo.",
        description=(
            "适用于 Java 模块；非 Java 模块（前端 / Node 等）不调用 lint naming/layers/di。"
            " Run heuristic static checks (naming/layers/di) against a target Java repo."
        ),
    )
    lint_subparsers = lint.add_subparsers(dest="lint_command", required=True)
    for lint_group, lint_help in (
        ("naming", "Check data-object suffix layering and collection naming."),
        ("layers", "Check cross-layer import / dependency direction."),
        ("di", "Check Spring bean host-new / lazy-factory patterns."),
        ("all", "Run all lint rule groups."),
    ):
        lint_group_parser = lint_subparsers.add_parser(lint_group, help=lint_help)
        lint_group_parser.add_argument(
            "--path",
            default=".",
            metavar="<目标仓路径>",
            help="Target repo root to scan (default: current directory).",
        )
        lint_group_parser.add_argument(
            "--rule",
            dest="rule",
            action="append",
            default=None,
            metavar="<id>",
            help="Restrict to specific rule id(s) within the group; repeatable.",
        )
        lint_group_parser.add_argument("--format", choices=("json", "text"), default="text")

    project = subparsers.add_parser(
        "project",
        help="Read-only project profile commands (stack detection).",
    )
    project_subparsers = project.add_subparsers(dest="project_command", required=True)
    project_detect = project_subparsers.add_parser(
        "detect",
        help=(
            "Detect project/module stack from manifests and config files. Read-only: never runs "
            "repository scripts, imports user code or touches the network."
        ),
    )
    project_detect.add_argument("--path", default=".", help="Target directory (default: current directory).")
    project_detect.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore the profile cache and always run detection.",
    )
    project_detect.add_argument(
        "--write-cache",
        action="store_true",
        help="Write the detected profile to ~/.config/devkit/cache/project-profiles/ (default: no write).",
    )
    project_detect.add_argument("--format", choices=("json", "text"), default="text")
    project_rules = project_subparsers.add_parser(
        "rules",
        help=(
            "Print the effective rule_context (routing, per-module binding, rule closure). Read-only: never "
            "mounts, switches sources, pulls or writes caches."
        ),
    )
    project_rules.add_argument("--path", default=".", help="Workspace directory (default: current directory).")
    project_rules.add_argument(
        "--module",
        default=None,
        metavar="<id>",
        help="Only output this module (relative path, root is '.'); default: all modules.",
    )
    project_rules.add_argument(
        "--stage",
        choices=("design", "coding"),
        default="coding",
        help="Closure stage (default: coding).",
    )
    project_rules.add_argument("--format", choices=("json", "text"), default="text")

    flow_complete = flow_subparsers.add_parser(
        "complete",
        help="Mark the feature 已完成, render its HTML report, and open it by default.",
    )
    flow_complete.add_argument("--feature", default=None)
    flow_complete.add_argument(
        "--no-open",
        action="store_true",
        help="Generate the HTML report without opening it in the system browser.",
    )
    add_no_ones_sync_argument(flow_complete)
    flow_complete.add_argument("--format", choices=("json", "text"), default="text")

    flow_archive = flow_subparsers.add_parser(
        "archive",
        help="Mark the feature 已归档 so it leaves the active feature set.",
    )
    flow_archive.add_argument("--feature", default=None)
    flow_archive.add_argument("--reason", default=None)
    add_no_ones_sync_argument(flow_archive)
    flow_archive.add_argument("--format", choices=("json", "text"), default="text")

    flow_clarify = flow_subparsers.add_parser(
        "clarify",
        help="Collect and apply feature clarifications as one durable batch.",
    )
    flow_clarify_subparsers = flow_clarify.add_subparsers(
        dest="flow_clarify_command", required=True
    )

    clarify_open = flow_clarify_subparsers.add_parser(
        "open", help="Open a clarification batch for the selected feature."
    )
    clarify_open.add_argument("--feature", default=None)
    clarify_open.add_argument("--batch-id", default=None)
    clarify_open.add_argument("--format", choices=("json", "text"), default="text")

    clarify_add = flow_clarify_subparsers.add_parser(
        "add", help="Add one question to the collecting batch."
    )
    clarify_add.add_argument("--feature", default=None)
    clarify_add.add_argument("--question-id", required=True)
    clarify_add.add_argument("--question", required=True)
    clarify_add.add_argument("--category", default="general")
    clarify_add.add_argument("--required", action="store_true")
    clarify_add.add_argument("--depends-on", action="append", default=[])
    clarify_add.add_argument("--recommendation", default="")
    clarify_add.add_argument("--source", default="")
    clarify_add.add_argument(
        "--type",
        dest="question_type",
        choices=("choice", "multi", "text", "confirm"),
        default="text",
    )
    clarify_add.add_argument("--option", action="append", default=[])
    clarify_add.add_argument("--context", default="")
    clarify_add.add_argument("--format", choices=("json", "text"), default="text")

    clarify_add_batch = flow_clarify_subparsers.add_parser(
        "add-batch",
        help="Add questions from a JSON file to the collecting batch.",
    )
    clarify_add_batch.add_argument("--feature", default=None)
    clarify_add_batch.add_argument("--file", required=True)
    clarify_add_batch.add_argument("--format", choices=("json", "text"), default="text")

    clarify_answer = flow_clarify_subparsers.add_parser(
        "answer", help="Record an answer without applying document changes."
    )
    clarify_answer.add_argument("--feature", default=None)
    clarify_answer.add_argument("--question-id", required=True)
    clarify_answer.add_argument("--answer", required=True)
    clarify_answer.add_argument("--format", choices=("json", "text"), default="text")

    clarify_present = flow_clarify_subparsers.add_parser(
        "present",
        help="Delegate to html-ui present for the current clarification batch.",
    )
    clarify_present.add_argument("--feature", default=None)
    clarify_present.add_argument("--format", choices=("json", "text"), default="text")

    clarify_status = flow_clarify_subparsers.add_parser(
        "status", help="Show the current clarification batch."
    )
    clarify_status.add_argument("--feature", default=None)
    clarify_status.add_argument("--format", choices=("json", "text"), default="text")

    clarify_apply = flow_clarify_subparsers.add_parser(
        "apply", help="Seal a fully answered batch for one impact-analysis cycle."
    )
    clarify_apply.add_argument("--feature", default=None)
    clarify_apply.add_argument("--format", choices=("json", "text"), default="text")

    clarify_close = flow_clarify_subparsers.add_parser(
        "close", help="Close an applied batch after recording its change entry."
    )
    clarify_close.add_argument("--feature", default=None)
    clarify_close.add_argument("--change-log-id", required=True)
    clarify_close.add_argument("--format", choices=("json", "text"), default="text")

    clarify_configure = flow_clarify_subparsers.add_parser(
        "configure", help="Configure batch clarification interaction."
    )
    clarify_configure.add_argument("--scope", choices=("global", "project"), default="project")
    clarify_configure.add_argument("--mode", choices=("batch", "single"), default=None)
    clarify_configure.add_argument(
        "--apply",
        dest="apply_policy",
        choices=("after-all-answered",),
        default=None,
    )
    clarify_configure.add_argument("--inherit", action="store_true")
    clarify_configure.add_argument("--clear", action="store_true")
    clarify_configure.add_argument("--format", choices=("json", "text"), default="text")

    flow_verify = flow_subparsers.add_parser(
        "verify",
        help="Read-only consistency inspection of STATUS.md, task.md and ONES outbox.",
    )
    flow_verify.add_argument("--feature", default=None)
    flow_verify.add_argument("--format", choices=("json", "text"), default="text")

    flow_mode = flow_subparsers.add_parser(
        "mode",
        help="Confirm the development mode; append the mode S5.x rows and close S0.5-H.",
    )
    flow_mode.add_argument("--feature", default=None)
    flow_mode.add_argument("--mode", required=True, choices=FLOW_MODES)
    add_no_ones_sync_argument(flow_mode)
    flow_mode.add_argument("--format", choices=("json", "text"), default="text")

    flow_migrate_steps = flow_subparsers.add_parser(
        "migrate-steps",
        help="Insert missing config-enabled pipeline rows and rewrite affected dependencies.",
    )
    flow_migrate_steps.add_argument("--feature", default=None)
    flow_migrate_steps.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the row diff without writing STATUS.md.",
    )
    flow_migrate_steps.add_argument("--format", choices=("json", "text"), default="text")

    flow_report = flow_subparsers.add_parser(
        "report",
        help="Render workflow-visualization.html from STATUS.md/task.md via the embedded template.",
    )
    flow_report.add_argument("--feature", default=None)
    flow_report.add_argument(
        "--output",
        default=None,
        help="Output path (relative to workspace); defaults to the feature directory.",
    )
    flow_report.add_argument("--format", choices=("json", "text"), default="text")

    flow_dashboard = flow_subparsers.add_parser(
        "dashboard",
        help="Render dashboard.html (process-state view) from STATUS.md/task.md via the embedded template.",
    )
    flow_dashboard.add_argument("--feature", default=None)
    flow_dashboard.add_argument(
        "--open",
        action="store_true",
        help="Open the rendered dashboard via the platform default browser dispatch.",
    )
    flow_dashboard.add_argument("--format", choices=("json", "text"), default="text")

    template = subparsers.add_parser(
        "template",
        help="Print an embedded spec template (fallback when spec/template/ is missing).",
    )
    template.add_argument("name", choices=embedded_template_names())
    template.add_argument("--format", choices=("json", "text"), default="text")

    self_update = subparsers.add_parser(
        "self-update",
        help="Install the latest devkit-flow from the public npm registry.",
        description=(
            "Query the latest version with npm view from https://registry.npmjs.org and install "
            "it with npm i -g devkit-flow@latest; --check only reports "
            "whether an update is available and never modifies anything."
        ),
    )
    self_update.add_argument(
        "--check",
        action="store_true",
        help="Only report whether an update is available; never modify anything.",
    )
    self_update.add_argument("--format", choices=("json", "text"), default="text")

    upgrade = subparsers.add_parser(
        "upgrade",
        help="Upgrade the global CLI, then synchronize runtime plugins from that CLI release.",
        description=(
            "Use devkit-flow as the single upgrade authority. The command first upgrades the global npm package, "
            "then installs or updates the selected runtime plugin from the isolated payload bundled with that CLI. "
            "--check is read-only and reports both the npm version and persistent plugin activation state."
        ),
    )
    upgrade.add_argument(
        "--runtime",
        choices=("claude-code", "codex", "grok-build", "all"),
        default="all",
        help="Which runtime plugin to check or synchronize (default: all).",
    )
    upgrade.add_argument(
        "--check",
        action="store_true",
        help="Read-only: check CLI freshness and runtime plugin activation without installing or deleting anything.",
    )
    upgrade.add_argument(
        "--force",
        action="store_true",
        help="Uncertified local experiment without release evidence.",
    )
    upgrade.add_argument(
        "--no-sso-login",
        action="store_true",
        help="Accepted for compatibility. This build does not prompt for identity.",
    )
    upgrade.add_argument("--format", choices=("json", "text"), default="text")

    setup = subparsers.add_parser(
        "setup",
        help="Initialize a workspace: specrules, routing files, and project rules.",
        description=(
            "Idempotent workspace bootstrap. Requires the global devkit-flow entrypoint and "
            "does not copy an executable into the project. Provisions specrules/ as a git submodule by default (plain file "
            "copy/incremental sync via --specrules-mode copy), writes minimal AGENTS.md / "
            "CLAUDE.md / project-rules skeletons when missing, and optionally renders "
            ".cursor/agents/ with --with-cursor."
        ),
    )
    setup.add_argument("--target", default=".", help="Workspace root (default: current directory).")
    setup.add_argument(
        "--with-cursor",
        action="store_true",
        help="Also render .cursor/agents/ from DEVKIT_AGENTS_SOURCE or the devkit repo's packages/agents/.",
    )
    setup.add_argument(
        "--skip-specrules-pull",
        action="store_true",
        help="Offline mode: use the local specrules cache as-is, never run git clone/pull.",
    )
    setup.add_argument(
        "--allow-new-specrules-files",
        action="store_true",
        help=(
            "When the workspace already has specrules/, also create canonical files that are "
            "missing locally (default: only update existing files, never add new ones)."
        ),
    )
    setup.add_argument(
        "--specrules-mode",
        choices=("submodule", "copy"),
        default="submodule",
        help=(
            "How to provision specrules/: git submodule mount (default; falls back to copy mode "
            "outside git repositories) or plain file copy/incremental sync from the local cache."
        ),
    )
    setup.add_argument(
        "--no-sso-login",
        action="store_true",
        help="Accepted for compatibility. This build does not prompt for identity.",
    )
    setup.add_argument("--format", choices=("json", "text"), default="text")

    workspace_doctor = subparsers.add_parser(
        "doctor",
        help="Read-only workspace health check; always exits 0.",
    )
    workspace_doctor.add_argument("--target", default=".", help="Workspace root (default: current directory).")
    workspace_doctor.add_argument("--format", choices=("json", "text"), default="text")

    runtime = subparsers.add_parser(
        "runtime",
        help="Manage AI runtime plugin installations (claude-code / codex / grok-build).",
    )
    runtime_subparsers = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_update = runtime_subparsers.add_parser(
        "update",
        help="Install or update the devkit plugin for each AI runtime.",
        description=(
            "Update every selected runtime through the unified lifecycle "
            "(claude-code → codex → grok-build). "
            "A failing runtime is reported and does not stop the others."
        ),
    )
    runtime_update.add_argument(
        "--runtime",
        choices=("claude-code", "codex", "grok-build", "all"),
        default="all",
        help="Which runtime to update (default: all).",
    )
    runtime_update.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Only print the commands that would run; never execute anything.",
    )
    runtime_update.add_argument(
        "--force",
        action="store_true",
        help="Uncertified local experiment without release evidence.",
    )
    runtime_update.add_argument("--timeout-seconds", type=int, default=10)
    runtime_update.add_argument("--format", choices=("json", "text"), default="text")

    runtime_check = runtime_subparsers.add_parser(
        "check",
        help="Read-only check that a runtime plugin is installed and enabled.",
    )
    runtime_check.add_argument(
        "--runtime",
        required=True,
        choices=("claude-code", "codex", "grok-build", "all"),
    )
    runtime_check.add_argument("--target", default=".")
    runtime_check.add_argument(
        "--hook-json",
        action="store_true",
        help="Emit a SessionStart hook envelope; never blocks session startup.",
    )
    runtime_check.add_argument("--timeout-seconds", type=int, default=10)
    runtime_check.add_argument(
        "--force",
        action="store_true",
        help=(
            "Check the experimental lifecycle root written by --force "
            "install/update instead of the formal root (R-25, BC-09)."
        ),
    )
    runtime_check.add_argument("--format", choices=("json", "text"), default="text")
    runtime_check.add_argument(
        "--hook-event",
        choices=("SessionStart", "PreToolUse"),
        default=None,
        help="Hook event for the check envelope (SessionStart uses the global-hint envelope).",
    )

    runtime_turn_start = runtime_subparsers.add_parser(
        "turn-start",
        help="Internal: UserPromptSubmit injects the next watch excerpt (always exit 0).",
    )
    runtime_turn_start.add_argument(
        "--runtime",
        required=True,
        choices=("claude-code", "codex", "grok-build"),
    )
    runtime_turn_start.add_argument("--target", default=".")
    runtime_turn_start.add_argument(
        "--hook-json",
        action="store_true",
        help="Emit a UserPromptSubmit hook envelope.",
    )
    runtime_turn_start.add_argument("--format", choices=("json", "text"), default="text")

    runtime_session_end = runtime_subparsers.add_parser(
        "session-end",
        help="Internal: SessionEnd hook enqueue session_ended (enqueue-only, always exit 0).",
    )
    runtime_session_end.add_argument(
        "--runtime",
        required=True,
        choices=("claude-code", "codex", "grok-build"),
        help="Runtime that fired the SessionEnd hook (self-reported).",
    )
    runtime_session_end.add_argument("--target", default=".")
    runtime_session_end.add_argument(
        "--hook-json",
        action="store_true",
        help="Emit a SessionEnd hook envelope; never blocks session shutdown.",
    )
    runtime_session_end.add_argument(
        "--hook-event",
        choices=("SessionEnd", "Stop"),
        default="SessionEnd",
        help="Hook event that fired session-end (default SessionEnd; Stop → trigger=stop-hook).",
    )
    runtime_session_end.add_argument("--format", choices=("json", "text"), default="text")

    runtime_install = runtime_subparsers.add_parser(
        "install",
        help="Install the managed runtime artifact through the unified lifecycle.",
    )
    runtime_install.add_argument("--runtime", required=True, metavar="<runtime|all>")
    runtime_install.add_argument("--timeout-seconds", type=int, default=10)
    runtime_install.add_argument(
        "--force",
        action="store_true",
        help="Uncertified local experiment: no formal ACTIVE state or evidence.",
    )
    runtime_install.add_argument("--format", choices=("json", "text"), default="text")

    runtime_uninstall = runtime_subparsers.add_parser(
        "uninstall",
        help="Uninstall the managed runtime artifact through the unified lifecycle.",
    )
    runtime_uninstall.add_argument("--runtime", required=True, metavar="<runtime|all>")
    runtime_uninstall.add_argument("--timeout-seconds", type=int, default=10)
    runtime_uninstall.add_argument("--force", action="store_true")
    runtime_uninstall.add_argument("--format", choices=("json", "text"), default="text")

    runtime_rollback = runtime_subparsers.add_parser(
        "rollback",
        help="Roll one runtime back to a verified snapshot artifact (single runtime).",
    )
    runtime_rollback.add_argument("--runtime", required=True, metavar="<runtime>")
    runtime_rollback.add_argument("--to", default=None, metavar="<artifactId>")
    runtime_rollback.add_argument("--timeout-seconds", type=int, default=10)
    runtime_rollback.add_argument("--force", action="store_true")
    runtime_rollback.add_argument("--format", choices=("json", "text"), default="text")

    runtime_recover = runtime_subparsers.add_parser(
        "recover",
        help="Explicitly recover one unfinished lifecycle transaction (single runtime).",
    )
    runtime_recover.add_argument("--runtime", required=True, metavar="<runtime>")
    runtime_recover.add_argument("--transaction-id", default=None, metavar="<uuid>")
    runtime_recover.add_argument("--timeout-seconds", type=int, default=10)
    runtime_recover.add_argument(
        "--force",
        action="store_true",
        help=(
            "Recover a transaction under the experimental lifecycle root "
            "written by --force install/update (R-25, BC-09)."
        ),
    )
    runtime_recover.add_argument("--format", choices=("json", "text"), default="text")

    migrate = subparsers.add_parser(
        "migrate",
        help="Safely migrate legacy workspace-managed runtime files.",
    )
    migrate_subparsers = migrate.add_subparsers(dest="migrate_command", required=True)
    legacy_workspace = migrate_subparsers.add_parser(
        "legacy-workspace",
        help="Remove only verified DevKit-generated files after runtime plugins are active.",
    )
    legacy_workspace.add_argument("--target", default=".")
    legacy_workspace.add_argument(
        "--runtime",
        required=True,
        choices=("claude-code", "codex", "all"),
    )
    legacy_workspace.add_argument("--apply", action="store_true")
    legacy_workspace.add_argument(
        "--yes",
        action="store_true",
        help="Confirm deletion in non-interactive use; without it --apply prompts on a TTY.",
    )
    legacy_workspace.add_argument("--format", choices=("json", "text"), default="text")

    plugin = subparsers.add_parser(
        "plugin",
        help="Install a runtime plugin carried by the CLI.",
    )
    plugin_subparsers = plugin.add_subparsers(dest="plugin_command", required=True)
    plugin_install = plugin_subparsers.add_parser(
        "install",
        help="Render and install the Kimi Code plugin into ~/.kimi-code/plugins/managed/.",
        description=(
            "Render the bundled agents + kimi overrides into Kimi inline skills and commands, "
            "install them as the managed plugin devkit under "
            "~/.kimi-code/plugins/managed/, and register it in "
            "~/.kimi-code/plugins/installed.json (the previous file is backed up to "
            "installed.json.bak). Run /plugins reload or /new in the Kimi TUI afterwards."
        ),
    )
    plugin_install.add_argument(
        "--runtime", choices=("claude-code", "codex", "grok-build"), default="claude-code"
    )
    plugin_install.add_argument("--scope", choices=("project", "global"), default="project", help="Install scope (project: .agents/plugins, global: ~/.gemini/config/plugins)")
    plugin_install.add_argument("--target", default=".", help="Target workspace path (default: current directory)")
    plugin_install.add_argument("--format", choices=("json", "text"), default="text")

    sessions = subparsers.add_parser(
        "sessions",
        help="List AI runtime sessions recorded for this workspace (session registry).",
    )
    sessions_subparsers = sessions.add_subparsers(dest="sessions_command", required=True)
    sessions_list = sessions_subparsers.add_parser(
        "list",
        help="List recorded sessions, deduplicated by runtime + session id.",
    )
    sessions_list.add_argument("--target", default=".", help="Workspace root (default: current directory).")
    sessions_list.add_argument(
        "--feature",
        default=None,
        help="Feature name; marks sessions that drove its flow steps (drove_flow).",
    )
    sessions_list.add_argument("--format", choices=("json", "text"), default="text")

    # 内部命令（不出现在顶层 help 命令列表）：pi extension 等宿主回调写 session registry。
    session_mark = subparsers.add_parser(
        "session-mark",
        description="Internal: append one session marker to the central registry (host callbacks).",
    )
    session_mark.add_argument("--runtime", required=True, choices=CANONICAL_RUNTIME_ORDER)
    session_mark.add_argument("--target", default=".", help="Workspace root (default: current directory).")
    session_mark.add_argument("--session-id", default=None)
    session_mark.add_argument("--pid", type=int, default=None)
    session_mark.add_argument("--session-file", default=None)
    session_mark.add_argument("--transcript-path", default=None)
    session_mark.add_argument("--format", choices=("json", "text"), default="json")

    args = parser.parse_args()
    if args.command == "specrules" and args.specrules_command == "configure":
        has_switch = bool(args.enable or args.disable or args.follow_global)
        has_url = bool(args.repo_url or args.use_default_repo)
        if not has_switch and not has_url:
            specrules_configure.error(
                "one of the arguments --enable --disable --follow-global "
                "--repo-url --use-default-repo is required"
            )
    if (
        args.command == "specrules"
        and args.specrules_command == "catalog"
        and args.specrules_catalog_command == "configure"
        and args.repo_url is None
        and not args.remove
        and args.routing is None
        and args.module is None
    ):
        specrules_catalog_configure.error("one of the arguments --repo-url --remove --routing --module is required")
    if args.command == "flow" and args.flow_command == "batch-set":
        members = args.batch_members or []
        if not members:
            flow_batch_set.error("at least one of the arguments --step --task is required")
        if any(kind == "step" for kind, _ in members) and not args.status:
            flow_batch_set.error("the argument --status is required when --step is given")
    if (
        args.command == "telemetry"
        and getattr(args, "telemetry_command", None) == "flush"
        and getattr(args, "background", False)
        and getattr(args, "replay_quarantine", False)
    ):
        # 016 D-65④：组合无效，直接报参数错误（argparse 口径 exit 2），不静默忽略 replay。
        telemetry_flush.error("argument --replay-quarantine: not allowed with argument --background")
    if (
        args.command == "pr-review"
        and args.pr_review_command == "worktree"
        and args.worktree_command == "cleanup"
    ):
        if not args.processes_stopped:
            pr_review_worktree_cleanup.error("the argument --processes-stopped is required")
        if args.all_stale and args.owner:
            pr_review_worktree_cleanup.error("--owner cannot be combined with --all-stale")
        if args.lease and not args.owner:
            pr_review_worktree_cleanup.error("--lease requires --owner")
    return args
