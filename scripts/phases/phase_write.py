"""
WRITE phase: the spec-writer model writes ``spec.md`` to disk.

The model receives the brief on stdin (persona ``spec-writer``) and must write
``spec.md`` into *workdir*. This phase then validates the file (existence +
YAML frontmatter) and stages/commits everything as
``write: <feature> — <summary>``.
"""
from adversarial_common import gitops

from . import run_role, runtime_metadata, validate_spec_file

__all__ = ["run_write"]


def _short_summary(brief_text, limit=60):
    """Derive a one-line commit summary from the first non-empty brief line."""
    for line in (brief_text or "").splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:limit]
    return "specification"


def run_write(brief_text, dev_cmd, workdir, timeout, feature, run=None):
    """
    Run the spec-writer with the brief as input, validate, commit.

    Returns ``{"phase": "write", "exit_code": 0, "commit_sha": "..."}``;
    on failure ``{"phase": "write", "exit_code": 1, "error": "..."}``.
    *run* is injectable for tests and defaults to :func:`run_role`.
    """
    run = run or run_role
    try:
        prompt = (
            "Write a complete specification for the brief below.\n"
            "Write the file `spec.md` to disk in the current working directory, "
            "with YAML frontmatter (name, version, author, status, tags, targets), "
            "then the sections: Problem, Requirements, Acceptance criteria.\n"
            "Do not print the spec body to stdout — write it to disk.\n\n"
            f"Brief:\n\n{brief_text}"
        )
        result = run(dev_cmd, prompt, "spec-writer", timeout, workdir, phase="write")
        stdout, stderr, code = result[0], result[1], result[2]
        runtime = runtime_metadata(result)
        if code != 0:
            return {
                "phase": "write",
                "exit_code": 1,
                "error": f"WRITE exited {code}: {(stderr or '')[:200]}",
                "stdout": stdout,
                "execution": runtime,
            }
        ok, err = validate_spec_file(workdir)
        if not ok:
            return {
                "phase": "write",
                "exit_code": 2,
                "error": f"spec validation failed: {err}",
                "stdout": stdout,
                "execution": runtime,
            }
        gitops.commit_all(workdir, f"write: {feature} — {_short_summary(brief_text)}")
        return {
            "phase": "write",
            "exit_code": 0,
            "commit_sha": gitops.head_sha(workdir),
            "stdout": stdout,
            "execution": runtime,
        }
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "write", "exit_code": 1, "error": str(exc)}
