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

Reliability, cost, and adaptive-control integration (R1/R3/R4/R5/R8):

  * R1/R3 — ``gates.check_context(kind="brief")`` runs before any provider or
    git mutation. A blocked brief writes final.json and exits with
    EXIT_CONTEXT_BLOCKED before a single ``run_cli`` call.
  * R4    — a shared ``CostLedger`` threads through every phase; per-model
    costs and the complexity estimate land in final.json.
  * R5    — retry/caps controls (``--max-retries``, ``--max-input-chars``,
    ``--max-output-chars``, ``--truncate-input``) flow through the shared runner.
  * R8    — challenger findings are epistemically normalized (confidence/basis
    defaulted to low/inference when absent) with a recorded warning.

Exit codes:
  0 APPROVED — spec squash-merged into the parent branch (or left on its
               branch with --no-merge)
  1 infrastructure failure (phase crash, git error, interrupt)
  2 usage error (bad flags, missing/empty brief)
  3 REJECT   — findings unresolved after max-loops
  5 CONTEXT_BLOCKED — brief failed the preflight gate before any provider/git

The machine-readable contract is <out>/<feature>/final.json.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
# skill root (for `scripts.phases.*`) and the adversarial-common sibling skill
# (for `adversarial_common.*`) must both be importable.
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR.parent.parent / "adversarial-common"))

from adversarial_common import CostLedger, gates, gitops, jsonio, runner
from adversarial_common.providers import resolve_role_cmd
from scripts.phases import phase_challenge, phase_revise, phase_verify, phase_write
from scripts.phases import run_role

EXIT_APPROVED = 0
EXIT_INFRA = 1
EXIT_USAGE = 2
EXIT_REJECTED = 3
EXIT_CONTEXT_BLOCKED = runner.CI_EXIT_CONTEXT_BLOCKED

DEFAULT_DEV_CMD = "pi --provider zai --model glm-5.2"
DEFAULT_REVIEW_CMD = "pi --provider deepseek --model deepseek-v4-pro"

# Verifier statuses that no longer block approval: "resolved" (fixed) and
# "rejected" (the verifier showed the original finding was wrong).
_SETTLED_STATUSES = {"resolved", "rejected"}

# Context-gate threshold precedence: CLI flag > adversarial-spec env > shared env.
_THRESHOLD_ENV = {
    "min_chars": ("ASPEC_MIN_CONTEXT_CHARS", "ADVERSARIAL_MIN_CHARS"),
    "min_tokens": ("ASPEC_MIN_CONTEXT_TOKENS", "ADVERSARIAL_MIN_TOKENS"),
}


# --- small helpers -------------------------------------------------------------

def _banner(title):
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}")


def _write_json(out_dir, name, payload):
    """Persist *payload* as a pretty-printed JSON artifact under *out_dir*."""
    jsonio.save_artifact(out_dir, name, json.dumps(payload, indent=2) + "\n")


def _ensure_ids(findings):
    """Guarantee every finding has a unique, non-empty string id (in place)."""
    seen = set()
    for i, finding in enumerate(findings, 1):
        fid = str(finding.get("id") or "").strip() or f"finding-{i}"
        while fid in seen:
            fid = f"{fid}-{i}"
        finding["id"] = fid
        seen.add(fid)
    return findings


def _finalize_finding_ids(findings, warnings=None):
    """Ensure stable finding ids and re-key epistemic warnings to them.

    ``adversarial_common.jsonio.normalize_findings`` stamps each warning's
    ``finding_id`` with the raw id or the 0-based list position *before* ids
    are stable, so a warning can reference ``"0"`` while the finding's final
    id is ``"finding-1"``. This captures the prior key for every finding,
    assigns the final ids via :func:`_ensure_ids`, then re-points the warnings
    so ``final.json`` warnings stay joinable to findings by id.
    """
    prior_keys = []
    for index, finding in enumerate(findings):
        raw = ""
        if isinstance(finding, dict):
            raw = str(finding.get("id") or "").strip()
        prior_keys.append(raw or str(index))
    _ensure_ids(findings)
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


def _unresolved(findings, results):
    """Findings whose verify status is neither resolved nor rejected."""
    settled = {
        r.get("id") for r in results
        if r.get("id") is not None and r.get("status") in _SETTLED_STATUSES
    }
    return [f for f in findings if f.get("id") not in settled]


