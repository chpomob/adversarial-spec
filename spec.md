---
name: quota-aware-provider-registry
version: "1.0"
author: adversarial-spec
status: draft
targets:
  - file: adversarial_common/quota.py
    description: "New module — quota resolver that checks provider availability and returns the best command to run."
  - file: adversarial_common/runner.py
    description: "Modified — run_phase() integrates quota check before executing commands, with automatic fallback chain."
  - file: adversarial_common/__init__.py
    description: "Export new quota module symbols."
  - file: adversarial_common/providers.py
    description: "Add ProviderConfig dataclass and YAML config loader for provider registries."
  - file: adversarial_common/report.py
    description: "Add quota metadata to the final report (which provider was used, quota state at decision time)."
  - file: adversarial_common/jsonio.py
    description: "No changes needed — already handles JSON read/write used by check-ai-quota.py output."
---

# Quota-Aware Provider Registry

## Problem

The adversarial pipeline (spec, plan, code loop) launches model commands blindly — it
executes `--dev-cmd` and `--review-cmd` without knowing whether the target model has
remaining quota. When a model exhausts its rate limit mid-pipeline (Claude 5h sliding
window, Codex monthly cap, GLM-5.2 80 req/5h, Fable 5 separate limit), the running
phase fails with a cryptic timeout or error, the pipeline restarts from scratch after
a manual `--resume`, and the user must manually check quotas with `check-ai-quota.py`
before each launch.

The user already has:
- A `hermes-quota-status` plugin with `quota_api.py` that checks Claude, Codex, Gemini,
  GLM (Z.AI), and DeepSeek quotas via direct API calls.
- A `check-ai-quota.py` script in adversarial-code-review that wraps the plugin into
  a CLI with `--json` output for programmatic consumption.
