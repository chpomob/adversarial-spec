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
return string. An earlier version of this fix was incomplete because
`run_challenge()` still appended the text as `--- current spec.md ---`; the
final fix removes that caller-level append as well. The only prompt supplied to
the provider is now the result of `_build_prompt(branch_point)` (plus the
static JSON-only reminder on retry).

Prompt size dropped from ~1-15KB (variable, depends on spec length) to under
1KB (the instruction plus the branch-point identifier). With the current
template it is 705 chars for a 40-character SHA and does not vary with the
spec length.

## Files changed

- `scripts/phases/phase_challenge.py`: `_build_prompt()` signature and body,
  call site `run_challenge()` updated to pass `branch_point` only and no longer
  append the on-disk spec text.
- `tests/test_phases.py`: regression coverage verifies that `run_challenge()`
  produces the same base prompt for short and long specs, excludes both known
  spec markers and both file contents, and preserves those properties on the
  invalid-JSON retry path.

## Validation

```
PASS — 8/8 checks
  prompt length: 705 chars with a 40-character branch-point SHA
  provider prompt equals _build_prompt(branch_point)
  prompt does not vary with short versus long spec content
  no spec marker or spec content in prompt
  retry prompt contains only the base prompt and static JSON reminder
  no spec_text in function signature
  JSON schema present
  branch_point still accepted
```

## Sibling fix

`adversarial-plan/scripts/phases/phase_challenge.py` was patched earlier
(2026-07-14) with the same approach: removed embedded plan+spec text, prompt
now ~724 chars.
