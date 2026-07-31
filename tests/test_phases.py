"""Unit tests for the adversarial-spec phase modules and shared helpers."""
import json
from pathlib import Path

import pytest

from conftest import VALID_SPEC, last_commit_message
from scripts import phases
from scripts.phases import phase_challenge, phase_revise, phase_verify, phase_write


def fake_run(stdout="", stderr="", code=0, side_effect=None, calls=None):
    """Build an injectable `run(cmd, prompt, role, timeout, cwd, phase=...)` stub."""
    def _run(cmd, prompt, role, timeout, cwd, phase=None, **kwargs):
        if calls is not None:
            calls.append({"cmd": cmd, "prompt": prompt, "role": role,
                          "timeout": timeout, "cwd": cwd, "phase": phase})
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
    assert result["exit_code"] == 2
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


def test_challenge_reads_spec_from_disk_without_embedding_it(tmp_path):
    spec_path = tmp_path / "spec.md"
    sentinel = "UNIQUE_SPEC_SENTINEL_CONTENT"
    long_spec = "UNIQUE_LONG_SPEC_CONTENT" * 1_000
    calls = []
    branch_point = "0123456789abcdef"

    spec_path.write_text(sentinel)
    phase_challenge.run_challenge(
        "rev", str(tmp_path), 60,
        run=fake_run(stdout=CHALLENGE_OK, calls=calls),
        branch_point=branch_point)

    spec_path.write_text(long_spec)
    phase_challenge.run_challenge(
        "rev", str(tmp_path), 60,
        run=fake_run(stdout="not json", calls=calls),
        branch_point=branch_point)

    base_prompt = phase_challenge._build_prompt(branch_point)
    assert calls[0]["prompt"] == base_prompt
    assert calls[1]["prompt"] == base_prompt
    assert calls[2]["prompt"].startswith(base_prompt)
    for call in calls:
        assert sentinel not in call["prompt"]
        assert long_spec not in call["prompt"]
        assert "--- spec.md ---" not in call["prompt"]
        assert "--- current spec.md ---" not in call["prompt"]
        assert call["cwd"] == str(tmp_path)
        assert branch_point in call["prompt"]
        assert "HEAD~1" not in call["prompt"]
        assert call["role"] == "spec-challenger"


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


