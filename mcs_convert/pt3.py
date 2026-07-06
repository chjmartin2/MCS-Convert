"""Import ProTracker 3 / Vortex Tracker II modules (.pt3) into the Song model.

PT3 is the ZX Spectrum / Atari ST scene's standard tracker format for the
AY-3-8910 — three pure tone channels, note-based patterns, no PCM samples.
That makes it the best-quality source for MCS conversion: the notes are stored
explicitly (no CPU emulation, no pitch detection), and the chip's constraints
already match MCS's.

This is a NOTE extractor, not a player: PT3 "samples" (per-frame volume/detune
tables), envelopes, and effects shape timbre on real hardware but don't change
which notes exist, so they are parsed past and dropped. Ornaments (per-frame
semitone-offset tables — the AY arpeggio mechanism) are read but only their
base offset is applied; a fast-cycling ornament chord collapses to its root.

Format reference: Vince Weaver's "How to decode a Vortex Tracker II PT3 File"
(deater.net) and the AY_emul source it derives from.

Timing: a pattern row lasts `delay` frames at 50 Hz. We map each row to 1, 2,
4, or 8 thirty-second ticks — whichever lets an MCS tempo byte (header byte 0)
match the row rate best — so converted songs play at their original speed.
"""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

from .model import NoteEvent, Song, Track

# Two signatures exist in the wild for the same header layout: ProTracker's own,
# and Vortex Tracker II's (e.g. "Vortex Tracker II 1.0 module: ...").
_MAGICS = (b"ProTracker 3.", b"Vortex Tracker II")

# Inline parameter bytes consumed immediately after a command byte.
_INLINE = {0x10: 1}                              # $10: sample number
# Effect parameter bytes consumed AFTER the line-ending byte, by on-disk code.
_FX_PARAMS = {0x01: 3, 0x02: 5, 0x03: 1, 0x04: 1, 0x05: 2, 0x08: 3, 0x09: 1}

_FX_SET_SPEED = 0x09


class PT3Error(ValueError):
    pass


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00")[0].decode("latin-1").strip()


def _u16(d: bytes, off: int) -> int:
    return struct.unpack_from("<H", d, off)[0]


class _Channel:
    """One AY channel's decoder state across the whole pattern order."""

    def __init__(self) -> None:
        self.row = 0                 # absolute row position
        self.skip = 1                # rows per line-event
        self.ornament = 0
        self.notes: List[Tuple[int, int]] = []   # (row, note_index) onsets
        self.offs: List[int] = []                # rows where the channel went silent

    def note_on(self, row: int, index: int, orn_base: int) -> None:
        self.notes.append((row, index + orn_base))

    def note_off(self, row: int) -> None:
        self.offs.append(row)


def _decode_pattern(data: bytes, addr: int, ch: _Channel, orn_base,
                    speed_changes: List[Tuple[int, int]]) -> int:
    """Walk one channel's nul-terminated stream for one pattern, starting at
    absolute row `ch.row`. Returns the number of rows the stream covered."""
    pos = addr
    start_row = ch.row
    pending_fx: List[int] = []
    while True:
        b = data[pos]
        pos += 1
        if b == 0x00:
            break
        if 0x01 <= b <= 0x0F:
            pending_fx.append(b)
            continue
        if b == 0x10:
            pos += 1                                # sample number
            continue
        if 0x11 <= b <= 0x1F:
            pos += 4                                # env period(2) + delay + sample
            continue
        if 0x20 <= b <= 0x3F:                        # noise value
            continue
        if 0x40 <= b <= 0x4F:                        # set ornament
            ch.ornament = b & 0x0F
            continue
        if 0xB0 == b:                                # env off, orn reset
            continue
        if b == 0xB1:
            ch.skip = data[pos]
            pos += 1
            continue
        if 0xB2 <= b <= 0xBF:
            pos += 2                                # envelope period
            continue
        if 0xC1 <= b <= 0xCF:                        # volume
            continue
        if 0xD1 <= b <= 0xEF:                        # set sample
            continue
        if 0xF0 <= b <= 0xFF:                        # init ornament + sample
            ch.ornament = b & 0x0F
            pos += 1                                # sample*2
            continue
        # --- line enders: a note, note-off, or empty line ---------------------
        if 0x50 <= b <= 0xAF:
            ch.note_on(ch.row, b - 0x50, orn_base(ch.ornament))
        elif b == 0xC0:
            ch.note_off(ch.row)
        elif b != 0xD0:                              # $D0 = empty line (sustain)
            raise PT3Error(f"unknown pattern byte 0x{b:02x} at 0x{pos - 1:x}")
        for fx in pending_fx:
            n = _FX_PARAMS.get(fx, 0)
            if fx == _FX_SET_SPEED:
                speed_changes.append((ch.row, data[pos]))
            pos += n
        pending_fx = []
        ch.row += ch.skip
    return ch.row - start_row


