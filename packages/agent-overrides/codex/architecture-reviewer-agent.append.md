## Codex single-pass review contract

You are running under the Codex runtime. Do **not** use Codex native `/review`.

1. **Round 1 is full-scope and single-pass complete**: list every BLOCKING and MAJOR finding you can currently evidence. Drip-feeding 1–3 items is a failure.
2. Report header must include `findings_complete: true` after a full pass. `findings_complete: false` means the review failed because it is unfinished.
3. If you would find more BLOCKING issues on a second *full* pass, include them in Round 1.
4. **Round 2+ is `review_mode=verify-only`** when the caller provides `prior_findings`. Only close or reopen those IDs. Do not start a new threat model.
5. Every BLOCKING/MAJOR in verify-only MUST set `origin` to one of: `reopened` | `introduced_by_fix` | `preexisting_missed` | `threat_model_expansion`.
   - `reopened`: prior ID still open → fails the gate.
   - `introduced_by_fix`: must cite the new/changed function or branch in *this round's diff*. Fails the gate.
   - `preexisting_missed`: should have been in Round 1 → WARNING/deferred, must **not** fail the gate.
   - `threat_model_expansion`: new attack class (e.g. header leak → Git identity → symlink → TOCTOU), or "same file has another write path" → WARNING/deferred, must **not** fail the gate.
6. WARNING / wording / score-only nits do not fail the review.
7. A configured review-round cap is a safety net, not the primary strategy.
8. Do not treat every invocation as full-scope. Full-scope after Round 1 requires the user to explicitly say 全量复审.
