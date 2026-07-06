"""Make the skill root and the adversarial-common sibling importable."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "adversarial-common"))


VALID_SPEC = """---
name: "demo-feature"
version: "1.0"
author: "adversarial-spec"
status: "draft"
tags: [adversarial, spec]
targets:
  - file: src/demo.py
    description: "Add the demo entry point"
---

# Demo feature

## Problem
Users cannot demo.

## Requirements
- R1: the tool exposes a `demo` command.

## Acceptance criteria
- AC1 (R1): running `demo` exits 0 and prints "ok".
"""


@pytest.fixture
def git_repo(tmp_path):
    """A fresh git repo with a pinned identity and an initial commit."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "Initial commit", "-q"],
                   cwd=tmp_path, check=True)
    return tmp_path


def last_commit_message(repo):
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
