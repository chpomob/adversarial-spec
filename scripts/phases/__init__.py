"""Phase modules for the adversarial-spec pipeline.

One module per phase, one public function per module (same layout as
adversarial-code-loop's ``scripts/phases``):

  phase_write      — spec-writer writes ``spec.md`` to disk (WRITE / BUILD)
  phase_challenge  — spec-challenger reviews ``spec.md`` (CHALLENGE / REVIEW)
  phase_revise     — spec-writer amends ``spec.md`` (REVISE / FIX)
  phase_verify     — spec-challenger checks the findings (VERIFY)

Helpers shared by more than one phase live here:

  run_role()           — persona-aware CLI execution with base-name fallback
  runtime_metadata()   — copy JSON-safe runner evidence from a RunResult
  try_parse_json()     — 3-strategy JSON extraction (fences, ``{..}``, ``[..]``)
  validate_spec_file() — ``spec.md`` existence + YAML frontmatter validation
"""

import sys
from collections.abc import Mapping
from pathlib import Path

# The adversarial-common sibling skill must be importable. The orchestrator
# inserts it on sys.path before importing us; the fallback below keeps the
# package importable on its own (tests, REPL).
try:
    from adversarial_common import (
        NoProviderAvailable,
        collect_provider_history,
        jsonio,
        persona_path,
        runner,
    )
    from adversarial_common.providers import enhance_cmd_for_project, persona_for_role
except ImportError:  # pragma: no cover - exercised only on bare imports
    _COMMON = Path(__file__).resolve().parents[3] / "adversarial-common"
    sys.path.insert(0, str(_COMMON))
    from adversarial_common import (
        NoProviderAvailable,
        collect_provider_history,
        jsonio,
        persona_path,
        runner,
    )
    from adversarial_common.providers import enhance_cmd_for_project, persona_for_role
from .phase_spec import validate_spec_file, validate_spec_text

__all__ = [
    "run_role",
    "resolve_persona",
    "runtime_metadata",
    "merge_runtime",
    "provider_history",
    "raise_no_provider_available",
    "try_parse_json",
    "extract_frontmatter",
    "validate_spec_file",
    "validate_spec_text",
]


# --- persona-aware execution --------------------------------------------------

def resolve_persona(role, cmd):
    """Absolute persona file path for *role*, or None when none exists.

    ``persona_for_role`` may return a provider-specific variant (e.g.
    ``spec-writer-pi``); when that file does not exist we fall back to the
    base persona instead of silently running without one (unlike
    ``providers.run_cmd``, which drops the persona entirely in that case).
    """
    for name in (persona_for_role(role, cmd), role):
        try:
            return persona_path(name)
        except FileNotFoundError:
            continue
    return None


def runtime_metadata(result):
    """Copy JSON-safe runner evidence without changing tuple compatibility.

    ``runner.run_cli`` returns a tuple-compatible ``RunResult`` whose
    ``metadata`` carries retry attempts, cap events, and native usage. Plain
    tuples (test stubs) safely yield an empty mapping.
    """
    metadata = getattr(result, "metadata", None)
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def merge_runtime(*runtimes):
    """Combine attempt/cap evidence across multiple billed provider calls.

    The bad-JSON retry path in challenge/verify issues two real (billed)
    ``run_cli`` calls but previously surfaced only the second attempt's
    metadata, silently dropping the first attempt's retry/cap events. Lists
    accumulate in call order; scalar usage fields keep the last value (the
    terminal attempt's reconciled usage).
    """
    merged = {}
    attempts = []
    cap_events = []
    for runtime in runtimes:
        if not isinstance(runtime, Mapping):
            continue
        attempts.extend(runtime.get("attempts", []))
        cap_events.extend(runtime.get("cap_events", []))
        merged.update(runtime)
    merged["attempts"] = attempts
    merged["cap_events"] = cap_events
    return merged


def provider_history(*results):
    """Collect quota-provider decisions from calls in execution order."""
    return collect_provider_history(list(results))


def raise_no_provider_available(result, role, provider_results=None):
    """Restore provider exhaustion represented by ``run_phase_cmd`` metadata.

    ``provider_results`` may include earlier attempts from the same phase.  Its
    ordered decisions are attached to the exception so a retry that exhausts
    the provider chain does not discard successful calls made before it.
    """
    metadata = getattr(result, "metadata", None)
    if not isinstance(metadata, Mapping):
        return
    snapshots = metadata.get("raw_snapshots")
    reasons = metadata.get("rejection_reasons")
    if not isinstance(snapshots, Mapping) or not isinstance(reasons, Mapping):
        return
    error = NoProviderAvailable(role, snapshots, reasons)
    decision = metadata.get("provider_decision")
    if isinstance(decision, Mapping):
        error.provider_decision = dict(decision)
    if provider_results is not None:
        error.provider_history = provider_history(*provider_results)
    raise error


def run_role(cmd, prompt, role, timeout, cwd, phase=None, execution=None, ledger=None):
    """Run a role command with its persona injected.

    Returns ``(stdout, stderr, returncode)`` from the hardened
    ``runner.run_cli`` (temp-file IO, process-group kill on timeout). When
    *execution* (retry/caps controls) and *ledger* (a shared ``CostLedger``)
    are supplied they are threaded into the runner so one run-wide ledger
    accounts for every phase and every provider call respects the same caps.

    *role* drives the persona (e.g. ``spec-writer``). *phase*, defaulting to
    *role*, drives the CostLedger's per-stage breakdown; pass the pipeline
    stage (``write``/``revise_2``/...) so per-stage cost is not collapsed onto
    the persona dimension.
    """
    cmd = enhance_cmd_for_project(cmd, cwd)
    kwargs = {
        "stdin_text": prompt,
        "timeout": timeout,
        "cwd": cwd,
        "persona_file": resolve_persona(role, cmd),
        "persona": role or "",
        "phase": phase or role or "",
    }
    if execution:
        kwargs.update(execution)
    if ledger is not None:
        kwargs["ledger"] = ledger
    return runner.run_cli(cmd, **kwargs)


# --- JSON extraction and frontmatter (shared jsonio re-exports) ----------------
# Callers import these names from ``phases``; the implementations live in
# adversarial_common.jsonio so all skills share one parser.
try_parse_json = jsonio.parse_json_output
extract_frontmatter = jsonio.extract_frontmatter
