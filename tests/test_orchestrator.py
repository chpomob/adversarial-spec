"""Orchestrator unit tests + end-to-end pipeline tests with scripted CLIs."""
import ast
import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from conftest import VALID_SPEC
from scripts import adversarial_spec as orch
from scripts.phases import phase_spec


PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --- documentation invariants --------------------------------------------------

def test_skill_provider_defaults_and_context_blocked_exit_are_honest():
    skill = (PROJECT_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "No model names appear anywhere in the pipeline code or SKILL.md" not in skill
    assert f"`{orch.DEFAULT_DEV_CMD}`" in skill
    assert f"`{orch.DEFAULT_REVIEW_CMD}`" in skill
    assert orch.EXIT_CONTEXT_BLOCKED == 5
    assert re.search(
        rf"^\|\s*{orch.EXIT_CONTEXT_BLOCKED}\s*\|[^\n]*\bCONTEXT_BLOCKED\b",
        skill,
        re.MULTILINE,
    )


def test_provider_agnostic_design_doc_does_not_contradict_defaults():
    """The provider-agnostic-design doc must not claim the two hardcoded
    legacy defaults were 'removed' while they still live in adversarial_spec.py.

    These literals are an intentional, documented zero-config fallback
    (legacy provider mode), not an oversight the doc can retroactively erase.
    """
    doc = (PROJECT_ROOT / "references" / "provider-agnostic-design.md").read_text(encoding="utf-8")

    removed_section = doc.split("## What was removed", 1)[-1].split("##", 1)[0]
    for literal in (orch.DEFAULT_DEV_CMD, orch.DEFAULT_REVIEW_CMD):
        assert literal not in removed_section, (
            f"'{literal}' still exists in source (adversarial_spec.py) but is "
            "listed under 'What was removed' in provider-agnostic-design.md. "
            "Reconcile as the intentional legacy zero-config fallback."
        )

    # Reconciliation: the doc must acknowledge these defaults are deliberately kept.
    for literal in (orch.DEFAULT_DEV_CMD, orch.DEFAULT_REVIEW_CMD):
        assert literal in doc
    assert "legacy" in doc.casefold()


def test_readme_output_example_satisfies_required_frontmatter_fields():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"```yaml\s*\n(?P<example>.*?)```", readme, re.DOTALL)

    assert match is not None
    example = match.group("example")
    assert "author" in phase_spec._REQUIRED_SCALARS
    for field in phase_spec._REQUIRED_SCALARS:
        assert re.search(rf"^{re.escape(field)}\s*:", example, re.MULTILINE)
    assert phase_spec.validate_spec_text(example) == (True, "")


def test_fenced_commands_do_not_use_claude_tmux_yolo_flag():
    command_languages = {"bash", "console", "sh", "shell", "zsh"}

    for filename in ("SKILL.md", "README.md"):
        markdown = (PROJECT_ROOT / filename).read_text(encoding="utf-8")
        fences = re.finditer(
            r"^```(?P<language>[^\n]*)\n(?P<body>.*?)^```\s*$",
            markdown,
            re.MULTILINE | re.DOTALL,
        )
        command_bodies = [
            match.group("body")
            for match in fences
            if match.group("language").strip().casefold() in command_languages
        ]
        assert all("--yolo" not in body for body in command_bodies), filename


# --- helpers -------------------------------------------------------------------

def test_extracted_lifecycle_helpers_are_not_locally_defined():
    tree = ast.parse(
        (PROJECT_ROOT / "scripts/adversarial_spec.py").read_text(encoding="utf-8")
    )
    local_functions = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    extracted = {
        "_banner", "_write_json", "_ensure_ids", "_unresolved",
        "_threshold_overrides", "_preflight", "_record_phase",
        "_log_retrospective", "_phase_failed", "_restore", "_setup_git",
        "_finish", "_final_md", "_ci_exit_code_from_final", "_positive_int",
        "_non_negative_int",
    }

    assert local_functions.isdisjoint(extracted)
    assert any(
        isinstance(node, ast.Import)
        and any(
            alias.name == "adversarial_common.pipeline_base"
            for alias in node.names
        )
        for node in tree.body
    )

