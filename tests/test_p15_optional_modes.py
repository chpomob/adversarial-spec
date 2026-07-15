"""P15 optional modes: --html, --ci/--fail-on, --deep-research, --delegated."""
import json
import subprocess
import textwrap
from pathlib import Path

from conftest import VALID_SPEC
from scripts import adversarial_spec as orch


# --- scripted CLIs -------------------------------------------------------------

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

# Hostile reviewer: emits a finding full of HTML-breaking characters, then
# disputes the revision so the run REJECTs and the hostile text lands in
# final.json (and thus the HTML report).
HOSTILE_REVIEWER = textwrap.dedent("""\
    import json, sys
    prompt = sys.stdin.read()
    hostile = \"<script title='x'>&'</script></details>\"
    if \"For each finding\" in prompt:
        print(json.dumps({\"results\": [{\"id\": \"X1\", \"status\": \"disputed\"}],
                          \"verdict\": \"REJECT\"}))
    else:
        print(json.dumps({
            \"findings\": [{\"id\": \"X1\", \"severity\": \"major\",
                           \"section\": \"Requirements\",
                           \"summary\": hostile, \"evidence\": hostile}],
            \"verdict\": \"REQUEST_CHANGES\", \"summary\": \"1 major\"}))
""")

# One script for every delegated role + the normal write, branched on the
# prompt text the orchestrator sends to each stage. The spec body is injected
# via the __SPEC__ marker (not str.format, which would choke on JSON braces).
BRANCHING_WRITER = textwrap.dedent("""\
    import json, pathlib, sys
    prompt = sys.stdin.read()
    if "Decompose this specification" in prompt:
        print(json.dumps({"tasks": [
            {"id": "t1", "scope": "requirements",
             "instructions": "write the requirements section"},
            {"id": "t2", "scope": "acceptance",
             "instructions": "write the acceptance criteria"}]}))
    elif "Draft the specification section" in prompt:
        print("## Drafted section\\n\\nContent for the delegated scope.")
    elif "Merge the delegated spec sections" in prompt:
        pathlib.Path("spec.md").write_text('''__SPEC__''')
        print("spec.md written (delegated)")
    else:
        pathlib.Path("spec.md").write_text('''__SPEC__''')
        print("spec.md written")
""")

RESEARCH_SCRIPT = textwrap.dedent("""\
    import json, sys
    sys.stdin.read()
    print(json.dumps({\"findings\": [
        {\"summary\": \"authoritative upstream standard\",
         \"source\": \"external docs\"}]}))
""")


def _write_standard_scripts(tmp_path):
    writer = tmp_path / "writer.py"
    writer.write_text(WRITER_SCRIPT.format(spec=VALID_SPEC))
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(APPROVE_REVIEWER)
    return writer, reviewer


def _run(tmp_path, brief_text, reviewer_source=None, extra_args=(),
         writer_source=None):
    workdir = tmp_path / "project"
    workdir.mkdir()
    if writer_source is None:
        writer = tmp_path / "writer.py"
        writer.write_text(WRITER_SCRIPT.format(spec=VALID_SPEC))
    else:
        writer = tmp_path / "writer.py"
        writer.write_text(writer_source)
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(reviewer_source or APPROVE_REVIEWER)
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
    feature_dir = workdir / ".adversarial-spec" / "brief"
    return code, workdir, feature_dir


# --- R9: HTML report + escaping -------------------------------------------------

def test_html_renders_self_contained_report_after_final_json(tmp_path):
    code, workdir, feature_dir = _run(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n",
        extra_args=("--html",))
    assert code == orch.EXIT_APPROVED
    report_path = feature_dir / "report.html"
    assert report_path.is_file()
    html = report_path.read_text(encoding="utf-8")
    final = json.loads((feature_dir / "final.json").read_text())
    assert final["html_report"] == str(report_path)
    # self-contained: no external resource references
    assert "<script src" not in html
    assert "<link" not in html
    # the recorded verdict is rendered
    assert "APPROVED" in html


def test_html_escapes_hostile_finding_content(tmp_path):
    code, workdir, feature_dir = _run(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n",
        reviewer_source=HOSTILE_REVIEWER, extra_args=("--html",))
    assert code == orch.EXIT_REJECTED
    html = (feature_dir / "report.html").read_text(encoding="utf-8")
    # no raw HTML injection is possible from finding fields
    assert "<script" not in html
    assert "</details></details>" not in html
    # the hostile payload survives only in escaped form
    assert "&lt;script" in html
    assert "<script title=" not in html


def test_html_without_flag_writes_no_report(tmp_path):
    code, workdir, feature_dir = _run(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n")
    assert code == orch.EXIT_APPROVED
    assert not (feature_dir / "report.html").exists()
    final = json.loads((feature_dir / "final.json").read_text())
    assert "html_report" not in final


def test_html_renders_on_context_blocked_preflight(tmp_path, monkeypatch):
    # A blocked brief still produces a report when --html is set.
    workdir = tmp_path / "project"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("too short")  # below the min gate
    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir),
        "--dev-cmd", "python3 -c pass",
        "--review-cmd", "python3 -c pass",
        "--html",
    ])
    assert code == orch.EXIT_CONTEXT_BLOCKED
    feature_dir = workdir / ".adversarial-spec" / "brief"
    assert (feature_dir / "report.html").is_file()


# --- R9/R11: CI exit contract ---------------------------------------------------