def _threshold_overrides(args):
    """Resolve brief context thresholds with CLI > spec env > shared env."""
    overrides = {}
    for name, env_names in _THRESHOLD_ENV.items():
        value = getattr(args, name, None)
        if value is None:
            for env_name in env_names:
                raw = os.environ.get(env_name)
                if raw is None:
                    continue
                try:
                    value = int(raw)
                except ValueError as exc:
                    raise ValueError(
                        f"${env_name} must be a non-negative integer") from exc
                if value < 0:
                    raise ValueError(
                        f"${env_name} must be a non-negative integer")
                break
        if value is not None:
            overrides[name] = value
    return overrides


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


def _preflight(args, brief_text, out_dir):
    """Run R1/R3 brief context gate before any provider or git call.

    Returns ``(effective_text, context, complexity, cap_events, ok)``. On a blocked brief
    writes a complete ``final.json`` (with empty costs and the complexity
    estimate) so CI callers see a machine-readable CONTEXT_BLOCKED verdict and
    no provider was ever invoiced.
    """
    cap_limit = getattr(args, "max_input_chars", runner.DEFAULT_MAX_INPUT_CHARS)
    capped, truncated = gates.enforce_input_cap(brief_text, cap_limit)
    cap_events = []
    if truncated:
        cap_events.append({
            "kind": "input",
            "phase": "preflight",
            "limit": cap_limit,
            "original_chars": len(brief_text),
            "truncated": bool(getattr(args, "truncate_input", False)),
        })
    truncate = bool(getattr(args, "truncate_input", False))
    effective_text = capped if truncate else brief_text
    context = gates.check_context(
        "brief", effective_text, _threshold_overrides(args))
    if truncated and not truncate and context["ok"]:
        context = dict(context)
        context.update({
            "ok": False,
            "reason": "input_exceeds_max_chars",
            "max_input_chars": cap_limit,
            "input_chars": len(brief_text),
        })
    complexity = gates.estimate_complexity(
        effective_text, max_agents=getattr(args, "max_agents", 6))
    if context["ok"]:
        return effective_text, context, complexity, cap_events, True

    jsonio.write_final_json(
        out_dir, "CONTEXT_BLOCKED",
        status="blocked",
        context_blocked=True,
        reason=context["reason"],
        context=context,
        thresholds=context.get("thresholds", {}),
        complexity=complexity,
        execution=_execution_record(args),
        attempts=[],
        cap_events=cap_events,
        calls=[],
        costs=CostLedger().summary(),
        findings=[],
        epistemic_labels=jsonio.epistemic_distribution([]),
        warnings=[],
    )
    print(f"X context blocked: {context['reason']}", file=sys.stderr)
    return effective_text, context, complexity, cap_events, False


def _record_phase(state, label, result, ledger):
    """Attach bounded runner evidence and the current ledger to the run state."""
    runtime = result.get("execution", {}) if isinstance(result, dict) else {}
    if not isinstance(runtime, dict):
        runtime = {}
    call = {
        "label": label,
        "ok": bool(isinstance(result, dict) and result.get("exit_code") == 0),
        "attempts": list(runtime.get("attempts", [])),
        "cap_events": list(runtime.get("cap_events", [])),
    }
    state.setdefault("calls", []).append(call)
    state.setdefault("attempts", []).extend(
        {"phase": label, **attempt} for attempt in call["attempts"])
    state.setdefault("cap_events", []).extend(
        {"phase": label, **event} for event in call["cap_events"])
    state["costs"] = ledger.summary()
    for warning in result.get("warnings", []) if isinstance(result, dict) else []:
        if warning not in state.setdefault("warnings", []):
            state["warnings"].append(warning)


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


