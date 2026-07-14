"""Validate adversarial specifications before downstream phases consume them."""
import argparse
import re
import sys
from pathlib import Path

try:
    from adversarial_common import jsonio
except ImportError:  # pragma: no cover - direct execution outside the package
    _COMMON = Path(__file__).resolve().parents[3] / "adversarial-common"
    sys.path.insert(0, str(_COMMON))
    from adversarial_common import jsonio

__all__ = [
    "extract_requirement_ids",
    "validate_requirement_coverage",
    "validate_spec_file",
    "validate_spec_text",
]

EXIT_VALID = 0
EXIT_USAGE = 2
_SPEC_FILENAME = "spec.md"
_REQUIRED_SCALARS = ("name", "version", "author")
_SECTION_RE = re.compile(
    r"^##\s+(?P<name>Requirements|Acceptance\s+criteria)\s*$"
    r"(?P<body>.*?)(?=^##\s+|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_REQUIREMENT_RE = re.compile(
    r"^\s*(?:[-*+] |\d+[.)]\s+)"
    r"(?P<id>R\d+[A-Za-z0-9_.-]*|REQ(?:UIREMENT)?[-_ ]?\d+"
    r"[A-Za-z0-9_.-]*)(?=\s*[:.)-])",
    re.IGNORECASE | re.MULTILINE,
)
_CRITERION_RE = re.compile(
    r"^\s*(?:[-*+] |\d+[.)]\s+)"
    r"(?P<id>AC\d+[A-Za-z0-9_.-]*)\s*"
    r"\((?P<requirements>[^)]+)\)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
_CRITERION_REQUIREMENT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:R\d+[A-Za-z0-9_.-]*|"
    r"REQ(?:UIREMENT)?[-_ ]?\d+[A-Za-z0-9_.-]*)(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)


def _canonical_id(value):
    return re.sub(r"[\s_-]+", "", value).casefold()


def _sections(text):
    return {
        re.sub(r"\s+", " ", match.group("name").strip()).casefold():
        match.group("body")
        for match in _SECTION_RE.finditer(text or "")
    }


def extract_requirement_ids(spec_text):
    """Extract ordered, unique ids from the Requirements section."""
    body = _sections(spec_text).get("requirements", "")
    requirements = []
    seen = set()
    for match in _REQUIREMENT_RE.finditer(body):
        requirement = match.group("id").strip()
        canonical = _canonical_id(requirement)
        if canonical not in seen:
            requirements.append(requirement)
            seen.add(canonical)
    return requirements


def validate_requirement_coverage(spec_text):
    """Require every requirement to map to a criterion and reject orphans."""
    sections = _sections(spec_text)
    requirements = extract_requirement_ids(spec_text)
    if not requirements:
        return False, "Requirements section has no identifiable requirement ids"

    criteria = list(_CRITERION_RE.finditer(
        sections.get("acceptance criteria", "")
    ))
    if not criteria:
        return False, "Acceptance criteria section has no criteria"

    known = {_canonical_id(item): item for item in requirements}
    covered = set()
    orphaned = []
    for criterion in criteria:
        references = _CRITERION_REQUIREMENT_RE.findall(
            criterion.group("requirements")
        )
        if not references:
            orphaned.append(criterion.group("id"))
            continue
        for reference in references:
            canonical = _canonical_id(reference)
            if canonical in known:
                covered.add(canonical)
            else:
                orphaned.append(f"{criterion.group('id')} -> {reference}")

    if orphaned:
        return False, (
            "acceptance criteria reference unknown requirements: "
            + ", ".join(orphaned)
        )
    uncovered = [value for key, value in known.items() if key not in covered]
    if uncovered:
        return False, (
            "requirements without acceptance criteria: " + ", ".join(uncovered)
        )
    return True, ""


def _non_empty_string(value):
    return isinstance(value, str) and bool(value.strip())


def validate_spec_text(text):
    """Validate a complete specification string. Return (ok, error)."""
    if not isinstance(text, str) or not text.strip():
        return False, f"{_SPEC_FILENAME} is empty"

    frontmatter = jsonio.extract_frontmatter(text)
    if frontmatter is None:
        return False, f"{_SPEC_FILENAME} has no YAML frontmatter (--- ... ---)"
    data, error = jsonio.parse_frontmatter(frontmatter)
    if error:
        return False, f"{_SPEC_FILENAME} frontmatter: {error}"

    for field in _REQUIRED_SCALARS:
        if not _non_empty_string(data.get(field)):
            return False, (
                f"{_SPEC_FILENAME} frontmatter is missing a non-empty "
                f"'{field}' key"
            )

    targets = data.get("targets")
    if not isinstance(targets, list) or not targets:
        return False, (
            f"{_SPEC_FILENAME} frontmatter is missing a non-empty 'targets' list"
        )
    for index, target in enumerate(targets, 1):
        if not isinstance(target, dict):
            return False, f"{_SPEC_FILENAME} target {index} is not a mapping"
        if not _non_empty_string(target.get("file")):
            return False, (
                f"{_SPEC_FILENAME} target {index} has no non-empty 'file'"
            )
        if not _non_empty_string(target.get("description")):
            return False, (
                f"{_SPEC_FILENAME} target {index} has no non-empty 'description'"
            )

    return validate_requirement_coverage(text)


def validate_spec_file(workdir):
    """Validate <workdir>/spec.md. Return (ok, error_message)."""
    path = Path(workdir) / _SPEC_FILENAME
    if not path.is_file():
        return False, f"{_SPEC_FILENAME} not found in {workdir}"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"{_SPEC_FILENAME} unreadable: {exc}"
    return validate_spec_text(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate an adversarial spec")
    parser.add_argument("spec", nargs="?", default=_SPEC_FILENAME)
    path = Path(parser.parse_args(argv).spec)
    if not path.is_file():
        print(f"X {_SPEC_FILENAME} not found: {path}")
        return EXIT_USAGE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"X {_SPEC_FILENAME} unreadable: {exc}")
        return EXIT_USAGE
    ok, error = validate_spec_text(text)
    if not ok:
        print(f"X {error}")
        return EXIT_USAGE
    return EXIT_VALID


if __name__ == "__main__":
    sys.exit(main())