- Hardcoded fallback rules in SKILL.md ("if Claude quota low → GLM", "if Fable 5
  blocked → Sonnet") that the user applies manually.
- A clear preference: **Claude as primary**, **Codex as secondary**, with fallback
  chains that differ per role (DEV vs REVIEW vs CHALLENGER).

What's missing: an automated, model-agnostic layer that selects the right command for
each phase based on real-time quota data, without the pipeline needing to know what
"Claude" or "Codex" actually are.

## Requirements

- R1: The pipeline shall check provider quotas before launching each phase (DEV,
  REVIEW, VERIFY, ARBITER, CHALLENGE) and select the best available command.
- R2: The quota integration shall be fully model-agnostic — the pipeline never
  hardcodes model names; it works with provider aliases resolved by an external
  quota checker.
- R3: The system shall support a configurable ordered list of providers per role
  with fallback semantics (try #1 → quota low → try #2 → quota exhausted → try #3).
- R4: When no provider in the fallback chain has available quota, the phase shall
  report a clear "no provider available" error with per-provider quota snapshots
  and exit the pipeline cleanly (not timeout).
- R5: The quota check shall be fast — a single parallel call to
  `check-ai-quota.py --json` that resolves all known providers in one shot, using a
  **global cache** shared across all roles (Claude à 80% l'est pour tous les rôles).
  Configurable TTL (default 30s) to avoid hammering provider APIs on every sub-phase.
  The cache is keyed by provider alias, not by role.
- R6: The system shall log which provider was selected, why (quota state), and the
  raw quota snapshot in the pipeline's final report (final.json / final.md).
- R7: The provider registry shall be loaded from an external YAML file specified via
  `--provider-config` CLI flag, `ADVERSARIAL_PROVIDER_CONFIG` environment variable,
  or default path `~/.config/adversarial/providers.yaml`. No provider config file
  shall be shipped inside any skill directory.
- R8: The quota resolver shall handle the case where `check-ai-quota.py` is not
  installed or returns errors — fall back to executing the primary command directly
  (legacy behaviour) with a warning logged.
- R9: The system shall support environment variable overrides per role
  (`ADVERSARIAL_DEV_PROVIDERS`, `ADVERSARIAL_REVIEW_PROVIDERS`) as inline JSON
  that overrides the YAML config without touching files — useful for pipeline
  orchestration where the config file is read-only or in CI.
- R10: The existing `--dev-cmd`, `--review-cmd`, `--arbiter-cmd` CLI flags shall
  still work as positional overrides: when the user passes an explicit `--dev-cmd`,
  quota checking for that role is skipped (the explicit command wins). This preserves
  backward compatibility and manual override.
- R11: The report shall include a `provider_history` array tracking every phase's
  provider decision: which alias was selected, quota state at decision time, and
  whether a fallback was triggered.
- R12: All error messages and report fields shall be in English (pipeline convention).
  User-facing CLI output (--help, warnings) stays in the conversation's language.
- R13: Command strings in the provider config shall support `{workdir}` as a placeholder
  that the resolver substitutes with the effective workdir at execution time. This allows
  commands like claude-tmux's `--cwd` to point to the correct project directory without
  hardcoding paths.
- R14: The system shall include and maintain a `check-ai-quota.py` CLI wrapper that exposes
  `--glm` and `--deepseek` flags in addition to the existing `--claude`, `--codex`,
  `--gemini` flags. This ensures all providers used in the fallback chain have a quota
  check path, preventing silent UNKNOWN fallback for GLM and DeepSeek.
- R15: Each provider entry in the config may specify a `stop_threshold` field. For
  **percentage-based** providers (Claude, Codex, GLM sliding window), it represents the
  max used_pct before the provider is skipped (default: 100). For **balance-based**
  providers (DeepSeek, Gemini credits), it represents the minimum remaining balance
  before the provider is skipped (default: 0 — use until empty). The resolver shall
  detect which model a provider uses from its quota response schema (presence of
  `balance` vs `session.used_pct`).
- R16: A `--force` CLI flag shall bypass all quota checks for all roles. The pipeline
  uses the first provider in each role's config chain regardless of quota state.
  Useful for degraded mode, testing, or when quota APIs are down.
- R17: A `--force-provider <role>:<alias>` CLI flag shall force a specific provider
  alias for a single role (e.g. `--force-provider review:deepseek`). Other roles
  still check quotas normally. This allows unblocking a specific phase without
  disabling quota awareness globally.

## Acceptance criteria

- AC1 (R1): Run a pipeline with two providers configured (claude→deepseek). Block
  Claude's quota artificially (set session_pct=100 in mock). Pipeline selects deepseek.
  Phase completes. Verified via final.md showing deepseek as selected provider.
- AC2 (R2): Add a new provider with alias "my-model" to the YAML config. Provide
  a working quota check script that returns OK for it. Pipeline uses it without any
  code changes. No model name string appears in runner.py or quota.py source.
- AC3 (R3): Configure providers.prod: cmd1, cmd2, cmd3. Set cmd1 to simulate
  RATE-LIMITED, cmd2 DRAINING, cmd3 OK. Pipeline selects cmd3. Set cmd3 to
  RATE-LIMITED too. Pipeline reports "no provider available" with a snapshot of
  all three states and exits code 3 (REJECT).
- AC4 (R5): Run a pipeline with 4 phases. First check-quota call takes ~1s (parallel
  HTTP calls). Subsequent phase checks in the same 30s window return cached results
  (sub-millisecond). Verify only one HTTP batch per TTL window.
- AC5 (R6): After a pipeline run, final.json contains a `provider_history` array.
  Each entry has phase name, selected alias, quota state (OK/DRAINING/RATE-LIMITED),
  and `fallback: true/false`.
- AC6 (R8): Run a pipeline on a system without check-ai-quota.py or quota_api.py.
  Pipeline runs normally using the primary command for each role, with a warning
  logged to stderr. Exit code is the same as if the script had run without quota
  awareness (legacy behaviour).
- AC7 (R9): Set `ADVERSARIAL_DEV_PROVIDERS='[{"alias":"claude", "cmd":"..."}]'`
  in environment. Pipeline ignores the YAML file's dev section and uses the env var.
- AC8 (R10): Run pipeline with `--dev-cmd "echo primary"`. Pipeline skips quota
  check for DEV role, executes the explicit command directly. Other roles still
  check quotas.
- AC9 (R11): After a 3-phase pipeline run, provider_history has exactly 3 entries
  (one per phase that checks quotas). Each entry has `phase`, `alias`, `quota_state`,
  `fallback` fields. Fields are non-empty.
- AC10 (R12): All fields in final.json are in English. CLI --help output may be in
  French when the user's session language is French.
- AC11 (R13): A provider config entry with cmd containing `{workdir}` executes with
  `{workdir}` replaced by the absolute path of the pipeline's working directory.
  Verified by running `echo {workdir}` as a provider command and checking the phase log
  contains the expected path. Trailing slashes shall be stripped.
- AC12 (R14): `check-ai-quota.py --json --glm` returns a structured JSON response
  with GLM quota data. `check-ai-quota.py --json --deepseek` returns structured JSON
  with DeepSeek balance data. Both return exit 0 on success, exit 1 with an error
  message if credentials are missing.
- AC13 (R15): Configure DeepSeek with `stop_threshold: 2.0`. Set mock balance to $1.50.
  Pipeline skips DeepSeek and falls to next provider. Set mock balance to $5.00.
  Pipeline selects DeepSeek. Same test with Claude: `stop_threshold: 90`, mock
  used_pct=95 → skipped, mock used_pct=80 → selected.
- AC14 (R16): Run pipeline with `--force`. All providers in RATE-LIMITED state.
  Pipeline uses the first provider for each role anyway. Phases execute normally.
- AC15 (R17): Run pipeline with `--force-provider review:deepseek`. Set Claude to
  RATE-LIMITED, DeepSeek to OK. Review phase uses DeepSeek (the forced one, not
  the fallback chain). DEV phase still checks quotas normally for its own chain.

## Provider config file — user provided

The user creates this file (e.g. at `~/.config/adversarial/providers.yaml`)
and passes it to the pipeline via `--provider-config`. Example content:

```yaml
# One provider section per role. Ordered by preference.
# First provider with available quota wins.
dev:
  - alias: codex
    cmd: "codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --sandbox workspace-write"
    quota_check: --codex
    stop_threshold: 95       # skip if used_pct > 95%

  - alias: glm
    cmd: "pi -p --provider zai --model glm-5.2 --thinking high"
    # No quota_check → treated UNKNOWN (used anyway with warning)

review:
  - alias: claude
    cmd: "python3 /path/to/claude-tmux.py --timeout 600 --hard-timeout 1800 --cwd {workdir}"
    quota_check: --claude
    stop_threshold: 90       # skip if used_pct > 90%

  - alias: glm
    cmd: "pi -p --provider zai --model glm-5.2 --thinking high"

  - alias: deepseek
    cmd: "pi -p --provider deepseek --model deepseek-v4-pro --thinking high"
    quota_check: --deepseek
    stop_threshold: 2.0      # skip if balance < $2.00

challenger:
  - alias: claude
    cmd: "python3 /path/to/claude-tmux.py --timeout 900 --hard-timeout 1800 --cwd {workdir}"
    quota_check: --claude

  - alias: glm
    cmd: "pi -p --provider zai --model glm-5.2 --thinking high"

  - alias: deepseek
    cmd: "pi -p --provider deepseek --model deepseek-v4-pro --thinking high"
    quota_check: --deepseek
    stop_threshold: 3.0

# Global quota_check command (used when per-entry quota_check is absent)
quota_cmd: "python3 /path/to/check-ai-quota.py --json"

# Cache TTL in seconds (default 30)
quota_cache_ttl: 30
```

## Provider configuration is purely external

No skill — adversarial-code-loop, adversarial-spec, adversarial-plan, or adversarial-code-review —
shall hardcode or ship defaults for any provider alias, command string, or fallback chain.
The skills know only about **roles** (dev, review, verify, arbiter, writer, challenger).
Which provider commands map to which role is entirely determined by the user's config.

**Rationale:** The skills are a model-agnostic orchestration framework. They run commands
and check quotas; they do not know what "Claude", "Codex", "GLM", or "DeepSeek" are.
Hardcoding a default chain in any skill would:
- Break when the user's preferred model lineup changes
- Create a false implicit coupling between skills and specific providers
- Violate separation of concerns (provider selection is operational config, not skill logic)

## Quota state resolution

The quota resolver interprets `check-ai-quota.py --json` output via a simple
state machine:

```
check-ai-quota.py --json --claude
  → {"results": {"claude": {"session": {"used_pct": 45}, "status": "OK"}}}

State → OK:         used_pct < 50  → green, command can run
State → DRAINING:   used_pct 50-99 → yellow, command can run but warn
State → RATE-LIMITED: used_pct >= 100 or HTTP 429 → skip, try next provider
State → KEY_INVALID: token missing or expired → skip, try next provider
State → UNKNOWN:    no data or error → use command anyway (conservative)
```

When `--all` is passed, the script checks all known providers in parallel, returns
a combined JSON. The resolver picks the first provider in the config whose state
is OK or DRAINING (in order of preference). RATE-LIMITED and KEY_INVALID are
skipped; UNKNOWN is used but logged as a warning.

## Impact analysis

### adversarial-common (new code)
- `quota.py` (~150 lines) — quota_cache, resolve_provider(), parse_quota_state().
- `providers.py` (~60 lines added) — ProviderConfig dataclass, load_provider_config()
  with YAML path arg + env override support.
- `runner.py` (~40 lines modified) — run_phase_cmd() wraps command execution with
  pre-flight provider selection.
- `report.py` (~20 lines added) — provider_history appended to final report.

### No defaults shipped
No skill ships a `.adversarial-providers.yaml`. Provider config is entirely external —
loaded from a path provided by the user via:
- `--provider-config <path>` CLI flag (available on all pipeline entry points)
- `ADVERSARIAL_PROVIDER_CONFIG` environment variable
- Fallback: `~/.config/adversarial/providers.yaml`

Skills are clean of any model references. The pipeline is a pure orchestration framework.

### Pipeline entry points affected
- `adversarial-code-loop/scripts/adversarial_loop.py` — accept `--provider-config` flag
- `adversarial-spec/scripts/adversarial_spec.py` — accept `--provider-config` flag
- `adversarial-plan/scripts/adversarial_plan.py` — accept `--provider-config` flag
- `adversarial-code-review/scripts/adversarial_review.py` — accept `--provider-config` flag

Each entry point loads the config and passes it to `adversarial_common.runner.run_phase()`.

### Not modified
- `adversarial_common/jsonio.py` — already handles structured JSON output.
- `adversarial_common/gitops.py` — no quota awareness needed.
- `adversarial_common/gates.py` — no quota awareness needed.
- `adversarial_common/snapshot.py` — no quota awareness needed.
- `adversarial_common/costs.py` — orthogonal, already tracks costs post-hoc.

## Out of scope (v2)

- **Automatic retry after quota reset** — detecting that a provider came back and
  retrying a failed phase. v1 just fails cleanly with a useful message.
- **Quota-aware step scheduling** — reordering pipeline steps to fit within
  available quota windows. v1 selects a provider per phase independently.
- **Cost-aware provider selection** — preferring cheaper models when within quota.
  v1 selects by availability only (preference order from config).
- **Cross-pipeline quota coordination** — two concurrent pipelines sharing quota
  state. v1's cache is per-process.
- **HTML / visual quota dashboard** — the quota data is available in final.json
  but no dashboard is built in v1.

## Known open questions

1. **How to detect Fable 5 separate quota from Claude Pro quota?** The
   `quota_api.py` fetches Claude's 5h sliding window. Fable 5 has an independent
   limit that requires a separate endpoint or heuristic (probe a small prompt
   before launching the real one). v1 may treat Fable 5 as claude-claude alias
   with an additional heuristic probe.
2. **Should the quota check be per-phase or per-call?** The pipeline has
   BUILD→REVIEW→FIX→VERIFY cycles. A single phase (e.g. REVIEW) may run a model
   for 10+ minutes and consume quota. v1 checks before the phase starts; it cannot
   detect mid-phase exhaustion. That's acceptable — the error would surface as a
   phase timeout which is already handled.
3. **What happens when two providers share the same alias name?** (e.g. "claude"
   for both DEV and REVIEW). They have separate entries in separate role sections
   of the YAML, so no collision. Same alias can have different quota_check commands
   per role if needed.
4. **How to handle GLM and Codex quotas?** `quota_api.py` already supports both.
   The resolver just passes `--glm` or `--codex` to `check-ai-quota.py --json`.
