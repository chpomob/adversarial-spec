# Adversarial Skills Enhancement Spec

**Status:** Draft · **Scope:** adversarial-common + code-loop + code-review + plan + spec
**Source:** competitive analysis of 6 projects (adversarial-spec, adverse, adversarial-review, agent-review-panel, devils-advocate, claude-wizard)

## Purpose

Close identified capability gaps in the adversarial skill suite by adding gates
(reliability, safety, cost), adaptive control, richer output, and two optional
operating modes. All features hook into the existing `runner.run_cli()` /
`providers.run_cmd()` / `jsonio.write_final_json(**extra)` core without rewriting it.

## Non-goals

- New model providers (existing `detect_provider` universal path already covers new CLIs).
- A web UI / server. HTML report (R9) is a static single file only.
- Replacing the persona system or the BUILD→REVIEW→FIX→VERIFY→ARBITER pipeline shape.

## Feature → file map

| Req | Skill(s) touched | New file(s) | Modified file(s) |
|-----|------------------|-------------|------------------|
| R1  | common           | `gates.py`  | `runner.py`, scripts |
| R2  | common           | —           | `providers.py`, `runner.py` |
| R3  | common           | `gates.py`  | `jsonio.py`, scripts |
| R4  | common           | `costs.py`  | `runner.py`, `providers.py`, scripts |
| R5  | common + all pipelines | `gates.py` | `runner.py`, scripts |
| R6  | common + code-loop | `gates.py` | `scripts/adversarial_loop.py` |
| R7  | common           | —           | `runner.py` |
| R8  | common + code-review | —       | `personas/*.md`, `jsonio.py` |
| R9  | common           | `report.py` | scripts |
| R10 | common + all pipelines | —      | `runner.py`, scripts |
| R11 | common + spec/plan | —         | `scripts/adversarial_spec.py`, `scripts/adversarial_plan.py` |
| R12 | common           | —           | `runner.py` |

## Requirements

- R1: Context gate (P0). A pre-flight gate in `adversarial_common/gates.py`
  (`check_context(input, kind, thresholds)`) that inspects the run's primary
  input (brief text, spec text, or diff) before any provider call is made. It
  rejects runs whose context is too thin to produce meaningful adversarial
  output: empty/whitespace input, below a minimum character or token floor,
  missing required sections (e.g. a spec with no "Requirements" heading), or a
  diff that touches zero source lines. Every pipeline script calls
  `check_context()` as its first action; on refusal it writes a blocked
  `final.json` (via `jsonio.write_final_json`) and exits with a distinct
  non-zero code, so CI can tell "blocked on context" apart from "failed".
  Thresholds are configurable per pipeline (env var + CLI flag), with sane
  built-in defaults.

- R2: Retry with backoff (P0). Transient provider failures are retried inside
  the execution layer (`providers.run_cmd` / `runner.run_cli`) so every
  pipeline inherits it for free. Retry is triggered by returncode 124 (timeout
  only when the elapsed wall-time is far below the configured timeout, i.e. a
  fast hang), network-style stderr (connection reset, TLS, EOF), and any
  provider-specific transient signal. Backoff is exponential with jitter:
  `delay = base * 2**attempt + random(0, jitter)`. Retries stop on
  non-retryable failures (hard 4xx, malformed-output-after-parse, missing
  binary / FileNotFoundError) — those propagate immediately. Parameters
  `max_retries` (default 3), `base` (default 2s), `jitter` (default 1s) are
  configurable. Each attempt is logged; the final failure still routes through
  `runner.fail_phase()`.

- R3: Size caps (P0). Hard input and output size limits enforced before and
  after each provider call to bound cost and prevent context overflow.
  `gates.py` exposes `enforce_input_cap(text, max_chars, max_tokens_est)` and
  `enforce_output_cap(text, max_chars)`. The input cap rejects (or, when
  permitted by a flag, head-truncates with a recorded marker) oversized stdin
  before it is sent. The output cap guards `runner.run_cli` returns: output
  exceeding the cap is truncated and flagged rather than silently fed
  downstream, and `jsonio.parse_json_output` sees the truncation marker so it
  can fail loudly instead of producing a half-object. Defaults: 256 KiB input,
  128 KiB output per phase.

- R4: Cost tracking (P1). `adversarial_common/costs.py` provides a
  `CostLedger` that accumulates per-model token usage and estimated cost across
  a whole run, phase by phase, persona by persona. Token counts come from
  provider usage metadata when present (claude/codex native), falling back to a
  deterministic char/4 estimate otherwise. Prices live in a small in-module
  table keyed by model id, overridable via env/flag. The ledger is threaded
  through `run_cli`/`run_cmd` calls (return path augmented to include an
  optional usage record) and its summary is merged into `final.json` through
  `write_final_json(... costs=ledger.summary())`. A `--show-costs` flag prints
  the per-model breakdown to stderr.

