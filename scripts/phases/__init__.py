"""Phase modules for the adversarial-spec pipeline.

One module per phase, one public function per module (same layout as
adversarial-code-loop's ``scripts/phases``):

  phase_write      — spec-writer writes ``spec.md`` to disk (WRITE / BUILD)
  phase_challenge  — spec-challenger reviews ``spec.md`` (CHALLENGE / REVIEW)
  phase_revise     — spec-writer amends ``spec.md`` (REVISE / FIX)
  phase_verify     — spec-challenger checks the findings (VERIFY)

Helpers shared by more than one phase live here:

  run_role()           — persona-aware CLI execution with base-name fallback
  try_parse_json()     — 3-strategy JSON extraction (fences, ``{..}``, ``[..]``)
  validate_spec_file() — ``spec.md`` existence + YAML frontmatter validation
"""

import json
import re
import sys
from pathlib import Path

# The adversarial-common sibling skill must be importable. The orchestrator
# inserts it on sys.path before importing us; the fallback below keeps the
# package importable on its own (tests, REPL).
try:
    from adversarial_common import persona_path, runner
    from adversarial_common.providers import enhance_cmd_for_project, persona_for_role
except ImportError:  # pragma: no cover - exercised only on bare imports
    _COMMON = Path(__file__).resolve().parents[3] / "adversarial-common"
    sys.path.insert(0, str(_COMMON))
    from adversarial_common import persona_path, runner
    from adversarial_common.providers import enhance_cmd_for_project, persona_for_role

__all__ = [
    "run_role",
    "resolve_persona",
    "try_parse_json",
    "extract_frontmatter",
    "validate_spec_file",
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


def run_role(cmd, prompt, role, timeout, cwd):
    """Run a role command with its persona injected.

    Returns ``(stdout, stderr, returncode)`` from the hardened
    ``runner.run_cli`` (temp-file IO, process-group kill on timeout).
    """
    cmd = enhance_cmd_for_project(cmd, cwd)
    return runner.run_cli(
        cmd, stdin_text=prompt, timeout=timeout, cwd=cwd,
        persona_file=resolve_persona(role, cmd),
    )


# --- JSON extraction (3 strategies, as in code-loop phase_verify) --------------

def try_parse_json(text):
    """Parse JSON from model output, trying multiple extraction strategies.

    1. strip markdown code fences (```json ... ```)
    2. ``json.loads`` on the whole text
    3. extract the outermost ``{ ... }`` object, then the outermost ``[ ... ]``
       array

    Returns the parsed object or ``None`` when nothing decodes.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()

    # Strategy 1: markdown fence wrapper.
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    for _name, parse_fn in (
        ("json.loads", lambda t: json.loads(t)),
        ("extract { }",
         lambda t: json.loads(t[t.find("{"):t.rfind("}") + 1]) if "{" in t else None),
        ("extract [ ]",
         lambda t: json.loads(t[t.find("["):t.rfind("]") + 1]) if "[" in t else None),
    ):
        try:
            result = parse_fn(text)
            if result is not None:
                return result
        except (json.JSONDecodeError, ValueError, IndexError):
            continue
    return None


# --- spec.md validation ---------------------------------------------------------

# Frontmatter = a leading '---' line, a block, then a closing '---' line.
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)

_SPEC_FILENAME = "spec.md"


def extract_frontmatter(text):
    """Return the raw YAML frontmatter block of *text*, or None."""
    if not isinstance(text, str):
        return None
    match = _FRONTMATTER_RE.match(text.lstrip("﻿"))
    return match.group(1) if match else None


def _parse_frontmatter(fm_text):
    """Parse a frontmatter block. Returns ``(mapping_or_None, error_or_None)``.

    Uses PyYAML when available; otherwise falls back to a minimal
    ``key: value`` scan so the pipeline never hard-depends on PyYAML.
    """
    try:
        import yaml
    except ImportError:
        yaml = None

    if yaml is not None:
        try:
            data = yaml.safe_load(fm_text)
        except yaml.YAMLError as exc:
            return None, f"invalid YAML: {exc}"
        if not isinstance(data, dict):
            return None, "frontmatter is not a YAML mapping"
        return data, None

    # Fallback: top-level "key: value" lines only. Good enough to check the
    # required keys exist; nested structure is not validated.
    data = {}
    for line in fm_text.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            data[m.group(1)] = m.group(2).strip().strip("\"'")
    if not data:
        return None, "no top-level keys found in frontmatter"
    return data, None


def validate_spec_file(workdir):
    """Validate ``<workdir>/spec.md``. Returns ``(ok, error_message)``.

    Checks: file exists, is readable, non-empty, has a YAML frontmatter
    block that parses to a mapping containing a non-empty ``name`` key
    (the minimum contract from the spec-writer persona).
    """
    path = Path(workdir) / _SPEC_FILENAME
    if not path.is_file():
        return False, f"{_SPEC_FILENAME} not found in {workdir}"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"{_SPEC_FILENAME} unreadable: {exc}"
    if not text.strip():
        return False, f"{_SPEC_FILENAME} is empty"

    fm_text = extract_frontmatter(text)
    if fm_text is None:
        return False, f"{_SPEC_FILENAME} has no YAML frontmatter (--- ... ---)"
    data, err = _parse_frontmatter(fm_text)
    if err:
        return False, f"{_SPEC_FILENAME} frontmatter: {err}"
    name = data.get("name")
    if not (isinstance(name, str) and name.strip()):
        return False, f"{_SPEC_FILENAME} frontmatter is missing a non-empty 'name' key"
    return True, ""
