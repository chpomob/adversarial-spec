#!/usr/bin/env python3
"""Adversarial Spec — git-native orchestrator.

WRITE -> CHALLENGE -> (REVISE -> VERIFY)^N, on a dedicated ``spec/<feature>/<N>``
branch. One model (spec-writer) writes ``spec.md`` from a brief, another
(spec-challenger) challenges it; the writer revises, the challenger verifies.
On approval the branch is squash-merged into the parent; otherwise a
``[REJECTED]`` marker commit is recorded.

Phase logic lives in scripts/phases/*; the shared engine (gitops, providers,
jsonio) lives in the adversarial-common sibling skill. This file only wires
phases together and maps verdicts to exit codes (same layout as
adversarial-code-loop's adversarial_loop.py, minus gates/arbiter/resume).

Exit codes:
  0 APPROVED — spec squash-merged into the parent branch (or left on its
               branch with --no-merge)
  1 infrastructure failure (phase crash, git error, interrupt)
  2 usage error (bad flags, missing/empty brief)
  3 REJECT   — findings unresolved after max-loops

The machine-readable contract is <out>/<feature>/final.json.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
# skill root (for `scripts.phases.*`) and the adversarial-common sibling skill
# (for `adversarial_common.*`) must both be importable.
sys.path.insert(0, str(_SCRIPTS_DIR.parent))
sys.path.insert(0, str(_SCRIPTS_DIR.parent.parent / "adversarial-common"))

from adversarial_common import gitops, jsonio
from adversarial_common.providers import resolve_role_cmd
from scripts.phases import phase_challenge, phase_revise, phase_verify, phase_write

EXIT_APPROVED = 0
EXIT_INFRA = 1
EXIT_USAGE = 2
EXIT_REJECTED = 3

DEFAULT_DEV_CMD = "pi --provider zai --model glm-5.2"
DEFAULT_REVIEW_CMD = "pi --provider deepseek --model deepseek-v4-pro"

# Verifier statuses that no longer block approval: "resolved" (fixed) and
# "rejected" (the verifier showed the original finding was wrong).
_SETTLED_STATUSES = {"resolved", "rejected"}


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


def _unresolved(findings, results):
    """Findings whose verify status is neither resolved nor rejected."""
    settled = {
        r.get("id") for r in results
        if r.get("id") is not None and r.get("status") in _SETTLED_STATUSES
    }
    return [f for f in findings if f.get("id") not in settled]


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

def _setup_git(workdir, feature):
    """Branch `spec/<feature>/<N>`, stash dirty, record branch-point, gitignore."""
    try:
        if gitops.detect_enclosing_repo(workdir):
            gitops.ensure_git_identity(workdir)
            parent = gitops.get_current_branch(workdir)
        else:
            gitops.auto_init(workdir)  # pins the initial branch to main
            parent = "main"
        stash_id = gitops.stash_dirty(workdir)
        branch = gitops.create_loop_branch(workdir, feature, parent, prefix="spec")
        gitops.checkout(workdir, branch)
        branch_point = gitops.record_branch_point(workdir, parent)
        gitops.ensure_gitignore(workdir, ".adversarial-spec/")
        return {"exit_code": 0, "parent_branch": parent, "branch": branch,
                "branch_point": branch_point, "stash_id": stash_id}
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
        # The verdict stands even when git finalize fails; final.json records
        # merged=false + the error so the caller can inspect and retry.
        error = str(exc)
        print(f"X git finalize failed ({verdict}): {exc}")
    jsonio.write_final_json(
        out_dir, verdict,
        reason=reason, loops=loops,
        branch=state.get("branch", ""),
        merged=merged,
        error=error,
        artifacts_dir=str(out_dir),
    )
    print(f"\n{verdict}" + (f" — {reason}" if reason else ""))
    return EXIT_APPROVED if verdict == "APPROVED" else EXIT_REJECTED


# --- pipeline -------------------------------------------------------------------

def _pipeline(args, dev_cmd, review_cmd, workdir, feature, out_dir,
              brief_text, state):
    """Run the full workflow. Returns the process exit code."""
    # PHASE 0 — GIT SETUP
    setup = _setup_git(workdir, feature)
    if setup["exit_code"] != 0:
        print(f"X git setup failed: {setup.get('error', 'unknown error')}")
        return EXIT_INFRA
    state.update(setup)
    state["feature"] = feature
    _banner(f"SPEC BRANCH  {setup['branch']}  (from {setup['parent_branch']})")
    jsonio.save_artifact(out_dir, "00_brief.txt", brief_text)

    # PHASE 1 — WRITE
    _banner("WRITE  (SPEC-WRITER)")
    write = phase_write.run_write(brief_text, dev_cmd, workdir,
                                  args.timeout, feature)
    _write_json(out_dir, "01_write.json", write)
    if write["exit_code"] != 0:
        return _phase_failed("write", write, state, out_dir)
    print(f"  OK commit {write.get('commit_sha', '')[:12]}")

    # PHASE 2 — CHALLENGE
    _banner("CHALLENGE  (SPEC-CHALLENGER)")
    challenge = phase_challenge.run_challenge(review_cmd, workdir, args.timeout)
    _write_json(out_dir, "02_challenge.json", challenge)
    if challenge["exit_code"] != 0:
        return _phase_failed("challenge", challenge, state, out_dir)
    findings = _ensure_ids(challenge.get("findings", []))
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
                                         args.timeout, feature, n)
        _write_json(out_dir, f"03_revise_{n}.json", revise)
        if revise["exit_code"] != 0:
            return _phase_failed(f"revise_{n}", revise, state, out_dir)

        _banner(f"VERIFY  (round {n}/{args.max_loops})")
        verify = phase_verify.run_verify(findings, review_cmd, workdir,
                                         args.timeout)
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

    ok, info = gitops.ensure_git_available()
    if not ok:
        print(f"X {info}")
        return EXIT_INFRA

    dev_cmd = resolve_role_cmd("dev", args.dev_cmd, "ASPEC_DEV_CMD",
                               DEFAULT_DEV_CMD)
    review_cmd = resolve_role_cmd("review", args.review_cmd, "ASPEC_REVIEW_CMD",
                                  DEFAULT_REVIEW_CMD)

    feature = _derive_feature(args, brief_text)
    if not feature:
        print("X Could not derive a feature name; pass --feature")
        return EXIT_USAGE

    out_base = Path(args.out)
    if not out_base.is_absolute():
        out_base = Path(workdir) / out_base
    out_dir = out_base / feature
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'#' * 60}\n  ADVERSARIAL SPEC\n"
          f"  Feature: {feature}\n  Max loops: {args.max_loops}\n"
          f"  WRITER: {dev_cmd[:60]}\n  CHALLENGER: {review_cmd[:60]}\n{'#' * 60}")

    state = {}
    try:
        code = _pipeline(args, dev_cmd, review_cmd, workdir, feature,
                         out_dir, brief_text, state)
    except KeyboardInterrupt:
        print("\nX Interrupted — restoring workdir (spec branch kept)")
        code = EXIT_INFRA
    except gitops.GitError as exc:
        print(f"\nX git error: {exc}")
        code = EXIT_INFRA
    finally:
        _restore(workdir, state)

    return code


if __name__ == "__main__":
    sys.exit(main())
