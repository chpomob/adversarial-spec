#!/usr/bin/env bash
# Install adversarial-spec with its single dependency: adversarial-common (sibling layout).
# Usage:
#   Bootstrap (from anywhere):  curl -fsSL https://raw.githubusercontent.com/chpomob/adversarial-spec/main/scripts/install.sh | bash
#   From a checkout:            bash scripts/install.sh [TARGET_DIR]
# Target layout (siblings required):
#   <TARGET>/adversarial-spec            (this skill)
#   <TARGET>/adversarial-common
set -euo pipefail

SKILL_DIR="adversarial-spec"
SKILL_NAME="adversarial-spec"
COMMON_REPO="adversarial-common"
COMMON_URL="https://github.com/chpomob/${COMMON_REPO}.git"
SKILL_URL="https://github.com/chpomob/${SKILL_DIR}.git"

TARGET="${1:-${HERMES_HOME:-$HOME/.hermes}/skills}"
HERE=""
if [ -n "${BASH_SOURCE[0]:-}" ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

mkdir -p "$TARGET"

if [ -n "$HERE" ] && git -C "$HERE" rev-parse --git-dir >/dev/null 2>&1; then
  echo "detected: running from a checkout ($HERE) — skill already present"
else
  if [ ! -d "$TARGET/$SKILL_DIR" ]; then
    git clone --depth 1 "$SKILL_URL" "$TARGET/$SKILL_DIR"
  else
    echo "skip: $TARGET/$SKILL_DIR already present"
  fi
fi

if [ ! -d "$TARGET/$COMMON_REPO" ]; then
  git clone --depth 1 "$COMMON_URL" "$TARGET/$COMMON_REPO"
else
  echo "skip: $TARGET/$COMMON_REPO already present"
fi

echo
echo "Installed:"
echo "  $TARGET/$SKILL_DIR      (skill)"
echo "  $TARGET/$COMMON_REPO    (dependency, sibling layout required)"
echo
echo "Sanity check (import adversarial_common + adversarial_spec)..."
SKILL_SCRIPTS_DIR="$TARGET/$SKILL_DIR/scripts"
if [ -n "$HERE" ] && git -C "$HERE" rev-parse --git-dir >/dev/null 2>&1; then
  SKILL_SCRIPTS_DIR="$(git -C "$HERE" rev-parse --show-toplevel)/scripts"
fi
PYTHONPATH="$TARGET/$COMMON_REPO:$SKILL_SCRIPTS_DIR" python3 -c "import adversarial_common, adversarial_spec; print('OK: adversarial_common + adversarial_spec imported')"
echo "Done."
