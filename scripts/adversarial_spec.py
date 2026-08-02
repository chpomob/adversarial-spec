#!/usr/bin/env python3
"""Adversarial Spec — git-native orchestrator.

WRITE -> CHALLENGE -> (REVISE -> VERIFY)^N, on a dedicated ``spec/<feature>/<N>``
branch. One model (spec-writer) writes ``spec.md`` from a brief, another
(spec-challenger) challenges it; the writer revises, the challenger verifies.
On approval the branch is squash-merged into the parent; otherwise a
``[REJECTED]`` marker commit is recorded.

Phase logic lives in scripts/phases/*; the shared engine (gitops, providers,
jsonio, costs, gates, runner) lives in the adversarial-common sibling skill.
This file only wires phases together and maps verdicts to exit codes (same
layout as adversarial-code-loop's adversarial_loop_v4.py, minus gates/arbiter).

Reliability, cost, and adaptive-control integration (R1/R3/R4/R5/R8/R9/R10/R11/R12):

  * R1/R3 — ``gates.check_context(kind="brief")`` runs before any provider or
    git mutation. A blocked brief writes final.json and exits with
    EXIT_CONTEXT_BLOCKED before a single ``run_cli`` call.
  * R4    — a shared ``CostLedger`` threads through every phase; per-model
    costs and the complexity estimate land in final.json.
  * R5    — retry/caps controls (``--max-retries``, ``--max-input-chars``,
    ``--max-output-chars``, ``--truncate-input``) flow through the shared runner.
  * R8    — challenger findings are epistemically normalized (confidence/basis
    defaulted to low/inference when absent) with a recorded warning.
  * R9    — ``--html`` renders a self-contained ``report.html`` next to
    ``final.json``; ``--ci`` maps the verdict to stable, policy-driven exit
    codes so CI callers can branch on the process result.
  * R10   — ``--deep-research`` runs bounded, non-fatal external research
    after preflight and feeds the evidence into the spec-writer's brief.
  * R11   — ``--fail-on`` selects the CI failure policy (findings, severities,
    verdicts, confidence/basis); ``none`` disables findings-based failure.
  * R12   — ``--delegated`` routes high-complexity briefs to bounded workers
    (decomposition -> workers -> synthesis); below the ``high`` tier it
    records a fallback and continues with the direct write.

Exit codes:
  0 APPROVED — spec squash-merged into the parent branch (or left on its
               branch with --no-merge)
  1 infrastructure failure (phase crash, git error, interrupt)
  2 usage error (bad flags, missing/empty brief)
  3 REJECT   — findings unresolved after max-loops, or no provider available
  5 CONTEXT_BLOCKED — brief failed the preflight gate before any provider/git

The machine-readable contract is <out>/<feature>/final.json.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
# skill root (for `scripts.phases.*`) and the adversarial-common sibling skill
# (for `adversarial_common.*`) must both be importable.
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR.parent.parent / "adversarial-common"))

from adversarial_common import (
    CostLedger,
    NoProviderAvailable,
    ProviderConfigError,
    QuotaResolver,
    gitops,
    jsonio,
    load_provider_config,
    report,
    run_contract_gate,
    runner,
)
import adversarial_common.pipeline_base as pipeline_base
from adversarial_common.providers import enhance_cmd_for_project, resolve_role_cmd
from scripts.phases import phase_challenge, phase_revise, phase_verify, phase_write
from scripts.phases import (
    merge_runtime,
    provider_history,
    resolve_persona,
    runtime_metadata,
    validate_spec_file,
)

EXIT_APPROVED = 0
EXIT_INFRA = 1
EXIT_USAGE = 2
EXIT_REJECTED = 3
EXIT_CONTEXT_BLOCKED = runner.CI_EXIT_CONTEXT_BLOCKED

DEFAULT_DEV_CMD = "pi --provider zai --model glm-5.2"
DEFAULT_REVIEW_CMD = "pi --provider deepseek --model deepseek-v4-pro"

# Context-gate threshold precedence: CLI flag > adversarial-spec env > shared env.
_THRESHOLD_ENV = {
    "min_chars": ("ASPEC_MIN_CONTEXT_CHARS", "ADVERSARIAL_MIN_CHARS"),
    "min_tokens": ("ASPEC_MIN_CONTEXT_TOKENS", "ADVERSARIAL_MIN_TOKENS"),
}

_FORCE_PROVIDER_ROLES = frozenset({"writer", "challenger", "verify"})

_SPEC_RESTORE_POLICY = pipeline_base.RestorePolicy()
_SPEC_RETROSPECTIVE_POLICY = pipeline_base.RetrospectivePolicy(
    infrastructure_exit=EXIT_INFRA,
)


# --- small helpers -------------------------------------------------------------


def _finalize_finding_ids(findings, warnings=None):
    """Ensure stable finding ids and re-key epistemic warnings to them.

    ``adversarial_common.jsonio.normalize_findings`` stamps each warning's
    ``finding_id`` with the raw id or the 0-based list position *before* ids
    are stable, so a warning can reference ``"0"`` while the finding's final
    id is ``"finding-1"``. This captures the prior key for every finding,
    assigns the final ids via the shared lifecycle helper, then re-points the warnings
    so ``final.json`` warnings stay joinable to findings by id.
    """
    prior_keys = []
    for index, finding in enumerate(findings):
        raw = ""
        if isinstance(finding, dict):
            raw = str(finding.get("id") or "").strip()
        prior_keys.append(raw or str(index))
    pipeline_base.ensure_finding_ids(findings)
    if warnings:
        remap = {
            old: finding["id"]
            for finding, old in zip(findings, prior_keys)
            if isinstance(finding, dict) and old != finding.get("id")
        }
        for warning in warnings:
            if isinstance(warning, dict) and warning.get("finding_id") in remap:
                warning["finding_id"] = remap[warning["finding_id"]]
    return findings


def _execution_settings(args):
    """Return runner settings shared by every model phase."""
    return {
        "max_retries": getattr(args, "max_retries", 3),
        "max_input_chars": getattr(
            args, "max_input_chars", runner.DEFAULT_MAX_INPUT_CHARS),
        "max_output_chars": getattr(
            args, "max_output_chars", runner.DEFAULT_MAX_OUTPUT_CHARS),
        "truncate_input": getattr(args, "truncate_input", False),
    }


def _execution_record(args):
    """Return serializable effective execution controls for artifacts."""
    return {
        **_execution_settings(args),
        "show_costs": getattr(args, "show_costs", False),
        "max_agents": getattr(args, "max_agents", 6),
    }


# --- optional modes: report / CI / research / delegation (R9-R12) --------------

def _json_safe(value):
    """Return a JSON-serializable copy, dropping non-serializable leaves.

    Delegated/research records carry tuple-like ``result`` objects and other
    non-serializable runner internals; this keeps ``final.json`` writable
    without losing the surrounding audit evidence. ``RunResult`` is a tuple
    subclass that also carries a ``.metadata`` mapping (retry attempts, cap
    events, native usage); a plain tuple iteration walks only the positional
    values and silently drops that metadata, so a RunResult is expanded into
    a dict that preserves both the values and the metadata, keeping the
    retry/cap evidence recoverable from ``01_delegated.json``.
    """
    metadata = getattr(value, "metadata", None)
    if isinstance(value, tuple) and isinstance(metadata, Mapping):
        return {
            "values": [_json_safe(item) for item in value],
            "metadata": _json_safe(dict(metadata)),
        }
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _warn(state, code, message):
    """Append a structured warning to the run state (deduplicated)."""
    warning = {"code": code, "message": message}
    if warning not in state.setdefault("warnings", []):
        state["warnings"].append(warning)


def _write_final_with_options(args, out_dir, verdict, payload=None, **extra):
    """Write ``final.json`` with an optional CI block, then render HTML.

    Centralizes the R9 (``--html`` / ``--ci``) post-processing so the
    preflight-blocked path and the normal finish path share one contract:
    the CI exit code is recorded inside the artifact, and the HTML report is
    rendered from the same bytes a caller will read back.
    """
    if payload is not None:
        extra = {**dict(payload), **extra}
    ci_exit_override = extra.pop("_ci_exit_override", None)
    # ponytail: a failed merge must not be read as APPROVED. pipeline_base
    # leaves the original verdict string in place and signals failure via the
    # ``infrastructure`` flag / a ``git finalize failed:`` error, so a CI
    # consumer reading only ``verdict`` would be misled. Override the written
    # sentinel here (the same INFRA value this function already treats as
    # infrastructure) before the CI exit computation and the write.
    _error_text = str(extra.get("error") or "")
    if extra.get("infrastructure") or _error_text.startswith("git finalize failed:"):
        verdict = "INFRA"
    if getattr(args, "html", False):
        extra.setdefault("html_report", str(Path(out_dir) / "report.html"))
    if getattr(args, "ci", False):
        policy = runner.parse_fail_on(getattr(args, "fail_on", None))
        exit_code = (
            ci_exit_override
            if isinstance(ci_exit_override, int)
            and not isinstance(ci_exit_override, bool)
            else runner.ci_exit_code(
                verdict,
                findings=extra.get("findings", []),
                fail_on_selector=getattr(args, "fail_on", None),
                context_blocked=bool(extra.get("context_blocked"))
                or verdict == "CONTEXT_BLOCKED",
                infrastructure=verdict in {"ERROR", "INFRA"},
            )
        )
        extra.setdefault("ci", {
            "enabled": True,
            "fail_on": sorted(policy),
            "exit_code": exit_code,
        })
    final_path = jsonio.write_final_json(out_dir, verdict, **extra)
    if getattr(args, "html", False):
        report.render_html_report(final_path)
    return final_path


def _run_research(args, brief_text, workdir, out_dir):
    """Run bounded external research after preflight (R10, opt-in).

    Returns ``(record, findings, warnings)``. The JSON-safe record is
    persisted as ``00_research.json``. Research is non-fatal: every failure
    mode (no provider, provider crash, bad JSON) returns a skip record so the
    pipeline continues without it.
    """
    provider_cmd = getattr(args, "research_cmd", None) or os.environ.get(
        "ASPEC_RESEARCH_CMD")
    query = "\n\n".join([
        "Gather authoritative external context for the specification brief below.",
        "Return bounded JSON with findings/results/items and source identifiers.",
        "=== BRIEF ===",
        brief_text,
    ])
    try:
        research = runner.run_research(
            query,
            provider_cmd=provider_cmd,
            enabled=True,
            max_results=getattr(args, "max_research_results",
                                runner.DEFAULT_RESEARCH_MAX_RESULTS),
            timeout=getattr(args, "research_timeout",
                            runner.DEFAULT_RESEARCH_TIMEOUT),
            max_input_chars=min(
                getattr(args, "max_input_chars", runner.DEFAULT_MAX_INPUT_CHARS),
                runner.DEFAULT_RESEARCH_MAX_INPUT_CHARS),
            max_output_chars=min(
                getattr(args, "max_output_chars", runner.DEFAULT_MAX_OUTPUT_CHARS),
                runner.DEFAULT_RESEARCH_MAX_OUTPUT_CHARS),
            ledger=args._ledger,
            cwd=workdir,
            stderr=sys.stderr,
        )
    except Exception as exc:  # defensive: research never aborts the pipeline
        reason = f"research integration error: {type(exc).__name__}: {exc}"
        print(f"Research skipped: {reason}", file=sys.stderr)
        research = {
            "enabled": True, "status": "skipped", "non_fatal": True,
            "reason": reason, "findings": [], "calls": [], "warnings": [],
        }
    if not isinstance(research, dict):
        research = {
            "enabled": True, "status": "skipped", "non_fatal": True,
            "reason": "research provider returned no record", "findings": [],
            "calls": [], "warnings": [],
        }
    record = _json_safe(research)
    findings = [
        finding for finding in (record.get("findings") or [])
        if isinstance(finding, dict)
    ] if isinstance(record, dict) else []
    warnings = [
        warning for warning in (record.get("warnings") or [])
        if isinstance(warning, dict)
    ] if isinstance(record, dict) else []
    pipeline_base.write_json(out_dir, "00_research.json", record)
    return record, findings, warnings


def _enrich_brief_with_research(brief_text, research_findings):
    """Append external research evidence to the brief for the spec-writer."""
    return "\n\n".join([
        brief_text,
        "=== EXTERNAL RESEARCH EVIDENCE ===",
        json.dumps(research_findings, ensure_ascii=False, sort_keys=True),
        "Incorporate this evidence only where independently relevant.",
    ])


def _delegated_execution(delegated):
    """Merge retry/cap-event metadata from every delegated provider call.

    Each delegated stage (decomposition, workers, synthesis) issues a real
    billed ``run_cli`` call whose ``RunResult.metadata`` records retries,
    input/output cap events, and native usage. Routing that evidence through
    :func:`merge_runtime` mirrors how the direct write/challenge/verify phases
    surface ``execution`` via :func:`runtime_metadata`, so shared phase recording
    feeds ``state["attempts"]``/``state["cap_events"]``/``state["calls"]``
    identically for delegated and direct runs, instead of emitting an empty
    ``execution`` that silently drops the delegated audit trail.
    """
    runtimes = []
    if isinstance(delegated, dict):
        decomposition = delegated.get("decomposition")
        if isinstance(decomposition, dict):
            runtimes.append(runtime_metadata(decomposition.get("result")))
        for worker in delegated.get("workers") or []:
            if isinstance(worker, dict):
                runtimes.append(runtime_metadata(worker.get("result")))
        synthesis = delegated.get("synthesis")
        if isinstance(synthesis, dict):
            runtimes.append(runtime_metadata(synthesis.get("result")))
    return merge_runtime(*runtimes)


def _delegated_provider_history(delegated):
    """Collect provider decisions from delegated calls in execution order."""
    results = []
    if isinstance(delegated, dict):
        decomposition = delegated.get("decomposition")
        if isinstance(decomposition, dict):
            results.append(decomposition.get("result"))
        for worker in delegated.get("workers") or []:
            if isinstance(worker, dict):
                results.append(worker.get("result"))
        synthesis = delegated.get("synthesis")
        if isinstance(synthesis, dict):
            results.append(synthesis.get("result"))
    return provider_history(*results)


def _run_delegated_role(args, cmd, prompt, cwd, phase):
    """Run one delegated writer stage through the shared provider contract."""
    resolver = getattr(args, "_provider_resolver", None)
    selected_cmd = enhance_cmd_for_project(cmd, cwd) if cmd else cmd
    explicit_cmd = selected_cmd if resolver is not None and selected_cmd else None
    command_args = {}
    if resolver is None:
        command_args["cmd"] = selected_cmd
    persona_cmd = explicit_cmd or (selected_cmd if resolver is None else "")
    execution = _execution_settings(args)
    return runner.run_phase_cmd(
        phase_name=phase,
        role="writer",
        workdir=cwd,
        resolver=resolver,
        explicit_cmd=explicit_cmd,
        force=bool(getattr(args, "force", False)),
        force_provider=getattr(args, "_force_providers", {}).get("writer"),
        stdin_text=prompt,
        timeout=args.timeout,
        persona="spec-writer",
        persona_file=resolve_persona("spec-writer", persona_cmd),
        ledger=args._ledger,
        **command_args,
        **execution,
    )


def _run_delegated_write(args, brief_text, dev_cmd, workdir, feature,
                         complexity, state, out_dir):
    """Delegate spec writing for high-complexity briefs (R12, opt-in).

    Returns a write-phase result dict when delegation produces a valid
    ``spec.md``, or ``None`` to signal the caller should run the direct write.
    Below the ``high`` complexity tier, or when synthesis fails to produce a
    valid spec, a fallback is recorded on *state* and ``None`` is returned so
    the pipeline always completes.
    """
    if not getattr(args, "delegated", False):
        return None
    level = complexity.get("level") if isinstance(complexity, dict) else None
    if level != "high":
        reason = (f"delegated write skipped: complexity level {level!r} "
                  f"is below required level 'high'")
        state["delegated"] = {
            "delegated": False, "mode": "direct", "status": "fallback",
            "reason": reason, "complexity": complexity,
        }
        _warn(state, "delegation_fallback", reason)
        print(f"! {reason}; continuing with direct write", file=sys.stderr)
        return None

    orchestrator_cmd = args.orchestrator_cmd or dev_cmd
    worker_cmd = args.worker_cmd or dev_cmd
    synth_cmd = args.synth_cmd or dev_cmd
    max_concurrency = min(getattr(args, "max_agents", 6),
                          getattr(args, "max_concurrency", 6))

    # Filesystem isolation for concurrent delegated calls. Decomposition and
    # each worker run in their own private working directory OUTSIDE the git
    # tree, so a misbehaving worker (the default dev_cmd is a full agentic CLI
    # that may run shell commands or edit files despite the prompt) cannot
    # stomp on a sibling worker or pollute *workdir*; otherwise the synthesis
    # stage's subsequent ``gitops.commit_all`` would silently fold any stray
    # concurrent mutations into the delegated-spec commit. Synthesis itself
    # must keep ``cwd=workdir`` because its job is to write ``spec.md`` there
    # for that commit, and it runs alone after every worker has finished.
    isolated_dirs = []

    def _isolated_cwd(prefix):
        path = tempfile.mkdtemp(prefix=prefix)
        isolated_dirs.append(path)
        return path

    def decomposition_call(payload):
        prompt = "\n\n".join([
            "Decompose this specification brief into independent concern areas.",
            ('Return JSON only: {"tasks":[{"id":"stable-id",'
             '"scope":"concern","instructions":"what to specify"}]}.'),
            f"Create at most {max_concurrency} tasks.",
            "=== BRIEF ===",
            payload if isinstance(payload, str) else brief_text,
        ])
        return partial(
            _run_delegated_role, args, orchestrator_cmd, prompt,
            _isolated_cwd("aspec-decompose-"), "delegated_decomposition",
        )

    def worker_call(task):
        prompt = "\n\n".join([
            "Draft the specification section for the delegated scope below.",
            "Return markdown for your section only (no full-file frontmatter).",
            "=== DELEGATED TASK ===",
            json.dumps(_json_safe(task), ensure_ascii=False, sort_keys=True),
            "=== FULL BRIEF ===",
            brief_text,
        ])
        return partial(
            _run_delegated_role, args, worker_cmd, prompt,
            _isolated_cwd("aspec-worker-"), "delegated_worker",
        )

    def synthesis_call(survivors):
        sections = "\n\n".join(
            survivor.get("stdout", "") for survivor in survivors
            if isinstance(survivor, dict))
        prompt = "\n\n".join([
            "Merge the delegated spec sections below into one complete spec.",
            "Write the file `spec.md` to disk in the current working directory,",
            "with YAML frontmatter (name, version, author, status, tags, targets)",
            "then Problem, Requirements, Acceptance criteria. Do not print body.",
            "=== DELEGATED SECTIONS ===",
            sections,
        ])
        return partial(
            _run_delegated_role, args, synth_cmd, prompt, workdir,
            "delegated_synthesis",
        )

    pipeline_base.banner("WRITE  (DELEGATED)")
    try:
        delegated = runner.run_delegated(
            brief_text, decomposition_call, worker_call, synthesis_call,
            max_concurrency=max_concurrency, complexity=complexity)
    finally:
        # Best-effort: tear down every per-stage working directory once the
        # concurrent calls (all completed or failed by now) no longer need it.
        for path in isolated_dirs:
            shutil.rmtree(path, ignore_errors=True)
    state["delegated"] = _json_safe(delegated)
    pipeline_base.write_json(out_dir, "01_delegated.json", state["delegated"])
    delegated_history = _delegated_provider_history(delegated)

    synthesis = delegated.get("synthesis") if isinstance(delegated, dict) else None
    if isinstance(delegated, dict) and delegated.get("status") == "synthesized" \
            and isinstance(synthesis, dict):
        ok, err = validate_spec_file(workdir)
        if ok:
            gitops.commit_all(workdir, f"write: {feature} — delegated spec")
            print(f"  OK delegated ({delegated.get('tasks_dispatched', 0)} tasks) "
                  f"commit {gitops.head_sha(workdir)[:12]}")
            return {
                "phase": "write", "exit_code": 0, "delegated": True,
                "commit_sha": gitops.head_sha(workdir),
                "stdout": synthesis.get("stdout", ""),
                "execution": _delegated_execution(delegated),
                "provider_history": delegated_history,
            }
        reason = f"delegated spec validation failed: {err}"
    else:
        status = delegated.get("status") if isinstance(delegated, dict) else None
        reason = (f"delegated synthesis did not complete "
                  f"(status={status!r}); falling back to direct write")
    state.setdefault("provider_history", []).extend(delegated_history)
    _warn(state, "delegation_fallback", reason)
    print(f"! {reason}", file=sys.stderr)
    return None


def _ensure_out_gitignored(args, workdir, out_dir):
    """Anchor a ``.gitignore`` entry to the resolved ``--out`` dir.

    A ``.gitignore`` governs only its own directory subtree, so the entry is
    written to the nearest tracked ancestor of ``--out``:

    * inside *workdir* — the common case (default ``.adversarial-spec``, or a
      relative ``--out`` like ``./my-artifacts``) normalized to a pattern git
      actually matches and anchors to *workdir* (``/my-artifacts/``, not the
      inert ``./my-artifacts/`` nor the unanchored ``my-artifacts/``, which
      would also match same-named directories anywhere below it);
    * elsewhere inside the enclosing repository — at the repo root, because a
      ``workdir/.gitignore`` cannot reach a sibling directory and ``git add -A``
      stages the whole working tree, so the artifacts would otherwise leak;
    * outside the repository — nothing to ignore; git never stages those files,
      and a repo-rooted pattern would only mislead.

    ``args.out`` can itself resolve *to* the anchor directory (``--out .``, or
    an absolute path equal to *workdir* or the repo root): the naive relative
    pattern would then be ``""``/``"."``, and a literal ``"./"`` entry ignores
    nothing (git never matches a ``./`` prefix), leaving every artifact at
    that root free to be swept into ``commit_all``'s ``git add -A``. In that
    case *out_dir* — the concrete per-feature directory the pipeline actually
    writes into (``<out>/<feature>/``) — is a genuine descendant of the anchor,
    so its relative pattern is used instead.

    Runs after :func:`setup_git` (which owns the stash/branch transaction) and
    before the first ``commit_all``, so the entry is in place before any commit.
    """
    workdir_abs = os.path.abspath(workdir)
    abs_out = os.path.normpath(os.path.join(workdir_abs, args.out))
    out_dir_abs = os.path.abspath(str(out_dir))

    def _pattern(target_abs, base):
        rel = os.path.relpath(target_abs, base)
        if rel.startswith("..") or os.path.isabs(rel):
            return None  # target_abs is not beneath base
        if rel in ("", "."):
            # target_abs *is* base ("--out ." or an absolute path equal to
            # base) — fall back to the effective per-feature artifacts dir,
            # always a real descendant of base.
            return _pattern(out_dir_abs, base)
        # Leading "/" anchors the pattern to *base* (the .gitignore's own
        # directory); without it a slashless single-segment name like
        # "demo-feature/" would match that directory at any depth, hiding
        # unrelated paths such as "pkg/demo-feature/".
        return f"/{rel}/"

    entry = _pattern(abs_out, workdir_abs)
    target = workdir_abs
    if entry is None:
        root = gitops.detect_enclosing_repo(workdir_abs)
        if root is None:
            return  # no enclosing repo and --out is outside workdir => outside git
        entry = _pattern(abs_out, root)
        target = root
    if entry is not None:
        gitops.ensure_gitignore(target, entry)


def _spec_preflight_policy(args):
    """Bind spec artifact callbacks to the shared preflight lifecycle."""
    return pipeline_base.PreflightPolicy(
        context_kind="brief",
        threshold_env=_THRESHOLD_ENV,
        execution_settings=_execution_record,
        blocked_writer=partial(_write_final_with_options, args),
        blocked_extra={
            "findings": [],
            "epistemic_labels": jsonio.epistemic_distribution([]),
            "provider_history": [],
        },
    )


def _provider_call_args(args, role, explicit_cmd):
    """Return quota controls for one provider-backed phase invocation."""
    forced = getattr(args, "_force_providers", {})
    return {
        "explicit_cmd": explicit_cmd,
        "force": bool(getattr(args, "force", False)),
        "force_provider": forced.get(role),
    }


def _normalize_findings(findings, state=None):
    """Normalize R8 epistemic labels without dropping or replacing identity."""
    payload = {"findings": findings}
    warnings = []
    jsonio.normalize_findings(payload, warnings=warnings)
    if state is not None:
        for warning in warnings:
            if warning not in state.setdefault("warnings", []):
                state["warnings"].append(warning)
    return findings


def _settle_contract_gate(workdir, state):
    """Run the shared F1 contract gate and return its settle verdict (R1).

    Parses the ``ac-directive`` blocks in ``spec.md`` and executes each one in
    *workdir* via :func:`adversarial_common.run_contract_gate`. Invoked before
    a CHALLENGE/VERIFY settle: a failing directive settles ``"REJECT"`` so the
    run keeps looping and ends REJECT at max-loops. A vacuous spec (no
    directives) settles ``"APPROVE"``. The full result is recorded on *state*
    (``state["contract"]``) so it lands in ``final.json`` for auditability.

    Fail-closed: a gate that cannot run (missing spec.md, engine crash)
    settles ``"REJECT"`` and records a warning — a broken gate never lets a
    failing spec through to APPROVE.
    """
    spec_path = Path(workdir) / "spec.md"
    try:
        gate = run_contract_gate(spec_path, workdir)
    except Exception as exc:  # defensive: fail-closed on engine failure
        _warn(state, "contract_gate_error",
              f"contract gate crashed: {type(exc).__name__}: {exc}")
        gate = {
            "settle": "REJECT", "ac_status": {}, "failures": [],
            "directives": [], "error": str(exc),
        }
    state["contract"] = gate
    return gate["settle"]


# --- PHASE 0: git setup / finalize ---------------------------------------------


def _spec_final_md(context):
    """Render the spec-specific human-readable final artifact."""
    verdict = context["verdict"]
    feature = context["feature"]
    loops = context["loops"]
    reason = context["reason"]
    lines = [
        f"# Adversarial Spec — {feature}",
        "",
        f"- Verdict: {verdict}",
        f"- Revise/verify loops: {loops}",
        f"- Finished: {datetime.now(timezone.utc).isoformat()}",
    ]
    if reason:
        lines.append(f"- Reason: {reason}")
    return "\n".join(lines) + "\n"


def _spec_final_payload(context):
    """Build the spec-owned portion of the shared final payload."""
    args = context["args"]
    state = context["state"]
    ledger = getattr(args, "_ledger", None)
    costs = ledger.summary() if ledger is not None else state.get("costs", {})
    findings = _normalize_findings(list(state.get("findings", [])), state)
    distribution = jsonio.epistemic_distribution(findings)
    payload = {
        "context": state.get("context", getattr(args, "_context", {})),
        "thresholds": state.get("thresholds", {}),
        "execution": state.get("execution", _execution_record(args)),
        "attempts": state.get("attempts", []),
        "cap_events": state.get("cap_events", []),
        "calls": state.get("calls", []),
        "costs": costs,
        "complexity": state.get("complexity", getattr(args, "_complexity", {})),
        "findings": findings,
        "epistemic_labels": distribution,
        "epistemic_distribution": distribution,
        "warnings": state.get("warnings", []),
        "provider_history": state.get("provider_history", []),
    }
    if state.get("research") is not None:
        payload["research"] = state["research"]
        payload["research_findings"] = state.get("research_findings", [])
    if state.get("delegated") is not None:
        payload["delegated"] = state["delegated"]
    if state.get("contract") is not None:
        payload["contract"] = state["contract"]
    return payload


def _spec_finish_policy(args):
    """Bind spec artifact and verdict policy to shared finalization."""
    return pipeline_base.FinishPolicy(
        pipeline_name="Adversarial Spec",
        approval_label="spec",
        loop_label="Revise/verify loops",
        exit_by_verdict={"APPROVED": EXIT_APPROVED},
        rejected_exit=EXIT_REJECTED,
        infrastructure_exit=EXIT_INFRA,
        git_adapter=gitops,
        final_md_builder=_spec_final_md,
        payload_builder=_spec_final_payload,
        final_writer=partial(_write_final_with_options, args),
    )


# --- pipeline -------------------------------------------------------------------

def _pipeline(args, dev_cmd, review_cmd, workdir, feature, out_dir,
              brief_text, state):
    """Run the full workflow. Returns the process exit code."""
    ledger = args._ledger
    execution = _execution_settings(args)
    resolver = getattr(args, "_provider_resolver", None)

    # PHASE 0 — GIT SETUP
    # gitignore_entry=None: _ensure_out_gitignored writes the entry itself,
    # anchored to workdir or the enclosing repo root as appropriate (R-SP4/5).
    setup = pipeline_base.setup_git(
        workdir, feature, state,
        policy=pipeline_base.GitSetupPolicy(prefix="spec", gitignore_entry=None),
    )
    state.update(setup)
    if setup["exit_code"] != 0:
        print(f"X git setup failed: {setup.get('error', 'unknown error')}")
        return EXIT_INFRA
    _ensure_out_gitignored(args, workdir, out_dir)
    state["feature"] = feature
    pipeline_base.banner(
        f"SPEC BRANCH  {setup['branch']}  (from {setup['parent_branch']})",
    )
    jsonio.save_artifact(out_dir, "00_brief.txt", brief_text)

    # PHASE 1 — WRITE (optionally delegated for high-complexity briefs, R12)
    write = _run_delegated_write(args, brief_text, dev_cmd, workdir, feature,
                                 state.get("complexity", {}), state, out_dir)
    if write is None:
        pipeline_base.banner("WRITE  (SPEC-WRITER)")
        write = phase_write.run_write(
            brief_text, dev_cmd, workdir, args.timeout, feature,
            resolver=resolver,
            execution=execution,
            ledger=ledger,
            **_provider_call_args(args, "writer", args.dev_cmd),
        )
    pipeline_base.record_phase(state, "write", write, ledger)
    pipeline_base.write_json(out_dir, "01_write.json", write)
    if write["exit_code"] != 0:
        if write.get("exit_code") == EXIT_USAGE:
            print(f"X spec validation failed: {write.get('error', 'invalid spec')}")
            return EXIT_USAGE
        return pipeline_base.phase_failure(
            "write", write, state, out_dir,
            policy=_SPEC_RETROSPECTIVE_POLICY,
        )
    print(f"  OK commit {write.get('commit_sha', '')[:12]}")

    # PHASE 2 — CHALLENGE
    pipeline_base.banner("CHALLENGE  (SPEC-CHALLENGER)")
    challenge = phase_challenge.run_challenge(
        review_cmd, workdir, args.timeout,
        branch_point=state["branch_point"],
        resolver=resolver,
        execution=execution,
        ledger=ledger,
        **_provider_call_args(args, "challenger", args.review_cmd),
    )
    # Assign stable finding ids and re-key the challenger's epistemic
    # warnings to those ids BEFORE recording/persisting: the shared parser
    # stamps each warning's finding_id from the raw id or the 0-based list
    # index, which would otherwise point at "0" while the finding's id is
    # "finding-1".
    findings = _finalize_finding_ids(
        challenge.get("findings", []), challenge.get("warnings"))
    pipeline_base.record_phase(state, "challenge", challenge, ledger)
    pipeline_base.write_json(out_dir, "02_challenge.json", challenge)
    if challenge["exit_code"] != 0:
        return pipeline_base.phase_failure(
            "challenge", challenge, state, out_dir,
            policy=_SPEC_RETROSPECTIVE_POLICY,
        )
    # R8: normalize epistemic labels (confidence/basis) on challenger findings.
    _normalize_findings(findings, state)
    state["findings"] = findings
    verdict = challenge.get("verdict", "APPROVE")
    print(f"  OK {len(findings)} findings — verdict {verdict}")

    # PHASES 3/4 — REVISE / VERIFY loop. An empty findings list only approves
    # when the challenger's verdict is also APPROVE.
    approved = not findings and verdict == "APPROVE"
    # R1 — invoke the shared contract gate before the CHALLENGE settle. A
    # failing ac-directive blocks APPROVE here so the run loops and ends REJECT
    # at max-loops instead of approving an unenforceable spec.
    if approved and _settle_contract_gate(workdir, state) != "APPROVE":
        approved = False
    loops_run = 0
    for n in range(1, args.max_loops + 1):
        if approved:
            break
        loops_run = n

        pipeline_base.banner(f"REVISE  (round {n}/{args.max_loops})")
        revise = phase_revise.run_revise(
            findings, dev_cmd, workdir, args.timeout, feature, n,
            resolver=resolver,
            execution=execution,
            ledger=ledger,
            **_provider_call_args(args, "writer", args.dev_cmd),
        )
        pipeline_base.record_phase(state, f"revise_{n}", revise, ledger)
        pipeline_base.write_json(out_dir, f"03_revise_{n}.json", revise)
        if revise["exit_code"] != 0:
            return pipeline_base.phase_failure(
                f"revise_{n}", revise, state, out_dir,
                policy=_SPEC_RETROSPECTIVE_POLICY,
            )

        pipeline_base.banner(f"VERIFY  (round {n}/{args.max_loops})")
        verify = phase_verify.run_verify(
            findings, review_cmd, workdir, args.timeout,
            branch_point=state["branch_point"], round_n=n,
            resolver=resolver,
            execution=execution,
            ledger=ledger,
            **_provider_call_args(args, "verify", args.review_cmd),
        )
        pipeline_base.record_phase(state, f"verify_{n}", verify, ledger)
        pipeline_base.write_json(out_dir, f"04_verify_{n}.json", verify)
        if verify["exit_code"] != 0:
            return pipeline_base.phase_failure(
                f"verify_{n}", verify, state, out_dir,
                policy=_SPEC_RETROSPECTIVE_POLICY,
            )

        results = verify.get("results", [])
        remaining = pipeline_base.unresolved_findings(
            findings, results,
        )
        print(f"  Verdict {verify.get('verdict')} — "
              f"{len(findings) - len(remaining)}/{len(findings)} settled")
        if verify.get("verdict") == "APPROVE" and (
                not findings or (results and not remaining)):
            # R1 — before the VERIFY settle, the executable contract must also
            # pass. Findings may all be resolved while an ac-directive still
            # fails; keep looping (-> REJECT at max-loops) in that case.
            # A1: when the challenge had no findings, a valid verifier response
            # has `results: []`; re-run the gate here too so a spec that became
            # valid during revision can recover instead of rejecting on the
            # stale initial gate result.
            if _settle_contract_gate(workdir, state) == "APPROVE":
                approved = True
                break
        # Narrow to the still-open findings for the next round; if the verifier
        # rejected overall while marking everything settled (contradiction),
        # keep the current list so the next round sees real content.
        if remaining:
            findings = remaining
            state["findings"] = findings

    if approved:
        return pipeline_base.finish_pipeline(
            args, workdir, feature, out_dir, state, "APPROVED",
            loops=loops_run, policy=_spec_finish_policy(args),
        )
    return pipeline_base.finish_pipeline(
        args, workdir, feature, out_dir, state, "REJECT",
        reason=f"findings unresolved after {args.max_loops} loops",
        loops=loops_run, policy=_spec_finish_policy(args),
    )


# --- CLI --------------------------------------------------------------------------


def _fail_on_arg(value):
    """argparse type: validate a --fail-on selector via the shared parser."""
    try:
        runner.parse_fail_on(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def _force_provider_value(value):
    """argparse type for repeatable ``ROLE:ALIAS`` provider overrides."""
    role, separator, alias = value.partition(":")
    role = role.strip().lower()
    alias = alias.strip()
    if not separator or role not in _FORCE_PROVIDER_ROLES or not alias:
        allowed = ", ".join(sorted(_FORCE_PROVIDER_ROLES))
        raise argparse.ArgumentTypeError(
            f"expected <role>:<alias> with role in: {allowed}"
        )
    return role, alias


def _force_provider_map(values):
    """Validate repeatable overrides and return one alias per role."""
    result = {}
    for role, alias in values:
        if role in result:
            raise ValueError(f"--force-provider specified more than once for {role}")
        result[role] = alias
    return result


def build_parser():
    p = argparse.ArgumentParser(
        description="Adversarial Spec "
                    "(WRITE -> CHALLENGE -> (REVISE -> VERIFY)^N, git-native)")
    p.add_argument("--brief", default=None,
                   help="File containing the brief (default: read stdin)")
    p.add_argument("--dev-cmd", default=None,
                   help=f"spec-writer command (default: $ASPEC_DEV_CMD or "
                        f"'{DEFAULT_DEV_CMD}')")
    p.add_argument("--review-cmd", default=None,
                   help=f"spec-challenger command (default: $ASPEC_REVIEW_CMD or "
                        f"'{DEFAULT_REVIEW_CMD}')")
    p.add_argument(
        "--provider-config",
        default=None,
        metavar="PATH",
        help="provider registry YAML (env: ADVERSARIAL_PROVIDER_CONFIG)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="skip quota checks and select each role's primary provider",
    )
    p.add_argument(
        "--force-provider",
        action="append",
        default=[],
        type=_force_provider_value,
        metavar="ROLE:ALIAS",
        help="force an alias for one role; repeat for multiple roles",
    )
    p.add_argument("--workdir", default=".", help="Target directory (default: .)")
    p.add_argument("--max-loops", type=pipeline_base.positive_int, default=2)
    p.add_argument("--feature", default=None,
                   help="Branch/artifact name (default: brief filename)")
    p.add_argument("--timeout", type=pipeline_base.positive_int, default=600,
                   help="Per-subprocess timeout (s)")
    p.add_argument("--out", default=".adversarial-spec", help="Artifacts directory")
    p.add_argument("--no-merge", action="store_true",
                   help="On approval, leave the spec branch unmerged")
    p.add_argument(
        "--min-chars", "--min-context-chars", dest="min_chars",
        type=pipeline_base.non_negative_int, default=None,
        help="minimum brief characters (env: ASPEC_MIN_CONTEXT_CHARS)",
    )
    p.add_argument(
        "--min-tokens", "--min-context-tokens", dest="min_tokens",
        type=pipeline_base.non_negative_int, default=None,
        help="minimum estimated brief tokens (env: ASPEC_MIN_CONTEXT_TOKENS)",
    )
    p.add_argument(
        "--max-retries", type=pipeline_base.non_negative_int, default=3,
        help="transient retries per provider phase (default: 3)",
    )
    p.add_argument(
        "--max-input-chars", type=pipeline_base.non_negative_int,
        default=runner.DEFAULT_MAX_INPUT_CHARS,
        help="hard input cap per provider phase",
    )
    p.add_argument(
        "--max-output-chars", type=pipeline_base.non_negative_int,
        default=runner.DEFAULT_MAX_OUTPUT_CHARS,
        help="hard output cap per provider phase",
    )
    p.add_argument(
        "--truncate-input", action="store_true",
        help="head-truncate oversized provider input instead of rejecting it",
    )
    p.add_argument(
        "--show-costs", action="store_true",
        help="print the final per-model cost breakdown to stderr",
    )
    p.add_argument(
        "--max-agents", type=pipeline_base.positive_int, default=6,
        help="complexity recommendation cap recorded for adaptive execution",
    )
    p.add_argument("--html", action="store_true",
                   help="render a self-contained report.html after final.json")
    p.add_argument("--ci", action="store_true",
                   help="enable deterministic CI exit-code policy")
    p.add_argument("--fail-on", type=_fail_on_arg, default=None,
                   help="comma-separated CI selectors (default: findings)")
    p.add_argument("--deep-research", action="store_true",
                   help="run bounded external research after preflight")
    p.add_argument("--research-cmd", default=None,
                   help="research provider command (env: ASPEC_RESEARCH_CMD "
                        "or ADVERSARIAL_RESEARCH_CMD)")
    p.add_argument(
        "--max-research-results", "--research-max-results",
        dest="max_research_results", type=pipeline_base.non_negative_int,
        default=runner.DEFAULT_RESEARCH_MAX_RESULTS,
        help="maximum external evidence items (default: 5)",
    )
    p.add_argument(
        "--research-timeout", type=pipeline_base.positive_int,
        default=runner.DEFAULT_RESEARCH_TIMEOUT,
        help="research provider timeout in seconds (default: 60)",
    )
    p.add_argument("--delegated", action="store_true",
                   help="delegate high-complexity spec writing to bounded workers")
    p.add_argument("--orchestrator-cmd", default=None,
                   help="delegation decomposition command (default: --dev-cmd)")
    p.add_argument("--worker-cmd", default=None,
                   help="delegated worker command (default: --dev-cmd)")
    p.add_argument("--synth-cmd", default=None,
                   help="delegation synthesis command (default: --dev-cmd)")
    p.add_argument(
        "--max-concurrency", type=pipeline_base.positive_int, default=6,
        help="delegated worker concurrency cap (default: 6)",
    )
    return p


def _derive_feature(args, brief_text):
    """Feature name: --feature > brief filename stem > first brief line."""
    raw = args.feature or (Path(args.brief).stem if args.brief else "")
    if not raw:
        for line in brief_text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                raw = stripped
                break
    return gitops.sanitize_feature_name(raw)


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        args._force_providers = _force_provider_map(args.force_provider)
        args._provider_config = load_provider_config(args.provider_config)
        if args._provider_config is None:
            args._provider_resolver = None
        else:
            if not args._provider_config.quota_cmd:
                raise ProviderConfigError(
                    "PROVIDER_CONFIG_QUOTA_CMD_REQUIRED",
                    "quota_cmd is required when the spec pipeline uses a "
                    "provider registry",
                )
            # One resolver owns quota caching and selection history for every
            # phase and every schema retry in this process.
            args._provider_resolver = QuotaResolver(
                args._provider_config, args._provider_config.quota_cmd
            )
    except (ProviderConfigError, TypeError, ValueError) as exc:
        print(f"X invalid provider configuration: {exc}", file=sys.stderr)
        return EXIT_USAGE

    workdir = str(Path(args.workdir).resolve())
    if not os.path.isdir(workdir):
        print(f"X Workdir not found: {args.workdir}")
        return EXIT_USAGE

    if args.brief:
        try:
            brief_text = Path(args.brief).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"X Could not read brief {args.brief}: {exc}")
            return EXIT_USAGE
    else:
        brief_text = sys.stdin.read()
    if not brief_text.strip():
        print("X Empty brief (pass --brief <file> or pipe the brief on stdin)")
        return EXIT_USAGE

    feature = _derive_feature(args, brief_text)
    if not feature:
        print("X Could not derive a feature name; pass --feature")
        return EXIT_USAGE

    out_base = Path(args.out)
    if not out_base.is_absolute():
        out_base = Path(workdir) / out_base
    out_dir = out_base / feature
    out_dir.mkdir(parents=True, exist_ok=True)

    # R1/R3 — brief preflight. Runs before git availability, command
    # resolution, or any provider call. A blocked brief exits with
    # EXIT_CONTEXT_BLOCKED and writes final.json with zero run_cli calls.
    try:
        preflight_result = pipeline_base.preflight(
            args, brief_text, out_dir, policy=_spec_preflight_policy(args),
        )
    except (TypeError, ValueError) as exc:
        print(f"X invalid preflight configuration: {exc}", file=sys.stderr)
        return EXIT_USAGE
    brief_text = preflight_result.effective_text
    context = preflight_result.context
    complexity = preflight_result.complexity
    cap_events = preflight_result.cap_events
    if not preflight_result.ok:
        if getattr(args, "ci", False):
            return pipeline_base.ci_exit_from_final(
                out_dir, EXIT_CONTEXT_BLOCKED,
                fail_on_selector=getattr(args, "fail_on", None),
            )
        return EXIT_CONTEXT_BLOCKED

    # R1/R3 succeeded. Only now may git or command resolution run.
    ok, info = gitops.ensure_git_available()
    if not ok:
        print(f"X {info}")
        return EXIT_INFRA

    if args._provider_config is None:
        dev_cmd = resolve_role_cmd(
            "dev", args.dev_cmd, "ASPEC_DEV_CMD", DEFAULT_DEV_CMD
        )
        review_cmd = resolve_role_cmd(
            "review", args.review_cmd, "ASPEC_REVIEW_CMD", DEFAULT_REVIEW_CMD
        )
    else:
        # Registry commands are selected immediately before each phase.
        # Non-empty CLI commands remain explicit quota-bypassing overrides.
        dev_cmd = (args.dev_cmd or "").strip()
        review_cmd = (args.review_cmd or "").strip()

    # R4 — one shared ledger accounts for every provider call across phases.
    args._ledger = CostLedger()

    provider_mode = "registry" if args._provider_resolver is not None else "legacy"
    command_banner = ""
    if provider_mode == "legacy":
        command_banner = (f"  WRITER: {dev_cmd[:60]}\n"
                          f"  CHALLENGER: {review_cmd[:60]}\n")
    print(f"\n{'#' * 60}\n  ADVERSARIAL SPEC\n"
          f"  Feature: {feature}\n  Max loops: {args.max_loops}\n"
          f"  Provider mode: {provider_mode}\n{command_banner}{'#' * 60}")

    state = {
        "context": context,
        "thresholds": context.get("thresholds", {}),
        "complexity": complexity,
        "execution": _execution_record(args),
        "attempts": [],
        "calls": [],
        "cap_events": list(cap_events),
        "warnings": [],
        "costs": args._ledger.summary(),
        "findings": [],
        "provider_history": [],
    }
    # R10 — opt-in bounded external research, run after preflight succeeds.
    # Findings enrich the spec-writer's input; the record is persisted in
    # final.json. Research is non-fatal and never blocks the pipeline.
    if getattr(args, "deep_research", False):
        _research, _research_findings, _research_warnings = _run_research(
            args, brief_text, workdir, out_dir)
        state["research"] = _research
        state["research_findings"] = _research_findings
        for _warning in _research_warnings:
            if _warning not in state["warnings"]:
                state["warnings"].append(_warning)
        if _research_findings:
            brief_text = _enrich_brief_with_research(brief_text, _research_findings)
    try:
        code = _pipeline(args, dev_cmd, review_cmd, workdir, feature,
                         out_dir, brief_text, state)
    except KeyboardInterrupt:
        print("\nX Interrupted — restoring workdir (spec branch kept)")
        state["costs"] = args._ledger.summary()
        code = EXIT_INFRA
    except NoProviderAvailable as exc:
        print(f"X no provider available for role '{exc.role}'", file=sys.stderr)
        aliases = set(exc.snapshots) | set(exc.reasons)
        for alias in sorted(aliases):
            snapshot = json.dumps(
                exc.snapshots.get(alias, {}), sort_keys=True, default=str
            )
            reason = exc.reasons.get(alias, "ineligible")
            print(
                f"  {alias}: {reason}; snapshot={snapshot}", file=sys.stderr
            )
        history = getattr(exc, "provider_history", None)
        if isinstance(history, list):
            for decision in history:
                if isinstance(decision, Mapping):
                    state.setdefault("provider_history", []).append(
                        dict(decision)
                    )
        else:
            decision = getattr(exc, "provider_decision", None)
            if isinstance(decision, Mapping):
                state.setdefault("provider_history", []).append(dict(decision))
        state["costs"] = args._ledger.summary()
        _write_final_with_options(
            args,
            out_dir,
            "REJECT",
            status="provider_unavailable",
            reason=str(exc),
            error=str(exc),
            branch=state.get("branch", ""),
            merged=False,
            context=state.get("context", context),
            thresholds=state.get("thresholds", {}),
            execution=state.get("execution", _execution_record(args)),
            attempts=state.get("attempts", []),
            cap_events=state.get("cap_events", []),
            calls=state.get("calls", []),
            costs=state["costs"],
            complexity=state.get("complexity", complexity),
            findings=state.get("findings", []),
            warnings=state.get("warnings", []),
            provider_history=state.get("provider_history", []),
            provider_snapshots=_json_safe(exc.snapshots),
            provider_reasons=dict(exc.reasons),
            _ci_exit_override=EXIT_REJECTED,
        )
        code = EXIT_REJECTED
    except gitops.GitError as exc:
        print(f"\nX git error: {exc}")
        state["costs"] = args._ledger.summary()
        code = EXIT_INFRA
    finally:
        pipeline_base.restore_git(
            workdir, state, out_dir, policy=_SPEC_RESTORE_POLICY,
        )

    if args.show_costs:
        args._ledger.print_summary(file=sys.stderr)
    if getattr(args, "ci", False):
        code = pipeline_base.ci_exit_from_final(
            out_dir, code, fail_on_selector=getattr(args, "fail_on", None),
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
