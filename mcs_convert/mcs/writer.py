"""Emit Music Construction Set (IBM-PC 1984) .MCS files.

The inverse of reader.py, and the format's strongest validation: serializing the records
parsed from any of the 80 corpus songs reproduces the original file byte-for-byte
(see tests/test_mcs_writer.py), and a generated file loaded in the real program under
MartyPC is the ground-truth check that our encoding matches MCS's own.

File layout (see docs/mcs-format.md):
  0x00..0x04  scroll/view bytes (display state; pitch-independent)
  0x05        uint16 TIME-SIGNATURE word = 0x3AF9 + 3*code (0=2/4,1=4/4,2=6/8,3=3/4)
  0x07        uint16 (usually 0)
  0x09        uint16 staff-1 section size   = serialized length of staff 1 up to but
  0x0B        uint16 staff-2 section size      NOT including its (0,0) separator
  0x0D        uint16 total file length
  0x0F        one 0x00 pad byte, then the FF FF (count,prev) record stream
"""

from __future__ import annotations

import struct
from typing import List, Sequence, Tuple

from .reader import (
    MIDI_ANCHOR, Record, TIMESIG_BASE, TIMESIG_STEP, _BASS_WINDOW, _TREBLE_WINDOW,
    parse_records,
)

# Default scroll bytes (view state only; copied from a normal treble+bass song).
DEFAULT_SCROLL = bytes([0x83, 0x86, 0x86, 0x77, 0x77])
Entry = Tuple[int, int]


# --- entry encoding (inverse of reader's field extraction) -----------------------

def make_entry(symbol: int, v: int, x_slot: int) -> Entry:
    """Pack (symbol, vertical position v, horizontal slot) into a (byte0, byte1) entry."""
    byte0 = (symbol & 0x1F) | ((v & 7) << 5)
    byte1 = ((x_slot & 0x1F) << 3) | ((v >> 3) & 7)
    return byte0, byte1


def v_for_midi(midi: int, v_base: int) -> int:
    """Vertical position for a natural MIDI note in the staff whose window starts at
    v_base (1 = treble E7..G4, 21 = bass G5..B2). Raises if the note is off the window."""
    window = _TREBLE_WINDOW if v_base == 1 else _BASS_WINDOW
    want = (midi - MIDI_ANCHOR) * 2
    for idx, val in enumerate(window):
        if val == want:
            return idx + v_base
    raise ValueError(f"MIDI {midi} is not on the {'treble' if v_base == 1 else 'bass'} "
                     f"natural staff")


# --- record / file assembly ------------------------------------------------------

def serialize_records(records: Sequence[Record]) -> bytes:
    """Serialize records to the FF FF (count, prev) entry stream. Byte-exact inverse of
    parse_records (verified against all 80 corpus songs)."""
    out = bytearray()
    for r in records:
        out += b"\xff\xff"
        out.append(r.count & 0xFF)
        out.append(r.prev & 0xFF)
        for b0, b1 in r.entries:
            out.append(b0 & 0xFF)
            out.append(b1 & 0xFF)
    return bytes(out)


def _link_prev(measures: List[List[Entry]]) -> List[Record]:
    """Turn per-measure entry lists into records, filling the (count, prev) back-links
    and appending the (0, last_count) end-of-staff record MCS expects."""
    records: List[Record] = []
    prev = 0
    for entries in measures:
        records.append(Record(len(entries), prev, list(entries)))
        prev = len(entries)
    records.append(Record(0, prev, []))          # end-of-staff marker
    return records


def build_file(staves: List[List[List[Entry]]], *, time_sig: int = 1,
               scroll: bytes = DEFAULT_SCROLL, word7: int = 0,
               tempo_level: int = None) -> bytes:
    """Assemble a complete .MCS from staves (each a list of measures, each a list of
    entries). Section sizes, total length, and the (0,0) staff separators are computed.

    `time_sig` is the meter CODE written at 0x05 (0=2/4, 1=4/4, 2=6/8, 3=3/4) — the
    real tempo lives in scroll[0], not here. (`tempo_level` is the old, wrong name
    for this argument, kept so existing callers don't break.)"""
    if tempo_level is not None:
        time_sig = tempo_level
    body = bytearray()
    sizes: List[int] = []
    for measures in staves:
        staff_bytes = serialize_records(_link_prev(measures))
        sizes.append(len(staff_bytes))           # size excludes the (0,0) separator
        body += staff_bytes
        body += b"\xff\xff\x00\x00"               # staff separator
    # 0x09 = staff-1 size; 0x0B = EVERYTHING after staff-1's separator up to the
    # final one (= body - s1 - the two 4-byte separators). For two staves this is
    # exactly staff-2's size, so the corpus round-trip is unchanged; for 3-4
    # staves it correctly spans staves 2..N (writing only s2 here is what made an
    # earlier multi-staff attempt load as "Not an MCS song"). Verified against the
    # 3-staff PRETTY6 (522) and 4-staff GOOD.
    s1 = sizes[0] if len(sizes) > 0 else 0
    s2 = (len(body) - s1 - 8) if len(sizes) > 1 else 0
    total = 0x0F + 1 + len(body)                  # header + pad + records

    header = bytearray(0x0F)
    header[0:5] = scroll
    struct.pack_into("<H", header, 0x05, TIMESIG_BASE + TIMESIG_STEP * time_sig)
    struct.pack_into("<H", header, 0x07, word7)
    struct.pack_into("<H", header, 0x09, s1)
    struct.pack_into("<H", header, 0x0B, s2)
    struct.pack_into("<H", header, 0x0D, total)
    return bytes(header) + b"\x00" + bytes(body)


def rewrite(path_in: str) -> bytes:
    """Read a .MCS and re-emit it from its parsed records. Byte-identical to the input —
    the round-trip that proves the record model. (Header/pad preserved verbatim.)"""
    with open(path_in, "rb") as fh:
        d = fh.read()
    first = d.index(b"\xff\xff")
    return d[:first] + serialize_records(parse_records(d))