def _log_retrospective(label, result, feature, branch, out_dir):
    """Append a pipeline failure to <out_dir>/ISSUES.md (best-effort).

    Lives in the per-feature artifacts dir, not the skill install tree, so
    logging works on read-only installs and runs don't share one file.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    entry = (
        f"\n### {now.split()[0]} — {label} failed for {feature}\n\n"
        f"- **Phase:** {label}\n"
        f"- **Branch:** {branch}\n"
        f"- **Error:** {result.get('error', 'unknown error')}\n"
        f"- **Stdout (last 200 chars):** {result.get('stdout', '')[-200:]!r}\n"
        f"- **Auto-logged by pipeline**\n"
    )
    with (Path(out_dir) / "ISSUES.md").open("a", encoding="utf-8") as fh:
        fh.write(entry)


def _phase_failed(label, result, state, out_dir):
    """Report a phase failure, log it to the retrospective. Returns EXIT_INFRA."""
    print(f"X {label} failed: {result.get('error', 'unknown error')}")
    try:
        _log_retrospective(label, result, state.get("feature", "unknown"),
                           state.get("branch", ""), out_dir)
    except Exception as exc:
        print(f"! could not write retrospective log: {exc}")
    return EXIT_INFRA


def _restore(workdir, state):
    """Best-effort cleanup on every exit path: back to parent, pop stash."""
    parent = state.get("parent_branch", "")
    try:
        if parent and gitops.get_current_branch(workdir) != parent:
            gitops.checkout(workdir, parent)
    except gitops.GitError as exc:
        # Never unstash onto the wrong branch.
        print(f"! could not restore branch {parent!r}: {exc}")
        return
    stash_id = state.get("stash_id", "")
    if stash_id:
        try:
            gitops.unstash(workdir, stash_id)
            state["stash_id"] = ""
        except gitops.GitError as exc:
            print(f"! could not pop {stash_id}: {exc}")


# --- PHASE 0: git setup / finalize ---------------------------------------------

def _setup_git(workdir, feature, state=None):
    """Branch `spec/<feature>/<N>`, stash dirty, record branch-point, gitignore."""
    state = state if state is not None else {}
    # Establish the recovery slot before setup performs any git mutation.
    state.setdefault("stash_id", "")
    try:
        if gitops.detect_enclosing_repo(workdir):
            gitops.ensure_git_identity(workdir)
            parent = gitops.get_current_branch(workdir)
        else:
            gitops.auto_init(workdir)  # pins the initial branch to main
            parent = "main"
        state["parent_branch"] = parent
        state["stash_id"] = gitops.stash_dirty(workdir)
        branch = gitops.create_loop_branch(workdir, feature, parent, prefix="spec")
        state["branch"] = branch
        gitops.checkout(workdir, branch)
        branch_point = gitops.record_branch_point(workdir, parent)
        state["branch_point"] = branch_point
        gitops.ensure_gitignore(workdir, ".adversarial-spec/")
        return {"exit_code": 0, "parent_branch": parent, "branch": branch,
                "branch_point": branch_point, "stash_id": state["stash_id"]}
    except Exception as exc:
        return {"exit_code": 1, "error": str(exc)}


def _final_md(verdict, feature, loops, reason):
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


def _finish(args, workdir, feature, out_dir, state, verdict, reason="", loops=0):
    """Squash-merge (APPROVED) or [REJECTED] marker, write final artifacts."""
    jsonio.save_artifact(out_dir, "final.md",
                         _final_md(verdict, feature, loops, reason))
    merged = False
    error = ""
    try:
        if verdict == "APPROVED":
            if not args.no_merge:
                gitops.squash_merge(
                    workdir, state["branch"], state["parent_branch"],
                    f"squash: {feature} — spec approved")
                merged = True
        else:
            gitops.reject_marker(workdir, f"{feature} — spec {verdict}")
    except gitops.GitError as exc:
        error = f"git finalize failed: {exc}"
        print(f"X git finalize failed ({verdict}): {exc}")

    ledger = getattr(args, "_ledger", None)
    costs = ledger.summary() if ledger is not None else state.get("costs", {})
    findings = _normalize_findings(list(state.get("findings", [])), state)
    distribution = jsonio.epistemic_distribution(findings)
    final_extra = {
        "reason": reason,
        "loops": loops,
        "branch": state.get("branch", ""),
        "merged": merged,
        "artifacts_dir": str(out_dir),
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
    }
    if error:
        final_extra["error"] = error
    jsonio.write_final_json(out_dir, verdict, **final_extra)

    print(f"\n{verdict}" + (f" — {reason}" if reason else ""))
    if error:
        return EXIT_INFRA
    return EXIT_APPROVED if verdict == "APPROVED" else EXIT_REJECTED


# --- pipeline -------------------------------------------------------------------

def _pipeline(args, dev_cmd, review_cmd, workdir, feature, out_dir,
              brief_text, state):
    """Run the full workflow. Returns the process exit code."""
    ledger = args._ledger
    execution = _execution_settings(args)
    # Bake retry/caps/ledger into a single run callable so every phase shares
    # one cost ledger and identical execution controls. Phase-level schema
    # retries (bad JSON) remain owned by each phase module.
    runner_fn = partial(run_role, execution=execution, ledger=ledger)

    # PHASE 0 — GIT SETUP
    setup = _setup_git(workdir, feature, state)
    state.update(setup)
    if setup["exit_code"] != 0:
        print(f"X git setup failed: {setup.get('error', 'unknown error')}")
        return EXIT_INFRA
    state["feature"] = feature
    _banner(f"SPEC BRANCH  {setup['branch']}  (from {setup['parent_branch']})")
    jsonio.save_artifact(out_dir, "00_brief.txt", brief_text)

    # PHASE 1 — WRITE
    _banner("WRITE  (SPEC-WRITER)")
    write = phase_write.run_write(brief_text, dev_cmd, workdir,
                                  args.timeout, feature, run=runner_fn)
    _record_phase(state, "write", write, ledger)
    _write_json(out_dir, "01_write.json", write)
    if write["exit_code"] != 0:
        if write.get("exit_code") == EXIT_USAGE:
            print(f"X spec validation failed: {write.get('error', 'invalid spec')}")
            return EXIT_USAGE
        return _phase_failed("write", write, state, out_dir)
    print(f"  OK commit {write.get('commit_sha', '')[:12]}")

    # PHASE 2 — CHALLENGE
    _banner("CHALLENGE  (SPEC-CHALLENGER)")
    challenge = phase_challenge.run_challenge(
        review_cmd, workdir, args.timeout, run=runner_fn,
        branch_point=state["branch_point"])
    # Assign stable finding ids and re-key the challenger's epistemic
    # warnings to those ids BEFORE recording/persisting: the shared parser
    # stamps each warning's finding_id from the raw id or the 0-based list
    # index, which would otherwise point at "0" while the finding's id is
    # "finding-1".
    findings = _finalize_finding_ids(
        challenge.get("findings", []), challenge.get("warnings"))
    _record_phase(state, "challenge", challenge, ledger)
    _write_json(out_dir, "02_challenge.json", challenge)
    if challenge["exit_code"] != 0:
        return _phase_failed("challenge", challenge, state, out_dir)
    # R8: normalize epistemic labels (confidence/basis) on challenger findings.
    _normalize_findings(findings, state)
    state["findings"] = findings
    verdict = challenge.get("verdict", "APPROVE")
    print(f"  OK {len(findings)} findings — verdict {verdict}")

    # PHASES 3/4 — REVISE / VERIFY loop. An empty findings list only approves
    # when the challenger's verdict is also APPROVE.
    approved = not findings and verdict == "APPROVE"
    loops_run = 0
    for n in range(1, args.max_loops + 1):
        if approved:
            break
        loops_run = n

        _banner(f"REVISE  (round {n}/{args.max_loops})")
        revise = phase_revise.run_revise(findings, dev_cmd, workdir,
                                         args.timeout, feature, n, run=runner_fn)
        _record_phase(state, f"revise_{n}", revise, ledger)
        _write_json(out_dir, f"03_revise_{n}.json", revise)
        if revise["exit_code"] != 0:
            return _phase_failed(f"revise_{n}", revise, state, out_dir)

        _banner(f"VERIFY  (round {n}/{args.max_loops})")
        verify = phase_verify.run_verify(
            findings, review_cmd, workdir, args.timeout, run=runner_fn,
            branch_point=state["branch_point"], round_n=n)
        _record_phase(state, f"verify_{n}", verify, ledger)
        _write_json(out_dir, f"04_verify_{n}.json", verify)
        if verify["exit_code"] != 0:
            return _phase_failed(f"verify_{n}", verify, state, out_dir)

        results = verify.get("results", [])
        remaining = _unresolved(findings, results)
        print(f"  Verdict {verify.get('verdict')} — "
              f"{len(findings) - len(remaining)}/{len(findings)} settled")
        if verify.get("verdict") == "APPROVE" and results and not remaining:
            approved = True
            break
        # Narrow to the still-open findings for the next round; if the verifier
        # rejected overall while marking everything settled (contradiction),
        # keep the current list so the next round sees real content.
        if remaining:
            findings = remaining
            state["findings"] = findings

    if approved:
        return _finish(args, workdir, feature, out_dir, state, "APPROVED",
                       loops=loops_run)
    return _finish(args, workdir, feature, out_dir, state, "REJECT",
                   reason=f"findings unresolved after {args.max_loops} loops",
                   loops=loops_run)


# --- CLI --------------------------------------------------------------------------

def _positive_int(value):
    """argparse type: strictly positive integer."""
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}")
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return ivalue


def _non_negative_int(value):
    """argparse type: integer greater than or equal to zero."""
    try:
        ivalue = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if ivalue < 0:
        raise argparse.ArgumentTypeError(
            f"must be a non-negative integer, got {value!r}")
    return ivalue


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
    p.add_argument("--workdir", default=".", help="Target directory (default: .)")
    p.add_argument("--max-loops", type=_positive_int, default=2)
    p.add_argument("--feature", default=None,
                   help="Branch/artifact name (default: brief filename)")
    p.add_argument("--timeout", type=_positive_int, default=600,
                   help="Per-subprocess timeout (s)")
    p.add_argument("--out", default=".adversarial-spec", help="Artifacts directory")
    p.add_argument("--no-merge", action="store_true",
                   help="On approval, leave the spec branch unmerged")
    p.add_argument(
        "--min-chars", "--min-context-chars", dest="min_chars",
        type=_non_negative_int, default=None,
        help="minimum brief characters (env: ASPEC_MIN_CONTEXT_CHARS)",
    )
    p.add_argument(
        "--min-tokens", "--min-context-tokens", dest="min_tokens",
        type=_non_negative_int, default=None,
        help="minimum estimated brief tokens (env: ASPEC_MIN_CONTEXT_TOKENS)",
    )
    p.add_argument(
        "--max-retries", type=_non_negative_int, default=3,
        help="transient retries per provider phase (default: 3)",
    )
    p.add_argument(
        "--max-input-chars", type=_non_negative_int,
        default=runner.DEFAULT_MAX_INPUT_CHARS,
        help="hard input cap per provider phase",
    )
    p.add_argument(
        "--max-output-chars", type=_non_negative_int,
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
        "--max-agents", type=_positive_int, default=6,
        help="complexity recommendation cap recorded for adaptive execution",
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
        brief_text, context, complexity, cap_events, preflight_ok = _preflight(
            args, brief_text, out_dir)
    except (TypeError, ValueError) as exc:
        print(f"X invalid preflight configuration: {exc}", file=sys.stderr)
        return EXIT_USAGE
    args._context = context
    args._complexity = complexity
    args._preflight_cap_events = cap_events
    if not preflight_ok:
        return EXIT_CONTEXT_BLOCKED

    # R1/R3 succeeded. Only now may git or command resolution run.
    ok, info = gitops.ensure_git_available()
    if not ok:
        print(f"X {info}")
        return EXIT_INFRA

    dev_cmd = resolve_role_cmd("dev", args.dev_cmd, "ASPEC_DEV_CMD",
                               DEFAULT_DEV_CMD)
    review_cmd = resolve_role_cmd("review", args.review_cmd, "ASPEC_REVIEW_CMD",
                                  DEFAULT_REVIEW_CMD)

    # R4 — one shared ledger accounts for every provider call across phases.
    args._ledger = CostLedger()

    print(f"\n{'#' * 60}\n  ADVERSARIAL SPEC\n"
          f"  Feature: {feature}\n  Max loops: {args.max_loops}\n"
          f"  WRITER: {dev_cmd[:60]}\n  CHALLENGER: {review_cmd[:60]}\n{'#' * 60}")

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
    }
    try:
        code = _pipeline(args, dev_cmd, review_cmd, workdir, feature,
                         out_dir, brief_text, state)
    except KeyboardInterrupt:
        print("\nX Interrupted — restoring workdir (spec branch kept)")
        state["costs"] = args._ledger.summary()
        code = EXIT_INFRA
    except gitops.GitError as exc:
        print(f"\nX git error: {exc}")
        state["costs"] = args._ledger.summary()
        code = EXIT_INFRA
    finally:
        _restore(workdir, state)

    if args.show_costs:
        args._ledger.print_summary(file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
