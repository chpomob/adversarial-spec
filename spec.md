---
name: "quota-aware-provider-registry"
version: "1.0"
author: "adversarial-spec"
status: "draft"
tags: [adversarial, spec]
targets:
  - file: adversarial-common/adversarial_common/providers.py
    description: "Define and validate the external provider configuration and resolve its single effective path."
  - file: adversarial-common/adversarial_common/quota.py
    description: "Add cached quota collection, provider-state evaluation, role fallback selection, force handling, and selection history."
  - file: adversarial-common/adversarial_common/runner.py
    description: "Resolve a quota-eligible command immediately before each configured model phase and expose selection metadata."
  - file: adversarial-common/adversarial_common/report.py
    description: "Add sanitized provider-selection history to final run data and rendered reports."
  - file: adversarial-common/adversarial_common/tests/test_providers.py
    description: "Cover configuration precedence, schema validation, and model-agnostic alias and role loading."
  - file: adversarial-common/adversarial_common/tests/test_quota.py
    description: "Cover cache single-flight behavior, quota states, thresholds, fallbacks, force modes, and placeholder expansion."
  - file: adversarial-common/adversarial_common/tests/test_runner.py
    description: "Cover per-phase quota selection, explicit-command bypasses, failures, and provider metadata propagation."
  - file: adversarial-common/adversarial_common/tests/test_report.py
    description: "Cover provider history in final and human-readable reports without command or credential leakage."
  - file: adversarial-spec/scripts/adversarial_spec.py
    description: "Accept provider-registry flags and map specification phases to writer, challenger, and verify roles."
  - file: adversarial-spec/tests/test_orchestrator.py
    description: "Verify provider configuration and force flags across specification phases."
  - file: adversarial-plan/scripts/adversarial_plan.py
    description: "Accept provider-registry flags and map planning phases to writer, challenger, and verify roles."
  - file: adversarial-plan/tests/test_orchestrator.py
    description: "Verify provider configuration and force flags across planning phases."
  - file: adversarial-code-loop/scripts/adversarial_loop_v4.py
    description: "Accept provider-registry flags and map build, review, verify, and arbitration phases to their roles."
  - file: adversarial-code-loop/scripts/test_loop_fixes.py
    description: "Verify quota fallback, explicit overrides, reporting, and resume behavior in the code loop."
  - file: adversarial-code-review/scripts/adversarial_review.py
    description: "Accept provider-registry flags and use review and arbiter role chains for review and synthesis phases."
  - file: adversarial-code-review/scripts/check-ai-quota.py
    description: "Add GLM and DeepSeek probes and return all requested quota checks as one parallel JSON batch."
  - file: adversarial-code-review/tests/test_review_flags.py
    description: "Verify registry flags, role mapping, explicit overrides, and final provider history for code review."
  - file: adversarial-code-review/tests/test_quota_checker.py
    description: "Verify GLM and DeepSeek flags, normalized states, parallel batching, and partial-error JSON output."
---

# Quota-Aware Provider Registry for Adversarial Pipelines

## Problem

The specification, planning, code-loop, and code-review pipelines currently launch each configured model command without checking whether its account can accept more work. Exhausted sliding windows, invalid keys, rate limits, and depleted prepaid balances therefore appear during a run as timeouts or provider errors. Users must inspect quota manually, replace commands, and resume the pipeline.

Version 1 introduces one model-agnostic registry in `adversarial-common`. Pipelines identify only the logical role needed by a phase; an external configuration owns provider aliases, commands, quota-result mappings, thresholds, and ordered role fallbacks. Quota information is refreshed at most once per in-process TTL window and shared by all roles and concurrent phases. Cross-process cache coordination, waiting for resets, automatic retry after reset, quota-aware scheduling, cost-based selection, and a quota dashboard remain out of scope.

The following compatibility assumptions apply. A user-supplied legacy command flag remains authoritative for the phase or phases it already controls and does not invoke the registry. If no configuration exists at the unoverridden default path, the existing command/environment/default resolution remains available. Once a configuration file is selected, it is the only registry source; an invalid or unreadable explicitly selected file is an error rather than a reason to try another source.

