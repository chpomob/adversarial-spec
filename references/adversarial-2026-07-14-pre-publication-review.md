# Pre-Publication Adversarial Review — adversarial-spec (2026-07-14)

**Scope:** Full `--project-dir` review of `adversarial-spec` (10 Python files, ~1.8 KLOC).
**Models:** Claude Fable 5 (Architect + Synthesis), Codex GPT-5.6-Sol (Inspector + Cross).
**Privacy pre-scan:** Clean — no hardcoded paths, credentials, or personal info.
**Verdict:** REQUEST_CHANGES (both reviewers, before synthesis).

## Findings Summary

| Bucket | Count | Severities |
|--------|-------|-----------|
| Architect | 10 | 1 major, 5 minor, 4 nit |
| Inspector | 10 | 1 blocker, 7 major, 1 minor, 1 nit |
| **Total unique** | **20** | **1 blocker, 8 major, 6 minor, 5 nit** |

## Architect Findings (Claude Fable 5)

| ID | Severity | File | Summary |
|----|----------|------|---------|
| A1 | **major** | `scripts/adversarial_spec.py:138` | Stash stranded if `_setup_git` fails after `stash_dirty` — error path returns without `stash_id`, `_restore()` in `finally` can't pop it, user's uncommitted work silently lost |
| A2 | minor | `scripts/adversarial_spec.py:260` | Verifier REJECT-with-all-settled causes non-converging re-litigation — `remaining` is empty so `findings` stays at full list, next round revises already-settled findings → exhausts `max_loops` → REJECT |
| A3 | minor | `scripts/adversarial_spec.py:100` | `final.json` contract broken on infra failures — `_phase_failed` returns `EXIT_INFRA` without calling `write_final_json`, caller polls stale or absent artifact |
| A4 | minor | `scripts/adversarial_spec.py:355` | Artifacts dir keyed only by feature name — reruns silently overwrite `final.json`, `ISSUES.md`, etc. across runs of the same feature |
| A5 | minor | `scripts/adversarial_spec.py:165` | APPROVED exit code (0) masks failed squash-merge — `_finish` catches `GitError`, sets `merged=false` in JSON, but still returns 0 |
| A6 | minor | `scripts/adversarial_spec.py:385` | Stash-pop after squash-merge can conflict when user's dirty tree touched the same files — no conflict detection or specific warning |
| A7 | nit | `_retrospective/ISSUES.md:3` | Stale claim that failures are auto-appended to this file — actually goes to `<out_dir>/ISSUES.md` now |
| A8 | nit | `scripts/adversarial_spec.py:78` | `_ensure_ids` dedup generates awkward compound ids like `S1-3-3` |
| A9 | nit | `scripts/phases/phase_verify.py:55` | Prompt hard-codes `git diff HEAD~1..HEAD` — wrong if writer CLI made multiple commits |
| A10 | nit | `scripts/phases/phase_challenge.py:44` | Full spec embedded in prompt with no size guard — large specs can overflow context window |

## Inspector Findings (Codex GPT-5.6-Sol)

| ID | Severity | File | Summary |
|----|----------|------|---------|
| B1 | **blocker** | `scripts/adversarial_spec.py:185` | Git finalization failure still returns exit 0 — CI receives success despite `merged=false` |
| B2 | **major** | `scripts/adversarial_spec.py:127` | Stash restoration failure doesn't change exit result — `_restore` only warns, `main` returns EXIT_APPROVED while user's changes remain stashed |
| B3 | **major** | `scripts/adversarial_spec.py:146` | Partial setup failure loses recovery state — stash done before branch/checkout/record, if later ops fail, `parent_branch` and `stash_id` never set in state |
| B4 | **major** | `scripts/phases/__init__.py:108` | Spec validation enforces only the `name` field — accepts empty specs missing version, author, targets, requirements |
| B5 | **major** | `scripts/phases/phase_write.py:59` | Writer can commit arbitrary repo changes — `commit_all` stages everything, not just `spec.md`; `phase_revise.py:51` repeats the same |
| B6 | **major** | `scripts/adversarial_spec.py:269` | Later revisions can regress already-settled findings — unresolved-only narrowing means round 2 never re-checks earlier fixes |
| B7 | **major** | `scripts/phases/phase_verify.py:28` | Contradictory duplicate verification results accepted — `{id:S1,status:resolved}` AND `{id:S1,status:disputed}` both pass, APPROVE can win incorrectly |
| B8 | **major** | `scripts/adversarial_spec.py:150` | Custom artifact dirs can be committed and merged — only literal `.adversarial-spec/` in `.gitignore`, relative `--out` paths get swept by `commit_all` |
| B9 | **major** | `scripts/adversarial_spec.py:105` | Infrastructure failures leave stale final verdicts — earlier `final.json` from a previous successful run persists when a later run fails mid-pipeline |
| B10 | minor | `scripts/adversarial_spec.py:368` | Output-dir errors escape error contract — `out_dir.mkdir` before guarded block, permission errors raise uncaught traceback |

## Privacy Pre-Scan Results

All scans passed clean:
- **Hardcoded home paths:** None found
- **Emails/phones:** None found
- **Credentials/secrets/tokens:** None found
- **Path.home()/expanduser:** None found

## Git Repository State

The adversarial-spec directory has its own `.git` (not a submodule of Hermes skills). The review did NOT push or modify origin — findings are advisory for the next maintainer.

## Key Action Items

1. **Fix stash loss (A1, B2, B3):** Retain `stash_id` in error paths; `_restore` or auto-pop on setup failure.
2. **Fix exit code masking (A5, B1):** Return `EXIT_INFRA` (or distinct code) on failed squash-merge.
3. **Write final.json on infra failures (A3, B9):** Write `{"verdict":"ERROR","error":...}` before early return.
4. **Tighten spec validation (B4):** Require `version`, `author`, `targets`, `requirements`, `acceptance_criteria`.
5. **Scope commit_all (B5):** Only commit `spec.md`, not arbitrary working-tree changes.
6. **Fix convergence loop (A2):** Treat "all settled + REJECT" as terminal disagreement, not loop-retry.
7. **Deduplicate results (B7):** Reject duplicate IDs in verification output.
8. **Prevent artifact leakage (B8):** `.gitignore` any `--out` path, not just the default.
9. **Version artifact directories (A4):** Mirror branch numbering `out_base/<feature>/<N>`.
10. **Fix ISSUES.md header (A7):** Update stale claim about auto-append location.
11. **Make verify prompt exact (A9):** Pass actual revise commit SHA instead of hardcoded `HEAD~1`.
