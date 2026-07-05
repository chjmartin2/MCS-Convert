"""Parse a Music Construction Set (IBM-PC 1984) song into our neutral Song model.

This is the inverse of the (still-blocked) writer and doubles as living documentation of
the format decoded in docs/mcs-format.md. Pitch decoding is solid; note *durations* and
*rests* now come from byte0's low nibble (note value + rest flag). Accidentals are still
dropped.

Structure (confirmed):
  0x00..0x0C  header (tempo/view/staff-offsets — partially decoded)
  0x0D uint16 total file length
  0x0F..      body: FF FF (count, prev_count) records; each record's `count` note
              entries follow as (byte0, byte1) pairs.
  Staves are separated by (0, prev) then (0, 0) terminator records. The first record of
  each staff is the clef glyph (byte0 0x06 treble / 0x0d bass); its byte1 is the staff's
  vertical anchor.

Pitch (confirmed against Minuet in G's rising G-A-B-C-D opening):
  byte1 rises 16 units per diatonic staff step; higher byte1 = higher pitch.
  steps_above_G4 = (byte1 - clef_byte1 + 80) / 16     # 80 = G4's offset below the
                                                       # treble clef's stored anchor
  Then walk the C-major white keys (accidentals not yet decoded — see byte0 TODO).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from ..model import NoteEvent, Song, Track

CLEF_TREBLE_B0 = 0x06
CLEF_BASS_B0 = 0x0D
G4_MIDI = 67
# byte1 is a vertical PIXEL position; DIATONIC_STEP is the pixels per diatonic staff step.
# NOTE: this is a per-song zoom — 16 for most songs (calibrated on MINUETG), but MCS zooms
# out for wide-range pieces (SCALE.MCS uses 8). Not yet auto-detected; see docs/mcs-format.md.
DIATONIC_STEP = 16
# Pitch of each clef's stored anchor (byte1 == clef's own byte1), in diatonic steps from G4:
#   treble anchor = E5 (+5), from MINUETG ground truth (clef 114, its G4 at byte1 34).
#   bass   anchor = F3 (-8) — what the F-clef points at — from SCALE.MCS, whose continuous
#   white-key scale crosses cleanly from the bass staff (…C4) into the treble staff (D4…).
TREBLE_ANCHOR_STEPS = 5
BASS_ANCHOR_STEPS = -8

# byte0 layout (decoded over the ~80-song corpus + emulator ground truth; see
# docs/mcs-format.md):
#   bits[7:5] = stem/beam render length — varies *linearly with pitch* inside a beam group
#               (a drawing artifact, not musical), so we ignore it for timing.
#   bits[3:0] = note/rest SYMBOL. bit3 (0x08) is the REST flag; the low value picks the value:
#                 notes 1..5 = 16th, 8th, quarter, half, whole
#                 rests 8..12 = 16th, 8th, quarter, half, whole rest
#               Duration (in sixteenth-ticks) is 2**(v-1) for a note (v=nibble) and
#               2**(v-8) for a rest.
#   Ground truth: MINUETG's opening reads quarter + 4 eighths (nibbles 3,2,2,2,2), and an
#   8th note edited to an 8th rest in an emulator changed byte0 0x82 -> 0x89 (nibble 2 -> 9).
#   Records turn out to be whole *measures*: MINUETG's sum to 12 sixteenths = 3/4.
#   Nibbles 0,6,7 (and rest mirrors 13,14,15) are uncommon (~13%) and not yet pinned —
#   likely dotted/ornamented; mapped provisionally below to their measure-completion mode.
REST_FLAG = 0x08
_NOTE_TICKS = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16, 6: 2, 7: 4, 0: 4}   # 6,7,0 provisional
_REST_TICKS = {8: 1, 9: 2, 10: 4, 11: 8, 12: 16, 13: 8, 14: 2, 15: 4}   # 13,14,15 provisional


def decode_duration(byte0: int) -> tuple[bool, int]:
    """(is_rest, duration_ticks) for a note entry's byte0. One tick = one sixteenth."""
    nib = byte0 & 0x0F
    if nib & REST_FLAG:
        return True, _REST_TICKS[nib]
    return False, _NOTE_TICKS[nib]

# Semitone offsets of the white keys within an octave: C D E F G A B.
_WHITE = [0, 2, 4, 5, 7, 9, 11]


def _white_key(steps_above_g4: int) -> int:
    """MIDI note `steps_above_g4` diatonic (white-key) steps above G4."""
    # G is white-key index 4 (C=0). Walk the ladder, carrying octaves.
    idx = 4 + steps_above_g4
    octave, degree = divmod(idx, 7)
    return G4_MIDI + 12 * octave + (_WHITE[degree] - _WHITE[4])


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
    """Group records into staves, breaking on empty (count==0) terminator records."""
    staves: List[List[Record]] = []
    cur: List[Record] = []
    for r in recs:
        if r.count == 0:
            if cur:
                staves.append(cur)
                cur = []
            continue
        cur.append(r)
    if cur:
        staves.append(cur)
    return staves


def treble_pitch(byte1: int, clef_byte1: int, step: int = DIATONIC_STEP) -> int:
    """Decode a treble-staff note's byte1 to a MIDI note (naturals only; accidentals dropped)."""
    steps = round((byte1 - clef_byte1) / step) + TREBLE_ANCHOR_STEPS
    return _white_key(steps)


def bass_pitch(byte1: int, clef_byte1: int, step: int = DIATONIC_STEP) -> int:
    """Decode a bass-staff note's byte1 to a MIDI note (naturals only; accidentals dropped)."""
    steps = round((byte1 - clef_byte1) / step) + BASS_ANCHOR_STEPS
    return _white_key(steps)


def parse(path: str, diatonic_step: int = DIATONIC_STEP) -> Song:
    """Parse an .MCS/.MCD file into a Song (pitch + duration + rests decoded).

    diatonic_step is the byte1 pixels-per-staff-step (the per-song vertical zoom); 16 fits
    most songs, but wide-range pieces are zoomed out (SCALE.MCS uses 8). See docs/mcs-format.md.
    """
    with open(path, "rb") as fh:
        d = fh.read()
    song = Song(title="", source=f"mcs:{path}")
    for si, staff in enumerate(split_staves(parse_records(d))):
        if not staff or not staff[0].entries:
            continue
        clef_b0, clef_b1 = staff[0].entries[0]
        name = {CLEF_TREBLE_B0: "Treble", CLEF_BASS_B0: "Bass"}.get(clef_b0, f"Staff {si}")
        track = Track(name=name)
        tick = 0
        for rec in staff[1:]:               # skip the clef record
            for byte0, byte1 in rec.entries:
                # byte0 -> duration + rest flag; byte1 -> pitch (accidentals still dropped).
                is_rest, dur = decode_duration(byte0)
                # For a rest, byte1 is a glyph position, not a pitch — don't sound it.
                midi = 0 if is_rest else (
                    treble_pitch(byte1, clef_b1, diatonic_step) if clef_b0 == CLEF_TREBLE_B0
                    else bass_pitch(byte1, clef_b1, diatonic_step))
                track.add(NoteEvent(start_tick=tick, duration_ticks=dur,
                                    midi_note=midi, is_rest=is_rest))
                tick += dur
        song.add_track(track)
    return song
