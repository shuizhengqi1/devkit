## Codex single-pass review contract

You are running under the Codex runtime. Do **not** use Codex native `/review`.

1. **Single-pass complete**: enumerate every `class=blocking` documentation defect (P0/P1 is only a display grouping of `blocking`; WARNING is `advisory`) in this report. Do not drip-feed findings.
2. Report header (before the first `## ` heading, next to the `结果文件:` / `结论:` lines) must include `findings_complete: true`. Unfinished reviews are failed reviews.
3. If a later round would surface more `blocking` gaps, write them now.
4. `review_mode=verify-only` with `prior_findings` only verifies those IDs. Round 2+ is verify-only unless the user explicitly requests 全量复审. A verify-only round still rewrites the **complete** `review-design.result.json` (`round` + 1, `previousResultSha256` = sha256 of the existing result file bytes computed **before** overwriting it — equal to the current header `sha256=` value; this value is not stored inside the JSON — all findings including unchanged ones with stable ids) and updates the header `结果文件:` sha256 / `结论:` lines.
5. Classification and verdict follow `specrules/02_design/review_classification.md` only: `verdict = pass ⇔ openBlocking == 0 ∧ score ≥ 90`, computed from `findings[]`. Do not soften or override the verdict for Codex round-cap reasons; record non-blocking nits as `advisory`.