## Requirements

- R1: Every pipeline must resolve at most one provider configuration path in this precedence order: a non-empty `--provider-config` value, then a non-empty `ADVERSARIAL_PROVIDER_CONFIG` value, then `~/.config/adversarial/providers.yaml`. The selected path must support `~` expansion. The loader must not merge files or fall through to a lower-precedence path when the selected file is unreadable or invalid. An absent default file disables registry selection and preserves the legacy resolution path with a warning; a missing path selected explicitly by flag or environment is a startup error.

- R2: The configuration must be a versioned YAML mapping with the following public field contract: `version: 1`; a `checker` mapping containing non-empty `command`, optional positive `ttl_seconds` (default 30), and optional positive `timeout_seconds` (default 10); a `providers` mapping from arbitrary alias to non-empty `command` and a `quota` mapping; and a `roles` mapping containing ordered alias lists for any of the fixed roles `dev`, `review`, `verify`, `arbiter`, `writer`, and `challenger`. Each `quota` mapping must contain `result_key` and may contain `used_pct_path` plus `stop_above_pct`, `balance_path` plus `stop_below_usd`, or both pairs; paths use dot-separated object keys relative to that provider's checker result. All referenced aliases must exist; role lists must be non-empty and contain no duplicate alias; paired policy fields must appear together; percentage thresholds must be between 0 and 100 inclusive; balance thresholds must be non-negative. Unknown roles, unsupported configuration versions, malformed values, and a required role missing for a phase must produce an actionable configuration error before that phase launches a model.

- R3: Provider aliases, model names, commands, result keys, quota thresholds, and fallback ordering used by registry selection must come only from the selected configuration. Shared selection logic and pipeline entry points must work unchanged with invented aliases and commands. Provider-specific knowledge may exist in the external quota-checker adapter, but no pipeline may use it to choose a command.

- R4: Before the first registry-managed model phase in a TTL window, the quota layer must invoke the configured checker once for all configured provider result keys and parse its JSON response as one batch. The resulting snapshot and warnings must be shared across roles within the process until the positive TTL expires. A checker-wide failure as defined in R9 must be cached for the same `checker.ttl_seconds` duration (default 30 seconds), measured from completion of the failed refresh, and that duration is the cached failure window. Concurrent selection requests at an empty or expired cache must share one in-flight refresh rather than starting duplicate checker processes. Cache age must use a monotonic clock; a changed effective configuration or checker command must not reuse a successful or failed snapshot created for another configuration.

- R5: Each provider candidate must retain the checker's normalized state `OK`, `DRAINING`, `RATE-LIMITED`, `KEY_INVALID`, or `UNKNOWN` and a human-readable reason. `RATE-LIMITED` and `KEY_INVALID` candidates are ineligible. `OK` candidates are eligible. `DRAINING` candidates are eligible with a warning. A missing result, malformed provider result, partial checker error, or unrecognized checker state resolves only that candidate to `UNKNOWN`; `UNKNOWN` remains eligible with a warning so quota infrastructure failure does not break a previously runnable pipeline.

- R6: A configured percentage policy must make a candidate ineligible when the selected numeric `used_pct` value is strictly greater than its stop-above threshold. A configured prepaid policy must make a candidate ineligible when the selected numeric balance is strictly less than its stop-below threshold. Equality at either threshold remains eligible. Valid runtime percentage values are finite numbers from 0 through 100 inclusive, and valid runtime balance values are finite non-negative numbers. A missing, non-numeric, non-finite, or out-of-range runtime metric must resolve the policy to `UNKNOWN` and warn rather than being treated as zero or as unlimited quota. The reason recorded for an ineligible candidate must distinguish checker state, percentage threshold, and balance threshold.

- R7: For each registry-managed phase, selection must inspect the configured aliases for that phase's role in order, skip every ineligible candidate, and select the first eligible `OK`, `DRAINING`, or `UNKNOWN` candidate. Selection must not reorder candidates by cost, model identity, or remaining quota. Selecting `DRAINING` or `UNKNOWN` must emit a warning before execution. If no candidate is eligible, the phase must fail before any model command starts and report the role plus every attempted alias and its rejection reason.

