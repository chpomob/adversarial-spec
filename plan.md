---
spec: "file-based-prompts"
version: "1.0"
author: "adversarial-plan"
based-on: "adversarial-spec"
findings-input: true
---

# Implementation Plan

## Steps

### P1: Expose ReadGatePolicy and artifact helpers in `scripts/phases/__init__.py`
- **Files:** [scripts/phases/__init__.py, tests/test_phases.py]
- **Description:** (a) Add `ReadGatePolicy` and `ReadGateResult` to the `__init__` re-exports so phase modules can `from . import ReadGatePolicy` instead of importing directly from `adversarial_common.readgate`. (b) Add a helper `artifact_temp_dir(workdir)` that returns a `Path` to `<base>/<workdir_name>/` where `<base>` is `/tmp/adversarial-spec/` (overridable via env var `ADV_SPEC_TEMP_DIR` or a `tmp_path` fixture in tests), creating it if it does not exist. The `workdir` parameter (a `Path`) is used to derive a unique subdirectory name via `workdir.resolve().name` so that two pipelines running against different workdirs on the same host do not collide on artifact paths. The env var `ADV_SPEC_TEMP_DIR` replaces the entire base path (the subdirectory derivation from workdir still applies). (c) Extend `fake_run` in `tests/test_phases.py` to support variable per-call output: accept an optional `stdouts` list (or augment the existing `side_effect` to return `(stdout, stderr, code)` tuples from a queue). The default `stdout`/`stderr`/`code` kwargs remain the fallback when the queue is exhausted. This is the foundation that P2–P5 depend on.
- **Dependencies:** []
- **Tests:** Existing `test_try_parse_json_strategies`, `test_validate_spec_*`, `test_resolve_persona_*` must still pass. Add a small self-check test `test_fake_run_variable_output` that verifies the queue mechanism returns two different `stdout` values on consecutive calls and falls back to the default on third call.
- **Risks:** Low. Pure import plumbing + a well-scoped test-helper extension. The env-var override must not leak side effects between parallel test runs; use `monkeypatch` in tests. The `fake_run` queue must be thread-safe for parallel test invocation (Python list `.pop(0)` is atomic under the GIL for single-threaded tests).

### P2: File-based prompts and ReadGatePolicy in VERIFY phase (R1, R4)
- **Files:** [scripts/phases/phase_verify.py]
- **Description:** Rewrite `run_verify` prompt construction so that:
  1. The findings list is written as JSON to `<temp_dir>/verify_findings.json` (using the helper from P1) **before** the prompt is built.
  2. The prompt no longer embeds `spec_text` or `json.dumps(findings, …)`. Instead it contains `READ: <spec_path>` and `READ: <findings_path>` marker instructions telling the model to use its `read` tool.
  3. After every model call (`_attempt`), the output is checked with a `ReadGatePolicy` instance for the findings file path. On `WARNING` (first miss): re-run the same prompt with a `READ MARKER REMINDER: …` suffix appended. On `HARD_ERROR` (second consecutive miss): return `{"phase": "verify", "exit_code": 1, "error": "readgate hard error for <path>"}`.
  4. The readgate check is independent of the existing JSON-schema retry: both can fire in sequence. The JSON retry output also passes through readgate checking.
- **Dependencies:** [P1]
- **Tests:** Write in P5. Manually verify that the prompt string contains neither `spec_text` nor `json.dumps(findings)` via a temporary inline assertion (or rely on P5).
- **Risks:** The interaction between readgate retry and JSON retry could produce up to 3 model calls (initial → readgate retry → JSON retry). Both retry paths must merge runtime metadata via `merge_runtime()` and preserve `provider_history`. The `_attempt` closure captures the prompt — the readgate reminder must be appended to the *same* base prompt, not a stale closure. **Compound scenario (R4):** when a response simultaneously misses the READ marker AND fails JSON schema validation, the readgate retry fires first. The JSON retry that follows runs under the *same* `ReadGatePolicy` instance (the miss-streak continues), so if the JSON-retry call also misses the READ marker it escalates to HARD_ERROR. The readgate check always runs before JSON validation on every model output — a response must pass readgate before its JSON payload is examined.

### P3: File-based prompts and ReadGatePolicy in REVISE phase (R3, R4)
- **Files:** [scripts/phases/phase_revise.py]
- **Description:** Rewrite `run_revise` prompt construction:
  1. Write findings as JSON to `<temp_dir>/revise_findings.json` before building the prompt.
  2. Prompt no longer embeds `json.dumps(findings, …)`. Instead it contains `READ: <findings_path>` and instructs the model to read the findings file **and** to edit `spec.md` on disk.
  3. After the model call, check output with `ReadGatePolicy` for the findings file path. On `WARNING`: re-run with a reminder suffix. On `HARD_ERROR`: fail with exit code 1 and a readgate hard-error message.
  4. The existing spec-validation check (and commit) runs after readgate passes. If the re-run passes readgate but produces a broken spec, the existing validation failure path handles it.