def test_ensure_ids_fills_and_deduplicates():
    findings = [{"summary": "a"}, {"id": "S1"}, {"id": "S1"}, {"id": ""}]
    out = orch.pipeline_base.ensure_finding_ids(findings)
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
    assert orch.pipeline_base.unresolved_findings(findings, results) == [
        {"id": "S3"},
    ]


def test_unresolved_ignores_results_without_id():
    findings = [{"id": "S1"}]
    results = [{"status": "resolved"}]  # no id: must not settle anything
    assert orch.pipeline_base.unresolved_findings(findings, results) == findings


def test_finalize_finding_ids_rekeys_index_warnings():
    # A2: a finding without an id is assigned "finding-1"; its epistemic
    # warning was keyed by the 0-based list index "0" by the shared parser.
    findings = [{"severity": "major", "section": "X", "summary": "s",
                 "evidence": "e"}]
    warnings = [{"code": "epistemic_label_defaulted", "finding_id": "0",
                 "message": "missing or invalid confidence/basis"}]
    out = orch._finalize_finding_ids(findings, warnings)
    assert out[0]["id"] == "finding-1"
    assert warnings[0]["finding_id"] == "finding-1"  # re-keyed to final id


def test_finalize_finding_ids_preserves_existing_id_warnings():
    # A finding that already carried an id keeps its warning reference stable.
    findings = [{"id": "S1", "severity": "major", "section": "X",
                 "summary": "s", "evidence": "e"}]
    warnings = [{"code": "epistemic_label_defaulted", "finding_id": "S1"}]
    orch._finalize_finding_ids(findings, warnings)
    assert findings[0]["id"] == "S1"
    assert warnings[0]["finding_id"] == "S1"


def test_positive_int_rejects_zero_and_garbage():
    with pytest.raises(Exception):
        orch.pipeline_base.positive_int("0")
    with pytest.raises(Exception):
        orch.pipeline_base.positive_int("nope")
    assert orch.pipeline_base.positive_int("3") == 3


def test_phase_failure_uses_shared_retrospective_record(tmp_path):
    state = {"feature": "demo", "branch": "spec/demo/1"}
    result = {"error": "provider crashed", "stdout": "x" * 250}

    code = orch.pipeline_base.phase_failure(
        "challenge", result, state, tmp_path,
        policy=orch._SPEC_RETROSPECTIVE_POLICY,
    )

    assert code == orch.EXIT_INFRA
    record = (tmp_path / "ISSUES.md").read_text(encoding="utf-8")
    assert "challenge failed for demo" in record
    assert "- **Branch:** spec/demo/1" in record
    assert "- **Error:** provider crashed" in record
    assert repr("x" * 200) in record


def test_finish_merge_failure_returns_infra_and_records_error(
        tmp_path, monkeypatch):
    def fail_merge(*_args, **_kwargs):
        raise orch.gitops.GitError("squash merge spec/demo/1 -> main failed: conflict")

    monkeypatch.setattr(orch.gitops, "squash_merge", fail_merge)
    args = orch.build_parser().parse_args([])
    state = {"branch": "spec/demo/1", "parent_branch": "main"}

    code = orch.pipeline_base.finish_pipeline(
        args, str(tmp_path), "demo", tmp_path, state, "APPROVED",
        policy=orch._spec_finish_policy(args),
    )

    assert code == orch.EXIT_INFRA
    final = json.loads((tmp_path / "final.json").read_text())
    assert final["merged"] is False
    assert "squash merge" in final["error"]


def test_verdict_not_approved_on_merge_failure(tmp_path, monkeypatch):
    def fail_merge(*_args, **_kwargs):
        raise orch.gitops.GitError("squash merge spec/demo/1 -> main failed: conflict")

    monkeypatch.setattr(orch.gitops, "squash_merge", fail_merge)
    args = orch.build_parser().parse_args(["--ci"])
    state = {"branch": "spec/demo/1", "parent_branch": "main"}

    code = orch.pipeline_base.finish_pipeline(
        args, str(tmp_path), "demo", tmp_path, state, "APPROVED",
        policy=orch._spec_finish_policy(args),
    )

    assert code == orch.EXIT_INFRA
    final = json.loads((tmp_path / "final.json").read_text())
    assert final["verdict"] != "APPROVED"
    assert final["verdict"] == "INFRA"
    assert final["merged"] is False
    # ponytail: the recorded CI exit code must also reflect infrastructure failure.
    assert final["ci"]["exit_code"] == orch.EXIT_INFRA


