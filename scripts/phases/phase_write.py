"""
WRITE phase: the spec-writer model writes ``spec.md`` to disk.

The model receives the brief.  When the brief exceeds 2000 bytes (UTF-8
encoded), it is written to a temp file outside the workdir and the prompt
carries a READ marker instead of the inline text.  This phase validates the
file and stages/commits everything as ``write: <feature> — <summary>``.
A ReadGatePolicy enforces proof-of-read when the brief is file-based.
"""
import tempfile
from pathlib import Path

from adversarial_common import NoProviderAvailable, gitops, run_phase_cmd

from . import (
    ReadGatePolicy,
    enhance_cmd_for_project,
    merge_runtime,
    provider_history,
    raise_no_provider_available,
    resolve_persona,
    runtime_metadata,
    validate_spec_file,
)

__all__ = ["run_write"]

_BRIEF_SIZE_THRESHOLD = 2000
def _write_tmpdir():
    """Create a unique temp dir per run (avoids symlink races and cross-run collisions)."""
    return Path(tempfile.mkdtemp(prefix="adversarial-spec-write-"))

_READGATE_REMINDER = (
    "\n\nIMPORTANT: You must read the brief from disk before writing. "
    "Include 'READ: <path>' markers in your output to confirm you read "
    "each file. For example: 'READ: /tmp/adversarial-spec/brief.md'"
)


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
    max_spec_retries=3,
):
    """
    Run the spec-writer with the brief as input, validate, commit.

    Returns ``{"phase": "write", "exit_code": 0, "commit_sha": "..."}``;
    on failure ``{"phase": "write", "exit_code": 1, "error": "..."}``.
    *run* preserves the legacy injectable execution hook used by unit tests.

    On validation failure, the validator error is fed back to the spec-writer
    and the write is retried up to *max_spec_retries* times. A valid spec on
    the first try exits immediately with zero retries.
    """
    try:
        # Decide whether to use a file-based brief.
        brief_bytes = (brief_text or "").encode("utf-8")
        file_based = len(brief_bytes) > _BRIEF_SIZE_THRESHOLD
        brief_marker = None
        if file_based:
            tmpdir = _write_tmpdir()
            brief_path = tmpdir / "brief.md"
            brief_path.write_text(brief_text, encoding="utf-8")
            brief_marker = str(brief_path)

        if file_based:
            base_prompt = (
                "Read the brief from the file at this path before writing: "
                f"`{brief_marker}`.\n\n"
                "Write a complete specification for the brief.\n"
                "Write the file `spec.md` to disk in the current working "
                "directory, with YAML frontmatter (name, version, author, "
                "status, tags, targets), then the sections: Problem, "
                "Requirements, Acceptance criteria.\n"
                "Do not print the spec body to stdout — write it to disk.\n\n"
                f"Include 'READ: {brief_marker}' markers in your response."
            )
        else:
            base_prompt = (
                "Write a complete specification for the brief below.\n"
                "Write the file `spec.md` to disk in the current working "
                "directory, with YAML frontmatter (name, version, author, "
                "status, tags, targets), then the sections: Problem, "
                "Requirements, Acceptance criteria.\n"
                "Do not print the spec body to stdout — write it to disk.\n\n"
                f"Brief:\n\n{brief_text}"
            )
        prompt = base_prompt
        readgate = ReadGatePolicy() if file_based else None

        last_error = None
        last_stdout = ""
        prev_runtime = {}
        all_results = []
        readgate_checked = False  # only check readgate on first attempt

        # Pre-compute provider args (stable across retries).
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

        for attempt in range(1, max_spec_retries + 2):
            if run is not None:
                result = run(
                    dev_cmd, prompt, "spec-writer", timeout, workdir, phase="write"
                )
            else:
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
            all_results.append(result)
            prev_runtime = merge_runtime(prev_runtime, runtime_metadata(result))

            # Readgate check (file-based brief only, first attempt only).
            if readgate is not None and not readgate_checked:
                readgate_checked = True
                rg = readgate.check(stdout, brief_marker)
                if rg.status == "HARD_ERROR":
                    return {
                        "phase": "write", "exit_code": 1,
                        "error": "readgate HARD_ERROR: missing READ marker "
                                "on consecutive attempts",
                        "stdout": stdout, "execution": prev_runtime,
                        "provider_history": provider_history(*all_results),
                    }
                if rg.status == "WARNING":
                    # Re-run with readgate reminder.
                    rem_prompt = prompt + _READGATE_REMINDER
                    if run is not None:
                        result = run(dev_cmd, rem_prompt, "spec-writer",
                                     timeout, workdir, phase="write")
                    else:
                        result = run_phase_cmd(
                            phase_name="write", role="writer",
                            workdir=workdir, resolver=resolver,
                            explicit_cmd=selected_explicit,
                            force=force, force_provider=force_provider,
                            stdin_text=rem_prompt, timeout=timeout,
                            persona="spec-writer",
                            persona_file=resolve_persona("spec-writer", persona_cmd),
                            **command_args, **execution_args,
                        )
                        raise_no_provider_available(result, "writer")
                    stdout, stderr, code = result[0], result[1], result[2]
                    all_results.append(result)
                    prev_runtime = merge_runtime(
                        prev_runtime, runtime_metadata(result))
                    rg2 = readgate.check(stdout, brief_marker)
                    if rg2.status == "HARD_ERROR":
                        return {
                            "phase": "write", "exit_code": 1,
                            "error": "readgate HARD_ERROR: missing READ "
                                    "marker after retry",
                            "stdout": stdout, "execution": prev_runtime,
                            "provider_history": provider_history(*all_results),
                        }

            if code != 0:
                return {
                    "phase": "write",
                    "exit_code": 1,
                    "error": f"WRITE exited {code}: {(stderr or '')[:200]}",
                    "stdout": stdout,
                    "execution": prev_runtime,
                    "provider_history": provider_history(*all_results),
                }
            ok, err = validate_spec_file(workdir)
            if ok:
                gitops.commit_all(workdir, f"write: {feature} — {_short_summary(brief_text)}")
                return {
                    "phase": "write",
                    "exit_code": 0,
                    "commit_sha": gitops.head_sha(workdir),
                    "stdout": stdout,
                    "execution": prev_runtime,
                    "provider_history": provider_history(*all_results),
                }
            last_error = err
            last_stdout = stdout
            if attempt <= max_spec_retries:
                print(f"  X spec validation failed (attempt {attempt}): {err}")
                print(f"  -> retrying ({attempt}/{max_spec_retries})...")
                correction = (
                    f"=== CORRECTION FEEDBACK ===\n"
                    f"Your previous spec.md was rejected by validation:\n"
                    f"  {err}\n\n"
                    f"Fix these issues and write the corrected spec.md to disk.\n"
                    f"Do not print the spec body to stdout — write it to disk."
                )
                if file_based:
                    correction = (
                        f"=== CORRECTION FEEDBACK ===\n"
                        f"Your previous spec.md was rejected by validation:\n"
                        f"  {err}\n\n"
                        f"Re-read the brief from `{brief_marker}`, fix the "
                        f"issues above, and write the corrected spec.md to "
                        f"disk. Do not print the spec body to stdout.\n"
                        f"Include 'READ: {brief_marker}' in your response."
                    )
                prompt = f"{base_prompt}\n\n{correction}"

        # all retries exhausted
        return {
            "phase": "write",
            "exit_code": 2,
            "error": f"spec validation failed after {max_spec_retries + 1} attempts: {last_error}",
            "stdout": last_stdout,
            "execution": prev_runtime,
            "provider_history": provider_history(*all_results),
        }
    except NoProviderAvailable:
        raise
    except Exception as exc:  # defensive: never leak an exception to the loop
        return {"phase": "write", "exit_code": 1, "error": str(exc)}