- R8: All entry points must accept `--force` and repeatable `--force-provider <role>:<alias>`. Global force selects the first configured alias for each role without invoking the checker or applying checker state or thresholds. A role force selects that exact alias without checking quota and therefore also acts as an explicit choice among fallbacks; the alias must exist in that role's configured chain. A role force takes precedence over global force for its role. Unforced roles in a partially forced run continue to use the shared quota snapshot. For force-option validation, registry configuration is active only when at least one phase reachable under the chosen options remains registry-managed after applying explicit legacy-command overrides and a configuration is selected for that phase; a loaded configuration that every reachable phase bypasses is not active. Invalid syntax, duplicate assignments for one role, unknown roles, unknown aliases, or force options when no registry configuration is active must fail during argument/configuration validation. Forced selections must still expand the work directory, execute normally, and be marked as forced in history.

- R9: A legacy model-command flag explicitly present on the command line, including phase-specific code-review command flags, must bypass configuration lookup, quota refresh, threshold evaluation, and force-provider selection for every phase controlled by that flag. Mixed runs are allowed: explicitly overridden phases bypass quota while other phases use their configured role chains. When registry configuration is inactive, existing role-specific environment variables, built-in defaults, optional-command behavior, exit codes, and resume behavior must remain unchanged. If the checker executable is missing, not executable, times out, exits unsuccessfully without usable JSON, or returns unusable top-level JSON, registry-managed phases must run the preferred configured command. The first phase observing such a failure must emit one visible legacy-behavior warning; later phases sharing that cached failure must emit no additional copy, and the failure may be retried only after the cached failure window defined in R4 expires.

- R10: Immediately before a registry-managed model invocation, every literal `{workdir}` token in the selected command string must be replaced with the absolute work directory for that phase. Commands without the token must be unchanged. Expansion must work for paths containing whitespace and must not reinterpret work-directory characters as additional command arguments. No other placeholder is part of the v1 contract; an unsupported brace-delimited placeholder must be rejected during configuration validation.

- R11: The execution layer must perform selection immediately before every model-command invocation managed by the registry, including a second invocation made by a phase to recover from invalid model output and invocations reached after `--resume`. A still-valid snapshot may be reused, but an expired snapshot must be refreshed before selection. Attempts made internally by the existing transient execution retry loop reuse the command already selected for that invocation. Provider fallback precedes the existing command execution, timeout, retry, persona, cost, input-cap, and output-cap behavior; those behaviors must receive the selected command without changing their existing contracts. Concurrent phases must be able to select independently while sharing the quota snapshot.

- R12: Phase-to-role mapping must be stable and provider-neutral. Specification and planning WRITE/PLAN and REVISE phases use `writer`, CHALLENGE uses `challenger`, and VERIFY uses `verify`. Code-loop BUILD and FIX use `dev`, REVIEW uses `review`, VERIFY uses `verify`, and ARBITER uses `arbiter`. Code-review architect, inspector, and cross-review phases use `review`, while final synthesis uses `arbiter`. A pipeline must validate only the roles it can reach under the chosen options; for example, a disabled arbiter must not require an `arbiter` chain. Existing explicit command flags continue to control their historical phases as specified by R9.

- R13: The quota checker must accept composable `--glm` and `--deepseek` flags in addition to its existing provider flags. With no provider flag, it must request every supported provider. Its `--json` output must remain a single object containing `results` keyed by checker result key and an `errors` list, and each successful result must include a normalized state from R5 plus the raw metrics needed for configured percentage or balance policies. Requested checks must run concurrently within one checker invocation. A failure in one check must preserve successful results, identify the failed check in `errors`, and retain the existing non-zero partial-failure exit behavior.