# --- CLI parsing ------------------------------------------------------------------

def test_parser_defaults():
    args = orch.build_parser().parse_args([])
    assert args.brief is None
    assert args.workdir == "."
    assert args.max_loops == 2
    assert args.timeout == 600
    assert args.out == ".adversarial-spec"
    assert args.no_merge is False
    assert args.provider_config is None
    assert args.force is False
    assert args.force_provider == []


def test_parser_accepts_provider_controls():
    args = orch.build_parser().parse_args([
        "--provider-config", "providers.yaml",
        "--force",
        "--force-provider", "writer:codex",
        "--force-provider", "verify:claude",
    ])
    assert args.provider_config == "providers.yaml"
    assert args.force is True
    assert orch._force_provider_map(args.force_provider) == {
        "writer": "codex",
        "verify": "claude",
    }


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


def _provider_config(tmp_path, writer_cmd, reviewer_cmd, snapshots):
    checker = tmp_path / "quota-checker.py"
    checker.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({snapshots!r}))\n"
    )
    checker.chmod(0o755)
    config = tmp_path / "providers.yaml"
    config.write_text(textwrap.dedent(f"""\
        quota_cmd: {json.dumps(str(checker))}
        quota_cache_ttl: 0
        roles:
          writer:
            - alias: writer-provider
              cmd: {json.dumps(writer_cmd)}
              quota_check: writer
          challenger:
            - alias: challenger-provider
              cmd: {json.dumps(reviewer_cmd)}
              quota_check: challenger
          verify:
            - alias: verify-provider
              cmd: {json.dumps(reviewer_cmd)}
              quota_check: verify
    """))
    return config


def _registry_pipeline(
    tmp_path, snapshots, extra_args=(), reviewer_source=APPROVE_REVIEWER,
    writer_source=None, brief_text=None,
):
    workdir = tmp_path / "registry-project"
    workdir.mkdir()
    writer = tmp_path / "registry-writer.py"
    writer.write_text(writer_source or WRITER_SCRIPT.format(spec=VALID_SPEC))
    reviewer = tmp_path / "registry-reviewer.py"
    reviewer.write_text(reviewer_source)
    config = _provider_config(
        tmp_path,
        f"python3 {writer}",
        f"python3 {reviewer}",
        snapshots,
    )
    brief = tmp_path / "registry-feature.md"
    brief.write_text(
        brief_text
        or "# Registry feature\n\nUsers need provider routing coverage.\n"
    )
    code = orch.main([
        "--brief", str(brief),
        "--workdir", str(workdir),
        "--provider-config", str(config),
        "--max-loops", "1",
        "--timeout", "60",
        *extra_args,
    ])
    final_path = (
        workdir / ".adversarial-spec" / "registry-feature" / "final.json"
    )
    return workdir, code, json.loads(final_path.read_text())


def test_registry_routes_write_and_challenge_and_records_history(tmp_path):
    _, code, final = _registry_pipeline(tmp_path, {
        "writer-provider": {"used_pct": 1},
        "challenger-provider": {"used_pct": 1},
        "verify-provider": {"used_pct": 1},
    })
    assert code == orch.EXIT_APPROVED
    assert [item["phase"] for item in final["provider_history"]] == [
        "write", "challenge"
    ]
    assert [item["alias"] for item in final["provider_history"]] == [
        "writer-provider", "challenger-provider"
    ]


