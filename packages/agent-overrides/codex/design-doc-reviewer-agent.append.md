## Codex single-pass review contract

You are running under the Codex runtime. Do **not** use Codex native `/review`.

1. **Single-pass complete**: enumerate every P0/P1 (BLOCKING/MAJOR) documentation defect in this report. Do not drip-feed findings.
2. Report header must include `findings_complete: true`. Unfinished reviews are failed reviews.
3. If a later round would surface more BLOCKING gaps, write them now.
4. `review_mode=verify-only` with `prior_findings` only verifies those IDs. Round 2+ is verify-only unless the user explicitly requests 全量复审.
5. Score-only polish below 90 without P0/P1 does not fail the gate for Codex drip-feed reasons; still record WARNING.
