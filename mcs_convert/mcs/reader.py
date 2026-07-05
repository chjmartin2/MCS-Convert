"""Parse a Music Construction Set (IBM-PC 1984) song into our neutral Song model.

The decoding below is no longer inferred — it matches MCSDISK.EXE's own player,
located by disassembly (see docs/mcs-format.md for the full write-up):

  0x00..0x08  header: view/scroll bytes (display only — pitch does NOT depend on
              them) and a tempo word at 0x05 stored as 0x3AF9 + setting.
  0x09 uint16 byte size of the staff-1 section (CONFIRMED)
  0x0B uint16 byte size of the staff-2 section (CONFIRMED)
  0x0D uint16 total file length (CONFIRMED)
  0x0F..      body: FF FF (count, prev_count) records = MEASURES; (0, prev!=0) is an
              empty measure, (0, 0) terminates a staff. Staff 1 then staff 2.

Note entry = 16-bit little-endian word (byte0, byte1):

  bits [15:11] (byte1 top 5) = HORIZONTAL slot, 8-px units
  bits [10:5]  = v, the 6-bit VERTICAL staff position (1-based, smaller = higher;
                 staff 1 uses v 1..20, staff 2 uses v 21..41 — the engine splits at
                 v*4 >= 0x54). The old models missed that byte1's low 3 bits are the
                 high half of the vertical.
  bits [4:0]   = symbol:
                 0x01..0x05  note: 16th, 8th, quarter, half, whole (2**(n-1) ticks)
                 0x06 / 0x0D treble / bass clef glyph
                 0x08..0x0C  rest of the same ladder (n-7)
                 0x0E / 0x0F / 0x10  natural / sharp / flat glyph. In the clef record
                             these build the KEY SIGNATURE (applied at the glyph's
                             staff degree in every octave); inside a measure they set
                             a measure-scoped accidental at that exact position.
                 0x11        augmentation dot: the engine adds half the previous
                             note's duration to it (handler at MCSDISK 0x245c).
                 0x12        in the clef record: 8va for the staff (+12 semitones).
                 0x1F        the FF FF record marker seen as an entry; skipped.

  Pitch: the engine xlats v-1 through a fixed 41-byte grand-staff ladder chosen by
  the two staves' clefs (tables at MCSDISK image 0x5c88..): value = 2 x semitone.
  Accidentals add +/-2, the 8va adds +24, and the result indexes a 68-entry chromatic
  period table whose entry for G4 is PIT divisor 3044 = 392.00 Hz exactly, anchoring
  MIDI = value/2 + 44. Treble staff window = E7..G4, bass = G5..B2 (they overlap by
  an octave; the windows are FIXED — the header scroll bytes are pure view state).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from ..model import NoteEvent, Song, Track

CLEF_TREBLE = 0x06
CLEF_BASS = 0x0D
SYM_NATURAL, SYM_SHARP, SYM_FLAT = 0x0E, 0x0F, 0x10
SYM_DOT = 0x11
SYM_OCTAVA = 0x12
SYM_MARKER = 0x1F

# The per-clef vertical windows, lifted verbatim from MCSDISK.EXE (image 0x5cb1..):
# 2 x semitone per byte, one byte per staff position top-down, 0 = unusable.
_TREBLE_WINDOW = bytes.fromhex("706c686662 5e5a5854504e4a4642403c 3836322e")  # E7..G4
_BASS_WINDOW = bytes.fromhex("4642403c38 3632 2e2a2824201e1a16 12100c080600")  # G5..B2,-
MIDI_ANCHOR = 44          # ladder 0 = G#2: G4's PIT divisor 3044 = 392.00 Hz exactly

_NOTE_TICKS = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16}


def vertical(byte0: int, byte1: int) -> int:
    """The 6-bit staff position: byte1's low 3 bits are the high half."""
    return ((byte1 & 7) << 3) | (byte0 >> 5)


def symbol(byte0: int) -> int:
    return byte0 & 0x1F


def x_slot(byte1: int) -> int:
    """Horizontal position in 8-px slots (byte1's top 5 bits)."""
    return byte1 >> 3


def decode_duration(byte0: int) -> tuple[bool, int]:
    """(is_rest, ticks) for a note/rest symbol. One tick = one sixteenth."""
    sym = symbol(byte0)
    if 1 <= sym <= 5:
        return False, _NOTE_TICKS[sym]
    if 8 <= sym <= 12:
        return True, _NOTE_TICKS[sym - 7]
    raise ValueError(f"symbol 0x{sym:02x} is not a note or rest")


def _u16(d: bytes, off: int) -> int:
    return d[off] | (d[off + 1] << 8)


@dataclass
class Record:
    count: int
    prev: int
    entries: List[Tuple[int, int]]   # (byte0, byte1)


def parse_records(d: bytes) -> List[Record]:
    recs: List[Record] = []
    i = 0x0F
    while i < len(d) - 1:
        if d[i] == 0xFF and d[i + 1] == 0xFF:
            count = d[i + 2] if i + 2 < len(d) else 0
            prev = d[i + 3] if i + 3 < len(d) else 0
            j = i + 4
            entries: List[Tuple[int, int]] = []
            while j < len(d) - 1 and not (d[j] == 0xFF and d[j + 1] == 0xFF):
                entries.append((d[j], d[j + 1]))
                j += 2
            recs.append(Record(count, prev, entries))
            i = j
        else:
            i += 1
    return recs


