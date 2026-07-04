"""Parse a Music Construction Set (IBM-PC 1984) song into our neutral Song model.

This is the inverse of the (still-blocked) writer and doubles as living documentation of
the format decoded in docs/mcs-format.md. Pitch decoding is solid; note *durations* now come
from byte0's low 2 bits (half/quarter/eighth/sixteenth). Accidentals and rests are still
undecoded (see docs) — rests are NOT stored as note entries, so the melody currently plays
gap-free.

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
DIATONIC_STEP = 16          # byte1 units per diatonic staff step
G4_MIDI = 67
# byte1 of the clef anchor minus byte1 of the G4 note (empirical: MINUETG clef 114, G4 34).
TREBLE_G4_OFFSET = 80

# byte0 layout (decoded empirically over the ~80-song corpus; see docs/mcs-format.md):
#   bits[7:5] = stem/beam render length — varies *linearly with pitch* inside a beam group
#               (a drawing artifact, not musical), so we ignore it for timing.
#   bits[1:0] = note-value code, longest-to-shortest: 0=half 1=quarter 2=eighth 3=sixteenth.
#               Confirmed by ground truth: DAISY's "Dai-sy" reads code 0 (long) then 1 (short),
#               and beamed runs in JINGLE/MINUETG read code 2/3. Corpus frequencies (9/25/35/31%)
#               match: long notes rare, eighths/sixteenths common.
#   bits[4:2] = flag bits (dot/accidental/beam?) — not yet decoded; ignored for now.
DURATION_MASK = 0x03
# Ticks per note value. One tick = one sixteenth here; the synth/GUI scale ticks to seconds.
_DURATION_TICKS = {0: 8, 1: 4, 2: 2, 3: 1}   # half, quarter, eighth, sixteenth


def note_duration(byte0: int) -> int:
    """Duration in ticks (sixteenth = 1) from a note entry's byte0."""
    return _DURATION_TICKS[byte0 & DURATION_MASK]

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


def treble_pitch(byte1: int, clef_byte1: int) -> int:
    """Decode a treble-staff note's byte1 to a MIDI note (naturals only, first pass)."""
    steps = round((byte1 - clef_byte1 + TREBLE_G4_OFFSET) / DIATONIC_STEP)
    return _white_key(steps)


def parse(path: str) -> Song:
    """Parse an .MCS/.MCD file into a Song (pitch decoded; durations placeholder)."""
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
                # byte1 -> pitch (accidentals still dropped); byte0 -> duration (see above).
                midi = treble_pitch(byte1, clef_b1) if clef_b0 == CLEF_TREBLE_B0 \
                    else treble_pitch(byte1, clef_b1)  # bass anchor TBD; same math for now
                dur = note_duration(byte0)
                track.add(NoteEvent(start_tick=tick, duration_ticks=dur, midi_note=midi))
                tick += dur
        song.add_track(track)
    return song
