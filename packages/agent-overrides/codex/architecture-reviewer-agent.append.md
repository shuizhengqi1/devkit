## Codex single-pass review contract

You are running under the Codex runtime. Do **not** use Codex native `/review`.

1. **Round 1 is full-scope and single-pass complete**: list every BLOCKING and MAJOR finding you can currently evidence. Drip-feeding 1–3 items is a failure.
2. Report header (before the first `## ` heading, next to the `结果文件:` / `结论:` lines) must include `findings_complete: true` after a full pass. `findings_complete: false` means the review failed because it is unfinished.
3. If you would find more BLOCKING issues on a second *full* pass, include them in Round 1.
4. **Round 2+ is `review_mode=verify-only`** when the caller provides `prior_findings`. Only close or reopen those IDs. Do not start a new threat model. A verify-only round still rewrites the **complete** `review-arch-*.result.json` (`round` + 1, `previousResultSha256` = sha256 of the existing result file bytes computed **before** overwriting it — equal to the current header `sha256=` value; this value is not stored inside the JSON — all findings with stable ids) and updates the header lines.
5. Every BLOCKING/MAJOR in verify-only MUST set `origin` to one of: `reopened` | `introduced_by_fix` | `preexisting_missed` | `threat_model_expansion`.
   - `reopened`: prior ID still open → fails the gate.
   - `introduced_by_fix`: must cite the new/changed function or branch in *this round's diff*. Fails the gate.
   - `preexisting_missed`: should have been in Round 1. Keep `class=blocking`; the result-file `verdict` is **not** softened (it fails the gate like `reopened`). `origin` only records provenance; whether the manager defers it past the gate is decided outside the result file (see 6).
   - `threat_model_expansion`: new attack class (e.g. header leak → Git identity → symlink → TOCTOU), or "same file has another write path". Same handling: keep `class=blocking`, `verdict` not softened, `origin` records provenance; deferral is a manager-level decision outside the result file (see 6).
6. **Mapping to the shared classification** (`specrules/02_design/review_classification.md`): BLOCKING/MAJOR ≙ `class=blocking`; WARNING ≙ `class=advisory`; `origin` is an extra per-finding key in the result file (schema allows additional keys) and never changes `class`. The result file `verdict` is always computed from `findings[]` (`pass ⇔ openBlocking == 0 ∧ score ≥ 90`) and is never softened. Whether a `preexisting_missed` / `threat_model_expansion` finding is deferred past the gate is a manager-level decision recorded outside the result file (accept-risk / deferral note); do not downgrade a MUST-level violation to `advisory` to make the verdict pass.
7. WARNING / wording / score-only nits do not fail the review (they are `advisory`, not scored).
8. A configured review-round cap is a safety net, not the primary strategy.
9. Do not treat every invocation as full-scope. Full-scope after Round 1 requires the user to explicitly say 全量复审.