def test_ci_approved_exits_clean(tmp_path):
    code, workdir, feature_dir = _run(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n",
        extra_args=("--ci",))
    assert code == orch.runner.CI_EXIT_CLEAN
    final = json.loads((feature_dir / "final.json").read_text())
    assert final["ci"]["enabled"] is True
    assert final["ci"]["exit_code"] == orch.runner.CI_EXIT_CLEAN
    assert "findings" in final["ci"]["fail_on"]


def test_ci_rejected_default_fail_on_exits_blocking(tmp_path):
    code, workdir, feature_dir = _run(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n",
        reviewer_source=HOSTILE_REVIEWER, extra_args=("--ci",))
    assert code == orch.runner.CI_EXIT_BLOCKING
    final = json.loads((feature_dir / "final.json").read_text())
    assert final["ci"]["exit_code"] == orch.runner.CI_EXIT_BLOCKING


def test_ci_rejected_fail_on_none_exits_clean(tmp_path):
    code, workdir, feature_dir = _run(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n",
        reviewer_source=HOSTILE_REVIEWER,
        extra_args=("--ci", "--fail-on", "none"))
    assert code == orch.runner.CI_EXIT_CLEAN
    final = json.loads((feature_dir / "final.json").read_text())
    assert final["ci"]["fail_on"] == []
    assert final["ci"]["exit_code"] == orch.runner.CI_EXIT_CLEAN


def test_ci_context_blocked_exits_context_code(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("too short")
    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir),
        "--dev-cmd", "python3 -c pass",
        "--review-cmd", "python3 -c pass",
        "--ci",
    ])
    assert code == orch.runner.CI_EXIT_CONTEXT_BLOCKED


def test_fail_on_invalid_selector_is_usage_error(tmp_path, capsys):
    import pytest
    with pytest.raises(SystemExit):
        orch.build_parser().parse_args(["--fail-on", "bogus:thing"])


# --- R10: deep research --------------------------------------------------------

def test_deep_research_records_evidence_and_enriches_brief(tmp_path):
    research = tmp_path / "research.py"
    research.write_text(RESEARCH_SCRIPT)
    code, workdir, feature_dir = _run(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n",
        extra_args=("--deep-research", "--research-cmd", f"python3 {research}"))
    assert code == orch.EXIT_APPROVED
    assert (feature_dir / "00_research.json").is_file()
    final = json.loads((feature_dir / "final.json").read_text())
    assert final["research"]["enabled"] is True
    assert final["research"]["status"] == "complete"
    assert final["research"]["result_count"] >= 1
    findings = final["research_findings"]
    assert findings and findings[0]["basis"] == "external"


def test_deep_research_without_provider_records_skip(tmp_path):
    # No --research-cmd and no env: research is non-fatal and recorded as skipped.
    code, workdir, feature_dir = _run(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n",
        extra_args=("--deep-research",))
    assert code == orch.EXIT_APPROVED
    final = json.loads((feature_dir / "final.json").read_text())
    assert final["research"]["enabled"] is True
    assert final["research"]["status"] == "skipped"
    assert final["research_findings"] == []


def test_deep_research_disabled_leaves_no_record(tmp_path):
    code, workdir, feature_dir = _run(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n")
    assert code == orch.EXIT_APPROVED
    assert not (feature_dir / "00_research.json").exists()
    final = json.loads((feature_dir / "final.json").read_text())
    assert "research" not in final


# --- R12: delegated threshold --------------------------------------------------

def test_delegated_below_threshold_falls_back_to_direct(tmp_path):
    # A trivial brief is below the 'high' complexity tier: delegation must
    # record a fallback and the normal direct write must still produce spec.md.
    branching = BRANCHING_WRITER.replace("__SPEC__", VALID_SPEC)
    code, workdir, feature_dir = _run(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n",
        writer_source=branching, extra_args=("--delegated",))
    assert code == orch.EXIT_APPROVED
    assert (workdir / "spec.md").is_file()
    final = json.loads((feature_dir / "final.json").read_text())
    assert final["delegated"]["delegated"] is False
    assert final["delegated"]["status"] == "fallback"
    assert any(w.get("code") == "delegation_fallback"
               for w in final["warnings"])


def test_delegated_high_complexity_dispatches_workers(tmp_path):
    # A brief with many requirements clears the 'high' tier (score >= 20000),
    # so delegation actually runs decomposition -> workers -> synthesis.
    branching = BRANCHING_WRITER.replace("__SPEC__", VALID_SPEC)
    high_brief = "# Mega feature\n\n" + "".join(
        f"R{n}: requirement number {n} for the platform.\n"
        for n in range(1, 80))
    code, workdir, feature_dir = _run(
        tmp_path, high_brief, writer_source=branching,
        extra_args=("--delegated",))
    assert code == orch.EXIT_APPROVED
    assert (workdir / "spec.md").is_file()
    assert (feature_dir / "01_delegated.json").is_file()
    final = json.loads((feature_dir / "final.json").read_text())
    assert final["complexity"]["level"] == "high"
    assert final["delegated"]["delegated"] is True
    assert final["delegated"]["status"] == "synthesized"
    assert final["delegated"]["tasks_dispatched"] >= 1


def test_delegated_disabled_uses_direct_write(tmp_path):
    code, workdir, feature_dir = _run(
        tmp_path, "# Demo feature\n\nUsers need a demo command for the app.\n")
    assert code == orch.EXIT_APPROVED
    assert (workdir / "spec.md").is_file()
    final = json.loads((feature_dir / "final.json").read_text())
    assert "delegated" not in final
