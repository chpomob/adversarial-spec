"""
VERIFY phase: the spec-challenger checks whether findings are resolved.

The findings are written to a JSON file on disk (outside the workdir). The
prompt directs the model to read ``spec.md`` from the workdir and the
findings file via READ markers; no payload is embedded.  JSON extraction
uses the shared 3-strategy parser; one retry with a stricter instruction on
invalid JSON.  A ReadGatePolicy enforces proof-of-read with WARNING →
HARD_ERROR escalation.
"""
import json
import tempfile
from pathlib import Path

from adversarial_common import NoProviderAvailable, run_phase_cmd

from . import (
    ReadGatePolicy,
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

def _verify_tmpdir():
    """Create a unique temp dir per run (avoids symlink races and cross-run collisions)."""
    return Path(tempfile.mkdtemp(prefix="adversarial-spec-verify-"))

_READGATE_REMINDER = (
    "\n\nIMPORTANT: You must read the required files from disk before "
    "responding. Include 'READ: <path>' markers in your output to confirm "
    "you read each file. For example: 'READ: spec.md' or "
    "'READ: /tmp/adversarial-spec/verify_findings.json'"
)


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
        # Verify spec.md exists (model reads it from disk, so we only
        # check availability — content is never embedded).
        (Path(workdir) / "spec.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"phase": "verify", "exit_code": 1,
                "error": f"could not read spec.md: {exc}"}

    # Write findings to a temp file outside the workdir.
    findings_dir = _verify_tmpdir()
    findings_path = findings_dir / "verify_findings.json"
    findings_path.write_text(json.dumps(findings, indent=2), encoding="utf-8")

    diff_base = branch_point or "<branch-point>"
    spec_marker = "spec.md"
    findings_marker = str(findings_path)
    prompt = (
        "Read `spec.md` from the current working directory and the findings "
        f"file at `{findings_marker}`.\n\n"
        "The spec was revised to address the findings. For each finding, "
        "decide whether it is **resolved** (the spec now addresses it), "
        "**rejected** (the finding was wrong), or **disputed** (still open "
        "/ unclear). You may also run the cumulative diff with "
        f"`git diff {diff_base}..HEAD`.\n\n"
        "Include 'READ: spec.md' and 'READ: "
        f"{findings_marker}' markers in your response.\n\n"
        "Output ONLY valid JSON:\n"
        '{"results": [{"id": "S1", "status": "resolved|rejected|disputed", '
        '"note": "optional"}], "verdict": "APPROVE|REJECT"}'
    )
    provider_results = []
    readgate = ReadGatePolicy()
    readgate_retried = False

    def _check_readgate(stdout):
        """Check stdout for READ markers on both required files."""
        r1 = readgate.check(stdout, spec_marker)
        r2 = readgate.check(stdout, findings_marker)
        # Return the worst status and the list of paths that missed.
        statuses = ["pass", "WARNING", "HARD_ERROR"]
        worst = max(r1.status, r2.status, key=lambda s: statuses.index(s))
        missing = []
        if r1.status != "pass":
            missing.append(spec_marker)
        if r2.status != "pass":
            missing.append(findings_marker)
        return worst, missing

    def _attempt(prompt_text, check_readgate=True):
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
            return None, f"VERIFY exited {code}: {(stderr or '')[:200]}", stdout, runtime_metadata(result), "pass"
        payload = try_parse_json(stdout)
        if check_readgate:
            rg_status, _rg_missing = _check_readgate(stdout)
        else:
            rg_status = "pass"
        return payload, None, stdout, runtime_metadata(result), rg_status

    try:
        payload, err, stdout, runtime, rg_status = _attempt(prompt)
        if rg_status == "HARD_ERROR":
            return {"phase": "verify", "exit_code": 1,
                    "error": "readgate HARD_ERROR: missing READ markers on "
                            "consecutive attempts",
                    "stdout": stdout, "execution": runtime,
                    "provider_history": provider_history(*provider_results)}
        if rg_status == "WARNING":
            readgate_retried = True
            prev_runtime = runtime
            payload, err, stdout, runtime, rg_status = _attempt(
                prompt + _READGATE_REMINDER
            )
            runtime = merge_runtime(prev_runtime, runtime)
            if rg_status == "HARD_ERROR":
                return {"phase": "verify", "exit_code": 1,
                        "error": "readgate HARD_ERROR: missing READ markers "
                                "after retry",
                        "stdout": stdout, "execution": runtime,
                        "provider_history": provider_history(*provider_results)}
        if err:
            return {"phase": "verify", "exit_code": 1, "error": err,
                    "stdout": stdout, "execution": runtime,
                    "provider_history": provider_history(*provider_results)}
        if not _validate(payload):
            prev_runtime = runtime
            retry_prompt = (
                prompt
                + ("" if not readgate_retried else _READGATE_REMINDER)
                + "\n\nIMPORTANT: Respond with raw JSON only. "
                  "No markdown, no code fences, no explanations."
            )
            payload, err, stdout, runtime, _rg = _attempt(retry_prompt,
                                                          check_readgate=False)
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
