#!/usr/bin/env python3
"""Compare the local and published skill versions without modifying files."""

from __future__ import annotations

import base64
import re
import subprocess
import urllib.request
from pathlib import Path

REPO = "Lzjin426/lzj-pdf-to-md"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/main/VERSION"
VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def parse_version(value: str) -> tuple[int, int, int] | None:
    value = value.strip()
    if not re.fullmatch(r"[vV]?\d+(?:\.\d+){0,2}", value):
        return None
    parts = [int(part) for part in value.lstrip("vV").split(".")]
    return tuple((parts + [0, 0])[:3])


def fetch_raw() -> str:
    with urllib.request.urlopen(RAW_URL, timeout=2) as response:
        return response.read(128).decode("utf-8").strip()


def fetch_gh() -> str:
    result = subprocess.run(
        ["gh", "api", f"repos/{REPO}/contents/VERSION", "--jq", ".content"],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    return base64.b64decode(result.stdout.strip()).decode("utf-8").strip()


def main() -> int:
    try:
        local = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        print("CHECK_SKIPPED missing-local-version")
        return 0
    local_version = parse_version(local)
    if local_version is None:
        print("CHECK_SKIPPED invalid-local-version")
        return 0

    remote = ""
    for fetch in (fetch_raw, fetch_gh):
        try:
            remote = fetch()
            if remote:
                break
        except Exception:
            continue
    if not remote:
        print("CHECK_SKIPPED network-unavailable")
        return 0
    remote_version = parse_version(remote)
    if remote_version is None:
        print("CHECK_SKIPPED invalid-remote-version")
    elif remote_version > local_version:
        print(f"UPDATE_AVAILABLE local={local} remote={remote} https://github.com/{REPO}")
    else:
        print(f"UP_TO_DATE local={local} remote={remote}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