- **Dependencies:** [P1]
- **Tests:** Write in P5. Verify prompt does not contain findings JSON body.
- **Risks:** REVISE currently has a single-shot call path (no retry loop). Adding the readgate retry introduces a second call; both calls' runtime metadata must be merged. The `round_n`-based phase name (`revise_N`) must be used consistently for both calls so the cost ledger tracks both.

### P4: File-based brief in WRITE phase (R2, R4)
- **Files:** [scripts/phases/phase_write.py]
- **Description:** Rewrite `run_write` prompt construction:
  1. Measure `len(brief_text.encode("utf-8"))`. If > 2000 bytes: write the brief to `<temp_dir>/brief.md` and build a prompt with `READ: <brief_path>` instead of the inline brief text. If ≤ 2000 bytes: keep the brief inline (no readgate enforcement needed).
  2. When file-based, wrap the model-call loop with a `ReadGatePolicy` instance for the brief file path. On `WARNING` (first miss): re-run with a reminder. On `HARD_ERROR`: fail with exit code 1 and a readgate hard-error message.
  3. The readgate retry is independent of the existing spec-validation retry loop. The readgate check fires on every model output within the retry loop. A response that passes readgate but fails spec validation triggers the normal correction-feedback retry; that retry's output is again checked by readgate.
  4. The brief file must be written to a directory **outside the workdir** (so `git add -A` does not commit it). Use the helper from P1.
- **Dependencies:** [P1]
- **Tests:** Write in P5. Verify prompt references file path (not inline text) when brief > 2000 bytes; verify prompt still inline when brief ≤ 2000 bytes.
- **Risks:** The existing `max_spec_retries` loop already retries on validation failure. Adding readgate retry increases the maximum call count per loop iteration. The brief file path must be generated deterministically for testability (e.g. `<temp_dir>/brief.md`). The `_short_summary` commit-message helper still works because it receives the original `brief_text`, not the file path.

### P5: Tests for file-based prompts and readgate escalation (R5, AC1–AC5)
- **Files:** [tests/test_phases.py]
- **Description:** Add the following tests (append to the existing file, do not rewrite):
  1. **`test_verify_prompt_has_no_embedded_payloads`**: Call `run_verify` with a `fake_run` that captures the prompt. Assert the prompt string does **not** contain substring matches for `spec_text` (use a sentinel `UNIQUE_VERIFY_SENTINEL` written into `spec.md`) and does **not** contain the JSON-serialized findings body. Assert it contains `READ:` markers for both `spec.md` and the findings file path. (AC1)
  2. **`test_write_prompt_file_based_when_brief_large`**: Provide `brief_text` > 2000 bytes (UTF-8). Assert the prompt does **not** contain the brief body; it contains `READ: <brief_path>`. Assert the brief file exists on disk at the expected path. (AC2)
  3. **`test_write_prompt_inline_when_brief_small`**: Provide `brief_text` ≤ 2000 bytes. Assert the prompt contains the brief text inline, not a READ marker for a brief file. (AC2)
  4. **`test_revise_prompt_has_no_embedded_findings`**: Call `run_revise` with a `fake_run` capturing the prompt. Assert the prompt does **not** contain `json.dumps(findings)` body. Assert it contains `READ:` for the findings file path. (AC3)
  5. **`test_readgate_warning_triggers_retry`** (VERIFY): Configure `fake_run` with a `side_effect` that first returns output without a READ marker, then returns output with it. Assert exactly 2 calls were made and the phase succeeds. (AC4)
  6. **`test_readgate_hard_error_fails_phase`** (VERIFY): Configure `fake_run` to return output without a READ marker twice in a row. Assert exit code 1 and error message contains `readgate hard error`. (AC4)
  7. **`test_readgate_warning_retry_revise`**: Same as #5 but for the REVISE phase. (AC4)
  8. **`test_readgate_hard_error_revise`**: Same as #6 but for REVISE. (AC4)
  9. **`test_readgate_file_based_write`**: WRITE with brief > 2000 bytes; first output misses marker, second includes it. Assert 2 calls and success. (AC4)
  10. **`test_readgate_hard_error_write`**: WRITE with brief > 2000 bytes; both outputs miss marker. Assert exit code 1 with hard-error message. (AC4)
  11. **`test_readgate_independent_of_json_retry`** (VERIFY): First output passes readgate but has invalid JSON. JSON retry fires (second call). Second call output also passes readgate and has valid JSON. Assert 2 calls, success, and readgate did not interfere with JSON retry. (R4, AC4)
  12. **`test_write_brief_not_in_workdir_commit`**: WRITE phase with brief > 2000 bytes. Assert the brief file is written to a path outside the workdir (i.e., `workdir.resolve()` is not a parent of `brief_path.resolve()`), so `git add -A` run in the workdir does not pick it up. Also assert the brief file exists on disk at the expected temp-dir path. (AC2)
  13. **`test_readgate_then_json_retry_compound`** (VERIFY): First output misses the READ marker AND has invalid JSON. The readgate WARNING fires first → retry with reminder prompt. Second output (readgate retry) has the READ marker but still has invalid JSON → readgate passes, then JSON validation fails → JSON retry fires. Third output (JSON retry) has the READ marker and valid JSON → success. Assert exactly 3 model calls and phase exit code 0. This is the R4 compound scenario the spec explicitly requires. (R4, AC4)
  14. **`test_all_existing_tests_still_pass`**: Run `pytest tests/test_phases.py -x` and confirm zero failures. The 14 new tests plus all existing ones (≈37 total) must all pass. (R5, AC5)
