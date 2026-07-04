"""Analysis tool for reverse-engineering Music Construction Set (IBM-PC 1984) songs.

Not part of the shippable package — a workbench for dissecting sample .MCS/.MCD files.
Point it at extracted sample songs (kept out of git) to explore the byte layout.

    python tools/mcs_dump.py path/to/SONG.MCS          # annotated dump of one song
    python tools/mcs_dump.py --scan path/to/dir        # header-field table over a corpus

Confirmed so far (see docs/mcs-format.md):
  * offset 0x0D: uint16 total file length (holds for 80/80 sample songs).
  * note data is a series of FF FF <a> <b> delimited records; between markers are
    2-byte note entries whose 2nd byte tracks pitch (rises through a scale).
Everything else printed here is hypothesis to be confirmed.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys


def u16(d: bytes, off: int) -> int:
    return d[off] | (d[off + 1] << 8)


def scan(paths):
    """Print header-field table across many songs to spot invariants."""
    print(f"{'file':16} {'size':>5} {'len@0D':>6} {'w@09':>5} {'w@0B':>5} "
          f"{'b5':>3} {'b6':>3}  head[0:5]")
    for path in paths:
        d = open(path, "rb").read()
        if len(d) < 15:
            continue
        name = os.path.basename(path)
        flag = "" if u16(d, 0x0D) == len(d) else "  <-len mismatch"
        head = " ".join(f"{b:02x}" for b in d[0:5])
        print(f"{name:16} {len(d):5} {u16(d,0x0D):6} {u16(d,0x09):5} {u16(d,0x0B):5} "
              f"{d[5]:3} {d[6]:3}  {head}{flag}")


def parse_records(d):
    """Yield (offset, count, prev, [(byte0,byte1), ...]) for each FF FF record."""
    i = 0x0F
    while i < len(d) - 1:
        if d[i] == 0xFF and d[i + 1] == 0xFF:
            count = d[i + 2] if i + 2 < len(d) else 0
            prev = d[i + 3] if i + 3 < len(d) else 0
            j = i + 4
            pairs = []
            while j < len(d) - 1 and not (d[j] == 0xFF and d[j + 1] == 0xFF):
                pairs.append((d[j], d[j + 1]))
                j += 2
            yield i, count, prev, pairs
            i = j
        else:
            i += 1


def split_staves(records):
    """Group records into staves. A staff ends at a (0, prev) record; the trailing
    (0,0) separates staves. Returns list of staves, each a list of note entries
    (the leading clef record is kept as staff[0])."""
    staves, cur = [], []
    for _off, count, _prev, pairs in records:
        if count == 0:
            if cur:
                staves.append(cur)
                cur = []
            continue
        cur.append(pairs)
    if cur:
        staves.append(cur)
    return staves


def notes(path):
    """Print ordered pitch (byte1) stream per staff, for melody-contour matching."""
    d = open(path, "rb").read()
    print(f"# {os.path.basename(path)}  ({len(d)} bytes)")
    staves = split_staves(parse_records(d))
    for si, staff in enumerate(staves):
        clef = staff[0][0] if staff and staff[0] else (0, 0)
        melody = [n for rec in staff[1:] for n in rec]  # skip leading clef record
        b1 = [n[1] for n in melody]
        b0 = [n[0] for n in melody]
        diffs = [b1[k + 1] - b1[k] for k in range(len(b1) - 1)]
        kind = {106: "treble", 108: "bass"}.get(clef[1], f"clef?{clef[1]}")
        print(f"\n  staff {si} ({kind}, clef entry={clef}):  {len(b1)} notes")
        print(f"    pitch(byte1): {b1}")
        print(f"    diffs:        {diffs}")
        print(f"    attr (byte0): {[hex(x) for x in b0]}")


def dump(path):
    """Annotated structural dump of a single song."""
    d = open(path, "rb").read()
    print(f"# {os.path.basename(path)}  ({len(d)} bytes)\n")
    print("HEADER (first 16 bytes)")
    print("  " + " ".join(f"{b:02x}" for b in d[:16]))
    print(f"  0x00 bytes[0:5]      = {[hex(b) for b in d[:5]]}  (view/range? hypothesis)")
    print(f"  0x05 uint16          = {u16(d,0x05)}  (0x{u16(d,0x05):04x}) tempo? hypothesis")
    print(f"  0x09 uint16          = {u16(d,0x09)}  section/voice offset? hypothesis")
    print(f"  0x0B uint16          = {u16(d,0x0B)}  section/voice offset? hypothesis")
    print(f"  0x0D uint16          = {u16(d,0x0D)}  TOTAL FILE LENGTH (confirmed)"
          + ("  OK" if u16(d, 0x0D) == len(d) else "  MISMATCH"))

    print("\nRECORDS (FF FF <a> <b> markers, note-pairs between)")
    i = 0x0F
    rec = 0
    while i < len(d) - 1:
        if d[i] == 0xFF and d[i + 1] == 0xFF:
            a = d[i + 2] if i + 2 < len(d) else -1
            b = d[i + 3] if i + 3 < len(d) else -1
            # collect note-pairs until next FFFF
            j = i + 4
            pairs = []
            while j < len(d) - 1 and not (d[j] == 0xFF and d[j + 1] == 0xFF):
                pairs.append((d[j], d[j + 1]))
                j += 2
            pitches = [p[1] for p in pairs]
            durs = [p[0] for p in pairs]
            print(f"  rec {rec:2} @0x{i:03x}  tag=({a:3},{b:3})  n={len(pairs):2}  "
                  f"pitch(byte1)={pitches}")
            if pairs:
                print(f"                          dur/attr(byte0)={[hex(x) for x in durs]}")
            i = j
            rec += 1
        else:
            i += 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="song file, or directory when using --scan")
    ap.add_argument("--scan", action="store_true", help="header table over a directory")
    ap.add_argument("--notes", action="store_true", help="ordered pitch stream per staff")
    args = ap.parse_args(argv)

    if args.notes:
        notes(args.path)
    elif args.scan:
        paths = sorted(glob.glob(os.path.join(args.path, "*.MC[SD]")))
        if not paths:
            print(f"no .MCS/.MCD files under {args.path}", file=sys.stderr)
            return 1
        scan(paths)
    else:
        dump(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
