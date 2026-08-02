"""P7a — wire the shared F1 contract gate into the spec pipeline (R1).

AC1: a spec whose ac-directive fails does not produce APPROVE in final.json
     and ends REJECT at max-loops.
AC2: the gate is imported from adversarial_common (grep assertion).
AC3: covered by the full suite (``python3 -m pytest tests/ -q``).
"""
import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from scripts import adversarial_spec as orch

ROOT = Path(__file__).resolve().parents[1]
SPEC_SOURCE = ROOT / "scripts" / "adversarial_spec.py"


# --- AC2: the shared contract gate is imported from adversarial_common ---------

def test_contract_gate_is_imported_from_adversarial_common():
    """R1/AC2: ``from adversarial_common import ... run_contract_gate``.

    ``run_contract_gate`` is the shared F1 settle gate (P5); the spec pipeline
    must import it from the common package rather than re-implementing it.
    """
    source = SPEC_SOURCE.read_text(encoding="utf-8")
    assert "from adversarial_common import" in source
    # run_contract_gate is the shared contract gate; its name carries "contract".
    assert re.search(
        r"from adversarial_common import\b.*\brun_contract_gate\b",
        source, re.DOTALL,
    ), "spec pipeline must import run_contract_gate from adversarial_common"


# --- scripted CLIs ------------------------------------------------------------

# Writes spec.md (from a source path passed as argv[1]) plus a tracked
# src/demo.py so a scoped grep directive has a real file to search.
_WRITER = textwrap.dedent("""\
    import pathlib, sys
    sys.stdin.read()  # consume persona + prompt
    spec = pathlib.Path(sys.argv[1]).read_text()
    pathlib.Path("spec.md").write_text(spec)
    pathlib.Path("src").mkdir(exist_ok=True)
    pathlib.Path("src/demo.py").write_text('# demo entry point\\n')
    print("spec.md written")
""")

# Challenger that always clears the spec: no findings on challenge, and an
# APPROVE verify with no per-finding results. This isolates the contract gate
# as the sole thing that can block APPROVE.
_APPROVE_REVIEWER = textwrap.dedent("""\
    import json, sys
    prompt = sys.stdin.read()
    if "For each finding" in prompt:  # VERIFY round
        print(json.dumps({"results": [], "verdict": "APPROVE"}))
    else:  # CHALLENGE
        print(json.dumps({"findings": [], "verdict": "APPROVE", "summary": "clean"}))
""")


def _spec_with_directive(grep_token):
    """A valid spec whose AC1 binds a scoped grep directive for *grep_token*."""
    return textwrap.dedent(f"""\
        ---
        name: "contract-gate"
        version: "1.0"
        author: "adversarial-spec"
        status: "draft"
        tags: [adversarial, spec]
        targets:
          - file: src/demo.py
            description: "Add the demo entry point"
        ---

        # Contract-gate feature

        ## Problem
        A spec must satisfy its own executable acceptance directives.

        ## Requirements
        - R1: the spec is guarded by an ac-directive.

        ## Acceptance criteria
        - AC1 (R1): the demo entry point contains the sentinel token.
          ```ac-directive
          ac: AC1
          kind: grep
          command: grep {grep_token}
          files: [src/demo.py]
          ```
        """)


def _run_pipeline(tmp_path, spec_text, max_loops, *, writer_src=_WRITER):
    """Drive the full pipeline with scripted CLIs; return (code, final_json)."""
    workdir = tmp_path / "project"
    workdir.mkdir()
    writer = tmp_path / "writer.py"
    writer.write_text(writer_src)
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(_APPROVE_REVIEWER)
    spec_source = tmp_path / "spec.md"
    spec_source.write_text(spec_text)
    brief = tmp_path / "contract-gate.md"
    brief.write_text("# Contract gate\n\nGuard the spec with a directive.\n")
    code = orch.main([
        "--brief", str(brief),
        "--workdir", str(workdir),
        "--dev-cmd", f"python3 {writer} {spec_source}",
        "--review-cmd", f"python3 {reviewer}",
        "--max-loops", str(max_loops),
        "--timeout", "60",
    ])
    final_path = workdir / ".adversarial-spec" / "contract-gate" / "final.json"
    return code, json.loads(final_path.read_text())


# --- AC1: a failing ac-directive blocks APPROVE and ends REJECT at max-loops ---

def test_failing_ac_blocks_approve(tmp_path):
    code, final = _run_pipeline(
        tmp_path, _spec_with_directive("ZZZ_ABSENT_SENTINEL_ZZZ"), max_loops=1)

    # Does not produce APPROVE...
    assert code == orch.EXIT_REJECTED, code
    assert final["verdict"] != "APPROVED"
    assert final["verdict"] == "REJECT"
    # ...and ran the full budget before rejecting (at max-loops).
    assert final["loops"] == 1
    # The contract gate result is recorded for auditability.
    contract = final["contract"]
    assert contract["settle"] == "REJECT"
    assert contract["ac_status"]["AC1"] == "fail"
    assert any(f["ac"] == "AC1" for f in contract["failures"])


def test_passing_ac_still_approves(tmp_path):
    """Control: a passing directive does not false-block APPROVE.

    Proves the gate is wired both ways — AC1 is not passing merely because
    the gate always blocks.
    """
    # "demo" is present in src/demo.py ("# demo entry point").
    code, final = _run_pipeline(
        tmp_path, _spec_with_directive("demo"), max_loops=1)

    assert code == orch.EXIT_APPROVED, code
    assert final["verdict"] == "APPROVED"
    assert final["contract"]["settle"] == "APPROVE"
    assert final["contract"]["ac_status"]["AC1"] == "pass"


# A writer whose directive FAILS on the initial write (no sentinel) but is
# FIXED during revise (sentinel added). Isolates A1: when the challenge had
# no findings, the contract gate must be re-run after revision so a spec that
# becomes valid can recover instead of being rejected on the stale initial
# gate result.
_WRITER_FIXES_DIRECTIVE = textwrap.dedent("""\
    import pathlib, sys
    prompt = sys.stdin.read()  # persona + prompt; signals the round
    spec = pathlib.Path(sys.argv[1]).read_text()
    pathlib.Path("spec.md").write_text(spec)
    pathlib.Path("src").mkdir(exist_ok=True)
    # Round 1 (initial write): directive fails — no sentinel yet.
    body = '# demo entry point\\n'
    if 'Revise the specification' in prompt:
        # Revision: writer fixes the directive by adding the sentinel.
        body = '# demo entry point\\nSENTINEL_PRESENT\\n'
    pathlib.Path("src/demo.py").write_text(body)
    print("spec.md written")
""")


def test_gate_recovery_when_no_findings(tmp_path):
    """A1: a contract that becomes valid during revision must recover.

    Challenge APPROVEs with no findings; the initial gate fails (directive
    misses the sentinel). Revision adds the sentinel, so the directive now
    passes. The pipeline must re-run the gate and APPROVE rather than reject
    on the stale initial gate result.
    """
    code, final = _run_pipeline(
        tmp_path, _spec_with_directive("SENTINEL_PRESENT"), max_loops=1,
        writer_src=_WRITER_FIXES_DIRECTIVE)

    assert code == orch.EXIT_APPROVED, code
    assert final["verdict"] == "APPROVED"
    assert final["contract"]["settle"] == "APPROVE"
    assert final["contract"]["ac_status"]["AC1"] == "pass"
    assert final["loops"] == 1
