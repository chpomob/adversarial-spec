"""
VERIFY phase: the spec-challenger checks whether findings are resolved.

The revised ``spec.md`` and the findings are embedded in the prompt; the
model marks each finding resolved / rejected / disputed and gives an overall
APPROVE/REJECT verdict. JSON extraction uses the shared 3-strategy parser;
one retry with a stricter instruction on invalid JSON.
"""
import json
from pathlib import Path

from . import run_role, try_parse_json

__all__ = ["run_verify"]

_VALID_VERDICTS = {"APPROVE", "REJECT"}
_VALID_STATUS = {"resolved", "rejected", "disputed"}


def _validate(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("verdict") not in _VALID_VERDICTS:
        return False
    results = payload.get("results")
    if not isinstance(results, list):
        return False
    for item in results:
        if not isinstance(item, dict):
            return False
        if item.get("status") not in _VALID_STATUS:
            return False
    return True


def run_verify(findings, review_cmd, workdir, timeout, run=None):
    """
    Run the spec-challenger in VERIFY mode against the revised spec.

    Returns ``{"phase": "verify", "exit_code": 0, "results": [...],
    "verdict": "APPROVE|REJECT"}``; on failure ``{"phase": "verify",
    "exit_code": 1, "error": "..."}``. *run* is injectable for tests.
    """
    run = run or run_role
    try:
        spec_text = (Path(workdir) / "spec.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"phase": "verify", "exit_code": 1,
                "error": f"could not read spec.md: {exc}"}

    prompt = (
        "The specification `spec.md` was revised to address the findings "
        "below. For each finding, decide whether it is **resolved** (the spec "
        "now addresses it), **rejected** (the finding was wrong), or "
        "**disputed** (still open / unclear). You may also run "
        "`git diff HEAD~1..HEAD` in the current directory to see the exact "
        "revision.\n\n"
        f"Findings:\n{json.dumps(findings, indent=2)}\n\n"
        "Output ONLY valid JSON:\n"
        '{"results": [{"id": "S1", "status": "resolved|rejected|disputed", '
        '"note": "optional"}], "verdict": "APPROVE|REJECT"}\n\n'
        f"--- revised spec.md ---\n{spec_text}"
    )

    def _attempt(prompt_text):
        stdout, stderr, code = run(
            review_cmd, prompt_text, "spec-challenger", timeout, workdir)
        if code != 0:
            return None, f"VERIFY exited {code}: {(stderr or '')[:200]}", stdout
        return try_parse_json(stdout), None, stdout

    try:
        payload, err, stdout = _attempt(prompt)
        if err:
            return {"phase": "verify", "exit_code": 1, "error": err,
                    "stdout": stdout}
        if not _validate(payload):
            payload, err, stdout = _attempt(
                prompt + "\n\nIMPORTANT: Respond with raw JSON only. "
                         "No markdown, no code fences, no explanations."
            )
            if err:
                return {"phase": "verify", "exit_code": 1, "error": err,
                        "stdout": stdout}
            if not _validate(payload):
                return {
                    "phase": "verify", "exit_code": 1,
                    "results": [], "verdict": "UNKNOWN",
                    "error": "invalid JSON after retry", "stdout": stdout,
                }
        return {
            "phase": "verify", "exit_code": 0,
            "results": payload.get("results", []),
            "verdict": payload.get("verdict", "REJECT"),
            "stdout": stdout,
        }
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "verify", "exit_code": 1, "error": str(exc)}
