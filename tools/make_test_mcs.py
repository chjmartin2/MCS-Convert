"""Generate a comprehensive Music Construction Set test song.

Emits samples/MCSTEST.MCS, a single file that exercises every element the reader claims
to understand, so it can be loaded in the real program (MartyPC) and cross-checked
measure by measure. Each measure below is annotated with what it is meant to demonstrate;
the script also parses its own output back and prints the decoded result, so the intended
encoding and the reader agree before it ever reaches the emulator.

Run:  python tools/make_test_mcs.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcs_convert.mcs.reader import (           # noqa: E402
    CLEF_BASS, CLEF_TREBLE, SYM_DOT, SYM_FLAT, SYM_NATURAL, SYM_OCTAVA, SYM_SHARP, parse,
)
from mcs_convert.mcs.writer import build_file, make_entry, v_for_midi   # noqa: E402
from mcs_convert.pitch import midi_to_name     # noqa: E402

# Symbol values (see docs/mcs-format.md).
N16, N8, N4, N2, N1 = 1, 2, 3, 4, 5                 # note durations
B16, B8, B4, B2, B1 = 0x15, 0x16, 0x17, 0x18, 0x19  # beamed notes (same durations)
R16, R8, R4, R2, R1 = 8, 9, 10, 11, 12              # rest durations
TICKS = {N16: 1, N8: 2, N4: 4, N2: 8, N1: 16, B16: 1, B8: 2, B4: 4, B2: 8, B1: 16,
         R16: 1, R8: 2, R4: 4, R2: 8, R1: 16}
REST_V = {1: 10, 21: 30}                            # a mid-staff glyph slot for rests


class MeasureBuilder:
    """Accumulates entries for one measure, auto-spacing x by note duration."""

    def __init__(self, v_base: int):
        self.v_base = v_base
        self.entries = []
        self.x = 2

    def note(self, midi, sym):
        self.entries.append(make_entry(sym, v_for_midi(midi, self.v_base), self.x))
        self.x += TICKS[sym]
        return self

    def chord(self, midis, sym):
        for m in midis:                            # same x slot => a chord
            self.entries.append(make_entry(sym, v_for_midi(m, self.v_base), self.x))
        self.x += TICKS[sym]
        return self

    def rest(self, sym):
        self.entries.append(make_entry(sym, REST_V[self.v_base], self.x))
        self.x += TICKS[sym]
        return self

    def accidental(self, midi, glyph):             # glyph sits at the note's v, 2 slots before it
        self.entries.append(make_entry(glyph, v_for_midi(midi, self.v_base), self.x))
        self.x += 2
        return self

    def dot(self):                                 # dots the previous note
        self.entries.append(make_entry(SYM_DOT, REST_V[self.v_base], self.x))
        return self

    def done(self):
        return self.entries


def M(v_base):
    return MeasureBuilder(v_base)


def build():
    # ---- Treble staff: key of G major (one sharp in the clef record) --------------
    # Clef-record coordinates mirror MINUETG's exactly so the real program lays it out
    # identically: treble clef glyph at v16/x14, key-signature F-sharp at v7/x16.
    treble_clef = [make_entry(CLEF_TREBLE, 16, 14),
                   make_entry(SYM_SHARP, 7, 16)]                     # key sig: F sharp
    T = [
        treble_clef,
        M(1).note(72, N1).done(),                                     # whole note C5
        M(1).note(72, N2).note(76, N2).done(),                        # two half notes
        M(1).note(67, N4).note(71, N4).note(74, N4).note(79, N4).done(),   # 4 quarters
        M(1).note(72, N8).note(74, N8).note(76, N8).note(77, N8)
             .note(79, N8).note(77, N8).note(76, N8).note(74, N8).done(),  # 8 eighths
        M(1).note(72, N16).note(74, N16).note(76, N16).note(77, N16)
             .note(79, N16).note(81, N16).note(83, N16).note(84, N16)
             .note(83, N16).note(81, N16).note(79, N16).note(77, N16)
             .note(76, N16).note(74, N16).note(72, N16).note(71, N16).done(),  # 16 x 16th
        M(1).note(72, B16).note(74, B16).note(76, B16).note(77, B16)
             .note(79, B16).note(81, B16).note(83, B16).note(84, B16)
             .note(84, B8).note(83, B8).note(81, B8).note(79, B8).done(),  # beamed run
        M(1).rest(R1).done(),                                         # whole rest = whole bar
        M(1).rest(R2).rest(R2).done(),                               # two half rests
        M(1).rest(R4).note(72, N4).rest(R4).note(76, N4).done(),    # quarter rests + notes
        M(1).rest(R16).note(72, N16).rest(R16).note(74, N16)       # 16th rests + notes
             .rest(R8).note(76, N8).rest(R8).note(77, N8)           # 8th rests + notes
             .rest(R4).done(),                                      # + a quarter rest
        M(1).accidental(72, SYM_SHARP).note(72, N4)                 # C#5 (sharp)
             .accidental(76, SYM_FLAT).note(76, N4)                 # Eb5 (flat)
             .accidental(77, SYM_NATURAL).note(77, N4)              # F natural (cancels key sig)
             .note(79, N4).done(),
        M(1).note(72, N2).dot().note(79, N4).done(),               # dotted half + quarter
        M(1).chord([72, 76, 79], N2).chord([71, 74, 79], N2).done(),  # two triads (chords)
        [],                                                          # empty measure
    ]

    # ---- Bass staff: demonstrates the 8va glyph (whole staff sounds an octave up) --
    # Bass clef at v32/x14 (mirroring MINUETG's bass clef), then the 8va glyph placed
    # like INVENT.MCD's (v near the top of the staff, out to the right).
    bass_clef = [make_entry(CLEF_BASS, 32, 14),
                 make_entry(SYM_OCTAVA, 24, 22)]                    # 8va for the staff
    B = [
        bass_clef,
        M(21).note(48, N4).note(52, N4).note(55, N4).note(60, N4).done(),  # C3 E3 G3 C4 (+8va)
        M(21).note(60, N2).note(55, N2).done(),
        M(21).note(48, N1).done(),
    ]
    return build_file([T, B], tempo_level=1, word7=18)


def main():
    data = build()
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "samples", "MCSTEST.MCS")
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"wrote {out} ({len(data)} bytes)")

    song = parse(out)
    print(f"time={song.time_signature} key={song.key_signature} tempo={song.tempo_level}")
    for tr in song.tracks:
        print(f"\n{tr.name}: {len(tr.notes)} events")
        line = " ".join(("rest" if n.is_rest else midi_to_name(n.midi_note))
                        + f":{n.duration_ticks}@{n.start_tick}" for n in tr.notes)
        print("  " + line)


if __name__ == "__main__":
    main()
