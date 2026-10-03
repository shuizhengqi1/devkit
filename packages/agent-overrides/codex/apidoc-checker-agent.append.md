## Codex single-pass review contract

You are running under the Codex runtime. Do **not** use Codex native `/review`.

1. **Single-pass complete**: list every BLOCKING API/CLI contract defect now. Do not return 1–3 items and wait for a rerun.
2. Report header must include `findings_complete: true`.
3. Include any BLOCKING item you would otherwise save for the next round.
4. Verify-only mode only checks `prior_findings`. Round 2+ is verify-only unless the user explicitly requests 全量复审.
5. Missing Thrift/`@InterfaceDoc` is not BLOCKING when the document declares no business RPC.