def split_staves(recs: List[Record]) -> List[List[Record]]:
    """Group records into staves. Only a (0, 0) record terminates a staff; (0, prev)
    with prev != 0 is an EMPTY MEASURE and is kept in place."""
    staves: List[List[Record]] = []
    cur: List[Record] = []
    for r in recs:
        if r.count == 0 and r.prev == 0:
            if cur:
                staves.append(cur)
                cur = []
            continue
        cur.append(r)
    if cur:
        staves.append(cur)
    return staves


@dataclass
class _Staff:
    clef: int
    v_base: int                    # staff 1 notes live at v 1..20, staff 2 at 21..41
    keysig: List[int]              # semitone*2 offset per staff degree (0..6)
    octava: int                    # +24 if the clef record carries an 8va glyph
    records: List[Record]

    def window(self) -> bytes:
        return _TREBLE_WINDOW if self.clef == CLEF_TREBLE else _BASS_WINDOW

    def midi(self, v: int, acc2: int) -> int:
        """MIDI note for vertical position v with accidental offset (2x semitones)."""
        idx = v - self.v_base
        win = self.window()
        if not (0 <= idx < len(win)) or win[idx] == 0:
            return 0
        return (win[idx] + acc2 + self.octava) // 2 + MIDI_ANCHOR


def _read_staff(recs: List[Record], v_base: int) -> _Staff:
    clef = CLEF_TREBLE if v_base == 1 else CLEF_BASS
    keysig = [0] * 7
    octava = 0
    if recs and recs[0].entries:
        for b0, b1 in recs[0].entries:
            sym = symbol(b0)
            if sym in (CLEF_TREBLE, CLEF_BASS):
                clef = sym
            elif sym in (SYM_NATURAL, SYM_SHARP, SYM_FLAT):
                acc = {SYM_NATURAL: 0, SYM_SHARP: 2, SYM_FLAT: -2}[sym]
                keysig[(vertical(b0, b1) - 1) % 7] = acc
            elif sym == SYM_OCTAVA:
                octava = 24
    return _Staff(clef, v_base, keysig, octava, recs[1:])


@dataclass
class _Slot:
    """One rhythmic position in a measure: a note, rest, or chord (same x slot)."""
    slot_x: int
    duration: int
    is_rest: bool
    midis: List[int]


def _decode_measures(staff: _Staff) -> List[List[_Slot]]:
    measures: List[List[_Slot]] = []
    for rec in staff.records:
        slots: List[_Slot] = []
        measure_acc: Dict[int, int] = {}       # explicit accidentals, per position
        for b0, b1 in rec.entries:
            sym = symbol(b0)
            v = vertical(b0, b1)
            if sym in (SYM_NATURAL, SYM_SHARP, SYM_FLAT):
                # 0x0C is the engine's "forced natural" marker (beats the key sig)
                measure_acc[v] = {SYM_NATURAL: 0x0C, SYM_SHARP: 2, SYM_FLAT: -2}[sym]
                continue
            if sym == SYM_DOT:
                # augmentation dot: engine adds half the note's own duration
                for s in reversed(slots):
                    if not s.is_rest:
                        s.duration += s.duration // 2
                        break
                continue
            if 1 <= sym <= 5 or 8 <= sym <= 12:
                is_rest, dur = decode_duration(b0)
                if is_rest:
                    slots.append(_Slot(x_slot(b1), dur, True, []))
                    continue
                acc = measure_acc.get(v, staff.keysig[(v - 1) % 7])
                if acc == 0x0C:
                    acc = 0
                midi = staff.midi(v, acc)
                if slots and not slots[-1].is_rest and x_slot(b1) == slots[-1].slot_x:
                    slots[-1].midis.append(midi)          # chord: same 8-px slot
                    slots[-1].duration = max(slots[-1].duration, dur)
                else:
                    slots.append(_Slot(x_slot(b1), dur, False, [midi]))
            # clefs, 8va, markers, unknown symbols: no time, no pitch
        measures.append(slots)
    return measures


def _span(slots: List[_Slot]) -> int:
    return sum(s.duration for s in slots)


def parse(path: str) -> Song:
    """Parse an .MCS/.MCD file into a Song. Staves are time-aligned by measure;
    a staff entering a shared measure late (first x slot to the right of the other
    staff's) is front-padded by the measure's deficit."""
    with open(path, "rb") as fh:
        d = fh.read()
    song = Song(title="", source=f"mcs:{path}")

    staff_recs = split_staves(parse_records(d))
    decoded: List[Tuple[str, List[List[_Slot]]]] = []
    for si, recs in enumerate(staff_recs[:2]):
        staff = _read_staff(recs, 1 if si == 0 else 21)
        name = {CLEF_TREBLE: "Treble", CLEF_BASS: "Bass"}.get(staff.clef, f"Staff {si}")
        decoded.append((name, _decode_measures(staff)))

    measure_len = max((_span(m) for _, ms in decoded for m in ms), default=16)

    for idx, (name, measures) in enumerate(decoded):
        track = Track(name=name)
        for mi, slots in enumerate(measures):
            tick = mi * measure_len
            deficit = measure_len - _span(slots)
            if slots and deficit > 0:
                other_first = min(
                    (ms[mi][0].slot_x for j, (_, ms) in enumerate(decoded)
                     if j != idx and mi < len(ms) and ms[mi]),
                    default=None)
                if other_first is not None and slots[0].slot_x > other_first:
                    tick += deficit
            for s in slots:
                if s.is_rest:
                    track.add(NoteEvent(start_tick=tick, duration_ticks=s.duration,
                                        midi_note=0, is_rest=True))
                else:
                    for m in s.midis:
                        track.add(NoteEvent(start_tick=tick, duration_ticks=s.duration,
                                            midi_note=m))
                tick += s.duration
        song.add_track(track)
    return song
