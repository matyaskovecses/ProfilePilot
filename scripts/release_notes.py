#!/usr/bin/env python3
"""Print the CHANGELOG.md section of one version (the GitHub release notes).

Usage::

    python scripts/release_notes.py 0.1.0            # notes of 0.1.0
    python scripts/release_notes.py v0.1.0 --check   # also fail unless pyproject.toml has that version

A section starts at a ``## <version>`` heading (anything may follow the version, e.g.
``## 0.1.0 — first public release``) and ends at the next ``## `` heading. Only the standard library
is used.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NotesError(Exception):
    pass


def normalize_version(tag: str) -> str:
    """``v0.1.0`` / ``refs/tags/v0.1.0`` -> ``0.1.0``."""
    version = tag.strip().rsplit("/", 1)[-1]
    version = version[1:] if version[:1] in ("v", "V") else version
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[.-]?(?:a|b|rc|dev|post)\.?\d+)?", version):
        raise NotesError(f"'{tag}' is not a version tag like v1.2.3")
    return version


def section(changelog: str, version: str) -> str:
    lines = changelog.splitlines()
    heading = re.compile(rf"^##\s+v?{re.escape(version)}(?:\s|$)")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise NotesError(f"CHANGELOG.md has no '## {version}' section")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    body = "\n".join(lines[start + 1:end]).strip()
    if not body:
        raise NotesError(f"the CHANGELOG.md section of {version} is empty")
    return body + "\n"


def pyproject_version(root: Path = ROOT) -> str:
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"))
    if not match:
        raise NotesError("pyproject.toml has no version")
    return match.group(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="version or tag, e.g. 0.1.0 or v0.1.0")
    parser.add_argument("--check", action="store_true", help="fail unless pyproject.toml has this version")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        version = normalize_version(args.version)
        if args.check and pyproject_version(args.root) != version:
            raise NotesError(f"tag {args.version} does not match pyproject.toml version {pyproject_version(args.root)}")
        notes = section((args.root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
        sys.stdout.flush()
        sys.stdout.buffer.write(notes.encode("utf-8"))  # UTF-8 whatever the console's code page
        sys.stdout.flush()
    except (NotesError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
