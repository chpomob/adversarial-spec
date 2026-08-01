"""P14 integration: brief preflight (R1/R3), caps/costs (R4/R5), complexity (R5)."""
import json
import subprocess
import textwrap
from pathlib import Path

import adversarial_common.runner as runner_mod
from conftest import VALID_SPEC
from scripts import adversarial_spec as orch


# --- shared scripted CLIs -------------------------------------------------------

WRITER_SCRIPT = textwrap.dedent("""\
    import pathlib, sys
    sys.stdin.read()  # consume persona + prompt
    pathlib.Path("spec.md").write_text('''{spec}''')
    print("spec.md written")
""")

APPROVE_REVIEWER = textwrap.dedent("""\
    import json, sys
    sys.stdin.read()
    print(json.dumps({"findings": [], "verdict": "APPROVE", "summary": "clean"}))
""")


def _write_scripts(tmp_path):
    writer = tmp_path / "writer.py"
    writer.write_text(WRITER_SCRIPT.format(spec=VALID_SPEC))
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(APPROVE_REVIEWER)
    return writer, reviewer


def _run_pipeline(tmp_path, brief_text, extra_args=()):
    workdir = tmp_path / "project"
    workdir.mkdir()
    writer, reviewer = _write_scripts(tmp_path)
    brief = tmp_path / "brief.md"
    brief.write_text(brief_text)
    argv = [
        "--brief", str(brief),
        "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1", "--timeout", "60",
        *extra_args,
    ]
    code = orch.main(argv)
    final_path = workdir / ".adversarial-spec" / "brief" / "final.json"
    return code, workdir, final_path


# --- R1/R3: brief preflight with zero run_cli calls ----------------------------

def test_preflight_blocks_short_brief_writes_final_and_never_calls_provider(
        tmp_path, monkeypatch):
    calls = []

    def _trap_run_cli(*_args, **_kwargs):
        calls.append(_kwargs.get("phase"))
        return ("", "", 0)

    monkeypatch.setattr(runner_mod, "run_cli", _trap_run_cli)

    workdir = tmp_path / "project"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("too short")  # 9 chars < min_chars(40) and < min_tokens(10)

    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir),
        "--dev-cmd", "python3 -c pass",
        "--review-cmd", "python3 -c pass",
    ])

    assert code == orch.EXIT_CONTEXT_BLOCKED
    assert calls == []  # R1/R3 guarantee: zero run_cli calls on a blocked brief

    final = json.loads(
        (workdir / ".adversarial-spec" / "brief" / "final.json").read_text())
    assert final["verdict"] == "CONTEXT_BLOCKED"
    assert final["context_blocked"] is True
    assert final["context"]["ok"] is False
    # final.json still carries cost/complexity structure even on a block.
    assert "costs" in final and "total" in final["costs"]
    assert final["costs"]["total"]["est_cost_usd"] == 0
    assert final["complexity"]["level"]


