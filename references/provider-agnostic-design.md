# Provider-Agnostic Design — Architectural Decision

## Decision

All adversarial skills (spec, plan, code-loop, code-review) shall be **fully
model-agnostic**. No skill hardcodes model names, provider aliases, CLI commands,
or fallback chains. Provider selection is entirely external configuration.

**Exception — legacy zero-config defaults.** A single, narrow, explicitly
documented exception is permitted: a skill MAY carry a built-in last-resort
dev/review command pair, used ONLY when no provider registry
(`--provider-config`) and no role-specific env var / flag is supplied. This
exception exists solely so the skill runs zero-config. It is not a license to
hardcode model pairing rules, per-phase model choices, or fallback chains in
skill guidance; those remain prohibited. The two defaults are intentionally
different models so writer and challenger never collapse into one voice.

## Scope

This rule governs normative skill behavior and guidance. Reference notes may name
a provider, model, or wrapper when recording observed, provider-specific behavior,
but they must not prescribe provider selection or fallback chains. Any retry that
changes providers follows the externally configured provider order. The sole
deviation permitted is the legacy zero-config default-command exception named
above; nothing in skill guidance may resurrect hardcoded fallback chains.

## Rationale

The user corrected this directly on 2026-07-16:

> *"Les fallback devraient pas etre hardcodés, ca devrait aussi se configurer,
> j aimerais que les skills ne mentionnent pas specifiquement les modeles et outils,
> ca devrait etre juste notre configuration"*

## What changed

**Before:** adversarial-spec SKILL.md had:
- A "Model pairing rules" section with specific models (Codex writer, Claude Fable 5 challenger)
- Pitfalls with hardcoded commands and model-specific workarounds
- `--dev-cmd`/`--review-cmd` defaults hardcoded to specific model commands

**After:**
- "Provider selection" section says model selection is external, skills don't hardcode
- Pitfalls reference generic "provider command" failures, not specific models
- CLI docs show no default commands
- related_skills updated to include `grilling` alongside `grill-me`

## What was removed (model-specific content previously in the skills)

From adversarial-spec:
- "Preferred pairing: Codex (writer) + Claude Fable 5 via tmux (challenger)"
- "Fallback when Claude quota low / timeout: GLM-5.2..."
- "DeepSeek is NOT a fallback for spec-challenger unless explicitly requested"
- Pitfall about Fable 5 reliability, Codex stalling with reasoning=high
- "Validated pairing (2026-07): Codex DEV + Claude REVIEW"

From adversarial-plan:
- CLI default commands with pi/glm-5.2 and pi/deepseek
- Integration example with hardcoded commands
- Pitfall about Fable 5 success as plan-challenger
- "Validated end-to-end pairing: Codex DEV + Claude Fable 5 REVIEW"

## What remains as an intentional legacy zero-config fallback

Two literal defaults are **deliberately kept** in `scripts/adversarial_spec.py`
(`DEFAULT_DEV_CMD`, `DEFAULT_REVIEW_CMD`) as the last-resort command used when
no provider registry (`--provider-config`) and no role-specific env/flag is
supplied:

- Writer (dev): `pi --provider zai --model glm-5.2`
- Challenger (review): `pi --provider deepseek --model deepseek-v4-pro`

This is the **one narrow, documented exception** to the "no skill hardcodes
model names" rule, not an oversight. They exist so the skill runs zero-config,
and the two are intentionally different models so writer and challenger never
collapse into the same voice. Resolution order in this legacy mode is:
explicit `--dev-cmd`/`--review-cmd` → `ASPEC_DEV_CMD`/`ASPEC_REVIEW_CMD` →
these hardcoded defaults. With a provider registry configured, the registry's
ordered providers are used instead and these literals never apply.

## How provider selection works now

1. User configures `~/.config/adversarial/providers.yaml` with ordered provider lists per role
2. Each entry has: alias, command string, optional quota_check flag, optional stop_threshold
3. Pipeline loads this config at startup via `--provider-config` flag or default path
4. Before each phase, the resolver checks quotas and picks the first available provider
5. Explicit `--dev-cmd`/`--review-cmd` still bypass quota checks (backward compat)

See the `quota-aware-provider-registry` spec for the full design.

## Threshold-based provider selection

Providers can specify a `stop_threshold`:
- **Percentage-based** (Claude, Codex, GLM sliding window): provider skipped when
  `used_pct > threshold` (default 100).
- **Balance-based** (DeepSeek, Gemini credits): provider skipped when
  `balance < threshold` (default 0).

## Force modes

- `--force`: globally bypass quota checks, use first provider per role
- `--force-provider <role>:<alias>`: force one role's alias, others still check quotas

## User's ordering preference

**DeepSeek always last** in every fallback chain. Rationale: DeepSeek has prepaid
token balance (stop in $), so consume free/rate-limited options (Claude, GLM) first.
GLM-5.2 (Z.AI Lite, 80 req/5h) preferred before DeepSeek credits.

## Config is external

Config: `~/.config/adversarial/providers.yaml`. Skills never ship or install it.
