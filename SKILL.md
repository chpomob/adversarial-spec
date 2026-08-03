---
name: adversarial-spec
description: "Adversarial specification writer. Takes a brief (from grill-me or user) and produces a structured spec.md with YAML frontmatter, requirements, acceptance criteria, and target files. Git-aware pipeline: each run on its own branch, squash-merge on approval."
version: 1.1.0
author: Hermes Agent
license: 0BSD
platforms: [linux, macos]
metadata:
  hermes:
    tags: [adversarial, spec, planning, specification, requirements]
    related_skills: [grill-me, adversarial-plan, adversarial-code-loop]
---

# Adversarial Spec

**Brief → structured specification.** Two-role adversarial pipeline that transforms a
vague idea (from grill-me or direct input) into a formal spec.md with YAML frontmatter,
requirements, acceptance criteria, and target file descriptions.

## Installation

Requires the `adversarial-common` sibling repo (shared engine). One-line install:

curl -fsSL https://raw.githubusercontent.com/chpomob/adversarial-spec/main/scripts/install.sh | bash

or, from an existing checkout:

bash scripts/install.sh

Both place adversarial-spec and adversarial-common side by side under `~/.hermes/skills` (override the target with `$1` or `$HERMES_HOME`).

## Workflow

```
PHASE 0 ──→ GIT SETUP (branch, stash, init)
PHASE 1 ──→ WRITE  (spec-writer reads brief, writes spec.md)
PHASE 2 ──→ CHALLENGE (spec-challenger critiques for gaps/contradictions)
PHASE 3 ──→ REVISE (spec-writer amends spec.md per findings)
PHASE 4 ──→ VERIFY (spec-challenger checks findings resolved)
MERGE  ──→ squash-merge (APPROVED) or [REJECTED] commit
```

## CLI

```bash
python3 scripts/adversarial_spec.py \
  --brief <file>           # brief file (default: stdin)
  --dev-cmd <cmd>          # default: pi --provider zai --model glm-5.2
  --review-cmd <cmd>       # default: pi --provider deepseek --model deepseek-v4-pro
  --workdir <dir>          # default: .
  --max-loops <N>          # default: 2
  --feature <name>         # default: from brief filename
  --timeout <N>            # default: 600
  --out <dir>              # default: .adversarial-spec
  --provider-config <path> # external provider config (default: ~/.config/adversarial/providers.yaml)
  --no-merge
```

## Pre-flight checklist (orchestrator)

Run these BEFORE writing the brief, especially when contributing to an upstream project:

