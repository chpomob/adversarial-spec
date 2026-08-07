---
name: "file-based-prompts"
version: "1.0"
author: "adversarial-spec"
status: "draft"
tags: [adversarial, spec]
targets:
  - file: scripts/phases/phase_verify.py
    description: "Write findings to a JSON file on disk before invoking the verifier; remove spec-text and findings embedding; pass spec.md path + findings file path + READ marker requirement in the prompt"
  - file: scripts/phases/phase_write.py
    description: "Write brief to disk before invoking the writer when brief exceeds size threshold; prompt carries the brief file path + READ marker instead of inline text"
  - file: scripts/phases/phase_revise.py
    description: "Write findings to a JSON file on disk before invoking the writer; pass findings file path + READ marker requirement in the prompt instead of embedded JSON"
  - file: scripts/phases/__init__.py
    description: "Expose readgate helpers so phase modules can import ReadGatePolicy without direct adversarial_common imports"
  - file: tests/test_phases.py
    description: "Add tests asserting VERIFY/REVISE/WRITE prompts carry file paths, not embedded payloads; add tests for readgate transitions (WARNING → re-run, HARD_ERROR on second miss)"
---

# File-Based Prompts in adversarial-spec

## Problem

The "prompt is the program, working data lives in files" rule requires that
pipeline artifacts (spec.md, findings JSON) be read from disk by the model,
not embedded in prompts. No phase currently enforces proof-of-read (the
shared ReadGatePolicy helper in adversarial_common.readgate is defined but
unused — a repo-wide grep for "readgate|ReadGate" under scripts/ returns zero
hits). Three phases still embed payloads in their prompts:

- **VERIFY** (`phase_verify.py`): embeds both the spec text and the findings
  JSON in the prompt.
- **WRITE** (`phase_write.py`): embeds the brief text inline in the prompt.
- **REVISE** (`phase_revise.py`): embeds the findings JSON in the prompt.

## Requirements

- R1: The VERIFY phase writes the findings to a JSON file on disk (named
  `verify_findings.json` in a temporary directory outside the workdir, e.g.
  `/tmp/adversarial-spec/`) before constructing the prompt. The prompt
  instructs the model to read `spec.md` and the findings file from disk using
  a READ marker (`READ: <path>`). The spec text and findings body must not
  appear in the prompt.
- R2: The WRITE phase writes the brief to a file on disk before invoking the
  spec-writer when the brief exceeds a size threshold. The prompt carries the
  brief file path and a READ marker instruction. A brief of 2000 bytes or
  fewer (measured as UTF-8 encoded byte length) may be embedded inline;
  anything larger is written to a temporary directory outside the workdir
  (e.g. `/tmp/adversarial-spec/brief.md`) to prevent it from being committed
  alongside spec.md by `git add -A`.
- R3: The REVISE phase writes the findings to a JSON file on disk (named
  `revise_findings.json` in a temporary directory outside the workdir, e.g.
  `/tmp/adversarial-spec/`) before invoking the spec-writer. The prompt
  carries the findings file path and a READ marker instruction. Findings must
  not be embedded in the prompt.
- R4: ReadGatePolicy is wired into VERIFY, REVISE, and WRITE (when the brief
  is file-based). A missing READ marker in the model output triggers one
  automatic re-run with a reminder instruction appended. A second consecutive
  miss on the same path produces an infrastructure error (HARD_ERROR) that
  fails the phase. The readgate retry is independent of any pre-existing
  validation retry logic (e.g., the JSON schema retry in VERIFY); both may
  trigger in sequence if a response is missing the READ marker and also fails
  payload validation.
- R5: All existing `pytest` tests remain green. New tests assert that prompts
  carry file paths instead of embedded payloads and that readgate escalation
  (WARNING → HARD_ERROR) works end-to-end in each wired phase.

## Acceptance criteria

- AC1 (R1): A constructed VERIFY prompt contains neither the spec body
  (`spec_text`) nor the findings body (`json.dumps(findings, …)`). It
  contains a READ marker instruction referencing both `spec.md` and the
  findings file path. Verifiable via grep of the prompt string in tests.
- AC2 (R2): When a brief exceeds 2000 bytes (UTF-8 encoded), it is written to
  a temporary directory outside the workdir before the WRITE phase invokes
  the spec-writer, and the WRITE prompt references the brief file path with a
  READ marker instead of embedding the full text. When a brief is 2000 bytes
  or fewer, it may remain inline. The brief file must not appear in the
  workdir commit that contains spec.md.
- AC3 (R3): The REVISE prompt does not contain the findings JSON body. It
  references the findings file path with a READ marker instruction. Verifiable
  via grep of the prompt string in tests.
- AC4 (R4): A simulated model output without a READ marker causes the phase
  to re-run once with a reminder. A second consecutive miss triggers
  HARD_ERROR, failing the phase with exit code 1 and a message indicating the
  readgate hard error.
- AC5 (R5): `pytest tests/` passes 100%. Phase exit codes (0 for success, 1
  for failure, 2 for validation/usage errors, 3 for provider exhaustion or
  contract-gate rejection after max-loops, 5 for context-blocked brief
  preflight) remain unchanged. The CHALLENGE phase behavior is untouched.

## Caller enumeration

All callers of the affected phase functions are internal to the
`adversarial_spec.py` orchestrator. No external API consumers exist.

| File | Function/Method | Migration Note |
|------|----------------|----------------|
| `scripts/adversarial_spec.py` | `_run_pipeline()` — calls `run_write()` | Signature unchanged; the brief is written to disk by `run_write` internally |
| `scripts/adversarial_spec.py` | `_run_pipeline()` — calls `run_revise()` | Findings are written to disk by `run_revise` internally before prompt construction |
| `scripts/adversarial_spec.py` | `_run_pipeline()` — calls `run_verify()` | Findings are written to disk by `run_verify` internally before prompt construction |

Search method: `grep -rn "run_write\|run_revise\|run_verify" --include="*.py"`
across the repository. No dynamic dispatch or plugin loaders exist for these
functions.
