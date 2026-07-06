"""
REVISE phase: the spec-writer amends ``spec.md`` on disk from findings.

The findings are passed as JSON in the prompt; the model edits ``spec.md``
in place (keeping requirement/criterion ids stable). The file is re-validated
and committed as ``revise: <feature> — round N``.
"""
import json

from adversarial_common import gitops

from . import run_role, validate_spec_file

__all__ = ["run_revise"]


def run_revise(findings, dev_cmd, workdir, timeout, feature, round_n, run=None):
    """
    Run the spec-writer in FIX mode against the challenger's findings.

    Returns ``{"phase": "revise", "exit_code": 0, "commit_sha": "..."}``;
    on failure ``{"phase": "revise", "exit_code": 1, "error": "..."}``.
    *run* is injectable for tests and defaults to :func:`run_role`.
    """
    run = run or run_role
    try:
        prompt = (
            "Revise the specification `spec.md` in the current working "
            "directory to address every finding below. Edit the file on disk "
            "— do not rewrite it from scratch unless a blocker forces it, and "
            "keep existing requirement/criterion ids stable.\n"
            "Do not print the spec body to stdout.\n\n"
            f"Findings:\n{json.dumps(findings, indent=2)}"
        )
        stdout, stderr, code = run(dev_cmd, prompt, "spec-writer", timeout, workdir)
        if code != 0:
            return {
                "phase": "revise",
                "exit_code": 1,
                "error": f"REVISE exited {code}: {(stderr or '')[:200]}",
                "stdout": stdout,
            }
        ok, err = validate_spec_file(workdir)
        if not ok:
            return {
                "phase": "revise",
                "exit_code": 1,
                "error": f"spec validation failed after revise: {err}",
                "stdout": stdout,
            }
        gitops.commit_all(workdir, f"revise: {feature} — round {round_n}")
        return {
            "phase": "revise",
            "exit_code": 0,
            "commit_sha": gitops.head_sha(workdir),
            "stdout": stdout,
        }
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "revise", "exit_code": 1, "error": str(exc)}
