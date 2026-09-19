#!/usr/bin/env python3
"""Pre-deploy guard: make sure no secrets or datasets would be committed.

    python scripts/check_secrets.py

Exit code 0 = safe to push. Run it again right before submission.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files that must never be tracked.
FORBIDDEN_FILES = [
    ".env", ".env.local", ".env.production",
    "sdoc.db", "*.sqlite", "*.sqlite3",
    "ground_truth.json",           # official answer key — never commit
]

# Patterns that suggest a hardcoded credential. Matches containing a
# placeholder marker (<...>, YOUR_, <password>, example) are ignored.
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style API key"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{20,}"), "Google API key"),
    (re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:\s]+:[^@\s]{6,}@"),
     "Postgres URL with password"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "JWT token"),
]
PLACEHOLDER = re.compile(r"[<>]|YOUR_|\$\{|example|password\}?$|xxx", re.IGNORECASE)
SCAN_EXT = {".py", ".md", ".yml", ".yaml", ".json", ".txt", ""}
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", "data",
             "inbox", "attachments", ".pytest_cache"}
SKIP_FILES = {"check_secrets.py"}  # holds the patterns themselves


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, check=True)
        return out.stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []  # not a git repo yet — scan the tree instead


def walk_files() -> list[Path]:
    files: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        files.append(p)
    return files


def main() -> int:
    problems: list[str] = []
    warnings: list[str] = []
    tracked = set(tracked_files())

    # 1. sensitive files that git would actually commit
    for f in sorted(tracked):
        name = Path(f).name
        if name in {".env", "sdoc.db", "ground_truth.json"} or \
           name.endswith((".sqlite", ".sqlite3")):
            problems.append(f"TRACKED by git: {f}")
        if "attachment" in f.lower() or f.startswith("inbox/"):
            problems.append(f"dataset file would be committed: {f}")

    # 2. sensitive files present on disk but (hopefully) ignored
    for pattern in FORBIDDEN_FILES:
        for p in ROOT.glob(pattern):
            rel = str(p.relative_to(ROOT))
            if rel in tracked or Path(rel).name in {Path(t).name for t in tracked}:
                continue  # already reported above as a hard failure
            warnings.append(f"present but git-ignored: {rel}")

    # 3. hardcoded secrets in source
    for p in walk_files():
        if p.suffix not in SCAN_EXT or p.name in SKIP_FILES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx, label in SECRET_PATTERNS:
            for m in rx.finditer(text):
                if PLACEHOLDER.search(m.group(0)):
                    continue  # documentation example, not a real credential
                line = text[:m.start()].count("\n") + 1
                problems.append(f"{label} in {p.relative_to(ROOT)}:{line}")
                break

    for w in warnings:
        print("  !  ", w)

    if problems:
        print("NOT SAFE TO PUSH:")
        for p in problems:
            print("  ✗", p)
        return 1

    print("OK — no secrets or sensitive files found.")
    print(f"  scanned: {ROOT}")
    print(f"  git-tracked files: {len(tracked) or 'n/a (not a git repo)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
