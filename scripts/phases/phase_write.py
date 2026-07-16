"""
WRITE phase: the spec-writer model writes ``spec.md`` to disk.

The model receives the brief on stdin (persona ``spec-writer``) and must write
``spec.md`` into *workdir*. This phase then validates the file (existence +
YAML frontmatter) and stages/commits everything as
``write: <feature> — <summary>``.
"""
from adversarial_common import NoProviderAvailable, gitops, run_phase_cmd

from . import (
    enhance_cmd_for_project,
    provider_history,
    raise_no_provider_available,
    resolve_persona,
    runtime_metadata,
    validate_spec_file,
)

__all__ = ["run_write"]


def _short_summary(brief_text, limit=60):
    """Derive a one-line commit summary from the first non-empty brief line."""
    for line in (brief_text or "").splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:limit]
    return "specification"


def run_write(
    brief_text,
    dev_cmd,
    workdir,
    timeout,
    feature,
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
    Run the spec-writer with the brief as input, validate, commit.

    Returns ``{"phase": "write", "exit_code": 0, "commit_sha": "..."}``;
    on failure ``{"phase": "write", "exit_code": 1, "error": "..."}``.
    *run* preserves the legacy injectable execution hook used by unit tests.
    """
    try:
        prompt = (
            "Write a complete specification for the brief below.\n"
            "Write the file `spec.md` to disk in the current working directory, "
            "with YAML frontmatter (name, version, author, status, tags, targets), "
            "then the sections: Problem, Requirements, Acceptance criteria.\n"
            "Do not print the spec body to stdout — write it to disk.\n\n"
            f"Brief:\n\n{brief_text}"
        )
        if run is not None:
            result = run(
                dev_cmd, prompt, "spec-writer", timeout, workdir, phase="write"
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
                phase_name="write",
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
                "phase": "write",
                "exit_code": 1,
                "error": f"WRITE exited {code}: {(stderr or '')[:200]}",
                "stdout": stdout,
                "execution": runtime,
                "provider_history": history,
            }
        ok, err = validate_spec_file(workdir)
        if not ok:
            return {
                "phase": "write",
                "exit_code": 2,
                "error": f"spec validation failed: {err}",
                "stdout": stdout,
                "execution": runtime,
                "provider_history": history,
            }
        gitops.commit_all(workdir, f"write: {feature} — {_short_summary(brief_text)}")
        return {
            "phase": "write",
            "exit_code": 0,
            "commit_sha": gitops.head_sha(workdir),
            "stdout": stdout,
            "execution": runtime,
            "provider_history": history,
        }
    except NoProviderAvailable:
        raise
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "write", "exit_code": 1, "error": str(exc)}
