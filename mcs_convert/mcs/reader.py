"""Parse a Music Construction Set (IBM-PC 1984) song into our neutral Song model.

This is the inverse of the (still-blocked) writer and doubles as living documentation of
the format decoded in docs/mcs-format.md.

Structure (confirmed):
  0x00..0x08  header: 0x00 global scroll?; 0x01..0x04 per-staff vertical scroll values
              (a ladder in steps of 3: 0x77, 0x7a, ... 0x8c); 0x05..0x08 tempo? flags?
  0x09 uint16 byte size of the staff-1 section   (CONFIRMED via SCALE4: moving one note
  0x0B uint16 byte size of the staff-2 section    between staves moved 2 bytes here)
  0x0D uint16 total file length (CONFIRMED)
  0x0F..      body: FF FF (count, prev_count) records; each record's `count` note
              entries follow as (byte0, byte1) pairs.

  Records are MEASURES. A (0, prev) record is an EMPTY MEASURE (confirmed: SCALE's
  treble measure 1 is empty while the bass plays, and gained a note in SCALE4).
  A (0, 0) record is the staff terminator; the next chain is the second staff.
  The first record of each staff holds the clef glyph (byte0 0x06 treble / 0x0D bass)
  and optionally a time-signature glyph (low nibble 0xF).

Note entry (byte0, byte1) — ground-truthed with MartyPC edit-and-diff experiments:
  byte1      = HORIZONTAL pixel position within the measure (SCALE2: moving a note one
               slot right changed only byte1, +8). Layout only, except that it reveals
               chords (same x = same stem) and a staff's late entry into a measure.
  byte0[3:0] = note/rest symbol. bit3 = rest flag; value 1..5 = 16th/8th/quarter/half/
               whole on a doubling ladder (rests 8..12 mirror it).
  byte0[4]   = unknown flag (~22% of corpus notes; accidental or tie — open).
  byte0[7:5] = the LOW 3 BITS of the vertical staff position, inverted: one step UP in
               pitch decrements the class mod 8 (SCALE3: up one step, 0x21 -> 0x01;
               SCALE's 40-note scale counts down 0,7,6,...,1 continuously, even across
               the bass->treble staff hop).

  The octave/coarse vertical is NOT stored per note (proof: SCALE stores D4, E5 and F6
  as byte-identical 0x81 entries on one staff). MCS reconstructs it from context; we
  resolve each note to the class candidate nearest its predecessor, ties broken
  downward (D5 -> class 4 must give G4, a fourth down, not A5 a fifth up — MINUETG).
  That reproduces every ground-truth melody except leaps > a fifth (MINUETG bar 4's
  G5->G4 octave-ish drop decodes as A5); MCS's exact rule is still open.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..model import NoteEvent, Song, Track

CLEF_TREBLE_B0 = 0x06
CLEF_BASS_B0 = 0x0D
REST_FLAG = 0x08
# Duration in sixteenth-ticks per low-nibble symbol. Nibbles 6,7,0 (and rest mirrors
# 13,14,15) are uncommon and provisional — likely dotted values; see docs/mcs-format.md.
_NOTE_TICKS = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16, 6: 2, 7: 4, 0: 4}
_REST_TICKS = {8: 1, 9: 2, 10: 4, 11: 8, 12: 16, 13: 8, 14: 2, 15: 4}

# Semitone offsets of the white keys within an octave: C D E F G A B.
_WHITE = [0, 2, 4, 5, 7, 9, 11]
C4_MIDI = 60

# Staff positions are diatonic steps relative to C4 (C4=0, D4=1, ... C5=7, D5=8).
# The class frame is GLOBAL: class == (-pos) % 8 with C4 = 0 fits MINUETG, DIXIE and
# SCALE simultaneously (no per-song shift). A staff's first note is resolved into the
# octave window around a per-clef anchor, fitted to ground truth: D5 (+8) starts
# MINUETG's treble correctly, and G3/B3 (around -4) starts its bass chord correctly.
# (MCS's true first-note rule — probably the header's per-staff scroll state — is open.)
CLEF_ANCHOR = {CLEF_TREBLE_B0: 8, CLEF_BASS_B0: -4}
# Plausible position range per clef (staff plus generous ledger room). Octave
# resolution is contextual, so a mis-resolved big leap shifts everything after it by
# 8 steps; without a backstop that drift compounds and walks off the MIDI range in
# leap-heavy songs. Out-of-range resolutions are pulled back by whole 8-step wraps.
CLEF_RANGE = {CLEF_TREBLE_B0: (-7, 28), CLEF_BASS_B0: (-24, 11)}
# Entries this close in x share a stem — a chord (MINUETG's bass opens with two half
# notes at the same x; DIXIE has pairs 1 px apart).
CHORD_X_SLOP = 3


def decode_duration(byte0: int) -> tuple[bool, int]:
    """(is_rest, duration_ticks) for a note entry's byte0. One tick = one sixteenth."""
    nib = byte0 & 0x0F
    if nib & REST_FLAG:
        return True, _REST_TICKS[nib]
    return False, _NOTE_TICKS[nib]


