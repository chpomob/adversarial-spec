"""
REVISE phase: the spec-writer amends ``spec.md`` on disk from findings.

The findings are written to a JSON file on disk (outside the workdir). The
prompt directs the model to read ``spec.md`` and the findings file via READ
markers; no findings payload is embedded.  The file is re-validated and
committed as ``revise: <feature> — round N``.  A ReadGatePolicy enforces
proof-of-read with WARNING → HARD_ERROR escalation.
"""
import json
import tempfile
from pathlib import Path

from adversarial_common import NoProviderAvailable, gitops, run_phase_cmd

from . import (
    ReadGatePolicy,
    enhance_cmd_for_project,
    provider_history,
    raise_no_provider_available,
    resolve_persona,
    runtime_metadata,
    validate_spec_file,
)

__all__ = ["run_revise"]

def _revise_tmpdir():
    """Create a unique temp dir per run (avoids symlink races and cross-run collisions)."""
    return Path(tempfile.mkdtemp(prefix="adversarial-spec-revise-"))

_READGATE_REMINDER = (
    "\n\nIMPORTANT: You must read the required files from disk before "
    "responding. Include 'READ: <path>' markers in your output to confirm "
    "you read each file. For example: 'READ: spec.md' or "
    "'READ: /tmp/adversarial-spec/revise_findings.json'"
)


def run_revise(
    findings,
    dev_cmd,
    workdir,
    timeout,
    feature,
    round_n,
    run=None,
    resolver=None,
    *,
    explicit_cmd=None,
    force=False,
    force_provider=None,
    execution=None,
    ledger=None,
):
    """
    Run the spec-writer in FIX mode against the challenger's findings.

    Returns ``{"phase": "revise", "exit_code": 0, "commit_sha": "..."}``;
    on failure ``{"phase": "revise", "exit_code": 1, "error": "..."}``.
    *run* preserves the legacy injectable execution hook used by unit tests.
    """
    try:
        # Write findings to a temp file outside the workdir.
        findings_dir = _revise_tmpdir()
        findings_path = findings_dir / "revise_findings.json"
        findings_path.write_text(json.dumps(findings, indent=2), encoding="utf-8")

        findings_marker = str(findings_path)
        prompt = (
            "Read `spec.md` from the current working directory and the "
            f"findings file at `{findings_marker}`.\n\n"
            "Revise `spec.md` to address every finding. Edit the file on "
            "disk — do not rewrite it from scratch unless a blocker forces "
            "it, and keep existing requirement/criterion ids stable.\n"
            "Do not print the spec body to stdout.\n\n"
            "Include 'READ: spec.md' and 'READ: "
            f"{findings_marker}' markers in your response."
        )
        phase_name = f"revise_{round_n}"
        readgate = ReadGatePolicy()

        def _invoke(prompt_text):
            if run is not None:
                result = run(
                    dev_cmd, prompt_text, "spec-writer", timeout, workdir,
                    phase=phase_name,
                )
            else:
                execution_args = dict(execution or {})
                if ledger is not None:
                    execution_args["ledger"] = ledger
                legacy_cmd = enhance_cmd_for_project(dev_cmd, workdir)
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
                    phase_name=phase_name,
                    role="writer",
                    workdir=workdir,
                    resolver=resolver,
                    explicit_cmd=selected_explicit,
                    force=force,
                    force_provider=force_provider,
                    stdin_text=prompt_text,
                    timeout=timeout,
                    persona="spec-writer",
                    persona_file=resolve_persona("spec-writer", persona_cmd),
                    **command_args,
                    **execution_args,
                )
                raise_no_provider_available(result, "writer")
            return result

        result = _invoke(prompt)
        stdout, stderr, code = result[0], result[1], result[2]
        runtime = runtime_metadata(result)
        history = provider_history(result)

        # Readgate check on both files.
        spec_marker = "spec.md"
        rg_spec = readgate.check(stdout, spec_marker)
        rg_find = readgate.check(stdout, findings_marker)
        rg_worst = max(rg_spec.status, rg_find.status,
                       key=lambda s: ["pass", "WARNING", "HARD_ERROR"].index(s))

        if rg_worst == "HARD_ERROR":
            return {
                "phase": "revise", "exit_code": 1,
                "error": "readgate HARD_ERROR: missing READ markers on "
                        "consecutive attempts",
                "stdout": stdout, "execution": runtime,
                "provider_history": history,
            }
        if rg_worst == "WARNING":
            first_result = result
            result = _invoke(prompt + _READGATE_REMINDER)
            stdout, stderr, code = result[0], result[1], result[2]
            runtime = runtime_metadata(result)
            history = provider_history(first_result, result)
            rg_spec2 = readgate.check(stdout, spec_marker)
            rg_find2 = readgate.check(stdout, findings_marker)
            rg_worst2 = max(rg_spec2.status, rg_find2.status,
                            key=lambda s: ["pass", "WARNING", "HARD_ERROR"].index(s))
            if rg_worst2 == "HARD_ERROR":
                return {
                    "phase": "revise", "exit_code": 1,
                    "error": "readgate HARD_ERROR: missing READ markers "
                            "after retry",
                    "stdout": stdout, "execution": runtime,
                    "provider_history": history,
                }

        if code != 0:
            return {
                "phase": "revise",
                "exit_code": 1,
                "error": f"REVISE exited {code}: {(stderr or '')[:200]}",
                "stdout": stdout,
                "execution": runtime,
                "provider_history": history,
            }
        ok, err = validate_spec_file(workdir)
        if not ok:
            return {
                "phase": "revise",
                "exit_code": 1,
                "error": f"spec validation failed after revise: {err}",
                "stdout": stdout,
                "execution": runtime,
                "provider_history": history,
            }
        gitops.commit_all(workdir, f"revise: {feature} — round {round_n}")
        return {
            "phase": "revise",
            "exit_code": 0,
            "commit_sha": gitops.head_sha(workdir),
            "stdout": stdout,
            "execution": runtime,
            "provider_history": history,
        }
    except NoProviderAvailable:
        raise
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "revise", "exit_code": 1, "error": str(exc)}
