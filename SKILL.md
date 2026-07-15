---
name: adversarial-spec
description: "Adversarial specification writer. Takes a brief (from grill-me or user) and produces a structured spec.md with YAML frontmatter, requirements, acceptance criteria, and target files. Git-aware pipeline: each run on its own branch, squash-merge on approval."
version: 1.0.0
author: Hermes Agent
license: MIT
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
  --dev-cmd <cmd>          # spec-writer (default: pi ... glm-5.2)
  --review-cmd <cmd>       # spec-challenger (default: pi ... deepseek)
  --workdir <dir>          # default: .
  --max-loops <N>          # default: 2
  --feature <name>         # default: from brief filename
  --timeout <N>            # default: 600
  --out <dir>              # default: .adversarial-spec
  --no-merge
```

## Pre-flight checklist (orchestrator)

Run these BEFORE writing the brief, especially when contributing to an upstream project:

1. **Check CONTRIBUTING.md** — Project-specific rules for branch naming (`feat/`, `fix/`, `docs/`), commit message format (Conventional Commits), PR template fields, and test requirements. Incorporate these into the spec's acceptance criteria.
2. **Check for pre-existing PR review feedback** — If the feature already has an open PR with reviewer comments (automated or human), read the findings and incorporate them into the brief. The spec should address what the review flagged, not re-propose rejected patterns.
3. **Choose the right parent branch** — For upstream contributions, create a feature branch from `upstream/main` (not your fork's main): `git checkout upstream/main -b feat/my-feature`. For work on your own fork, use your fork's main. The parent branch determines what the pipeline's squash-merge targets.
4. **Gap analysis for partially-merged features** — If the feature already exists in upstream but is incomplete, run the gap-matrix pattern from `adversarial-code-loop`'s `references/partial-merge-gap-fill.md` before writing the brief. The spec should target gaps, not re-implement what's already merged.
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

## Language discipline

All pipeline-internal text (spec, commit messages, findings, JSON) is **English**.
User-facing conversation stays in the conversation's language. Write the brief in
English unless the user explicitly instructs otherwise. A spec written in French or
other non-English languages will break later pipeline stages (plan, code loop) that
expect English section headings and identifiers.

## Model pairing rules

- **Writer and challenger MUST be different models** (never the same model for both roles).
- **Preferred pairing:** Codex (writer) + Claude Fable 5 via tmux (challenger).
- **Fallback when Claude quota low / timeout:** GLM-5.2 via `pi -p --provider zai --model glm-5.2 --thinking high` (challenger).
- **Fallback when Codex quota low / no API:** GLM-5.2 via `pi` (writer).
- **DeepSeek is NOT a fallback for spec-challenger unless explicitly requested.**
  The user prefers Claude or GLM for this role.

## Prompt design

- **NEVER embed the brief or spec text in the challenge prompt.** The challenge
  prompt tells the model to read the brief and spec from the current directory
  (set via claude-tmux's `--cwd` flag). Embedding contradicts the adversarial
  design principle that context lives on disk / in git.
- The challenge prompt should be under ~2K chars: "Challenge the specification at
  \`spec.md\` against its brief at \`brief.md\` (both in the current directory).
  Output ONLY valid JSON: {"findings": [...], "verdict": "..."}"
- If the claude-tmux wrapper is used as challenger, ensure `--cwd` points to the
  workdir so the model can read the files. Without `--cwd`, the tmux session
  starts in the wrong directory and the model cannot find plan.md/spec.md.

## Pitfalls

- Keep the brief concise but complete. Grill-me can expand a vague idea before feeding it here.
- The spec-writer writes to disk (spec.md), not stdout. The challenger reads from disk.
- YAML frontmatter is validated: name, version, author, targets are required.
- The same code patterns as adversarial-code-loop v4: git branch isolation, phase modules, squash merge.
- **Claude Fable 5 (2026-07) IS reliable as spec-challenger.** Validated: 11 findings, REQUEST_CHANGES → REVISE → APPROVE (11/11 settled), all valid JSON. Use `--review-cmd "python3 /path/to/claude-tmux.py --yolo --model best --timeout 600 --hard-timeout 1800"` with `--timeout 1800`. (600s timeout is insufficient for Fable 5 extended thinking.)
- **Codex pipe-stdin + reasoning=high can stall 2-4 min** before producing output. Process stays running — not a crash. If stalled >5 min, switch `--dev-cmd` to `reasoning=medium`.
- **Fallback when Claude fails:** DeepSeek (`pi --provider deepseek --model deepseek-v4-pro`) or GLM-5.2 (`pi -p --provider zai --model glm-5.2 --thinking high`). Both output clean JSON reliably.
- **Validated pairing (2026-07): Codex DEV + Claude REVIEW** across all 3 stages (spec, plan, code loop). All in 1 cycle. See `adversarial-plan` references/codex-claude-full-pipeline.md.
- **Pre-existing PR review feedback shapes the brief.** When the user already has an open PR with reviewer comments (e.g. hermes-sweeper, teknium1), read the full review before writing the brief. The spec must explicitly address each review finding so the pipeline doesn't re-propose code the reviewer already rejected. Document the review verdict and each finding in the brief's context section. The spec-challenger will independently validate the approach, which serves as a second opinion on whether the review feedback was correctly interpreted.
- **Requirement ID format is regex-constrained.** The validation regex expects `R1:` or `R1-` (colon or hyphen after the ID). `R1 (P0) —` or `R1 —` with em dash will NOT match — shows "Requirements section has no identifiable requirement ids". Always write requirements as `- R1: description` or `- R1 - description`. The em dash `—` is not in the regex lookahead set.
- **Acceptance criteria format similarly constrained.** The regex expects `AC1 (R1):` at the start of a bullet line. No space between `)` and `:`. Wrong: `- AC1 (R1) : text`. Right: `- AC1 (R1): text`.
- **Keep the spec in English.** All pipeline-internal text (spec, plan, commit messages, findings) must be English. Only user-facing conversation stays in the user's language. Write the brief in English unless you explicitly instruct otherwise.
- **If the spec-writer produces a valid spec in the wrong format**, fix the format with a script (regex replace em dashes and parenthesized markers) rather than re-running the WRITE phase. The validation regexes are strict — small formatting issues cause silent failures.

## Known Issues (from 2026-07-14 pre-publication review)

A full adversarial pre-publication review surfaced 20 findings (1 blocker, 8 major, 6 minor, 5 nit). See `references/adversarial-2026-07-14-pre-publication-review.md` for the complete breakdown. The most critical issues:

- **Stash loss on setup failure (A1/B2/B3):** If `_setup_git` fails mid-way, the user's stash is lost with no recovery path.
- **Exit code masks merge failure (A5/B1):** A failed squash-merge still returns exit 0 (APPROVED), misleading CI callers.
- **Spec validation is too loose (B4):** Only the `name` field is required; specs missing `version`, `author`, `targets`, and `acceptance_criteria` can be approved.
- **Writer can commit arbitrary changes (B5):** `commit_all` stages everything, not just `spec.md`; a prompt-injected writer could modify source files.
- **Convergence loop can relitigate settled findings (A2/B6):** Pipeline can exhaust `max_loops` on a REJECT-with-all-settled contradiction from the verifier.
- **final.json not written on infra failures (A3/B9):** A phase crash leaves a stale `final.json` from a prior run.
- **Artifact directories overwrite silently (A4):** Reruns of the same feature overwrite prior artifacts.
- **Custom `--out` paths can be committed (B8):** Only the literal `.adversarial-spec/` is gitignored; relative custom paths get committed.
- **`_retrospective/ISSUES.md` header is stale (A7):** Says failures auto-append there, but they now go to `<out_dir>/ISSUES.md`.
