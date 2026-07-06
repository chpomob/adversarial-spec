"""
CHALLENGE phase: the spec-challenger model reviews ``spec.md``.

The spec text is embedded in the prompt (model-agnostic: works even for
providers without file access) and the file is also on disk for providers
that can read it. Output is validated JSON findings; one retry with a
stricter instruction on invalid JSON.
"""
from pathlib import Path

from . import run_role, try_parse_json

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


def _build_prompt(spec_text):
    return (
        "Challenge the specification below (also on disk at `spec.md` in the "
        "current directory).\n"
        "Look for, in priority order: missing requirements, contradictions, "
        "untestable acceptance criteria, scope creep, ambiguous wording, "
        "formatting/consistency issues.\n\n"
        "Output ONLY valid JSON:\n"
        '{"findings": [{"id": "S1", "severity": "blocker|major|minor|nit", '
        '"section": "Problem|Requirements|Acceptance criteria|targets|frontmatter", '
        '"summary": "one-line issue", "evidence": "exact spec text or id"}], '
        '"verdict": "REQUEST_CHANGES|APPROVE|REJECT", '
        '"summary": "counts by severity"}\n\n'
        f"--- spec.md ---\n{spec_text}"
    )


def run_challenge(review_cmd, workdir, timeout, run=None):
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

    prompt = _build_prompt(spec_text)

    def _attempt(prompt_text):
        stdout, stderr, code = run(
            review_cmd, prompt_text, "spec-challenger", timeout, workdir)
        if code != 0:
            return None, f"CHALLENGE exited {code}: {(stderr or '')[:200]}", stdout
        return try_parse_json(stdout), None, stdout

    try:
        payload, err, stdout = _attempt(prompt)
        if err:
            return {"phase": "challenge", "exit_code": 1, "error": err,
                    "stdout": stdout}
        if not _validate(payload):
            payload, err, stdout = _attempt(
                prompt + "\n\nIMPORTANT: Respond with raw JSON only, matching "
                         "the schema exactly. No markdown, no code fences, "
                         "no explanations."
            )
            if err:
                return {"phase": "challenge", "exit_code": 1, "error": err,
                        "stdout": stdout}
            if not _validate(payload):
                return {
                    "phase": "challenge", "exit_code": 1,
                    "findings": [], "verdict": "UNKNOWN",
                    "error": "invalid JSON after retry", "stdout": stdout,
                }
        return {
            "phase": "challenge", "exit_code": 0,
            "findings": payload["findings"],
            "verdict": payload["verdict"],
            "summary": payload.get("summary", ""),
            "stdout": stdout,
        }
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "challenge", "exit_code": 1, "error": str(exc)}
