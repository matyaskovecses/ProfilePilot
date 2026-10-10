"""Release tooling: CHANGELOG notes per version, the tag/version check, and the release workflow's shape."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def notes():
    spec = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts" / "release_notes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    yield module
    sys.modules.pop("release_notes", None)


CHANGELOG = """# Changelog

## 0.2.0 — next

- New thing.

## 0.1.0 — first public release

### Profiles
- Isolated profiles.

## 0.0.9

- Old.
"""


def test_sections_are_cut_at_the_next_version(notes):
    assert notes.section(CHANGELOG, "0.1.0") == "### Profiles\n- Isolated profiles.\n"
    assert notes.section(CHANGELOG, "0.2.0") == "- New thing.\n"
    assert notes.section(CHANGELOG, "0.0.9") == "- Old.\n"
    with pytest.raises(notes.NotesError, match="no '## 0.1.1' section"):
        notes.section(CHANGELOG, "0.1.1")
    with pytest.raises(notes.NotesError, match="no '## 0.1' section"):
        notes.section(CHANGELOG, "0.1")  # never matches 0.1.0 by prefix


def test_tags_are_normalized(notes):
    assert notes.normalize_version("v0.1.0") == notes.normalize_version("refs/tags/v0.1.0") == "0.1.0"
    assert notes.normalize_version("1.2.3rc1") == "1.2.3rc1"
    for bad in ("main", "v1.2", "v1.2.3; rm -rf /"):
        with pytest.raises(notes.NotesError):
            notes.normalize_version(bad)


def test_the_real_changelog_has_notes_for_the_current_version(notes, capsys):
    assert notes.main([f"v{notes.pyproject_version()}", "--check"]) == 0
    out = capsys.readouterr().out
    assert out.strip() and not out.startswith("## ")
    assert notes.main(["v99.0.0", "--check"]) == 1
    assert "does not match pyproject.toml" in capsys.readouterr().err


def test_release_workflow_builds_every_artifact_into_a_draft():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for fragment in ("tags:", "release_notes.py", "--check", "python -m build", "build_mcpb.py", "SHA256SUMS",
                     "--draft", "contents: write", "id-token: write", "vars.PYPI_PUBLISH == 'true'"):
        assert fragment in text, fragment
    assert "secrets.PYPI" not in text  # trusted publishing only: no long-lived token in the repo
