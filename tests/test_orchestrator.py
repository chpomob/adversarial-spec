"""Orchestrator unit tests + end-to-end pipeline tests with scripted CLIs."""
import json
import subprocess
import textwrap

import pytest

from conftest import VALID_SPEC
from scripts import adversarial_spec as orch


# --- helpers -------------------------------------------------------------------

def test_ensure_ids_fills_and_deduplicates():
    findings = [{"summary": "a"}, {"id": "S1"}, {"id": "S1"}, {"id": ""}]
    out = orch._ensure_ids(findings)
    ids = [f["id"] for f in out]
    assert len(ids) == len(set(ids))
    assert all(ids)


def test_unresolved_keeps_open_findings():
    findings = [{"id": "S1"}, {"id": "S2"}, {"id": "S3"}]
    results = [
        {"id": "S1", "status": "resolved"},
        {"id": "S2", "status": "rejected"},
        {"id": "S3", "status": "disputed"},
    ]
    assert orch._unresolved(findings, results) == [{"id": "S3"}]


def test_unresolved_ignores_results_without_id():
    findings = [{"id": "S1"}]
    results = [{"status": "resolved"}]  # no id: must not settle anything
    assert orch._unresolved(findings, results) == findings


def test_positive_int_rejects_zero_and_garbage():
    with pytest.raises(Exception):
        orch._positive_int("0")
    with pytest.raises(Exception):
        orch._positive_int("nope")
    assert orch._positive_int("3") == 3


def test_finish_merge_failure_returns_infra_and_records_error(
        tmp_path, monkeypatch):
    def fail_merge(*_args, **_kwargs):
        raise orch.gitops.GitError("squash merge spec/demo/1 -> main failed: conflict")

    monkeypatch.setattr(orch.gitops, "squash_merge", fail_merge)
    args = orch.build_parser().parse_args([])
    state = {"branch": "spec/demo/1", "parent_branch": "main"}

    code = orch._finish(args, str(tmp_path), "demo", tmp_path, state,
                        "APPROVED")

    assert code == orch.EXIT_INFRA
    final = json.loads((tmp_path / "final.json").read_text())
    assert final["merged"] is False
    assert "squash merge" in final["error"]


# --- CLI parsing ------------------------------------------------------------------

def test_parser_defaults():
    args = orch.build_parser().parse_args([])
    assert args.brief is None
    assert args.workdir == "."
    assert args.max_loops == 2
    assert args.timeout == 600
    assert args.out == ".adversarial-spec"
    assert args.no_merge is False


def test_derive_feature_prefers_flag_then_brief_filename():
    args = orch.build_parser().parse_args(
        ["--feature", "My Feature!", "--brief", "/x/rate_limiter.md"])
    assert orch._derive_feature(args, "ignored") == "my-feature"
    args = orch.build_parser().parse_args(["--brief", "/x/rate_limiter.md"])
    assert orch._derive_feature(args, "ignored") == "rate-limiter"


def test_derive_feature_falls_back_to_first_brief_line():
    args = orch.build_parser().parse_args([])
    assert orch._derive_feature(args, "\n# Add rate limiting\nmore") == \
        "add-rate-limiting"


def test_main_usage_errors(tmp_path, capsys):
    assert orch.main(["--brief", str(tmp_path / "missing.md")]) == orch.EXIT_USAGE
    assert orch.main(["--workdir", str(tmp_path / "nope"),
                      "--brief", str(tmp_path / "missing.md")]) == orch.EXIT_USAGE
    empty = tmp_path / "empty.md"
    empty.write_text("  \n")
    assert orch.main(["--brief", str(empty)]) == orch.EXIT_USAGE


# --- end-to-end with scripted fake CLIs ---------------------------------------------

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

# Challenges once, then never accepts the revision.
REJECT_REVIEWER = textwrap.dedent("""\
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
""")


def _scripted_pipeline(tmp_path, reviewer_source, extra_args=()):
    workdir = tmp_path / "project"
    workdir.mkdir()
    writer = tmp_path / "writer.py"
    writer.write_text(WRITER_SCRIPT.format(spec=VALID_SPEC))
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(reviewer_source)
    brief = tmp_path / "demo-feature.md"
    brief.write_text("# Demo feature\n\nUsers need a demo command.\n")
    argv = [
        "--brief", str(brief),
        "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1",
        "--timeout", "60",
        *extra_args,
    ]
    return workdir, orch.main(argv)


def _git(workdir, *args):
    return subprocess.run(["git", *args], cwd=workdir,
                          capture_output=True, text=True, check=True).stdout


def test_pipeline_approved_squash_merges(tmp_path):
    workdir, code = _scripted_pipeline(tmp_path, APPROVE_REVIEWER)
    assert code == orch.EXIT_APPROVED
    assert _git(workdir, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert (workdir / "spec.md").is_file()
    assert "squash: demo-feature — spec approved" in _git(workdir, "log", "--format=%s")
    # spec branch was deleted after the squash-merge
    assert "spec/demo-feature/1" not in _git(workdir, "branch", "--list", "spec/*")
    final = json.loads(
        (workdir / ".adversarial-spec" / "demo-feature" / "final.json").read_text())
    assert final["verdict"] == "APPROVED"
    assert final["merged"] is True


def test_pipeline_rejected_leaves_marker(tmp_path):
    workdir, code = _scripted_pipeline(tmp_path, REJECT_REVIEWER)
    assert code == orch.EXIT_REJECTED
    # back on the parent branch, spec branch kept with a [REJECTED] marker
    assert _git(workdir, "symbolic-ref", "--short", "HEAD").strip() == "main"
    branches = _git(workdir, "branch", "--list", "spec/*")
    assert "spec/demo-feature/1" in branches
    log = _git(workdir, "log", "spec/demo-feature/1", "--format=%s")
    assert "[REJECTED]" in log
    assert "revise: demo-feature — round 1" in log
    final = json.loads(
        (workdir / ".adversarial-spec" / "demo-feature" / "final.json").read_text())
    assert final["verdict"] == "REJECT"
    assert final["merged"] is False


def test_pipeline_no_merge_keeps_branch(tmp_path):
    workdir, code = _scripted_pipeline(tmp_path, APPROVE_REVIEWER,
                                       extra_args=("--no-merge",))
    assert code == orch.EXIT_APPROVED
    assert "spec/demo-feature/1" in _git(workdir, "branch", "--list", "spec/*")
    final = json.loads(
        (workdir / ".adversarial-spec" / "demo-feature" / "final.json").read_text())
    assert final["merged"] is False


def test_pipeline_restores_dirty_workdir(tmp_path):
    # A dirty file present before the run must be stashed and restored.
    workdir = tmp_path / "project"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "Initial commit", "-q"],
                   cwd=workdir, check=True)
    (workdir / "wip.txt").write_text("uncommitted work")

    writer = tmp_path / "writer.py"
    writer.write_text(WRITER_SCRIPT.format(spec=VALID_SPEC))
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(APPROVE_REVIEWER)
    brief = tmp_path / "demo-feature.md"
    brief.write_text("# Demo\n")
    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1", "--timeout", "60",
    ])
    assert code == orch.EXIT_APPROVED
    assert (workdir / "wip.txt").read_text() == "uncommitted work"
