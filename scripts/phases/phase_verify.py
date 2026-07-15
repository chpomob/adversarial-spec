"""
VERIFY phase: the spec-challenger checks whether findings are resolved.

The revised ``spec.md`` and the findings are embedded in the prompt; the
model marks each finding resolved / rejected / disputed and gives an overall
APPROVE/REJECT verdict. JSON extraction uses the shared 3-strategy parser;
one retry with a stricter instruction on invalid JSON.
"""
import json
from pathlib import Path

from . import run_role, runtime_metadata, try_parse_json, merge_runtime

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


def run_verify(findings, review_cmd, workdir, timeout, run=None, branch_point="", round_n=None):
    """
    Run the spec-challenger in VERIFY mode against the revised spec.

    Returns ``{"phase": "verify", "exit_code": 0, "results": [...],
    "verdict": "APPROVE|REJECT"}``; on failure ``{"phase": "verify",
    "exit_code": 1, "error": "..."}``. *run* is injectable for tests.
    *round_n* tags the per-round cost bucket (``verify_<n>``) so the shared
    ledger can attribute cost to a specific verify round.
    """
    run = run or run_role
    verify_phase = f"verify_{round_n}" if round_n else "verify"
    try:
        spec_text = (Path(workdir) / "spec.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"phase": "verify", "exit_code": 1,
                "error": f"could not read spec.md: {exc}"}

    diff_base = branch_point or "<branch-point>"
    prompt = (
        "The specification `spec.md` was revised to address the findings "
        "below. For each finding, decide whether it is **resolved** (the spec "
        "now addresses it), **rejected** (the finding was wrong), or "
        "**disputed** (still open / unclear). You may also run "
        "the cumulative diff in the current directory with "
        f"`git diff {diff_base}..HEAD` to see the exact "
        "revision.\n\n"
        f"Findings:\n{json.dumps(findings, indent=2)}\n\n"
        "Output ONLY valid JSON:\n"
        '{"results": [{"id": "S1", "status": "resolved|rejected|disputed", '
        '"note": "optional"}], "verdict": "APPROVE|REJECT"}\n\n'
        f"--- revised spec.md ---\n{spec_text}"
    )

    def _attempt(prompt_text):
        result = run(
            review_cmd, prompt_text, "spec-challenger", timeout, workdir,
            phase=verify_phase)
        stdout, stderr, code = result[0], result[1], result[2]
        if code != 0:
            return None, f"VERIFY exited {code}: {(stderr or '')[:200]}", stdout, runtime_metadata(result)
        return try_parse_json(stdout), None, stdout, runtime_metadata(result)

    try:
        payload, err, stdout, runtime = _attempt(prompt)
        if err:
            return {"phase": "verify", "exit_code": 1, "error": err,
                    "stdout": stdout, "execution": runtime}
        if not _validate(payload):
            prev_runtime = runtime
            payload, err, stdout, runtime = _attempt(
                prompt + "\n\nIMPORTANT: Respond with raw JSON only. "
                         "No markdown, no code fences, no explanations."
            )
            runtime = merge_runtime(prev_runtime, runtime)
            if err:
                return {"phase": "verify", "exit_code": 1, "error": err,
                        "stdout": stdout, "execution": runtime}
            if not _validate(payload):
                return {
                    "phase": "verify", "exit_code": 1,
                    "results": [], "verdict": "UNKNOWN",
                    "error": "invalid JSON after retry", "stdout": stdout,
                    "execution": runtime,
                }
        return {
            "phase": "verify", "exit_code": 0,
            "results": payload.get("results", []),
            "verdict": payload.get("verdict", "REJECT"),
            "stdout": stdout,
            "execution": runtime,
        }
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "verify", "exit_code": 1, "error": str(exc)}