def test_preflight_accepts_substantive_brief(tmp_path):
    """A real-sized brief passes the gate and reaches the git/provider stage."""
    code, _workdir, _final = _run_pipeline(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n")
    assert code == orch.EXIT_APPROVED


# --- R4: costs recorded --------------------------------------------------------

def test_costs_recorded_in_final_json(tmp_path):
    code, workdir, final_path = _run_pipeline(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n")
    assert code == orch.EXIT_APPROVED
    final = json.loads(final_path.read_text())
    # The shared CostLedger summary structure must be present and reconciled.
    assert "costs" in final
    assert "total" in final["costs"]
    assert {"models", "phases", "personas"} <= set(final["costs"])
    # The two scripted python phases were attributed by phase/persona.
    phases = final["costs"]["phases"]
    # A4: the cost-ledger phase bucket now tracks the pipeline stage
    # (write/challenge/revise_N/verify_N), not the persona, so phases and
    # personas are no longer redundant.
    assert "write" in phases or "challenge" in phases
    assert phases and phases.keys() != final["costs"]["personas"].keys()
    # Per-phase attempts/calls are aggregated for auditability.
    assert isinstance(final["calls"], list) and final["calls"]
    assert isinstance(final["attempts"], list)
    assert isinstance(final["cap_events"], list)
    assert "execution" in final and final["execution"]["max_retries"] == 3


def test_show_costs_prints_summary(tmp_path, capsys):
    code, _workdir, _final = _run_pipeline(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n",
        extra_args=("--show-costs",))
    assert code == orch.EXIT_APPROVED
    err = capsys.readouterr().err
    assert "Estimated model costs" in err


# --- R5: caps recorded ---------------------------------------------------------

def test_input_cap_recorded_with_truncate(tmp_path):
    # A long brief forced through a small input cap (with --truncate-input)
    # records a preflight cap event and still completes. The cap must cover
    # the persona framing (persona-inclusive budget), so the limit is sized
    # above the persona and the BRIEF is what gets truncated.
    long_brief = ("# Demo feature\n\n" + "Users need a demo. " * 600)
    code, workdir, final_path = _run_pipeline(
        tmp_path, long_brief,
        extra_args=("--max-input-chars", "6000", "--truncate-input"))
    assert code == orch.EXIT_APPROVED
    final = json.loads(final_path.read_text())
    cap_events = final["cap_events"]
    assert cap_events, "expected at least one recorded input cap event"
    preflight_caps = [e for e in cap_events if e.get("phase") == "preflight"]
    assert preflight_caps
    assert preflight_caps[0]["kind"] == "input"
    assert preflight_caps[0]["truncated"] is True
    assert final["execution"]["max_input_chars"] == 6000


def test_default_caps_reject_oversized_brief_without_truncate(tmp_path):
    # Without --truncate-input, an oversized brief blocks at preflight rather
    # than silently feeding truncated context to the provider.
    long_brief = ("# Demo feature\n\n" + "Users need a demo. " * 200)
    workdir = tmp_path / "project"
    workdir.mkdir()
    writer, reviewer = _write_scripts(tmp_path)
    brief = tmp_path / "brief.md"
    brief.write_text(long_brief)
    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-input-chars", "64",
        "--max-loops", "1", "--timeout", "60",
    ])
    assert code == orch.EXIT_CONTEXT_BLOCKED
    final = json.loads(
        (workdir / ".adversarial-spec" / "brief" / "final.json").read_text())
    assert final["context"]["reason"] == "input_exceeds_max_chars"


# --- R5: complexity scaling ----------------------------------------------------

