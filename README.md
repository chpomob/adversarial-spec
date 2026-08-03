# adversarial-spec

**Brief → structured spec.** Two-role adversarial pipeline that takes a product brief, specification request, or feature idea and produces a `spec.md` with YAML frontmatter, numbered requirements, and acceptance criteria.

For Hermes Agent, Claude Code, Codex, or any LLM CLI.

## How it works

```
PHASE 1 ──→ WRITE     (spec-writer turns brief into structured spec.md)
PHASE 2 ──→ CHALLENGE (spec-challenger attacks for gaps, contradictions, untestable criteria)
PHASE 3 ──→ REVISE    (spec-writer amends per findings)
PHASE 4 ──→ VERIFY    (spec-challenger checks findings resolved)
MERGE  ──→ squash-merge (APPROVED) or [REJECTED] commit
```

## Output format

```yaml
---
name: "feature-name"
version: "1.0"
author: "adversarial-spec"
status: "draft"
targets:
  - file: path/to/file
    description: "What this file must do"
---

## Requirements
- R1: …
- R2: …

## Acceptance criteria
- AC1 (R1): …
- AC2 (R2): …
```

Specs are consumed by `adversarial-plan` for planning and `adversarial-code-loop` for implementation.

## Comparison

| Feature | adversarial-spec | zscole/adversarial-spec |
|---------|-----------------|----------------------|
| Structured frontmatter | ✅ YAML + targets + requirements | ❌ Free-form |
| Acceptance criteria | ✅ Per-requirement ACs | ❌ |
| Git-native pipeline | ✅ Branch-per-spec, squash-merge | ❌ Single file |
| plan/code-loop integration | ✅ Direct feed to plan + loop | ❌ Standalone |

## Quick start

```bash
python3 scripts/adversarial_spec.py \\
  --brief /path/to/brief.md \\
  --dev-cmd "<your-dev-cmd>" \\
  --review-cmd "<your-review-cmd>"
```

## Dependencies

- Python ≥ 3.11
- Git ≥ 2.5
- Two LLM CLIs (spec-writer + spec-challenger)

Uses `adversarial-common` as the shared engine.

## License

0BSD — see [LICENSE](LICENSE).
