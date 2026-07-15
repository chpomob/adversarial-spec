"""
CHALLENGE phase: the spec-challenger model reviews ``spec.md``.

The spec text is embedded in the prompt (model-agnostic: works even for
providers without file access) and the file is also on disk for providers
that can read it. Output is validated JSON findings; one retry with a
stricter instruction on invalid JSON.
"""
from pathlib import Path

from . import run_role, runtime_metadata, try_parse_json, merge_runtime

__all__ = ["run_challenge"]

_VALID_VERDICTS = {"REQUEST_CHANGES", "APPROVE", "REJECT"}
_VALID_SEVERITIES = {"blocker", "major", "minor", "nit"}
_REQUIRED_FINDING_KEYS = {"id", "severity", "section", "summary", "evidence"}


def _validate(payload):
    """Lightweight schema check for challenger output. No jsonschema dep."""
    if not isinstance(payload, dict):
        return False
    if payload.get("verdict") not in _VALID_VERDICTS:
        return False
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return False
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        if not _REQUIRED_FINDING_KEYS.issubset(finding.keys()):
            return False
        if finding.get("severity") not in _VALID_SEVERITIES:
            return False
    return True


def _build_prompt(branch_point=""):
    diff_base = branch_point or "<branch-point>"
    return (
        "Challenge the specification at `spec.md` (in the current directory). "
        f"The branch-point SHA is `{diff_base}`. Inspect the cumulative "
        f"change with `git diff {diff_base}..HEAD`.\n"
        "Look for, in priority order: missing requirements, contradictions, "
        "untestable acceptance criteria, scope creep, ambiguous wording, "
        "formatting/consistency issues.\n\n"
        "Output ONLY valid JSON:\n"
        '{"findings": [{"id": "S1", "severity": "blocker|major|minor|nit", '
        '"section": "Problem|Requirements|Acceptance criteria|targets|frontmatter", '
        '"summary": "one-line issue", "evidence": "exact spec text or id"}], '
        '"verdict": "REQUEST_CHANGES|APPROVE|REJECT", '
        '"summary": "counts by severity"}\n'
    )


def run_challenge(review_cmd, workdir, timeout, run=None, branch_point=""):
    """
    Run the spec-challenger against ``<workdir>/spec.md``.

    Returns ``{"phase": "challenge", "exit_code": 0, "findings": [...],
    "verdict": "..."}``; on failure ``{"phase": "challenge", "exit_code": 1,
    "error": "..."}``. *run* is injectable for tests.
    """
    run = run or run_role
    try:
        spec_text = (Path(workdir) / "spec.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"phase": "challenge", "exit_code": 1,
                "error": f"could not read spec.md: {exc}"}

    prompt = _build_prompt(branch_point)
    parse_warnings = []

    def _attempt(prompt_text):
        result = run(
            review_cmd, prompt_text, "spec-challenger", timeout, workdir,
            phase="challenge")
        stdout, stderr, code = result[0], result[1], result[2]
        if code != 0:
            return None, f"CHALLENGE exited {code}: {(stderr or '')[:200]}", stdout, runtime_metadata(result)
        return try_parse_json(stdout, warnings=parse_warnings), None, stdout, runtime_metadata(result)

    try:
        payload, err, stdout, runtime = _attempt(prompt)
        if err:
            return {"phase": "challenge", "exit_code": 1, "error": err,
                    "stdout": stdout, "execution": runtime}
        if not _validate(payload):
            prev_runtime = runtime
            payload, err, stdout, runtime = _attempt(
                prompt + "\n\nIMPORTANT: Respond with raw JSON only, matching "
                         "the schema exactly. No markdown, no code fences, "
                         "no explanations."
            )
            runtime = merge_runtime(prev_runtime, runtime)
            if err:
                return {"phase": "challenge", "exit_code": 1, "error": err,
                        "stdout": stdout, "execution": runtime}
            if not _validate(payload):
                return {
                    "phase": "challenge", "exit_code": 1,
                    "findings": [], "verdict": "UNKNOWN",
                    "error": "invalid JSON after retry", "stdout": stdout,
                    "execution": runtime,
                }
        return {
            "phase": "challenge", "exit_code": 0,
            "findings": payload["findings"],
            "verdict": payload["verdict"],
            "summary": payload.get("summary", ""),
            "warnings": parse_warnings,
            "stdout": stdout,
            "execution": runtime,
        }
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "challenge", "exit_code": 1, "error": str(exc)}
