"""
CHALLENGE phase: the spec-challenger model reviews ``spec.md`` from disk.

The prompt directs the provider to the file in its working directory instead
of embedding the spec text. Output is validated JSON findings; one retry with
a stricter instruction on invalid JSON.
"""
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
        "Read and challenge `spec.md` from the current working directory. "
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


def run_challenge(
    review_cmd,
    workdir,
    timeout,
    run=None,
    branch_point="",
    resolver=None,
    *,
    explicit_cmd=None,
    force=False,
    force_provider=None,
    execution=None,
    ledger=None,
):
    """
    Run the spec-challenger against ``<workdir>/spec.md``.

    Returns ``{"phase": "challenge", "exit_code": 0, "findings": [...],
    "verdict": "..."}``; on failure ``{"phase": "challenge", "exit_code": 1,
    "error": "..."}``. *run* is injectable for tests.
    """
    try:
        # Fail before invoking a provider if the on-disk input is unavailable.
        # Do not append this content to the prompt: the challenger reads the
        # file from ``workdir``.
        (Path(workdir) / "spec.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"phase": "challenge", "exit_code": 1,
                "error": f"could not read spec.md: {exc}"}

    prompt = _build_prompt(branch_point)
    parse_warnings = []
    provider_results = []

    def _attempt(prompt_text):
        if run is not None:
            result = run(
                review_cmd, prompt_text, "spec-challenger", timeout, workdir,
                phase="challenge",
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
                phase_name="challenge",
                role="challenger",
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
                result, "challenger", provider_results=provider_results
            )
        stdout, stderr, code = result[0], result[1], result[2]
        if code != 0:
            return None, f"CHALLENGE exited {code}: {(stderr or '')[:200]}", stdout, runtime_metadata(result)
        return try_parse_json(stdout, warnings=parse_warnings), None, stdout, runtime_metadata(result)

    try:
        payload, err, stdout, runtime = _attempt(prompt)
        if err:
            return {"phase": "challenge", "exit_code": 1, "error": err,
                    "stdout": stdout, "execution": runtime,
                    "provider_history": provider_history(*provider_results)}
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
                        "stdout": stdout, "execution": runtime,
                        "provider_history": provider_history(*provider_results)}
            if not _validate(payload):
                return {
                    "phase": "challenge", "exit_code": 1,
                    "findings": [], "verdict": "UNKNOWN",
                    "error": "invalid JSON after retry", "stdout": stdout,
                    "execution": runtime,
                    "provider_history": provider_history(*provider_results),
                }
        return {
            "phase": "challenge", "exit_code": 0,
            "findings": payload["findings"],
            "verdict": payload["verdict"],
            "summary": payload.get("summary", ""),
            "warnings": parse_warnings,
            "stdout": stdout,
            "execution": runtime,
            "provider_history": provider_history(*provider_results),
        }
    except NoProviderAvailable:
        raise
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "challenge", "exit_code": 1, "error": str(exc)}
