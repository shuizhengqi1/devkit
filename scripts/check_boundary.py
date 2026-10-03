#!/usr/bin/env python3
"""Fail when a host-shaped hole survives the public export."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERN = re.compile(r"\.\.com")
SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".html", ".sh", ".txt", ".toml", ".js"}


def main() -> int:
    hits = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.name == "check_boundary.py":
            continue
        if path.suffix.lower() not in SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if PATTERN.search(text):
            hits.append(str(path.relative_to(ROOT)))
    if hits:
        print("\n".join(hits))
        return 1
    print("boundary ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
