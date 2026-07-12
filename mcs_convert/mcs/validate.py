"""Check whether a generated .MCS stays within the limits the real 1984 program
can play, derived empirically from the 80-song retail+collected corpus.

The corpus is ground truth: no real MCS song ever exceeds these, and files that
do exceed them corrupt on playback in the actual program (our own player is more
tolerant, so it can't catch this — hence a dedicated validator).

The load-bearing one is **entries per measure**: MCS reads each measure into a
fixed 32-entry / 64-byte buffer, so a measure with more than 32 note/rest/glyph
entries overflows it and garbles playback. (Observed: MAPLERAG at 18/measure
plays; a busy NSF conversion at 146/measure corrupts.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .reader import parse_records, split_staves, symbol, vertical, x_slot

# Empirical maxima across all 80 corpus songs (see scratchpad/mcs_envelope.py).
MAX_ENTRIES_PER_MEASURE = 32     # the fixed per-measure buffer — the hard one
MAX_X_SLOT = 30                  # horizontal slot never exceeds this on screen
MAX_STAVES = 4
EDITOR_MAX_BYTES = 4246          # the editor's save buffer (playback tolerates more)


@dataclass
class Issue:
    severity: str                # "corrupt" (won't play) or "warn" (may glitch)
    where: str
    detail: str


def validate(data: bytes) -> List[Issue]:
    """Return the ways `data` exceeds what real MCS can play (empty = good)."""
    issues: List[Issue] = []
    staves = split_staves(parse_records(data))
    if len(staves) > MAX_STAVES:
        issues.append(Issue("warn", "file",
                            f"{len(staves)} staves > {MAX_STAVES} the corpus uses"))
    for si, staff in enumerate(staves):
        for mi, rec in enumerate(staff):
            n = len(rec.entries)
            if n > MAX_ENTRIES_PER_MEASURE:
                issues.append(Issue(
                    "corrupt", f"staff {si} measure {mi}",
                    f"{n} entries > {MAX_ENTRIES_PER_MEASURE}-entry measure buffer "
                    f"(overflows and garbles playback)"))
            for b0, b1 in rec.entries:
                if x_slot(b1) > MAX_X_SLOT:
                    issues.append(Issue(
                        "warn", f"staff {si} measure {mi}",
                        f"x-slot {x_slot(b1)} > {MAX_X_SLOT} (off the staff width)"))
                    break
    if len(data) > EDITOR_MAX_BYTES:
        issues.append(Issue("warn", "file",
                            f"{len(data)} bytes > {EDITOR_MAX_BYTES} editor buffer "
                            f"(plays, but the 1984 editor can't open it to edit)"))
    return issues


def summary(data: bytes) -> str:
    issues = validate(data)
    if not issues:
        return "OK — within all real-MCS limits."
    lines = []
    for sev in ("corrupt", "warn"):
        for iss in issues:
            if iss.severity == sev:
                tag = "CORRUPT" if sev == "corrupt" else "warn"
                lines.append(f"  [{tag}] {iss.where}: {iss.detail}")
    n_corrupt = sum(1 for i in issues if i.severity == "corrupt")
    head = (f"{n_corrupt} corrupting issue(s)" if n_corrupt
            else f"{len(issues)} warning(s)")
    return head + ":\n" + "\n".join(lines)
