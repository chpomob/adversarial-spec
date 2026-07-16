"""
VERIFY phase: the spec-challenger checks whether findings are resolved.

The revised ``spec.md`` and the findings are embedded in the prompt; the
model marks each finding resolved / rejected / disputed and gives an overall
APPROVE/REJECT verdict. JSON extraction uses the shared 3-strategy parser;
one retry with a stricter instruction on invalid JSON.
"""
import json
from pathlib import Path

from adversarial_common import NoProviderAvailable, run_phase_cmd

from . import (
    enhance_cmd_for_project,
    merge_runtime,
    provider_history,
    raise_no_provider_available,
    resolve_persona,
    runtime_metadata,
    try_parse_json,
)

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


def run_verify(
    findings,
    review_cmd,
    workdir,
    timeout,
    run=None,
    branch_point="",
    round_n=None,
    resolver=None,
    *,
    explicit_cmd=None,
    force=False,
    force_provider=None,
    execution=None,
    ledger=None,
):
    """
    Run the spec-challenger in VERIFY mode against the revised spec.

    Returns ``{"phase": "verify", "exit_code": 0, "results": [...],
    "verdict": "APPROVE|REJECT"}``; on failure ``{"phase": "verify",
    "exit_code": 1, "error": "..."}``. *run* is injectable for tests.
    *round_n* tags the per-round cost bucket (``verify_<n>``) so the shared
    ledger can attribute cost to a specific verify round.
    """
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
    provider_results = []

    def _attempt(prompt_text):
        if run is not None:
            result = run(
                review_cmd, prompt_text, "spec-challenger", timeout, workdir,
                phase=verify_phase,
            )
        else:
            execution_args = dict(execution or {})
            if ledger is not None:
                execution_args["ledger"] = ledger
            legacy_cmd = enhance_cmd_for_project(review_cmd, workdir)
            selected_explicit = (
                enhance_cmd_for_project(explicit_cmd, workdir)
                if explicit_cmd is not None else None
            )
            command_args = {}
            if resolver is None and explicit_cmd is None:
                command_args["cmd"] = legacy_cmd
            persona_cmd = selected_explicit or (
                legacy_cmd if resolver is None else ""
            )
            result = run_phase_cmd(
                phase_name=verify_phase,
                role="verify",
                workdir=workdir,
                resolver=resolver,
                explicit_cmd=selected_explicit,
                force=force,
                force_provider=force_provider,
                stdin_text=prompt_text,
                timeout=timeout,
                persona="spec-challenger",
                persona_file=resolve_persona("spec-challenger", persona_cmd),
                **command_args,
                **execution_args,
            )
            provider_results.append(result)
            raise_no_provider_available(
                result, "verify", provider_results=provider_results
            )
        stdout, stderr, code = result[0], result[1], result[2]
        if code != 0:
            return None, f"VERIFY exited {code}: {(stderr or '')[:200]}", stdout, runtime_metadata(result)
        return try_parse_json(stdout), None, stdout, runtime_metadata(result)

    try:
        payload, err, stdout, runtime = _attempt(prompt)
        if err:
            return {"phase": "verify", "exit_code": 1, "error": err,
                    "stdout": stdout, "execution": runtime,
                    "provider_history": provider_history(*provider_results)}
        if not _validate(payload):
            prev_runtime = runtime
            payload, err, stdout, runtime = _attempt(
                prompt + "\n\nIMPORTANT: Respond with raw JSON only. "
                         "No markdown, no code fences, no explanations."
            )
            runtime = merge_runtime(prev_runtime, runtime)
            if err:
                return {"phase": "verify", "exit_code": 1, "error": err,
                        "stdout": stdout, "execution": runtime,
                        "provider_history": provider_history(*provider_results)}
            if not _validate(payload):
                return {
                    "phase": "verify", "exit_code": 1,
                    "results": [], "verdict": "UNKNOWN",
                    "error": "invalid JSON after retry", "stdout": stdout,
                    "execution": runtime,
                    "provider_history": provider_history(*provider_results),
                }
        return {
            "phase": "verify", "exit_code": 0,
            "results": payload.get("results", []),
            "verdict": payload.get("verdict", "REJECT"),
            "stdout": stdout,
            "execution": runtime,
            "provider_history": provider_history(*provider_results),
        }
    except NoProviderAvailable:
        raise
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "verify", "exit_code": 1, "error": str(exc)}
