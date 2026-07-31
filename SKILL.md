---
name: adversarial-spec
description: "Adversarial specification writer. Takes a brief (from grill-me or user) and produces a structured spec.md with YAML frontmatter, requirements, acceptance criteria, and target files. Git-aware pipeline: each run on its own branch, squash-merge on approval."
version: 1.1.0
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
  --dev-cmd <cmd>          # spec-writer command (default: pi ... glm-5.2)
  --review-cmd <cmd>       # spec-challenger command (default: pi ... deepseek)
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

## Language discipline

All pipeline-internal text (spec, commit messages, findings, JSON) is **English**.
User-facing conversation stays in the conversation's language. Write the brief in
English unless the user explicitly instructs otherwise. A spec written in French or
other non-English languages will break later pipeline stages (plan, code loop) that
expect English section headings and identifiers.

## Provider selection — external config, not hardcoded

**The pipeline does not know what models exist.** It only knows **roles** (writer,
challenger). Which command fills each role is defined in an external provider config
file, loaded via `--provider-config`:

```bash
python3 scripts/adversarial_spec.py \
  --brief brief.md \
  --provider-config ~/.config/adversarial/providers.yaml
```

The provider config is a YAML file where each role lists commands in preference order.
The pipeline checks real-time quota before each phase and picks the first available command.
This means:

- **Writer and challenger MUST be different models** — enforced by config, not by the code.
  The pipeline refuses to run the same alias for both roles (same-model debate is
  pointless).
- **No model names appear anywhere in the pipeline code or SKILL.md.** The user's
  preferred provider chain (Claude, Codex, GLM, DeepSeek, or any other) lives in
  their personal `~/.config/adversarial/providers.yaml`, not in the skill.
- **Fallback chains are the user's choice**, not built-in defaults. If Claude is
  primary and GLM is fallback, that's in the user's config. If they switch to Gemini
  + OpenRouter next month, they change one file — no code changes.

The provider config also feeds `--dev-cmd` and `--review-cmd` defaults. When these
CLI flags are passed explicitly, they override the config for that role (backward
compatible).

## Prompt design

- **NEVER embed the brief or spec text in the challenge prompt.** The challenge
  prompt tells the model to read the brief and spec from the current directory
  (set via claude-tmux's `--cwd` flag). Embedding contradicts the adversarial
  design principle that context lives on disk / in git.
- The challenge prompt should be under ~2K chars: "Challenge the specification at
  \`spec.md\` against its brief at \`brief.md\` (both in the current directory).
  Output ONLY valid JSON: {\"findings\": [...], \"verdict\": \"...\"}"
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

A full adversarial pre-publication review surfaced 20 findings (1 blocker, 8 major, 6 minor, 5 nit). The most critical issues:

- **Stash loss on setup failure (A1/B2/B3):** If `_setup_git` fails mid-way, the user's stash is lost with no recovery path.
- **Exit code masks merge failure (A5/B1):** A failed squash-merge still returns exit 0 (APPROVED), misleading CI callers.
- **Spec validation is too loose (B4):** Only the `name` field is required; specs missing `version`, `author`, `targets`, and `acceptance_criteria` can be approved.
- **Writer can commit arbitrary changes (B5):** `commit_all` stages everything, not just `spec.md`; a prompt-injected writer could modify source files.
- **Convergence loop can relitigate settled findings (A2/B6):** Pipeline can exhaust `max_loops` on a REJECT-with-all-settled contradiction from the verifier.
- **final.json not written on infra failures (A3/B9):** A phase crash leaves a stale `final.json` from a prior run.
- **Artifact directories overwrite silently (A4):** Reruns of the same feature overwrite prior artifacts.
- **Custom `--out` paths can be committed (B8):** Only the literal `.adversarial-spec/` is gitignored; relative custom paths get committed.
- **Issues header stale (A7):** Retrospective file header says failures auto-append there, but they now go to `<out_dir>/ISSUES.md`.