def test_complexity_recorded_in_final_json(tmp_path):
    code, _workdir, final_path = _run_pipeline(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n")
    assert code == orch.EXIT_APPROVED
    final = json.loads(final_path.read_text())
    assert final["complexity"]["level"] in ("trivial", "low", "medium", "high")
    assert final["complexity"]["recommended_agents"] >= 1


def test_complexity_scales_with_brief_size():
    """The shared preflight complexity estimate grows with brief substance."""
    args = orch.build_parser().parse_args([])
    small = "Add a login form so users can authenticate into the application today."
    small_result = orch.pipeline_base.preflight(
        args, small, "/tmp", policy=orch._spec_preflight_policy(args),
    )
    big = ("# Big feature\n\n## Requirements\n" +
           "".join(f"- R{n}: requirement number {n} for the system.\n"
                   for n in range(1, 400)))
    big_result = orch.pipeline_base.preflight(
        args, big, "/tmp", policy=orch._spec_preflight_policy(args),
    )

    assert small_result.ok and big_result.ok
    assert small_result.complexity["score"] < big_result.complexity["score"]
    assert (small_result.complexity["recommended_agents"]
            <= big_result.complexity["recommended_agents"])
    tiers = ("trivial", "low", "medium", "high")
    assert (tiers.index(small_result.complexity["level"])
            <= tiers.index(big_result.complexity["level"]))


def test_restored_stash_state_is_flushed_to_state_json(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=workdir, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=workdir, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workdir, check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Initial commit", "-q"],
        cwd=workdir, check=True,
    )
    (workdir / "wip.txt").write_text("uncommitted work", encoding="utf-8")
    writer, reviewer = _write_scripts(tmp_path)
    brief = tmp_path / "brief.md"
    brief.write_text(
        "# Demo feature\n\nUsers need restored stash state persistence.\n",
        encoding="utf-8",
    )

    code = orch.main([
        "--brief", str(brief),
        "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1",
        "--timeout", "60",
    ])

    assert code == orch.EXIT_APPROVED
    assert (workdir / "wip.txt").read_text(encoding="utf-8") == \
        "uncommitted work"
    state = json.loads((
        workdir / ".adversarial-spec" / "brief" / "state.json"
    ).read_text(encoding="utf-8"))
    assert state["stash_id"] == ""


def test_setup_git_partial_failure_leaves_recoverable_state(tmp_path, monkeypatch):
    """A2: a failure between stashing and branch creation must not orphan the
    stash. pipeline_base.setup_git records the stash id onto the *shared*
    state dict the instant `git stash push` succeeds (before branch creation
    even starts), so a later failure in that same setup_git call still leaves
    restore_git able to pop the stash back onto the parent branch.
    """
    import adversarial_common.gitops as gitops_mod

    workdir = tmp_path / "project"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=workdir, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=workdir, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workdir, check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Initial commit", "-q"],
        cwd=workdir, check=True,
    )
    (workdir / "wip.txt").write_text("uncommitted work", encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated branch-creation failure")

    # Stashing (adapter.stash_dirty) runs and succeeds; the very next git
    # operation in setup_git — branch creation — raises. This forces the
    # exact partial-failure window between "stash pushed" and "branch made".
    monkeypatch.setattr(gitops_mod, "create_loop_branch", _boom)

    writer, reviewer = _write_scripts(tmp_path)
    brief = tmp_path / "brief.md"
    brief.write_text(
        "# Demo feature\n\nUsers need partial setup-git failure coverage.\n",
        encoding="utf-8",
    )

    code = orch.main([
        "--brief", str(brief),
        "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1",
        "--timeout", "60",
    ])

    assert code == orch.EXIT_INFRA
    # The dirty file must be back: restore_git recovered the stash even
    # though setup_git failed after stashing but before the branch existed.
    assert (workdir / "wip.txt").read_text(encoding="utf-8") == \
        "uncommitted work"
    # No dangling stash entry left behind either.
    assert subprocess.run(
        ["git", "stash", "list"], cwd=workdir,
        capture_output=True, text=True, check=True,
    ).stdout.strip() == ""


# --- R8: finding normalization -------------------------------------------------

def test_challenge_findings_get_epistemic_labels(tmp_path):
    """Challenger findings without confidence/basis are normalized (R8)."""
    workdir = tmp_path / "project"
    workdir.mkdir()
    writer = tmp_path / "writer.py"
    writer.write_text(WRITER_SCRIPT.format(spec=VALID_SPEC))
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(textwrap.dedent("""\
        import json, sys
        sys.stdin.read()
        print(json.dumps({
            "findings": [{"id": "S1", "severity": "major",
                          "section": "Requirements",
                          "summary": "R1 untestable", "evidence": "R1"}],
            "verdict": "REQUEST_CHANGES", "summary": "1 major"}))
    """))
    # A verifier that never resolves S1 so the run REJECTs but keeps findings.
    verifier_reject = textwrap.dedent("""\
        import json, sys
        sys.stdin.read()
        print(json.dumps({"results": [{"id": "S1", "status": "disputed"}],
                          "verdict": "REJECT"}))
    """)
    # The reviewer script doubles as verifier (same "For each finding" branch).
    reviewer.write_text(textwrap.dedent("""\
        import json, sys
        prompt = sys.stdin.read()
        if "For each finding" in prompt:
            print(json.dumps({"results": [{"id": "S1", "status": "disputed"}],
                              "verdict": "REJECT"}))
        else:
            print(json.dumps({
                "findings": [{"id": "S1", "severity": "major",
                              "section": "Requirements",
                              "summary": "R1 untestable", "evidence": "R1"}],
                "verdict": "REQUEST_CHANGES", "summary": "1 major"}))
    """))
    brief = tmp_path / "brief.md"
    brief.write_text("# Demo feature\n\nUsers need a demo command for the app.\n")
    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1", "--timeout", "60",
    ])
    assert code == orch.EXIT_REJECTED
    final = json.loads(
        (workdir / ".adversarial-spec" / "brief" / "final.json").read_text())
    finding = final["findings"][0]
    # R8: missing labels normalized to low/inference, not dropped.
    assert finding["confidence"] == "low"
    assert finding["basis"] == "inference"
    assert final["epistemic_labels"]["confidence"]["low"] >= 1
    assert final["epistemic_labels"]["basis"]["inference"] >= 1
    assert any(
        isinstance(w, dict) and w.get("code") == "epistemic_label_defaulted"
        for w in final["warnings"]
    )