def test_verify_prompt_uses_branch_point(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    calls = []
    branch_point = "fedcba9876543210"
    phase_verify.run_verify(
        [{"id": "S1"}], "rev", str(tmp_path), 60,
        run=fake_run(stdout=VERIFY_OK, calls=calls),
        branch_point=branch_point)
    assert branch_point in calls[0]["prompt"]
    assert "HEAD~1" not in calls[0]["prompt"]


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


def test_validate_spec_requires_all_frontmatter_fields():
    invalid = VALID_SPEC.replace('version: "1.0"\n', "")
    ok, error = phases.validate_spec_text(invalid)
    assert not ok
    assert "version" in error


def test_validate_spec_requires_requirement_criterion_coverage():
    invalid = VALID_SPEC.replace("(R1)", "(R2)")
    ok, error = phases.validate_spec_text(invalid)
    assert not ok
    assert "unknown requirements" in error


def test_empty_spec_validator_exits_usage(tmp_path):
    from scripts.phases import phase_spec

    path = tmp_path / "spec.md"
    path.write_text("")
    assert phase_spec.main([str(path)]) == 2


# --- regression: A1/A3/A4 ----------------------------------------------------------

def _result_with_metadata(stdout="x", stderr="", code=0, metadata=None):
    """Build a RunResult tuple carrying execution metadata."""
    from adversarial_common.runner import RunResult
    return RunResult((stdout, stderr, code), metadata or {})


def _provider_decision(phase, alias):
    return {
        "phase": phase,
        "alias": alias,
        "quota_state": "available" if alias else "unknown",
        "fallback": False,
        "forced": False,
        "reason": "selected" if alias else "no provider available",
        "raw_snapshot": {},
    }


def test_write_validation_failure_carries_execution(git_repo):
    # A1: the billed spec-writer call ran, then spec validation failed
    # (exit_code 2). Its execution/runtime evidence must still be attached.
    run = fake_run(stdout="done")
    result = phase_write.run_write("brief", "dev", str(git_repo), 60, "f", run=run)
    assert result["exit_code"] == 2
    assert "execution" in result  # was silently dropped before A1


def test_challenge_retry_accumulates_attempt_metadata(tmp_path):
    # A3: the bad-JSON retry issues two billed calls; both attempts' runtime
    # evidence must survive (was overwritten by the retry before A3).
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    calls = []

    def _run(cmd, prompt, role, timeout, cwd, phase=None, **kwargs):
        calls.append(prompt)
        out = "garbage" if len(calls) == 1 else CHALLENGE_OK
        return _result_with_metadata(
            out, "", 0, {"attempts": [{"attempt": len(calls)}], "cap_events": []})

    result = phase_challenge.run_challenge("rev", str(tmp_path), 60, run=_run)
    assert result["exit_code"] == 0
    assert len(calls) == 2  # exactly one retry
    assert [a["attempt"] for a in result["execution"]["attempts"]] == [1, 2]


def test_verify_retry_accumulates_attempt_metadata(tmp_path):
    # A3: same accumulation guarantee for the verify phase.
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    calls = []

    def _run(cmd, prompt, role, timeout, cwd, phase=None, **kwargs):
        calls.append(prompt)
        out = "garbage" if len(calls) == 1 else VERIFY_OK
        return _result_with_metadata(
            out, "", 0, {"attempts": [{"attempt": len(calls)}]})

    result = phase_verify.run_verify([{"id": "S1"}], "rev", str(tmp_path), 60,
                                     run=_run)
    assert result["exit_code"] == 0
    assert [a["attempt"] for a in result["execution"]["attempts"]] == [1, 2]


@pytest.mark.parametrize(
    "phase_module,invoke,phase_name",
    [
        (
            phase_challenge,
            lambda path: phase_challenge.run_challenge(
                "rev", str(path), 60
            ),
            "challenge",
        ),
        (
            phase_verify,
            lambda path: phase_verify.run_verify(
                [{"id": "S1"}], "rev", str(path), 60, round_n=1
            ),
            "verify_1",
        ),
    ],
)
def test_json_retry_provider_exhaustion_carries_all_provider_decisions(
        tmp_path, monkeypatch, phase_module, invoke, phase_name):
    from adversarial_common import NoProviderAvailable

    (tmp_path / "spec.md").write_text(VALID_SPEC)
    first = _result_with_metadata(
        "invalid JSON", metadata={
            "provider_decision": _provider_decision(phase_name, "primary"),
        }
    )
    second = _result_with_metadata(
        "", "no provider", 3, metadata={
            "provider_decision": _provider_decision(phase_name, None),
            "raw_snapshots": {"primary": {"used_pct": 100}},
            "rejection_reasons": {"primary": "quota exhausted"},
        }
    )
    results = iter((first, second))
    monkeypatch.setattr(
        phase_module, "run_phase_cmd", lambda **_kwargs: next(results)
    )

    with pytest.raises(NoProviderAvailable) as raised:
        invoke(tmp_path)

    assert [
        decision["alias"] for decision in raised.value.provider_history
    ] == ["primary", None]


def test_write_tags_cost_phase_as_write(git_repo):
    # A4: the cost-ledger phase bucket tracks the pipeline stage, not persona.
    calls = []
    run = fake_run(side_effect=_write_spec, calls=calls)
    phase_write.run_write("brief", "dev", str(git_repo), 60, "f", run=run)
    assert calls[0]["phase"] == "write"
    assert calls[0]["role"] == "spec-writer"


def test_revise_tags_cost_phase_with_round(git_repo):
    (git_repo / "spec.md").write_text(VALID_SPEC)
    calls = []
    phase_revise.run_revise([{"id": "S1"}], "dev", str(git_repo), 60, "f", 2,
                            run=fake_run(side_effect=_write_spec, calls=calls))
    assert calls[0]["phase"] == "revise_2"


def test_verify_tags_cost_phase_with_round(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    calls = []
    phase_verify.run_verify([{"id": "S1"}], "rev", str(tmp_path), 60,
                            run=fake_run(stdout=VERIFY_OK, calls=calls),
                            round_n=3)
    assert calls[0]["phase"] == "verify_3"


def test_verify_phase_defaults_without_round(tmp_path):
    (tmp_path / "spec.md").write_text(VALID_SPEC)
    calls = []
    phase_verify.run_verify([{"id": "S1"}], "rev", str(tmp_path), 60,
                            run=fake_run(stdout=VERIFY_OK, calls=calls))
    assert calls[0]["phase"] == "verify"


# --- R2: required-section context refusal via check_context --------------------

def test_check_context_refuses_missing_required_section():
    from adversarial_common import gates
    # This text has headings that don't contain "Requirements" at all.
    brief = "## Background\n\nSome context here.\n\n## Summary\n\nAll done."
    result = gates.check_context(
        "brief", brief,
        thresholds={"required_sections": ["Requirements"]})
    assert result["ok"] is False
    assert "Requirements" in result["reason"]


def test_check_context_passes_with_required_section():
    from adversarial_common import gates
    brief = "## Problem\n\nSomething needs solving.\n\n## Requirements\n- R1"
    result = gates.check_context(
        "brief", brief,
        thresholds={"required_sections": ["Requirements"]})
    assert result["ok"] is True


def test_check_context_refuses_empty_input():
    from adversarial_common import gates
    result = gates.check_context("brief", "   ")
    assert result["ok"] is False
    assert result["reason"] == "empty_input"


# --- R8: epistemic normalization on challenge findings -------------------------

def test_normalize_findings_defaults_missing_confidence_basis():
    from adversarial_common import jsonio
    findings = [{"id": "F1", "summary": "needs more detail"}]
    payload = {"findings": findings}
    warnings = []
    jsonio.normalize_findings(payload, warnings=warnings)
    assert findings[0]["confidence"] == "low"
    assert findings[0]["basis"] == "inference"
    assert any(w["code"] == "epistemic_label_defaulted" for w in warnings)


def test_normalize_findings_preserves_valid_labels():
    from adversarial_common import jsonio
    findings = [{"id": "F1", "confidence": "high", "basis": "spec",
                  "summary": "ok"}]
    payload = {"findings": findings}
    warnings = []
    jsonio.normalize_findings(payload, warnings=warnings)
    assert findings[0]["confidence"] == "high"
    assert findings[0]["basis"] == "spec"
    assert len(warnings) == 0


def test_epistemic_distribution_counts_labels():
    from adversarial_common import jsonio
    findings = [
        {"confidence": "high", "basis": "spec"},
        {"confidence": "low", "basis": "inference"},
        {"confidence": "high", "basis": "code"},
    ]
    dist = jsonio.epistemic_distribution(findings)
    assert dist["confidence"]["high"] == 2
    assert dist["confidence"]["low"] == 1
    assert dist["basis"]["spec"] == 1
    assert dist["basis"]["code"] == 1
    assert dist["combined"]["high/spec"] == 1


# --- R4: complexity estimate integration ---------------------------------------

def test_estimate_complexity_levels():
    from adversarial_common import gates
    trivial = gates.estimate_complexity("x" * 50)
    assert trivial["level"] == "trivial"
    assert trivial["recommended_agents"] == 1
    low = gates.estimate_complexity("x" * 2000 + "\nR1: something\n" * 3)
    assert low["level"] == "low"
    assert low["recommended_agents"] == 2
    medium = gates.estimate_complexity("x" * 6000 + "\nR1: requirement\n" * 10)
    assert medium["level"] == "medium"
    assert medium["recommended_agents"] == 4
    high = gates.estimate_complexity("x" * 10000 + "\nR1: req\n" * 50)
    assert high["level"] == "high"
    assert high["recommended_agents"] == 6
