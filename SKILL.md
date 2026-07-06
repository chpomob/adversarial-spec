---
name: adversarial-spec
description: "WRITE → CHALLENGE → (REVISE → VERIFY)^N for specifications. Takes a brief, produces a structured spec.md (YAML frontmatter, requirements, acceptance criteria, target files) on an isolated git branch, squash-merged on approval."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [adversarial, spec, requirements, multi-model, persona, git]
    related_skills: [adversarial-code-loop, adversarial-code-review]
---

# Adversarial Spec

**WRITE → CHALLENGE → (REVISE → VERIFY)^N.** A sequential pipeline where one
model (the *spec-writer*) turns a brief into a structured `spec.md`, and another
(the *spec-challenger*) attacks it: missing requirements, contradictions,
untestable acceptance criteria, scope creep, ambiguous wording. The writer
revises, the challenger verifies. Every run happens on its own
`spec/<feature>/<N>` git branch; the approved spec is squash-merged into the
parent branch (or marked `[REJECTED]`).

The output `spec.md` is directly consumable by `adversarial-code-loop`
(`--spec spec.md`).

> **Rule: the orchestrator never writes the spec directly.** Writing goes
> through the spec-writer role; challenging through the spec-challenger role.

## Usage

```bash
python3 ~/.hermes/skills/adversarial-spec/scripts/adversarial_spec.py \
  --brief brief.md \
  --workdir ~/myproject
```

Or pipe the brief on stdin:

```bash
echo "Add rate limiting to the public API" | \
  python3 ~/.hermes/skills/adversarial-spec/scripts/adversarial_spec.py \
    --feature rate-limiting --workdir ~/myproject
```

## CLI

| Flag | Env var | Default | Meaning |
|------|---------|---------|---------|
| `--brief <file>` | — | stdin | File containing the brief |
| `--dev-cmd <cmd>` | `ASPEC_DEV_CMD` | `pi --provider zai --model glm-5.2` | spec-writer command |
| `--review-cmd <cmd>` | `ASPEC_REVIEW_CMD` | `pi --provider deepseek --model deepseek-v4-pro` | spec-challenger command |
| `--workdir <dir>` | — | `.` | Target directory (repo auto-init'd if needed) |
| `--max-loops <N>` | — | `2` | Max REVISE→VERIFY rounds |
| `--feature <name>` | — | brief filename | Branch/artifact name |
| `--timeout <N>` | — | `600` | Per-subprocess timeout (s) |
| `--out <dir>` | — | `.adversarial-spec` | Artifacts directory |
| `--no-merge` | — | off | On approval, leave the spec branch unmerged |

## Phases

0. **GIT SETUP** — create `spec/<feature>/<N>` from the current branch,
   stash dirty changes, record the branch-point, gitignore `.adversarial-spec/`.
   A non-repo workdir is `git init`'d with `main` pinned.
1. **WRITE** — spec-writer (persona `spec-writer`) receives the brief and writes
   `spec.md` to disk. The orchestrator validates it (file exists, YAML
   frontmatter parses, `name` key present) and commits
   `write: <feature> — <summary>`.
2. **CHALLENGE** — spec-challenger (persona `spec-challenger`) reviews
   `spec.md` and outputs JSON findings (`id`, `severity`, `section`, `summary`,
   `evidence`) plus a verdict. Zero findings + `APPROVE` → direct approval,
   revise/verify skipped.
3. **REVISE** — spec-writer amends `spec.md` on disk from the findings
   (ids kept stable). Commit: `revise: <feature> — round N`.
4. **VERIFY** — spec-challenger marks each finding `resolved` / `rejected` /
   `disputed` with an overall `APPROVE|REJECT`. Approval requires every finding
   settled *and* an `APPROVE` verdict; otherwise the still-open findings feed
   the next round. After `--max-loops`, an empty `[REJECTED]` commit is
   recorded and the branch is kept for inspection.

On approval the branch is squash-merged as `squash: <feature> — spec approved`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | APPROVED — spec merged (or left on its branch with `--no-merge`) |
| 1 | Infrastructure failure (phase crash, git error, interrupt) |
| 2 | Usage error (bad flags, missing/empty brief) |
| 3 | REJECT — findings unresolved after max-loops |

## Artifacts (`<out>/<feature>/`)

`00_brief.txt`, `01_write.json`, `02_challenge.json`, `03_revise_N.json`,
`04_verify_N.json`, `final.md`, and the machine-readable `final.json`
(`verdict`, `loops`, `branch`, `merged`, `reason`).

## spec.md contract

```yaml
---
name: "feature-name"
version: "1.0"
author: "adversarial-spec"
status: "draft"
tags: [adversarial, spec]
targets:
  - file: path/to/file.py
    description: "What changes in this file"
---

# Feature title

## Problem
## Requirements        (R1, R2, ... stable ids)
## Acceptance criteria (AC1 (R1), ... testable, each citing a requirement)
```

## Reuse

- Engine: `adversarial-common/adversarial_common/` — `gitops` (branch, commit,
  squash, stash), `providers` (provider detection, persona injection),
  `jsonio` (JSON extraction, artifacts). JSON parsing uses the same 3-strategy
  extraction as the code-loop verifier (fence strip, `{...}`, `[...]`).
- Personas: `adversarial-common/personas/spec-writer.md` and
  `spec-challenger.md` (single source of truth, loaded at runtime; a
  provider-specific `<persona>-pi` variant is used when present, with fallback
  to the base persona).
- Pattern: `adversarial-code-loop/scripts/adversarial_loop.py` (v4), minus
  gates, arbiter and resume — the spec pipeline is deliberately smaller.

## Failure logging

Phase failures append an entry to `_retrospective/ISSUES.md` (phase, branch,
error, last stdout) for post-mortem analysis.

## Tests

```bash
cd ~/.hermes/skills/adversarial-spec && python3 -m pytest tests/ -q
```