- R14: Every registry selection attempt must append a sanitized history entry containing the pipeline phase, logical role, selected alias or no-selection outcome, normalized state, forced/bypassed status, fallback/rejection reasons, warning text when applicable, and whether the quota snapshot was refreshed or reused. Ordered `provider_history` covering the run must appear in `final.json`, including failure outcomes and selections made after resume, and in the existing human-readable/HTML report when produced. History and warnings must not contain command strings, environment values, credentials, or checker response bodies. Any retained stderr-derived diagnostic must have secrets redacted, be limited to at most 2,000 Unicode code points including a visible `[truncated]` marker when input was omitted, and use no marker when the complete diagnostic fits within the limit.

- R15: Registry configuration and cached quota state must be scoped to one pipeline process. The feature must not wait for quota resets, retry a phase merely because quota may later recover, change phase ordering, select by monetary cost, coordinate cache state with another process, or add a quota-specific dashboard. Existing provider execution retries may continue only under their current transient-error rules after a command has been selected.

## Acceptance criteria

- AC1 (R1): Given three distinct paths in the CLI flag, environment variable, and default location, only the CLI path is opened; with no flag only the environment path is opened; with neither override only the expanded default path is opened. An invalid selected override does not cause any lower-precedence path to be opened.

- AC2 (R1): With no file at the default path and no explicit provider-config override, a legacy pipeline command resolves and runs as before, and exactly one registry-disabled warning is visible. A missing flag-selected or environment-selected file exits before a model subprocess starts and names the selected path.

- AC3 (R2): A valid configuration using all six roles, arbitrary aliases, percentage and balance policies, and omitted checker timing values loads with a 30-second TTL and 10-second checker timeout. Parameterized invalid configurations covering an unsupported version, unknown role, missing alias reference, duplicate role alias, empty command, incomplete policy pair, invalid metric path, and out-of-range thresholds are rejected with field-specific errors.

- AC4 (R2): If a reachable phase requests a role absent from an otherwise valid configuration, the phase fails before quota or model subprocesses start and identifies the missing role; a role used only by a disabled optional phase is not required.

- AC5 (R3): A test configuration whose aliases and commands contain no known provider or model names selects and executes its configured fallbacks without a pipeline source change, and the selected alias is the only provider identity exposed to the pipeline.

- AC6 (R4): Ten sequential selections for different roles within a 30-second monotonic-clock window cause exactly one checker invocation; a selection after the clock advances beyond the TTL causes exactly one additional invocation and observes the new results.

- AC7 (R4): Ten concurrent selections against an empty cache block on and share one checker invocation, all observe the same completed snapshot, and a snapshot created for a different effective config/checker identity is not reused.

- AC8 (R5): For candidates reported as `RATE-LIMITED`, `KEY_INVALID`, `DRAINING`, `OK`, and `UNKNOWN`, selection respectively skips the first two, selects the latter three when first eligible, and emits warnings only for `DRAINING` and `UNKNOWN`.

- AC9 (R5): A batch containing one valid result, one malformed result, one missing result, one result with an unsupported state string, and a named partial error preserves the valid state and resolves each of the other affected candidates to `UNKNOWN` with a reason that distinguishes malformed, missing, unrecognized-state, and partial-error cases.

- AC10 (R6): With a stop-above value of 80, runtime usage of 80 remains eligible and 80.01 is skipped; with a stop-below value of 5.00, a balance of 5.00 remains eligible and 4.99 is skipped. Each skipped history entry names the applicable threshold reason.

- AC11 (R6): Missing, non-numeric, non-finite, negative, and over-100 runtime percentage values and missing, non-numeric, non-finite, and negative runtime balance values produce `UNKNOWN` warnings and do not silently become numeric values; zero percentage and zero balance remain valid numeric values subject to their configured thresholds.

- AC12 (R7): Given the ordered chain `[primary, secondary, tertiary]` where primary is rate-limited, secondary exceeds its configured stop threshold, and tertiary is OK, the tertiary command is the only model command started and history records both earlier rejection reasons in order.

- AC13 (R7): When every alias for a role is ineligible, the phase starts no model command, returns an infrastructure/configuration failure through the pipeline's normal failure path, and its diagnostic lists the role and every alias with its reason.