- R5: Complexity gate (P1). `gates.py` provides
  `estimate_complexity(input, diff_stats) -> {score, level, recommended_agents}`
  deriving a lightweight complexity score from input length, number of files /
  lines / hunks in a diff, and breadth of the spec (count of requirements /
  modules touched). Pipelines use the recommended agent count to scale persona
  fan-out (e.g. code-review's perspectives, plan's decomposition depth, loop's
  FIX→VERIFY parallelism). Ranges are tiered: trivial=1, low=2-3, medium=3-4,
  high=4-6, capped by a configurable `max_agents` (default 6). The score is
  recorded in `final.json` so the choice is auditable. Never silently removes
  personas the user explicitly requested via flags — the gate only scales
  defaults.

- R6: Verification gates pre/post (P1). The code-loop gains explicit pre- and
  post-gates around BUILD and each FIX, defined in `gates.py`
  (`pre_build_gate`, `post_fix_gate`, `post_build_gate`). Pre-build gate checks
  the build is actually runnable (project markers present, test command
  resolvable) before spending a model call. Post-build / post-fix gates run the
  real verification command (tests, lint, typecheck — whatever the project
  configures) and block progression to REVIEW/ARBITER on failure, routing the
  failure back into FIX or, after `max_fix_rounds`, to ARBITER with the failing
  evidence attached. Gate results are recorded as structured entries in
  `final.json` (command, exit code, truncated log).

- R7: Parallel model calls (P1). Independent persona/phase calls execute
  concurrently instead of sequentially. `runner.py` adds a
  `run_parallel(calls, concurrency=...)` helper that dispatches a list of
  `(label, run_cli_args)` through a bounded pool (threading — providers are
  subprocess-bound, not CPU-bound) and returns results in input order. Concurrency
  defaults to the agent count from R5 (or a flat 3), capped by a configurable
  max. Applies to code-review perspectives and loop FIX+VERIFY pairs. Failures
  are reported per-call and do not silently collapse sibling results.

- R8: Epistemic labels (P1). Every finding emitted by a critic/inspector
  persona carries an explicit epistemic label so synthesis and the arbiter can
  weight evidence instead of treating all complaints equally. The label has two
  parts: `confidence` ∈ {high, medium, low} and `basis` ∈ {spec, code,
  inference, external}. Personas are updated (`personas/*.md`) to output the
  label per finding; `jsonio.parse_json_output` and the finding schema enforce
  its presence (missing/invalid label → finding flagged as
  `confidence=low,basis=inference` with a warning, never dropped). Synthesis
  surfaces label distribution and the arbiter down-weights `inference`-only
  findings.

- R9: HTML report (P2). `adversarial_common/report.py` renders a
  self-contained, dependency-free HTML artifact from a run's `final.json` (and
  intermediate artifacts). One file, inline CSS, no external requests, no JS
  frameworks — stdlib `html` escaping only. Includes: verdict, finding list
  with epistemic labels (R8), cost breakdown (R4), gate results (R6), and
  collapsible per-phase raw outputs. Produced when `--html` is passed; path is
  reported and written next to `final.json`. No new third-party dependency.

- R10: CI-gate mode (P2). A `--ci` flag putting every pipeline into a
  non-interactive, deterministic mode: no prompts, no color/progress noise on
  stdout (human detail goes to stderr), exit codes are the contract
  (0=clean/pass, 2=blocking findings, 3=non-blocking findings, 1=infra
  failure, context-block code from R1), and the single source of truth is
  `final.json`. A configurable `--fail-on` selector chooses which verdicts /
  epistemic levels fail CI. Designed for pre-merge and cron consumption.

- R11: Deep research mode (P2). An optional pre-step, enabled by
  `--deep-research`, that performs bounded web research to ground the run in
  current standards/docs (e.g. validating spec assumptions against upstream
  API/standard docs, or checking review findings against current best practice).
  Off by default, opt-in only, with a hard result cap and the findings folded
  in as `basis=external` evidence (R8). The spec and plan pipelines use it
  before generation; code-review can use it to validate "this is an
  anti-pattern" claims. Must respect R3 caps and R4 cost budget; failures are
  non-fatal (research unavailable → run continues without it, logged).

- R12: Delegated mode (P2). An orchestrator/workers split for large inputs:
  `--delegated` has an orchestrator model decompose the task and dispatch
  bounded worker calls (one worker per sub-task / file group), then
  re-synthesize. Implemented in `runner.py` as a thin layer over `run_parallel`
  (R7) and gated by complexity (R5) — only offered above a threshold, refused
  below it to avoid pointless overhead. Orchestrator and worker commands are
  independently configurable; worker results feed the normal synthesis/arbiter
  path with an `origin=worker` marker. Failures in a subset of workers degrade
  gracefully rather than aborting the whole run.

