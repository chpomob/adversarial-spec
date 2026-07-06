"""Unit tests for the adversarial-spec phase modules and shared helpers."""
import json
from pathlib import Path

import pytest

from conftest import VALID_SPEC, last_commit_message
from scripts import phases
from scripts.phases import phase_challenge, phase_revise, phase_verify, phase_write


def fake_run(stdout="", stderr="", code=0, side_effect=None, calls=None):
    """Build an injectable `run(cmd, prompt, role, timeout, cwd)` stub."""
    def _run(cmd, prompt, role, timeout, cwd):
        if calls is not None:
            calls.append({"cmd": cmd, "prompt": prompt, "role": role,
                          "timeout": timeout, "cwd": cwd})
        if side_effect:
            side_effect(cwd)
        return stdout, stderr, code
    return _run


# --- try_parse_json (3 strategies) ---------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('The verdict follows: {"a": 1} — done.', {"a": 1}),
    ('noise [1, 2, 3] noise', [1, 2, 3]),
])
def test_try_parse_json_strategies(text, expected):
    assert phases.try_parse_json(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "no json here", None, "{broken"])
def test_try_parse_json_rejects_garbage(text):
    assert phases.try_parse_json(text) is None


# --- validate_spec_file -----------------------------------------------------------

def test_validate_spec_ok(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    ok, err = phases.validate_spec_file(tmp_path)
    assert ok, err


def test_validate_spec_missing_file(tmp_path):
    ok, err = phases.validate_spec_file(tmp_path)
    assert not ok and "not found" in err


def test_validate_spec_empty_file(tmp_path):
    (tmp_path / "spec.md").write_text("   \n")
    ok, err = phases.validate_spec_file(tmp_path)
    assert not ok and "empty" in err


def test_validate_spec_no_frontmatter(tmp_path):
    (tmp_path / "spec.md").write_text("# Just a title\n\nBody.\n")
    ok, err = phases.validate_spec_file(tmp_path)
    assert not ok and "frontmatter" in err


def test_validate_spec_invalid_yaml(tmp_path):
    pytest.importorskip("yaml")  # the no-PyYAML fallback is deliberately lenient
    (tmp_path / "spec.md").write_text("---\nname: [unclosed\n---\n\n# T\n")
    ok, err = phases.validate_spec_file(tmp_path)
    assert not ok


def test_validate_spec_missing_name(tmp_path):
    (tmp_path / "spec.md").write_text("---\nversion: '1.0'\n---\n\n# T\n")
    ok, err = phases.validate_spec_file(tmp_path)
    assert not ok and "name" in err


def test_extract_frontmatter_requires_leading_marker():
    assert phases.extract_frontmatter("intro\n---\nname: x\n---\n") is None
    assert phases.extract_frontmatter("---\nname: x\n---\n") == "name: x"


# --- phase_write -------------------------------------------------------------------

def _write_spec(cwd):
    (Path(cwd) / "spec.md").write_text(VALID_SPEC)


def test_write_success_commits(git_repo):
    run = fake_run(stdout="wrote spec.md", side_effect=_write_spec)
    result = phase_write.run_write("# My brief\n\ndetails", "dev", str(git_repo),
                                   60, "my-feat", run=run)
    assert result["exit_code"] == 0
    assert result["commit_sha"]
    assert last_commit_message(git_repo) == "write: my-feat — My brief"


def test_write_dev_failure(git_repo):
    run = fake_run(stderr="boom", code=1)
    result = phase_write.run_write("brief", "dev", str(git_repo), 60, "f", run=run)
    assert result["exit_code"] == 1
    assert "WRITE exited 1" in result["error"]


def test_write_missing_spec_fails_validation(git_repo):
    run = fake_run(stdout="did nothing")
    result = phase_write.run_write("brief", "dev", str(git_repo), 60, "f", run=run)
    assert result["exit_code"] == 1
    assert "spec validation failed" in result["error"]


def test_write_uses_spec_writer_persona(git_repo):
    calls = []
    run = fake_run(side_effect=_write_spec, calls=calls)
    phase_write.run_write("brief", "dev", str(git_repo), 60, "f", run=run)
    assert calls[0]["role"] == "spec-writer"
    assert "brief" in calls[0]["prompt"]


# --- phase_challenge -----------------------------------------------------------------

CHALLENGE_OK = json.dumps({
    "findings": [{"id": "S1", "severity": "major", "section": "Requirements",
                  "summary": "R1 has no criterion", "evidence": "R1"}],
    "verdict": "REQUEST_CHANGES",
    "summary": "1 major",
})


def test_challenge_parses_findings(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    result = phase_challenge.run_challenge("rev", str(tmp_path), 60,
                                           run=fake_run(stdout=CHALLENGE_OK))
    assert result["exit_code"] == 0
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["findings"][0]["id"] == "S1"


def test_challenge_missing_spec(tmp_path):
    result = phase_challenge.run_challenge("rev", str(tmp_path), 60,
                                           run=fake_run(stdout=CHALLENGE_OK))
    assert result["exit_code"] == 1
    assert "spec.md" in result["error"]


def test_challenge_retries_then_fails_on_bad_json(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    calls = []
    result = phase_challenge.run_challenge(
        "rev", str(tmp_path), 60, run=fake_run(stdout="not json", calls=calls))
    assert result["exit_code"] == 1
    assert result["error"] == "invalid JSON after retry"
    assert len(calls) == 2  # exactly one retry


def test_challenge_rejects_bad_severity(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    bad = json.dumps({"findings": [{"id": "S1", "severity": "catastrophic",
                                    "section": "Problem", "summary": "x",
                                    "evidence": "y"}],
                      "verdict": "REJECT"})
    result = phase_challenge.run_challenge("rev", str(tmp_path), 60,
                                           run=fake_run(stdout=bad))
    assert result["exit_code"] == 1


def test_challenge_spec_embedded_in_prompt(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    calls = []
    phase_challenge.run_challenge("rev", str(tmp_path), 60,
                                  run=fake_run(stdout=CHALLENGE_OK, calls=calls))
    assert "demo-feature" in calls[0]["prompt"]
    assert calls[0]["role"] == "spec-challenger"


# --- phase_revise ----------------------------------------------------------------------

def test_revise_success_commits_round(git_repo):
    (git_repo / "spec.md").write_text(VALID_SPEC)
    findings = [{"id": "S1", "summary": "fix me"}]
    run = fake_run(side_effect=_write_spec)
    result = phase_revise.run_revise(findings, "dev", str(git_repo), 60,
                                     "my-feat", 2, run=run)
    assert result["exit_code"] == 0
    assert last_commit_message(git_repo) == "revise: my-feat — round 2"


def test_revise_findings_in_prompt(git_repo):
    (git_repo / "spec.md").write_text(VALID_SPEC)
    calls = []
    phase_revise.run_revise([{"id": "S9", "summary": "oops"}], "dev",
                            str(git_repo), 60, "f", 1,
                            run=fake_run(side_effect=_write_spec, calls=calls))
    assert "S9" in calls[0]["prompt"]
    assert calls[0]["role"] == "spec-writer"


def test_revise_broken_spec_fails_validation(git_repo):
    def _break_spec(cwd):
        (Path(cwd) / "spec.md").write_text("no frontmatter anymore")
    result = phase_revise.run_revise([], "dev", str(git_repo), 60, "f", 1,
                                     run=fake_run(side_effect=_break_spec))
    assert result["exit_code"] == 1
    assert "spec validation failed" in result["error"]


# --- phase_verify ----------------------------------------------------------------------

VERIFY_OK = json.dumps({
    "results": [{"id": "S1", "status": "resolved", "note": "criterion added"}],
    "verdict": "APPROVE",
})


def test_verify_parses_results(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    result = phase_verify.run_verify([{"id": "S1"}], "rev", str(tmp_path), 60,
                                     run=fake_run(stdout=VERIFY_OK))
    assert result["exit_code"] == 0
    assert result["verdict"] == "APPROVE"
    assert result["results"][0]["status"] == "resolved"


def test_verify_invalid_status_rejected(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    bad = json.dumps({"results": [{"id": "S1", "status": "maybe"}],
                      "verdict": "APPROVE"})
    result = phase_verify.run_verify([{"id": "S1"}], "rev", str(tmp_path), 60,
                                     run=fake_run(stdout=bad))
    assert result["exit_code"] == 1


def test_verify_fenced_json_accepted(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    fenced = f"```json\n{VERIFY_OK}\n```"
    result = phase_verify.run_verify([{"id": "S1"}], "rev", str(tmp_path), 60,
                                     run=fake_run(stdout=fenced))
    assert result["exit_code"] == 0


def test_verify_cli_failure(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    result = phase_verify.run_verify([], "rev", str(tmp_path), 60,
                                     run=fake_run(stderr="timeout", code=124))
    assert result["exit_code"] == 1
    assert "VERIFY exited 124" in result["error"]


# --- persona resolution -------------------------------------------------------------------

def test_resolve_persona_falls_back_to_base_for_pi():
    # 'pi' maps to 'spec-writer-pi', which does not exist -> base persona.
    path = phases.resolve_persona("spec-writer", "pi --provider zai --model glm-5.2")
    assert path is not None and path.endswith("spec-writer.md")


def test_resolve_persona_unknown_role_is_none():
    assert phases.resolve_persona("no-such-role", "somecli") is None
