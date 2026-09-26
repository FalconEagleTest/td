#!/usr/bin/env python3
"""Flag TDLib schema changes that could silently break plugin.video.telemedia.

TDLib's JSON API ignores fields it doesn't recognize instead of erroring, so
a rebase onto a newer upstream can silently break a caller with no build
failure and no crash -- the exact failure mode documented in this fork's
own hand-off doc's "Plugin schema fixes" table. This script makes that
failure mode loud instead of silent: it extracts the exact schema line for
every TDLib method/type telemedia actually uses (one line per entry in
td_api.tl, e.g. "searchChatMessages chat_id:int53 ... = FoundChatMessages;")
from two commits and diffs them.

Usage:
    python scripts/check_telemedia_api_compat.py <old-ref> <new-ref>
    python scripts/check_telemedia_api_compat.py upstream/master HEAD

Exit code is nonzero if any tracked method's schema changed or disappeared,
so this can gate CI (fail the rebase job loudly) instead of shipping a
silent break.

The tracked method list (scripts/telemedia_api_surface.txt) is a snapshot
of every distinct `'@type': '...'` value telemedia's default.py/service.py
send or construct, extracted via:
    grep -ohE "'@type':\\s*'[a-zA-Z]+'" default.py service.py | ...
Regenerate it if telemedia's own code starts using new TDLib methods --
this script only protects methods it's told about.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TL_PATH = "td/generate/scheme/td_api.tl"
SURFACE_FILE = Path(__file__).parent / "telemedia_api_surface.txt"


def git_show_lines(ref: str, path: str) -> list[str]:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.splitlines()


def extract_schema_line(lines: list[str], method: str) -> str | None:
    for line in lines:
        line = line.strip()
        if line.split(" ", 1)[0] == method:
            return line
    return None


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    old_ref, new_ref = sys.argv[1], sys.argv[2]

    methods = [m.strip() for m in SURFACE_FILE.read_text().splitlines() if m.strip()]

    try:
        old_lines = git_show_lines(old_ref, TL_PATH)
        new_lines = git_show_lines(new_ref, TL_PATH)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: could not read {TL_PATH} at both refs: {e}", file=sys.stderr)
        return 2

    changed = []
    removed = []
    for method in methods:
        old_def = extract_schema_line(old_lines, method)
        new_def = extract_schema_line(new_lines, method)
        if old_def is None and new_def is None:
            continue  # not a td_api.tl entry at all (e.g. a bare error/type literal) -- nothing to compare
        if old_def is not None and new_def is None:
            removed.append((method, old_def))
        elif old_def != new_def:
            changed.append((method, old_def, new_def))

    if not changed and not removed:
        print(f"OK: all {len(methods)} tracked telemedia API methods unchanged between "
              f"{old_ref} and {new_ref}.")
        return 0

    print(f"WARNING: {len(changed)} changed, {len(removed)} removed, out of "
          f"{len(methods)} tracked methods telemedia depends on.\n")
    for method, old_def in removed:
        print(f"REMOVED: {method}")
        print(f"  was: {old_def}\n")
    for method, old_def, new_def in changed:
        print(f"CHANGED: {method}")
        print(f"  old: {old_def}")
        print(f"  new: {new_def}\n")
    print("These are exactly the kind of changes TDLib's JSON API silently "
          "swallows instead of erroring -- check telemedia's call sites for "
          "each one above before trusting this build.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