def test_registry_routes_every_delegated_stage_through_writer(tmp_path):
    branching_writer = textwrap.dedent("""\
        import json, pathlib, sys
        prompt = sys.stdin.read()
        if "Decompose this specification" in prompt:
            print(json.dumps({"tasks": [
                {"id": "requirements", "scope": "requirements"},
                {"id": "acceptance", "scope": "acceptance"}]}))
        elif "Draft the specification section" in prompt:
            print("## Delegated section\\n\\nContent for this scope.")
        elif "Merge the delegated spec sections" in prompt:
            pathlib.Path("spec.md").write_text(%r)
            print("spec.md written")
    """ % VALID_SPEC)
    high_brief = "# Registry feature\n\n" + "".join(
        f"R{number}: requirement number {number} for the platform.\n"
        for number in range(1, 80)
    )
    _, code, final = _registry_pipeline(
        tmp_path,
        {
            "writer-provider": {"used_pct": 1},
            "challenger-provider": {"used_pct": 1},
            "verify-provider": {"used_pct": 1},
        },
        extra_args=("--delegated",),
        writer_source=branching_writer,
        brief_text=high_brief,
    )
    assert code == orch.EXIT_APPROVED
    assert final["delegated"]["status"] == "synthesized"
    phases = [item["phase"] for item in final["provider_history"]]
    assert phases == [
        "delegated_decomposition",
        "delegated_worker",
        "delegated_worker",
        "delegated_synthesis",
        "challenge",
    ]
    assert [item["alias"] for item in final["provider_history"][:-1]] == [
        "writer-provider",
        "writer-provider",
        "writer-provider",
        "writer-provider",
    ]


def test_registry_routes_revise_to_writer_and_verify_to_verify(tmp_path):
    _, code, final = _registry_pipeline(
        tmp_path,
        {
            "writer-provider": {"used_pct": 1},
            "challenger-provider": {"used_pct": 1},
            "verify-provider": {"used_pct": 1},
        },
        reviewer_source=FINDINGS_REVIEWER,
    )
    assert code == orch.EXIT_APPROVED
    assert [item["phase"] for item in final["provider_history"]] == [
        "write", "challenge", "revise_1", "verify_1"
    ]
    assert [item["alias"] for item in final["provider_history"]] == [
        "writer-provider", "challenger-provider",
        "writer-provider", "verify-provider",
    ]


def test_explicit_dev_cmd_bypasses_writer_quota_only(tmp_path):
    explicit_writer = tmp_path / "explicit-writer.py"
    explicit_writer.write_text(WRITER_SCRIPT.format(spec=VALID_SPEC))
    _, code, final = _registry_pipeline(
        tmp_path,
        {
            "writer-provider": {"used_pct": 100},
            "challenger-provider": {"used_pct": 1},
            "verify-provider": {"used_pct": 1},
        },
        extra_args=("--dev-cmd", f"python3 {explicit_writer}"),
    )
    assert code == orch.EXIT_APPROVED
    assert [item["phase"] for item in final["provider_history"]] == [
        "challenge"
    ]


def test_explicit_review_cmd_bypasses_challenger_and_verify_quota(tmp_path):
    explicit_review_cmd = f"python3 {tmp_path / 'registry-reviewer.py'}"
    _, code, final = _registry_pipeline(
        tmp_path,
        {
            "writer-provider": {"used_pct": 1},
            "challenger-provider": {"used_pct": 100},
            "verify-provider": {"used_pct": 100},
        },
        extra_args=("--review-cmd", explicit_review_cmd),
        reviewer_source=FINDINGS_REVIEWER,
    )
    assert code == orch.EXIT_APPROVED
    assert [item["phase"] for item in final["provider_history"]] == [
        "write", "revise_1"
    ]


def test_force_skips_quota_and_marks_provider_history(tmp_path):
    _, code, final = _registry_pipeline(
        tmp_path,
        {
            "writer-provider": {"used_pct": 100},
            "challenger-provider": {"used_pct": 100},
            "verify-provider": {"used_pct": 100},
        },
        extra_args=("--force",),
    )
    assert code == orch.EXIT_APPROVED
    assert all(item["forced"] for item in final["provider_history"])


def test_no_provider_exits_three_with_snapshots(tmp_path, capsys):
    _, code, final = _registry_pipeline(
        tmp_path,
        {
            "writer-provider": {"used_pct": 100},
            "challenger-provider": {"used_pct": 1},
            "verify-provider": {"used_pct": 1},
        },
        extra_args=("--ci",),
    )
    assert code == 3
    assert final["ci"]["exit_code"] == 3
    assert final["status"] == "provider_unavailable"
    assert final["provider_snapshots"]["writer-provider"]["used_pct"] == 100
    assert final["provider_history"][0]["phase"] == "write"
    assert "snapshot=" in capsys.readouterr().err