def row_ticks_and_tempo(delay: int) -> Tuple[int, int]:
    """Map a PT3 row (delay frames at 50 Hz) onto the 32nd-tick grid: returns
    (ticks_per_row, mcs_tempo_byte0) minimizing the tempo error."""
    row_seconds = max(1, delay) / 50.0
    best = (1, 0x77, 1e9)
    for ticks in (1, 2, 4, 8):
        want = row_seconds / ticks
        step = round((2 * want - 0.067) / 0.016)
        if not 0 <= step <= 9:
            continue
        got = (0.067 + 0.016 * step) / 2.0
        err = abs(got - want) / want
        if err < best[2]:
            best = (ticks, 0x77 + 3 * step, err)
    return best[0], best[1]


def parse_pt3(data: bytes) -> Tuple[Song, int]:
    """Parse a .pt3 module. Returns (song, mcs_tempo_byte0); song ticks are 32nds."""
    if not any(data.startswith(m) for m in _MAGICS):
        raise PT3Error("not a ProTracker 3 / Vortex Tracker module (bad magic)")
    title = _cstr(data[0x1E:0x3E])
    author = _cstr(data[0x42:0x62])
    delay = data[0x64]
    pat_table = _u16(data, 0x67)

    # ornament base offsets (value at the table's start; the arpeggio root)
    orn_bases = []
    for i in range(16):
        addr = _u16(data, 0xA9 + i * 2)
        if 0 < addr < len(data) - 2 and data[addr + 1] > 0:   # loop, length, values
            orn_bases.append(int.from_bytes(data[addr + 2:addr + 3], "big", signed=True))
        else:
            orn_bases.append(0)

    def orn_base(n: int) -> int:
        return orn_bases[n & 0x0F]

    # pattern order list at $C9, terminated by $FF, entries are pattern*3
    order = []
    pos = 0xC9
    while data[pos] != 0xFF:
        order.append(data[pos] // 3)
        pos += 1

    chans = [_Channel() for _ in range(3)]
    speed_changes: List[Tuple[int, int]] = []
    for pat in order:
        base = pat_table + pat * 6
        addrs = [_u16(data, base + k * 2) for k in range(3)]
        start = max(c.row for c in chans)
        covered = 0
        for ch, addr in zip(chans, addrs):
            ch.row = start
            covered = max(covered, _decode_pattern(data, addr, ch, orn_base,
                                                   speed_changes))
        for ch in chans:
            ch.row = start + covered              # keep channels in lockstep

    ticks_per_row, byte0 = row_ticks_and_tempo(delay)

    song = Song(title=title or "PT3 module", source=f"pt3:{author}" if author else "pt3")
    total_rows = max(c.row for c in chans)
    for name, ch in zip("ABC", chans):
        track = Track(name=f"AY {name}")
        events = sorted(ch.notes)
        offs = sorted(ch.offs)
        for i, (row, idx) in enumerate(events):
            nxt = events[i + 1][0] if i + 1 < len(events) else total_rows
            end = next((o for o in offs if row < o <= nxt), nxt)
            midi = 24 + idx                       # PT3 note 0 = C-1 (~MIDI 24)
            dur = max(1, (end - row) * ticks_per_row)
            track.add(NoteEvent(start_tick=row * ticks_per_row,
                                duration_ticks=dur, midi_note=midi))
        if track.notes:
            song.add_track(track)
    if speed_changes:
        # MCS is fixed-tempo; converted at the initial speed.
        song.source += f" ({len(speed_changes)} speed changes ignored)"
    return song, byte0
