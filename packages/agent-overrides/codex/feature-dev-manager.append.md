## Codex review-round guard

Codex runtime only. Shared manager agent body must **not** auto-stop review loops.
Before dispatching another fix+re-review for `S2.1-R` / `S2.2-R` / `S2.3-R` / `S5.5` / `S5.7` / `T-{id}-arch`, run:

`devkit-flow review-rounds check --feature {feature} --step {step} --format json`

Honor `review_mode` from check: `full` only when `attempt=0`; otherwise dispatch `review_mode=verify-only` with `prior_findings` copied from `last_finding_ids` / the previous report. Do not dispatch a full-scope adversarial rescan unless the user explicitly says 全量复审.

If `allowed=false` or `loop_status=human_required`, stop auto re-review and wait for the user. After each review outcome run `review-rounds tick --event failed|passed` and pass `--finding-ids`. When the failed round's BLOCKING items have `origin=introduced_by_fix` *and* a new invariant/class, also pass `--new-class`. Two new classes trip `human_required` even if `attempt < max_rounds`. Reopened prior IDs: tick `--event failed` without `--new-class`. After human takeover, `review-rounds reset` may restart the counter.

Round 1 must still be single-pass complete (`findings_complete`). Do not call Codex native `/review`. Do not treat the configured round cap as the primary review strategy.

「自动推进」covers the shared-body S5 continuous-execution contract only (Red/Green/Coverage/Refactor and the next task dispatch within the S5-H authorization; pauses only on the closed 7-code list, recorded as `[pause:<code>]`). It does **not** include accept-risk, `threat_model_expansion`, or a new full-scope review. Deferring a `preexisting_missed` / `threat_model_expansion` finding is a manager decision recorded outside the result file (accept-risk + `DECISIONS.md` entry); never edit `review-*.result.json`.

If the user says 不管了 / 跳过这个架构问题 / 继续 feature while task-architecture is blocked or `human_required`, run:

`devkit-flow flow accept-risk --gate S5.5 --task-id {task_id} --reason "{user text}" --confirmed`

(traditional mode: `--gate S5.3`). Then complete the task with architecture=`waived` and continue the same feature. Do **not** interpret those phrases as 退出 feature-dev-manager. Do not falsify the review as PASS. S5-R / S5.7 / S5-E2E still cannot be waived.

## Codex worker lifecycle (V1 close_agent)

Codex runtime only. Shared manager / shared SKILL / Kimi / Grok / Claude / Pi projections must **not** mention `close_agent`.

Dispatch Devkit workers as **one-shot** Codex subagents:

1. `spawn_agent` with `fork_turns: "none"`. Put the task brief in the spawn prompt. Do **not** use `fork_turns: "all"` (it copies the whole parent transcript into every child).
2. While you still have local work, do not `wait_agent`. When idle, `wait_agent` with `timeout_ms` **300000–600000** (5–10 minutes, up to 1 hour if a known long gate such as full `test-all`). This timeout is the **parent wait**, not the child: it does not interrupt the child. A completed child wakes the wait immediately. Do **not** use 30000 ms empty polls.
3. After the structured result is consumed, call `close_agent` on that worker **unless** you still need `followup_task` on the same implementer. Keep reviewers/executors/tdd one-shot: consume then close.
4. If the tool list has no `close_agent` (Codex multi-agent V2), skip close; do not invent the tool.
5. At every 阶段屏障, `list_agents` and `close_agent` remaining completed one-shots. `not found` means already gone. If `close_agent` hangs on an interrupted/stale child, **只试一次** / try once, then interrupt that tool call and continue; do not retry unbounded (Codex `close_agent` can wait forever on `wait_until_terminated`).
6. `wait_agent` does not free the slot. `completed` is a status, not closed. Completed agents remain open and count toward `agents.max_threads` until `close_agent`.

The `wait_agent` `timeout_ms` **300000–600000** (5–10 minutes) contract above is the **sub-agent** wait only. It 不覆盖 bash `html-ui present`. Do not reuse that 5–10 minute window as the present-wait floor.

## Codex html-ui present wait (D-1)

Codex runtime only. Shared manager task 3 / shared SKILL already wait for process exit; this append adds the Codex host-tool contract on top. Do **not** rewrite the shared bodies here.

When bash `html-ui present` is running (`devkit-flow html-ui present` / `flow clarify present`):

1. 等到 present 进程 exit（等到该进程退出 / wait until process exit）. Do not treat a short yield as the wait completing.
2. If the host tool uses `yield_time_ms`, the floor is `PRESENT_WAIT_SECONDS * 1000` (`1800000`), or 等到 present 进程 exit. 不得把 30s yield 当 present 下限.
3. `yield_time_ms` 不是 CLI 参数或产品开关名. It is only a Codex tool argument (not a CLI parameter, not a product switch). Do not invent a CLI flag for it.
4. Before present returns, 禁止 `final_answer` / `task_complete`. 不得 call `final_answer` or `task_complete` while the present process has not exited.

`wait_agent` 不覆盖 bash `html-ui present`. Keep the existing sub-agent `wait_agent` 300000–600000 contract unchanged.
