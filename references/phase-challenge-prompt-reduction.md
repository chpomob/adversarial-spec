# Challenge prompt reduction — 2026-07-15

`phase_challenge.py` in adversarial-spec was embedding the full spec text
(~1-15 KB) into the challenge prompt at line 55:

```python
f"--- spec.md ---\n{spec_text}"
```

This contradicted the SKILL.md's own rule: "NEVER embed the brief or spec text
in the challenge prompt." The model was instructed to read from disk via `--cwd`,
but then received the entire file again inline.

## Fix

`_build_prompt()` no longer accepts `spec_text` as a parameter. The signature
changed from `_build_prompt(spec_text, branch_point="")` to
`_build_prompt(branch_point="")`. The spec text reference was removed from the
return string.

Prompt size dropped from ~1-15KB (variable, depends on spec length) to ~637
chars (fixed, just the instruction).

## Files changed

- `scripts/phases/phase_challenge.py`: `_build_prompt()` signature and body,
  call site `run_challenge()` updated to pass `branch_point` only.

## Validation

```
PASS — 5/5 checks
  prompt length: 637 chars
  no "--- spec.md ---" in prompt
  no spec_text in function signature
  JSON schema present
  branch_point still accepted
```

## Sibling fix

`adversarial-plan/scripts/phases/phase_challenge.py` was patched earlier
(2026-07-14) with the same approach: removed embedded plan+spec text, prompt
now ~724 chars.
