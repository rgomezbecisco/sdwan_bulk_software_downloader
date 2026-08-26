#!/usr/bin/env python3
"""Root-level wrapper for bulk nohup wget software download to multiple cEdge sites.

Usage:
  python tool_bulk_download_software.py --file hostnames.txt
  python tool_bulk_download_software.py --diagnose --file hostnames.txt
  python tool_bulk_download_software.py --diagnose S10712-HUB
"""

import subprocess
import sys
from pathlib import Path


def _absolutize_file_args(argv: list[str]) -> list[str]:
    """Resolve --file paths against the caller's cwd, since core/ runs with cwd=core."""
    out = []
    expect_path = False

    for arg in argv:
        if expect_path:
            out.append(str(Path(arg).resolve()))
            expect_path = False
            continue

        if arg in ("--file", "-f"):
            out.append(arg)
            expect_path = True
        elif arg.startswith("--file="):
            out.append(f"--file={Path(arg.split('=', 1)[1]).resolve()}")
        else:
            out.append(arg)

    return out


def main() -> int:
    root_dir = Path(__file__).resolve().parent
    core_dir = root_dir / "core"
    target = core_dir / "bulk_download_software.py"
    cmd = [sys.executable, str(target), *_absolutize_file_args(sys.argv[1:])]
    return subprocess.run(cmd, cwd=str(core_dir), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
