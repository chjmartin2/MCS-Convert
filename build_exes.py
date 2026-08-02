#!/usr/bin/env python
"""Build the standalone Windows executables with PyInstaller.

    python build_exes.py            # RCTracker.exe + RCPlay.exe into dist/
    python build_exes.py --tracker  # just one of them

One-file, windowed (no console). Also drops a fresh RCPLAY.COM next to
them so a release folder is complete: the two Windows apps + the DOS
player + the docs.
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def build(entry: str, name: str) -> None:
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile",
        "--windowed", "--name", name, str(HERE / entry),
        "--distpath", str(HERE / "dist"),
        "--workpath", str(HERE / "build"),
        "--specpath", str(HERE / "build"),
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracker", action="store_true", help="only RCTracker")
    ap.add_argument("--player", action="store_true", help="only RCPlay")
    args = ap.parse_args()
    both = not (args.tracker or args.player)
    if args.tracker or both:
        build("rctracker.py", "RCTracker")
    if args.player or both:
        build("rcplay.py", "RCPlay")
    # a complete release folder gets the DOS player too
    sys.path.insert(0, str(HERE))
    from mcs_convert.rcplay_dos import save_rcplay
    out = HERE / "dist" / "RCPLAY.COM"
    n = save_rcplay(str(out))
    print(f"\ndist/ ready: RCTracker.exe, RCPlay.exe, RCPLAY.COM ({n} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