1. **Check CONTRIBUTING.md** — Project-specific rules for branch naming (`feat/`, `fix/`, `docs/`), commit message format (Conventional Commits), PR template fields, and test requirements. Incorporate these into the spec's acceptance criteria.
2. **Check for pre-existing PR review feedback** — If the feature already has an open PR with reviewer comments (automated or human), read the findings and incorporate them into the brief. The spec should address what the review flagged, not re-propose rejected patterns.
3. **Choose the right parent branch** — For upstream contributions, create a feature branch from `upstream/main` (not your fork's main): `git checkout upstream/main -b feat/my-feature`. For work on your own fork, use your fork's main. The parent branch determines what the pipeline's squash-merge targets.
4. **Gap analysis for partially-merged features** — If the feature already exists in upstream but is incomplete, run the gap-matrix pattern from `adversarial-code-loop` (partial-merge-gap-fill) before writing the brief. The spec should target gaps, not re-implement what's already merged.
5. **Stash/commit local changes** — Dirty trees are auto-stashed in PHASE 0, but stash-pop conflicts can abort the pipeline. Manual pre-commit is safer when `--workdir` is a live install.

## Output

spec.md with YAML frontmatter:

```yaml
---
name: "feature-name"
version: "1.0"
author: "adversarial-spec"
status: "draft"
targets:
  - file: path/to/file.rs
    description: "What changes in this file"
---

# Feature title

## Problem
What problem does this solve?

## Requirements
- Bullet list of functional requirements

## Acceptance criteria
1. Each requirement has at least one testable criterion
```

## Personas

Loaded from adversarial-common/personas/:
- spec-writer.md — reads brief, writes spec.md to disk
- spec-challenger.md — reads spec.md, outputs JSON findings

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | APPROVED — spec squash-merged |
| 1 | Infrastructure failure |
| 2 | Usage error |
| 3 | REJECT |
| 5 | CONTEXT_BLOCKED — CI/preflight context gate rejected the brief |

## Language discipline

All pipeline-internal text (spec, commit messages, findings, JSON) is **English**.
User-facing conversation stays in the conversation's language. Write the brief in
English unless the user explicitly instructs otherwise. A spec written in French or
other non-English languages will break later pipeline stages (plan, code loop) that
expect English section headings and identifiers.

## Provider selection — registry, explicit commands, then fallbacks

Provider-mode precedence is `--provider-config` (or the
`ADVERSARIAL_PROVIDER_CONFIG`/implicit default registry) > explicit role flags >
hardcoded fallback commands. A selected registry supplies the writer and challenger
chains and enables quota-aware selection:

```bash
python3 scripts/adversarial_spec.py \
  --brief brief.md \
  --provider-config ~/.config/adversarial/providers.yaml
```

The provider config is a YAML file where each role lists commands in preference order.
The pipeline checks real-time quota before each phase and picks the first available command.
Within registry mode, a non-empty `--dev-cmd` or `--review-cmd` is an explicit,
quota-bypassing override for that role. Without a registry, each role resolves its
command from the explicit flag, then `ASPEC_DEV_CMD`/`ASPEC_REVIEW_CMD`, then these
hardcoded fallbacks:

- Writer: `pi --provider zai --model glm-5.2`
- Challenger: `pi --provider deepseek --model deepseek-v4-pro`

This means:

- **Writer and challenger MUST be different models** — enforced by config, not by the code.
  The pipeline refuses to run the same alias for both roles (same-model debate is
  pointless).
- **Configured fallback chains remain the user's choice.** The commands above are
  legacy fallbacks only when no registry or role-specific command is available.
- **Explicit role commands remain backward compatible.** In registry mode they
  override that role's selected provider; in legacy mode they take precedence over
  the environment and hardcoded command.

## Prompt design

- **NEVER embed the brief or spec text in the challenge prompt.** The challenge
  prompt tells the model to read `spec.md` from the phase working directory.
  It includes only bounded metadata such as the branch-point SHA and output
  schema; the specification itself remains on disk. Embedding contradicts the
  adversarial design principle that context lives on disk / in git.
- Keep the challenge prompt under ~2K chars: "Read and challenge `spec.md` from
  the current working directory. Inspect the cumulative change from the
  branch-point SHA. Output ONLY valid JSON."
- **Fixed 2026-07-15:** `scripts/phases/phase_challenge.py` previously
  concatenated the full spec text into the prompt (`f"--- spec.md ---\n{spec_text}"`)
  despite the SKILL.md saying not to. Patched to an under-1KB instruction with
  no embedding; its size is independent of the spec length. The model reads
  spec.md from disk via `--cwd`. See `adversarial-plan` pitfall about CHALLENGE
  prompt reduction for the sibling fix.
- If the claude-tmux wrapper is used as challenger, ensure `--cwd` points to the
  workdir so the model can read the files. Without `--cwd`, the tmux session
  starts in the wrong directory and the model cannot find plan.md/spec.md.
- **claude-tmux wrapper v1 does NOT support `--yolo`.** The `--dangerously-skip-permissions`
  behaviour is the DEFAULT (set `danger=True` in the wrapper, no flag needed). Passing
  `--yolo` causes argparse exit code 2. Just omit it: `--model sonnet --timeout 900`
  is sufficient. Validated on the v1 wrapper at `/home/chpo/claude-tmux-wrapper/claude-tmux.py`.

## Pitfalls

- Keep the brief concise but complete. Grill-me can expand a vague idea before feeding it here.
- The spec-writer writes to disk (spec.md), not stdout. The challenger reads from disk.
- YAML frontmatter is validated: name, version, author, targets are required.
- The same code patterns as adversarial-code-loop v4: git branch isolation, phase modules, squash merge.
- **Never hardcode model-specific fallback chains in a spec.** Provider selection is
  purely configuration-driven. A spec that references "Claude", "Codex", or any model
  by name in its requirements or acceptance criteria creates an implicit dependency on
  specific providers and breaks when the user's lineup changes. The spec describes
  *what* to build — the *who* (which model builds it) belongs in the provider config.
- **Never list model-specific commands in a spec's targets.** The `cmd:` field in
  target file descriptions should describe the change ("add YAML config loader",
  "expose ProviderConfig dataclass"), not the tool that makes it.
- **Pre-existing PR review feedback shapes the brief.** When the user already has an open PR with reviewer comments (e.g. hermes-sweeper, teknium1), read the full review before writing the brief. The spec must explicitly address each review finding so the pipeline doesn't re-propose code the reviewer already rejected. Document the review verdict and each finding in the brief's context section. The spec-challenger will independently validate the approach, which serves as a second opinion on whether the review feedback was correctly interpreted.
- **Requirement ID format is regex-constrained.** The validation regex expects `R1:` or `R1-` (colon or hyphen after the ID). `R1 (P0) —` or `R1 —` with em dash will NOT match — shows "Requirements section has no identifiable requirement ids". Always write requirements as `- R1: description` or `- R1 - description`. The em dash `—` is not in the regex lookahead set.
- **Acceptance criteria format similarly constrained.** The regex expects `AC1 (R1):` at the start of a bullet line. No space between `)` and `:`. Wrong: `- AC1 (R1) : text`. Right: `- AC1 (R1): text`.
- **Keep the spec in English.** All pipeline-internal text (spec, plan, commit messages, findings) must be English. Only user-facing conversation stays in the user's language. Write the brief in English unless you explicitly instruct otherwise.
- **If the spec-writer produces a valid spec in the wrong format**, fix the format with a script (regex replace em dashes and parenthesized markers) rather than re-running the WRITE phase. The validation regexes are strict — small formatting issues cause silent failures.

## Known Issues (from 2026-07-14 pre-publication review)

A full adversarial pre-publication review surfaced 20 findings (1 blocker, 8 major, 6 minor, 5 nit). Status below is reconciled against current code and tests; items marked "fixed" carry a test pointer, the rest remain accurate as open issues.

- **Stash loss on setup failure (A1/B2/B3) — fixed.** `pipeline_base.setup_git` records the stash id onto the shared state the instant `git stash push` succeeds, before branch creation even runs, so a later failure within the same `setup_git` call still leaves `restore_git` able to pop it back onto the parent branch. See `tests/test_p14_integration.py::test_setup_git_partial_failure_leaves_recoverable_state`.
- **Exit code masks merge failure (A5/B1) — fixed.** A failed squash-merge now forces `EXIT_INFRA` and rewrites the persisted `final.json` verdict to `INFRA`, so a merge failure can never read as `APPROVED`. See `tests/test_orchestrator.py::test_finish_merge_failure_returns_infra_and_records_error` and `::test_verdict_not_approved_on_merge_failure`.
- **Spec validation is too loose (B4) — fixed.** `phase_spec._REQUIRED_SCALARS` now requires `name`, `version`, and `author`; a non-empty `targets` list (each entry needing `file`/`description`) and full requirement/acceptance-criteria coverage are enforced too. See `tests/test_phases.py::test_validate_spec_missing_name` and `::test_validate_spec_requires_all_frontmatter_fields`.
- **Writer can commit arbitrary changes (B5):** `commit_all` still stages everything (`git add -A`), not just `spec.md`; a prompt-injected writer could modify source files. Still open.
- **Convergence loop can relitigate settled findings (A2/B6):** partially mitigated — the REVISE/VERIFY loop no longer overwrites the findings list with an empty set when the verifier REJECTs while marking every finding settled (avoids a crash), but the underlying problem — the pipeline can still burn through `max_loops` re-running the same contradictory REJECT-with-all-settled findings — is unfixed and has no test coverage. Still open.
- **final.json not written on infra failures (A3/B9):** `pipeline_base.phase_failure` only logs to `ISSUES.md`; it never writes or clears `final.json`, so a stale `final.json` from a prior run survives a phase crash untouched. Still open.
- **Artifact directories overwrite silently (A4):** Artifact directories are keyed only by feature name, with no timestamp or run id; a rerun of the same feature silently overwrites all prior artifacts. Still open.
- **Custom `--out` paths can be committed (B8) — fixed.** `_ensure_out_gitignored` anchors a normalized `.gitignore` entry at the nearest tracked ancestor of `--out` before the first commit — covering relative paths, `./`-prefixed paths, paths outside `workdir` but inside the enclosing repo, and paths that resolve to the repo root itself (e.g. `--out .`, where the naive `./` pattern would ignore nothing). See `tests/test_orchestrator.py::test_custom_out_dir_is_gitignored`, `::test_relative_out_with_dot_prefix_is_gitignored`, `::test_outside_workdir_but_inside_repo_is_gitignored`, `::test_custom_out_dot_dir_is_gitignored`, `::test_custom_out_abs_path_inside_repo_is_gitignored`, and `::test_custom_out_abs_repo_root_outside_workdir_is_gitignored`.
- **Issues header stale (A7) — fixed.** `_retrospective/ISSUES.md`'s header now correctly states that pipeline failures go to `<out_dir>/ISSUES.md`, not this file.