- **Dependencies:** [P1, P2, P3, P4]
- **Tests:** Self-hosted — this step adds tests.
- **Risks:** The `fake_run` helper was extended in P1 with a per-call output queue; all new readgate tests depend on it. Tests that write temp files must use `tmp_path` fixtures and the env-var override for `ADV_SPEC_TEMP_DIR` so they don't write to `/tmp/adversarial-spec/` on the CI host. Readgate tests for WRITE interact with the spec-validation retry loop; fake a valid spec on the first call to avoid unrelated validation retries muddying the call count.

### P6: Full-branch review gate
- **Files:** [scripts/phases/phase_verify.py, scripts/phases/phase_revise.py, scripts/phases/phase_write.py, scripts/phases/__init__.py, tests/test_phases.py]
- **Description:** Before declaring the implementation ready, perform a full-branch review across all five changed files simultaneously — not per-commit diffs. Verify: (a) every READ marker instruction in prompts is paired with a corresponding `ReadGatePolicy` check after model output; (b) no phase embeds findings/spec/brief text in prompts where the spec requires file-based delivery; (c) all retry paths (`readgate WARNING → retry → HARD_ERROR`, `JSON invalid → retry`, `spec validation → retry`) correctly merge runtime metadata and provider history; (d) the `temp_dir` infrastructure handles concurrent runs (distinct PIDs) without collision; (e) no exit code regressions (0, 1, 2, 3, 5 remain assigned to the same error classes). Per-commit review cannot catch cross-file interactions — e.g., the `__init__.py` export added in P1 being consumed by P2/P3/P4 in different ways, or the readgate retry logic in VERIFY interacting with the JSON retry in a way that leaks metadata.
- **Dependencies:** [P1, P2, P3, P4, P5]
- **Tests:** Run `pytest tests/test_phases.py -v` one final time. Run `grep -rn "spec_text\|json\.dumps(findings\|brief_text" scripts/phases/phase_verify.py scripts/phases/phase_revise.py scripts/phases/phase_write.py` and assert zero hits in prompt strings (the variables may still exist for writing to disk, but must not appear in string concatenation for prompts).
- **Risks:** Cross-file bugs: a signature change to `artifact_temp_dir` in `__init__.py` could break P2–P4 if they import differently. The readgate reminder string appended to prompts could contain characters that break the model's JSON parsing. Path determinism: downstream phases (P2–P4) and their tests (P5) assume artifact paths derived from `artifact_temp_dir(workdir)` are deterministic for a given workdir; the P1 implementation must ensure `workdir.resolve().name` is stable across test runs (use `tmp_path` fixtures with consistent names).

## Ordering rationale

P1 (`__init__.py` exports) must come first — all three phase modules (P2, P3, P4) need `ReadGatePolicy` and `artifact_temp_dir` importable from `.`.

P2 (VERIFY), P3 (REVISE), and P4 (WRITE) are independent of each other but all depend on P1. They are ordered P2 → P3 → P4 because VERIFY is the most complex (two-path readgate + JSON retry interaction), REVISE is simpler (single path, no JSON retry), and WRITE adds a size-threshold decision and interacts with the spec-validation retry loop. Implementing VERIFY first establishes the readgate retry pattern that P3 and P4 follow.

P5 (tests) depends on P1–P4 because the tests exercise the new behavior added in each phase. Tests are written last to validate the complete implementation.

P6 (full-branch review gate) depends on all preceding steps and exists to catch cross-file integration bugs invisible to per-commit review — particularly the readgate retry pattern consistency across phases and the metadata-merging correctness across all retry paths.