## Acceptance criteria

- AC1 (R1): When the primary input is empty or whitespace-only, the pipeline
  writes `final.json` with `{"status":"blocked","reason":...}` and exits with
  the context-block exit code, before any provider subprocess is started
  (verified: zero `run_cli` invocations).
- AC2 (R1): A spec missing its required "Requirements" section, or a diff
  touching zero source lines, is rejected with the same blocked status, and the
  reason names the specific failed check.
- AC3 (R1): Thresholds are overridable via both an env var and a CLI flag, and
  the effective thresholds are echoed in `final.json` for auditability.
- AC4 (R2): Given a provider that fails returncode 124 with fast elapsed time
  on the first attempt then succeeds on the second, the run completes
  successfully and exactly 2 attempts are recorded in the log.
- AC5 (R2): A non-retryable failure (e.g. FileNotFoundError / hard 4xx) is not
  retried: `fail_phase` is reached on the first attempt.
- AC6 (R2): Retries observe `max_retries` and never exceed it; backoff delays
  are within `[base*2**n, base*2**n + jitter]` for attempt n.
- AC7 (R3): An input larger than `max_chars` is rejected before the subprocess
  starts (or, with `--truncate-input`, is head-truncated with a visible marker)
  and the cap event is recorded.
- AC8 (R3): A provider return larger than the output cap is truncated and the
  marker causes `parse_json_output` to return a recorded error rather than a
  silently truncated object.
- AC9 (R4): After a multi-phase run, `final.json` contains a `costs` object
  keyed by model with `{prompt_tokens, completion_tokens, est_cost_usd}` and a
  total; `--show-costs` prints the same breakdown to stderr.
- AC10 (R4): When native usage metadata is unavailable, token counts fall back
  to the char/4 estimate and the record is flagged `estimated=true`.
- AC11 (R5): Given diffs of trivial / low / medium / high size,
  `estimate_complexity` returns strictly increasing scores and the documented
  agent counts, capped at `max_agents`.
- AC12 (R5): Persona flags explicitly supplied by the user are never removed by
  the complexity gate; only default fan-out scales.
- AC13 (R5): The chosen `recommended_agents` is recorded in `final.json`.
- AC14 (R6): `pre_build_gate` refuses to start BUILD when the configured build
  command is not resolvable, exiting non-zero before any model call.
- AC15 (R6): On a failing post-fix gate, the loop routes back to FIX; after
  `max_fix_rounds` it reaches ARBITER with the failing gate log attached.
- AC16 (R6): Each gate execution is recorded in `final.json` with its command,
  exit code, and a truncated log.
- AC17 (R7): Running N independent persona calls via `run_parallel` completes
  in wall-time consistent with concurrency-limited parallel execution (not
  sequential N×latency) and returns results in input order.
- AC18 (R7): A failure in one of the parallel calls is reported for that call
  only; sibling calls' results are unaffected.
- AC19 (R8): A finding without a valid `confidence`/`basis` label is normalized
  to `confidence=low,basis=inference` with a recorded warning and is not
  dropped from the output.
- AC20 (R8): Synthesis output includes the distribution of epistemic labels
  across findings, and the arbiter's final weighting down-grades
  `inference`-only findings.
- AC21 (R9): `--html` writes a single `report.html` file with no external
  network requests or third-party runtime dependencies, containing verdict,
  findings (with labels), costs, and gate results.
- AC22 (R9): All model-supplied strings in the HTML are escaped via the stdlib
  `html` module (no raw injection of finding text).
- AC23 (R10): Under `--ci`, stdout carries no prompts or color, exit codes
  match the documented contract for clean/blocking/non-blocking/infra/context
  cases, and `final.json` is always written.
- AC24 (R10): `--fail-on` changes which epistemic levels / verdicts cause a
  CI-failing exit code.
- AC25 (R11): `--deep-research` adds research-derived findings tagged
  `basis=external`; with the flag absent, no research runs and output is
  byte-for-byte equivalent to a run without the feature.
- AC26 (R11): When research is unavailable, the run continues without it and
  logs the skip; no hard failure.
- AC27 (R12): `--delegated` is accepted only above the complexity threshold
  (R5); below it, the run proceeds non-delegated with a logged reason.
- AC28 (R12): A worker failure degrades gracefully — its sub-task is marked
  failed but the orchestrator still produces a synthesized result from the
  surviving workers.
- AC29 (R12): Worker-sourced findings carry an `origin=worker` marker visible
  in synthesis output.
- AC30 (R1,R2,R3): The gates/retry/caps layer adds zero new third-party
  dependencies to adversarial-common (stdlib only).
