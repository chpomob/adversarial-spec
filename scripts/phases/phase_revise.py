"""
REVISE phase: the spec-writer amends ``spec.md`` on disk from findings.

The findings are passed as JSON in the prompt; the model edits ``spec.md``
in place (keeping requirement/criterion ids stable). The file is re-validated
and committed as ``revise: <feature> — round N``.
"""
import json

from adversarial_common import NoProviderAvailable, gitops, run_phase_cmd

from . import (
    enhance_cmd_for_project,
    provider_history,
    raise_no_provider_available,
    resolve_persona,
    runtime_metadata,
    validate_spec_file,
)

__all__ = ["run_revise"]


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
        prompt = (
            "Revise the specification `spec.md` in the current working "
            "directory to address every finding below. Edit the file on disk "
            "— do not rewrite it from scratch unless a blocker forces it, and "
            "keep existing requirement/criterion ids stable.\n"
            "Do not print the spec body to stdout.\n\n"
            f"Findings:\n{json.dumps(findings, indent=2)}"
        )
        phase_name = f"revise_{round_n}"
        if run is not None:
            result = run(
                dev_cmd, prompt, "spec-writer", timeout, workdir,
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
                stdin_text=prompt,
                timeout=timeout,
                persona="spec-writer",
                persona_file=resolve_persona("spec-writer", persona_cmd),
                **command_args,
                **execution_args,
            )
            raise_no_provider_available(result, "writer")
        stdout, stderr, code = result[0], result[1], result[2]
        runtime = runtime_metadata(result)
        history = provider_history(result)
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
