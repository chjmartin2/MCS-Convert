"""Check whether a generated .MCS stays within the limits the real 1984 program
can play, derived from the 80-song corpus AND direct playback tests on hardware/
emulator.

The old "32 entries per measure" cap turned out to be a corpus artifact, NOT a
real limit: hand-built test files play measures of 96 entries fine — chords stack
vertically on one x-slot for free. What IS finite is HORIZONTAL POSITIONS: the
x-slot is a 5-bit field, so a measure holds at most 32 distinct onsets (x 0..31),
and a test measure of 32 plays (though MCS renders it cramped and the barline-
region slots x 0..1 draw loosely). The corpus never exceeds 23 positions; the
encoder targets 24 by default, 32 in force mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .reader import parse_records, split_staves, symbol, vertical, x_slot

MIN_X_SLOT = 2                   # x 0/1 are the barline/clef region — notes there
#                                  play but render loosely (force-32 mode uses them)
MAX_X_SLOT = 31                  # the 5-bit x-slot field's ceiling
MAX_POSITIONS = 32               # distinct onsets a measure can hold (= x 0..31)
MAX_STAVES = 4
EDITOR_MAX_BYTES = 4246          # the editor's save buffer (playback tolerates more)

_NOTE_OR_REST = set(range(0, 6)) | set(range(7, 13)) | set(range(0x14, 0x19))


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
    low_x = 0                                     # deduped count (one line)
    for si, staff in enumerate(staves):
        for mi, rec in enumerate(staff):
            positions = {x_slot(b1) for b0, b1 in rec.entries
                         if symbol(b0) in _NOTE_OR_REST}
            if len(positions) > MAX_POSITIONS:    # impossible in a 5-bit field, but
                issues.append(Issue(              # a collision would corrupt timing
                    "corrupt", f"staff {si} measure {mi}",
                    f"{len(positions)} horizontal positions > {MAX_POSITIONS} "
                    f"(x-slots collide — notes merge into chords)"))
            for b0, b1 in rec.entries:
                if mi > 0 and x_slot(b1) < MIN_X_SLOT and symbol(b0) in _NOTE_OR_REST:
                    low_x += 1
    if low_x:
        issues.append(Issue("warn", "file",
                            f"{low_x} note(s) at x-slot < {MIN_X_SLOT} (barline/clef "
                            f"region — plays but renders loosely)"))
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