def pitch_class(byte0: int) -> int:
    """The 3-bit vertical class from byte0[7:5] (inverted: +1 step up = class-1 mod 8)."""
    return byte0 >> 5


def resolve_position(cls: int, ref: int) -> int:
    """Absolute staff position (diatonic steps from C4) for a class, nearest `ref`.

    Candidates satisfy class == (-pos) % 8 and repeat every octave-plus-one (8 steps);
    the nearest to `ref` wins, ties broken DOWNWARD (ground truth: from D5, class 4 is
    G4 — a fourth below — not A5 a fifth above; both are 4 steps away).
    """
    base = (-cls) % 8
    lo = base + 8 * ((ref - base) // 8)      # nearest candidate at or below ref
    hi = lo + 8
    return lo if (ref - lo) <= (hi - ref) else hi


def position_to_midi(pos: int) -> int:
    """MIDI note of a staff position (diatonic steps from C4), naturals only."""
    octave, degree = divmod(pos, 7)
    return C4_MIDI + 12 * octave + _WHITE[degree]


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
    """Group records into staves. Only a (0, 0) record terminates a staff; a (0, prev)
    record with prev != 0 is an EMPTY MEASURE and is kept in place."""
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
class _Slot:
    """One rhythmic position in a measure: a note, rest, or chord (shared stem/x)."""
    x: int
    duration: int
    is_rest: bool
    positions: List[int]             # resolved staff positions (empty for a rest)


def _decode_staff_slots(staff: List[Record], clef_b0: int) -> List[List[_Slot]]:
    """Decode one staff's measures into slots with resolved vertical positions."""
    ref = CLEF_ANCHOR.get(clef_b0, 8)
    lo, hi = CLEF_RANGE.get(clef_b0, (-24, 28))
    measures: List[List[_Slot]] = []
    for rec in staff[1:]:                    # staff[0] is the clef/time-sig record
        slots: List[_Slot] = []
        for byte0, byte1 in rec.entries:
            is_rest, dur = decode_duration(byte0)
            if is_rest:
                slots.append(_Slot(byte1, dur, True, []))
                continue
            pos = resolve_position(pitch_class(byte0), ref)
            while pos < lo:
                pos += 8
            while pos > hi:
                pos -= 8
            ref = pos
            if slots and not slots[-1].is_rest and abs(byte1 - slots[-1].x) <= CHORD_X_SLOP:
                slots[-1].positions.append(pos)
                slots[-1].duration = max(slots[-1].duration, dur)
            else:
                slots.append(_Slot(byte1, dur, False, [pos]))
        measures.append(slots)
    return measures


def _measure_span(slots: List[_Slot]) -> int:
    return sum(s.duration for s in slots)


def parse(path: str) -> Song:
    """Parse an .MCS/.MCD file into a Song (pitch, duration, rests, chords, measures).

    Staves are time-aligned by measure: records are measures, empty (0, prev) records
    are silent measures, and a staff that enters a shared measure late (SCALE's treble
    picks up mid-measure where the bass leaves off) is front-padded when its first x
    is to the right of the other staff's.
    """
    with open(path, "rb") as fh:
        d = fh.read()
    song = Song(title="", source=f"mcs:{path}")

    staves = split_staves(parse_records(d))
    decoded: List[Tuple[str, List[List[_Slot]]]] = []
    for si, staff in enumerate(staves):
        if not staff or not staff[0].entries:
            continue
        clef_b0 = staff[0].entries[0][0]
        name = {CLEF_TREBLE_B0: "Treble", CLEF_BASS_B0: "Bass"}.get(clef_b0, f"Staff {si}")
        decoded.append((name, _decode_staff_slots(staff, clef_b0)))

    # One measure length for the whole song = the fullest measure seen (3/4 -> 12, 4/4 -> 16).
    measure_len = max((_measure_span(m) for _, ms in decoded for m in ms), default=16)

    for idx, (name, measures) in enumerate(decoded):
        track = Track(name=name)
        for mi, slots in enumerate(measures):
            tick = mi * measure_len
            span = _measure_span(slots)
            if slots and span < measure_len:
                # Late entry? Front-pad iff another staff starts this measure further left.
                other_first_x = min(
                    (ms[mi][0].x for j, (_, ms) in enumerate(decoded)
                     if j != idx and mi < len(ms) and ms[mi]),
                    default=None)
                if other_first_x is not None and slots[0].x > other_first_x:
                    tick += measure_len - span
            for s in slots:
                if s.is_rest:
                    track.add(NoteEvent(start_tick=tick, duration_ticks=s.duration,
                                        midi_note=0, is_rest=True))
                else:
                    for pos in s.positions:
                        track.add(NoteEvent(start_tick=tick, duration_ticks=s.duration,
                                            midi_note=position_to_midi(pos)))
                tick += s.duration
        song.add_track(track)
    return song