def test_verify_retry_exhaustion_preserves_first_decision_in_final(tmp_path):
    workdir = tmp_path / "retry-exhaustion-project"
    workdir.mkdir()
    marker = tmp_path / "first-verify-completed"
    writer = tmp_path / "retry-exhaustion-writer.py"
    writer.write_text(WRITER_SCRIPT.format(spec=VALID_SPEC))
    reviewer = tmp_path / "retry-exhaustion-reviewer.py"
    reviewer.write_text(textwrap.dedent(f"""\
        import json, pathlib, sys
        prompt = sys.stdin.read()
        if "For each finding" in prompt:
            pathlib.Path({str(marker)!r}).write_text("done")
            print("invalid JSON")
        else:
            print(json.dumps({{
                "findings": [{{"id": "S1", "severity": "major",
                              "section": "Requirements",
                              "summary": "R1 untestable", "evidence": "R1"}}],
                "verdict": "REQUEST_CHANGES", "summary": "1 major"}}))
    """))
    checker = tmp_path / "retry-exhaustion-quota.py"
    checker.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, pathlib
        exhausted = pathlib.Path({str(marker)!r}).exists()
        print(json.dumps({{
            "writer-provider": {{"used_pct": 1}},
            "challenger-provider": {{"used_pct": 1}},
            "verify-provider": {{"used_pct": 100 if exhausted else 1}},
        }}))
    """))
    checker.chmod(0o755)
    config = tmp_path / "retry-exhaustion-providers.yaml"
    config.write_text(textwrap.dedent(f"""\
        quota_cmd: {json.dumps(str(checker))}
        quota_cache_ttl: 0
        roles:
          writer:
            - alias: writer-provider
              cmd: {json.dumps(f'python3 {writer}')}
              quota_check: writer
          challenger:
            - alias: challenger-provider
              cmd: {json.dumps(f'python3 {reviewer}')}
              quota_check: challenger
          verify:
            - alias: verify-provider
              cmd: {json.dumps(f'python3 {reviewer}')}
              quota_check: verify
    """))
    brief = tmp_path / "retry-exhaustion.md"
    brief.write_text("# Retry exhaustion\n\nVerify provider history retention.\n")

    code = orch.main([
        "--brief", str(brief),
        "--workdir", str(workdir),
        "--provider-config", str(config),
        "--max-loops", "1",
        "--timeout", "60",
    ])

    final = json.loads((
        workdir / ".adversarial-spec" / "retry-exhaustion" / "final.json"
    ).read_text())
    assert code == orch.EXIT_REJECTED
    assert final["status"] == "provider_unavailable"
    assert [item["phase"] for item in final["provider_history"]] == [
        "write", "challenge", "revise_1", "verify_1", "verify_1",
    ]
    assert [item["alias"] for item in final["provider_history"][-2:]] == [
        "verify-provider", None,
    ]


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


def test_legacy_banner_prints_resolved_writer_and_challenger_commands(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(orch, "load_provider_config", lambda _path: None)
    _workdir, code = _scripted_pipeline(tmp_path, APPROVE_REVIEWER)
    assert code == orch.EXIT_APPROVED
    output = capsys.readouterr().out
    assert "  Provider mode: legacy\n" in output
    writer_cmd = f"python3 {tmp_path / 'writer.py'}"
    reviewer_cmd = f"python3 {tmp_path / 'reviewer.py'}"
    assert f"  WRITER: {writer_cmd[:60]}\n" in output
    assert f"  CHALLENGER: {reviewer_cmd[:60]}\n" in output


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


# --- SP4/SP5: gitignore honors --out -----------------------------------------

def test_custom_out_dir_is_gitignored(tmp_path):
    # --out my-artifacts: .gitignore gains my-artifacts/ before any commit_all,
    # so the artifacts exist on disk but are never swept into git history.
    workdir, code = _scripted_pipeline(
        tmp_path, APPROVE_REVIEWER, extra_args=("--out", "my-artifacts"))
    assert code == orch.EXIT_APPROVED
    gitignore = (workdir / ".gitignore").read_text(encoding="utf-8")
    assert "/my-artifacts/" in gitignore.splitlines()
    # artifacts really exist under the custom out dir (test is not vacuous)
    assert (workdir / "my-artifacts" / "demo-feature" / "final.json").is_file()
    # git status does not list anything under my-artifacts/ as untracked ...
    status = _git(workdir, "status", "--porcelain")
    assert not any("my-artifacts/" in line for line in status.splitlines()), status
    # ... and git confirms the path is ignored, not tracked.
    assert not any(line.startswith("my-artifacts/")
                   for line in _git(workdir, "ls-files").splitlines())
    ignored = _git(workdir, "status", "--ignored", "--porcelain")
    assert any("my-artifacts/" in line for line in ignored.splitlines())


def test_default_out_dir_still_gitignored(tmp_path):
    # Regression guard: the .adversarial-spec default stays protected.
    workdir, code = _scripted_pipeline(tmp_path, APPROVE_REVIEWER)
    assert code == orch.EXIT_APPROVED
    gitignore = (workdir / ".gitignore").read_text(encoding="utf-8")
    assert "/.adversarial-spec/" in gitignore.splitlines()


def test_relative_out_with_dot_prefix_is_gitignored(tmp_path):
    # A1: --out ./my-artifacts must normalize to my-artifacts/ in .gitignore.
    # The raw ./my-artifacts/ spelling is inert (git never matches a ./ prefix),
    # so without normalization the artifacts leak into the squashed commit.
    workdir, code = _scripted_pipeline(
        tmp_path, APPROVE_REVIEWER, extra_args=("--out", "./my-artifacts"))
    assert code == orch.EXIT_APPROVED
    gitignore = (workdir / ".gitignore").read_text(encoding="utf-8")
    assert "/my-artifacts/" in gitignore.splitlines()
    assert "./my-artifacts/" not in gitignore.splitlines()
    assert (workdir / "my-artifacts" / "demo-feature" / "final.json").is_file()
    status = _git(workdir, "status", "--porcelain")
    assert not any("my-artifacts/" in line for line in status.splitlines()), status
    assert not any(line.startswith("my-artifacts/")
                   for line in _git(workdir, "ls-files").splitlines())


def test_outside_workdir_but_inside_repo_is_gitignored(tmp_path):
    # A2: workdir nested inside an enclosing repo; --out lands outside workdir
    # but inside the repo. A workdir/.gitignore cannot reach a sibling dir, so
    # the entry must anchor at the repo root or git add -A (whole tree) stages
    # the artifacts and they leak into the squashed commit.
    repo = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    workdir = repo / "project"
    workdir.mkdir()
    writer = workdir / "writer.py"
    writer.write_text(WRITER_SCRIPT.format(spec=VALID_SPEC))
    reviewer = workdir / "reviewer.py"
    reviewer.write_text(APPROVE_REVIEWER)
    brief = workdir / "demo-feature.md"
    brief.write_text("# Demo feature\n\nUsers need a demo command.\n")
    # Commit helpers so the repo tree is clean: setup_git's stash_dirty runs
    # over the whole enclosing repo and would otherwise sweep them away.
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    out = repo / "artifacts"  # sibling of workdir: outside workdir, inside repo
    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir), "--out", str(out),
        "--dev-cmd", f"python3 {writer}", "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1", "--timeout", "60",
    ])
    assert code == orch.EXIT_APPROVED
    # artifacts really exist under the absolute --out (test is not vacuous)
    assert (out / "demo-feature" / "final.json").is_file()
    # the entry anchors at the repo root, never workdir
    assert (repo / ".gitignore").is_file()
    assert "/artifacts/" in (repo / ".gitignore").read_text(
        encoding="utf-8").splitlines()
    assert not (workdir / ".gitignore").exists()
    # git never tracks the artifacts under any spelling
    status = _git(repo, "status", "--porcelain")
    assert not any("artifacts" in line for line in status.splitlines()), status
    assert not any("artifacts" in line
                   for line in _git(repo, "ls-files").splitlines())


def test_custom_out_dot_dir_is_gitignored(tmp_path):
    # R2: --out . resolves to workdir itself. The naive relative pattern
    # would be "./", which git never matches, leaving every artifact at
    # workdir root free to be swept into commit_all's `git add -A`. The
    # effective per-feature artifacts dir must be gitignored instead.
    workdir, code = _scripted_pipeline(
        tmp_path, APPROVE_REVIEWER, extra_args=("--out", "."))
    assert code == orch.EXIT_APPROVED
    gitignore = (workdir / ".gitignore").read_text(encoding="utf-8")
    assert "./" not in gitignore.splitlines()
    assert "/demo-feature/" in gitignore.splitlines()
    # artifacts really exist at the repo root (test is not vacuous)
    assert (workdir / "demo-feature" / "final.json").is_file()
    status = _git(workdir, "status", "--porcelain")
    assert not any("demo-feature/" in line for line in status.splitlines()), status
    assert not any(line.startswith("demo-feature/")
                   for line in _git(workdir, "ls-files").splitlines())
    ignored = _git(workdir, "status", "--ignored", "--porcelain")
    assert any("demo-feature/" in line for line in ignored.splitlines())


def test_custom_out_abs_path_inside_repo_is_gitignored(tmp_path):
    # R2: an absolute --out that resolves to workdir itself hits the same
    # "./" trap as --out . and must fall back the same way.
    workdir = tmp_path / "project"
    workdir.mkdir()
    writer = tmp_path / "writer.py"
    writer.write_text(WRITER_SCRIPT.format(spec=VALID_SPEC))
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(APPROVE_REVIEWER)
    brief = tmp_path / "demo-feature.md"
    brief.write_text("# Demo feature\n\nUsers need a demo command.\n")
    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir),
        "--out", str(workdir),
        "--dev-cmd", f"python3 {writer}", "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1", "--timeout", "60",
    ])
    assert code == orch.EXIT_APPROVED
    gitignore = (workdir / ".gitignore").read_text(encoding="utf-8")
    assert "/demo-feature/" in gitignore.splitlines()
    assert (workdir / "demo-feature" / "final.json").is_file()
    status = _git(workdir, "status", "--porcelain")
    assert not any("demo-feature/" in line for line in status.splitlines()), status


def test_custom_out_abs_repo_root_outside_workdir_is_gitignored(tmp_path):
    # R2: --out is an absolute path equal to the enclosing repo root, while
    # workdir is a nested subdirectory. Same "./" trap, but anchored at the
    # repo root (mirrors test_outside_workdir_but_inside_repo_is_gitignored's
    # anchoring, exercising the trivial-match fallback there instead of at
    # workdir).
    repo = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    workdir = repo / "project"
    workdir.mkdir()
    writer = workdir / "writer.py"
    writer.write_text(WRITER_SCRIPT.format(spec=VALID_SPEC))
    reviewer = workdir / "reviewer.py"
    reviewer.write_text(APPROVE_REVIEWER)
    brief = workdir / "demo-feature.md"
    brief.write_text("# Demo feature\n\nUsers need a demo command.\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir), "--out", str(repo),
        "--dev-cmd", f"python3 {writer}", "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1", "--timeout", "60",
    ])
    assert code == orch.EXIT_APPROVED
    assert (repo / "demo-feature" / "final.json").is_file()
    assert (repo / ".gitignore").is_file()
    assert "/demo-feature/" in (repo / ".gitignore").read_text(
        encoding="utf-8").splitlines()
    assert not (workdir / ".gitignore").exists()
    status = _git(repo, "status", "--porcelain")
    assert not any("demo-feature/" in line for line in status.splitlines()), status


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
    brief.write_text("# Demo feature\n\nUsers need a demo command for testing.\n")
    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1", "--timeout", "60",
    ])
    assert code == orch.EXIT_APPROVED
    assert (workdir / "wip.txt").read_text() == "uncommitted work"


def test_pipeline_phase_failure_writes_shared_retrospective(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    writer = tmp_path / "writer.py"
    writer.write_text(
        "import sys\nsys.stdin.read()\n"
        "print('writer failed', file=sys.stderr)\nraise SystemExit(7)\n"
    )
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(APPROVE_REVIEWER)
    brief = tmp_path / "demo-feature.md"
    brief.write_text(
        "# Demo feature\n\nUsers need retrospective failure coverage.\n"
    )

    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-loops", "1", "--timeout", "60",
    ])

    assert code == orch.EXIT_INFRA
    retrospective = (
        workdir / ".adversarial-spec" / "demo-feature" / "ISSUES.md"
    ).read_text(encoding="utf-8")
    assert "write failed for demo-feature" in retrospective
    assert "WRITE exited 7" in retrospective
    assert "Auto-logged by pipeline" in retrospective


# --- R1: context gate blocks brief before any provider call -----------------

def test_context_gate_blocks_short_brief(tmp_path):
    workdir = tmp_path / "project"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("short")  # below 40 char threshold
    code = orch.main([
        "--brief", str(brief), "--workdir", str(workdir),
        "--dev-cmd", "python3 -c pass",
        "--review-cmd", "python3 -c pass",
    ])
    assert code == orch.EXIT_CONTEXT_BLOCKED
    feature_dir = workdir / ".adversarial-spec" / "brief"
    final = json.loads((feature_dir / "final.json").read_text())
    assert final["verdict"] == "CONTEXT_BLOCKED"
    assert final["context"]["ok"] is False
    # No provider was ever called: no call records, zero costs.
    assert final["calls"] == []
    assert final["costs"]["total"]["prompt_tokens"] == 0


def test_context_gate_passes_valid_brief(tmp_path):
    workdir, code = _scripted_pipeline(tmp_path, APPROVE_REVIEWER)
    assert code == orch.EXIT_APPROVED
    final = json.loads(
        (workdir / ".adversarial-spec" / "demo-feature" / "final.json").read_text())
    assert final["verdict"] == "APPROVED"
    assert final["context"]["ok"] is True


# --- R4: costs and complexity land in final.json ----------------------------

def test_final_json_contains_cost_and_complexity(tmp_path):
    workdir, code = _scripted_pipeline(tmp_path, APPROVE_REVIEWER)
    assert code == orch.EXIT_APPROVED
    final = json.loads(
        (workdir / ".adversarial-spec" / "demo-feature" / "final.json").read_text())
    assert "costs" in final
    assert "total" in final["costs"]
    assert final["costs"]["total"]["prompt_tokens"] > 0
    assert "est_cost_usd" in final["costs"]["total"]
    assert len(final["calls"]) > 0
    assert "complexity" in final
    assert "level" in final["complexity"]
    assert "score" in final["complexity"]
    assert "recommended_agents" in final["complexity"]


# --- R5: retry/caps controls via CLI ----------------------------------------

def test_cli_parses_retry_and_caps_args():
    args = orch.build_parser().parse_args([
        "--max-retries", "5",
        "--max-input-chars", "128000",
        "--max-output-chars", "64000",
        "--truncate-input",
    ])
    assert args.max_retries == 5
    assert args.max_input_chars == 128000
    assert args.max_output_chars == 64000
    assert args.truncate_input is True


def test_execution_record_captures_retry_caps(tmp_path):
    workdir, code = _scripted_pipeline(
        tmp_path, APPROVE_REVIEWER,
        extra_args=("--max-retries", "2", "--max-input-chars", "200000",
                    "--max-output-chars", "16000", "--truncate-input"))
    assert code == orch.EXIT_APPROVED
    final = json.loads(
        (workdir / ".adversarial-spec" / "demo-feature" / "final.json").read_text())
    assert final["execution"]["max_retries"] == 2
    assert final["execution"]["max_input_chars"] == 200000
    assert final["execution"]["max_output_chars"] == 16000
    assert final["execution"]["truncate_input"] is True


# --- R8: epistemic labels land in final.json findings -----------------------

FINDINGS_REVIEWER = textwrap.dedent("""\
    import json, sys
    prompt = sys.stdin.read()
    if "For each finding" in prompt:
        print(json.dumps({
            "results": [{"id": "S1", "status": "resolved"}],
            "verdict": "APPROVE"
        }))
    else:
        print(json.dumps({
            "findings": [{"id": "S1", "severity": "major",
                           "section": "Requirements",
                           "summary": "R1 untestable",
                           "evidence": "spec §3",
                           "confidence": "high", "basis": "spec"}],
            "verdict": "APPROVE", "summary": "acceptable"
        }))
""")


def test_final_json_contains_epistemic_distribution(tmp_path):
    workdir, code = _scripted_pipeline(tmp_path, FINDINGS_REVIEWER)
    assert code == orch.EXIT_APPROVED
    final = json.loads(
        (workdir / ".adversarial-spec" / "demo-feature" / "final.json").read_text())
    assert "epistemic_distribution" in final
    dist = final["epistemic_distribution"]
    assert "confidence" in dist
    assert "basis" in dist
    assert dist["confidence"]["high"] == 1
    assert dist["basis"]["spec"] == 1
    assert dist["combined"]["high/spec"] == 1