- AC14 (R8): Under `--force`, the first alias is selected without a checker invocation; under `--force-provider review:secondary`, `secondary` is selected without checking quota even when it is not first. Both history entries are marked forced, and an unforced role later in the partially forced run triggers the normal shared quota refresh.

- AC15 (R8): Each invalid force case listed in R8 exits before either checker or model execution. This includes a run where a valid configuration can be selected but explicit legacy flags override every reachable phase; either force option in that run fails as inactive, while the same option is accepted in a mixed run with at least one registry-managed phase. When both global and valid role force are present, the exact role-forced alias is used for that role and the first alias is used for another role.

- AC16 (R9): Supplying an explicit legacy command for one role starts that exact command without invoking the checker or reading that role's chain, while a later non-overridden role in the same run triggers registry selection. With only explicit commands, the checker is never invoked.

- AC17 (R9): For each of a missing checker executable, checker timeout, non-zero response with no parseable results, and malformed top-level JSON, the preferred configured command runs. With `checker.ttl_seconds: 12`, a first phase whose failed refresh completes at monotonic time 0 followed by phases at times 5 and 11 causes one checker invocation and exactly one visible legacy-behavior warning; the first phase at or after time 12 causes one new checker invocation and, if it fails, exactly one new warning for the new failure window.

- AC18 (R10): A command containing `{workdir}` receives the absolute phase work directory as one argument when that path contains spaces and shell metacharacters; a command without the token is byte-for-byte unchanged before normal parsing. A configuration containing `{unknown}` is rejected before execution.

- AC19 (R11): Two phases inside one TTL can select different aliases from different role chains using one snapshot, while a later phase after expiry selects against refreshed states. Existing timeout, transient retry, persona injection, cost accounting, and cap tests pass with a registry-selected command.

- AC20 (R11): Resuming before an unfinished model phase performs selection for that phase and does not rerun completed phases; resuming after the saved snapshot TTL performs a fresh checker invocation before the unfinished phase.

- AC21 (R12): End-to-end tests for each entry point observe the exact role sequence defined in R12 for all enabled phases, with no provider/model inference from a phase name or executable. Disabling arbitration removes the `arbiter` role requirement and invocation.

- AC22 (R8): `--force` and `--force-provider <role>:<alias>` appear with those exact spellings and value shape in `--help` for adversarial-spec, adversarial-plan, adversarial-code-loop, and adversarial-code-review. Each entry point accepts two `--force-provider` occurrences assigning different roles, rejects a duplicate role assignment before subprocess execution, and includes the duplicate role name in its error.

- AC23 (R13): `check-ai-quota.py --json --glm --deepseek` invokes exactly those two checks concurrently and returns both normalized results in one `results` object. With no provider flags, all five supported checks are requested.

- AC24 (R13): When the GLM check fails and the DeepSeek check succeeds, JSON still contains the DeepSeek state and balance metrics, `errors` identifies GLM, and the checker exits non-zero; existing Claude, Codex, and Gemini result fields remain present and compatible when their checks are requested.

- AC25 (R14): A run that rejects one alias, selects a fallback, later reuses the cache, and finally refreshes it writes ordered `provider_history` entries to `final.json` and renders the same phase, role, alias, state, force/bypass, fallback reason, and refresh/reuse facts in the human report.

- AC26 (R14): With commands and checker errors containing sentinel API keys, a checker response-body sentinel, an environment-value sentinel, and a 20,000-character stderr diagnostic, neither `final.json` nor the human/HTML report contains any sentinel, key, or command text; every stderr-derived field is at most 2,000 Unicode code points and ends with `[truncated]`. A separate complete 100-character stderr diagnostic is retained without a truncation marker.

- AC27 (R15): Integration tests with an exhausted provider confirm the pipeline neither sleeps until reset nor reruns the phase for quota recovery, does not reorder phases or choose a lower-cost alias, creates no cross-process cache artifact, and produces no quota-dashboard artifact.

- AC28 (R1): `--provider-config <path>` appears with that exact spelling and value shape in `--help` for adversarial-spec, adversarial-plan, adversarial-code-loop, and adversarial-code-review, and each entry point accepts the same valid configuration path for the precedence behavior in AC1.
