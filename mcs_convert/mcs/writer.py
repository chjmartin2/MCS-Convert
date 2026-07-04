"""Encode a Song into Music Construction Set (IBM-PC) format — STUB.

BLOCKED: the MCS IBM-PC song format has no public byte-level specification. Before this
can be written we must reverse-engineer the layout from real sample song files extracted
from the original disk images. See docs/mcs-format.md for the running investigation.

What we already know we'll need to decide once the format is known:
  * How MCS represents pitch (staff position + accidental? absolute semitone?).
  * Its duration model (note values: whole/half/quarter/...); we must quantize the
    NSF's frame-tick durations onto that grid, which needs a tempo/beat estimate.
  * How many simultaneous voices it stores, and how our 3 NES tracks map onto its
    staves (MCS uses a grand staff — treble + bass).
  * Header/metadata (title, tempo, key/time signature) and any checksum/terminator.

Until then, `write_mcs` raises. `quantize_durations` is a format-independent helper we
can build and test now, since duration quantization is needed regardless of byte layout.
"""

from __future__ import annotations

from typing import List

from ..model import Song


def quantize_durations(song: Song, ticks_per_beat: float) -> List:
    """Placeholder for mapping frame-tick durations onto notation note-values.

    Not implemented: the concrete note-value set is dictated by the MCS format, which is
    still unknown. Kept here to mark the seam.
    """
    raise NotImplementedError("duration quantization pending MCS format reverse-engineering")


def write_mcs(song: Song, path: str) -> None:
    """Serialize `song` to an MCS file. Blocked on reverse-engineering the format."""
    raise NotImplementedError(
        "MCS writer is blocked: the IBM-PC MCS song format is undocumented and must be "
        "reverse-engineered from sample files first. See docs/mcs-format.md."
    )
