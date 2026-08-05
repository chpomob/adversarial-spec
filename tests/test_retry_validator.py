"""P11 — Retry-on-validation-failure: validator feedback loop in WRITE phase."""
import json
from pathlib import Path

import pytest

from conftest import VALID_SPEC, last_commit_message
from scripts.phases import phase_write


# --- helpers ----------------------------------------------------------------

def _write_valid(cwd):
    (Path(cwd) / "spec.md").write_text(VALID_SPEC)


def _write_empty(cwd):
    (Path(cwd) / "spec.md").write_text("")


def _write_no_frontmatter(cwd):
    (Path(cwd) / "spec.md").write_text("# Just a heading\n\nNo frontmatter.\n")


def _write_no_requirements(cwd):
    spec = VALID_SPEC.replace("## Requirements", "## Notes").replace("- R1:", "- N1:")
    (Path(cwd) / "spec.md").write_text(spec)


# --- AC5: valid spec on first try exits 0 with zero retries -----------------

def test_valid_spec_first_try_no_retries(git_repo):
    """AC5: Valid spec on first try → exit 0, zero retries."""
    calls = []
    result = phase_write.run_write(
        "brief", "dev", str(git_repo), 60, "f",
        run=_fake_run(stdout="ok", side_effect=_write_valid, calls=calls),
        max_spec_retries=3,
    )
    assert result["exit_code"] == 0
    assert result["commit_sha"]
    assert len(calls) == 1  # no retry
    assert last_commit_message(git_repo).startswith("write: f —")


# --- AC1: first attempt validation failure does NOT exit 2, log shows retry -

def test_spec_missing_requirements_retries_not_exit_2(git_repo):
    """AC1: spec missing ## Requirements → first attempt does NOT exit 2;
    log shows retry 1/3 with the validator error fed back."""
    calls = []
    seen_prompts = []

    def _run(cmd, prompt, role, timeout, cwd, phase=None, **kwargs):
        seen_prompts.append(prompt)
        if len(calls) == 0:
            calls.append("first")
            _write_no_requirements(cwd)
            return "first attempt", "", 0
        else:
            calls.append("retry")
            _write_valid(cwd)
            return "corrected", "", 0

    result = phase_write.run_write(
        "brief", "dev", str(git_repo), 60, "f",
        run=_run, max_spec_retries=3,
    )
    assert result["exit_code"] == 0
    assert len(calls) == 2  # first + 1 retry
    # The retry prompt must contain the validation error
    assert "Requirements" in seen_prompts[1] or "requirement" in seen_prompts[1].lower()


# --- AC2: valid spec on retry 2 → pipeline succeeds --------------------------

def test_valid_spec_on_retry_2_succeeds(git_repo):
    """AC2: Valid spec on retry 2 → pipeline succeeds."""
    calls = []

    def _run(cmd, prompt, role, timeout, cwd, phase=None, **kwargs):
        calls.append("call")
        n = len(calls)
        if n == 1:
            _write_empty(cwd)     # fails: empty
        elif n == 2:
            _write_no_frontmatter(cwd)  # fails: no frontmatter
        else:
            _write_valid(cwd)     # succeeds on attempt 3
        return "out", "", 0

    result = phase_write.run_write(
        "brief", "dev", str(git_repo), 60, "f",
        run=_run, max_spec_retries=3,
    )
    assert result["exit_code"] == 0
    assert result["commit_sha"]
    assert len(calls) == 3  # 2 failures + 1 success


# --- AC3: invalid spec after all retries → exits with final validation error -

def test_invalid_after_all_retries_exits_with_final_error(git_repo):
    """AC3: Invalid spec after all retries → exits with the final validation
    error (not code 2 on first failure)."""
    calls = []

    def _run(cmd, prompt, role, timeout, cwd, phase=None, **kwargs):
        calls.append("call")
        _write_empty(cwd)  # always writes empty
        return "out", "", 0

    result = phase_write.run_write(
        "brief", "dev", str(git_repo), 60, "f",
        run=_run, max_spec_retries=2,
    )
    assert result["exit_code"] == 2
    assert "after 3 attempts" in result["error"]
    assert len(calls) == 3  # 1 initial + 2 retries


# --- AC4: --max-retries 1 → exactly one retry before failing -----------------

def test_max_retries_1_exactly_one_retry(git_repo):
    """AC4: max_spec_retries=1 → exactly one retry before failing."""
    calls = []

    def _run(cmd, prompt, role, timeout, cwd, phase=None, **kwargs):
        calls.append("call")
        _write_empty(cwd)
        return "out", "", 0

    result = phase_write.run_write(
        "brief", "dev", str(git_repo), 60, "f",
        run=_run, max_spec_retries=1,
    )
    assert result["exit_code"] == 2
    assert "after 2 attempts" in result["error"]
    assert len(calls) == 2  # 1 initial + 1 retry


# --- AC6: existing spec tests still pass (tested by running the suite) -------


# --- helper ----------------------------------------------------------------

def _fake_run(stdout="", stderr="", code=0, side_effect=None, calls=None):
    """Build an injectable run stub matching phase_write's legacy hook."""
    def _run(cmd, prompt, role, timeout, cwd, phase=None, **kwargs):
        if calls is not None:
            calls.append({"cmd": cmd, "prompt": prompt, "role": role,
                          "timeout": timeout, "cwd": cwd, "phase": phase})
        if side_effect:
            side_effect(cwd)
        return stdout, stderr, code
    return _run
