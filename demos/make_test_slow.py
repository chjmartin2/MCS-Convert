"""Generate TESTSLOW.MCS — an isolated-note test for unambiguous capture checking.

Where TESTTONE packs a fast chromatic run (hard to pitch-detect cleanly),
TESTSLOW plays widely-spaced notes, each a whole note followed by a whole rest,
so every note is an isolated tone with silence around it. In a capture, each
note is trivially separable: a gap of silence, then one clear pitch. Missing a
note shows as silence; a wrong pitch shows plainly. Perfect for confirming the
encoder + MCS play every note at the right pitch and time.

Layout (whole note + whole rest = ~4s per note at this tempo):
  1. Treble sweep: G4, B4, D5, F5, A5, C6, E6, G6, B6, D7 (rising ~3-4 semitones
     each, so adjacent notes never confuse a detector).
  2. Bass sweep: B2, D3, F3, A3, C4, E4 (rising, on the bass staff).
  3. Three isolated triads: C major, F major, G major — the 3-voice test.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcs_convert.mcs.encode import encode_song                 # noqa: E402
from mcs_convert.mcs.validate import summary                    # noqa: E402
from mcs_convert.model import NoteEvent, Song, Track            # noqa: E402
from mcs_convert.pitch import midi_to_name                      # noqa: E402

W = 32                         # a whole note / whole measure in 32nd-ticks


def build() -> Song:
    song = Song(title="TEST SLOW")
    treble = Track(name="Treble")
    bass = Track(name="Bass")
    t = 0

    treble_notes = [67, 71, 74, 77, 81, 84, 88, 91, 95, 98]     # G4..D7
    for midi in treble_notes:
        treble.add(NoteEvent(t, W, midi))                       # whole note
        t += 2 * W                                              # + whole rest

    bass_notes = [47, 50, 53, 57, 60, 64]                       # B2..E4
    for midi in bass_notes:
        bass.add(NoteEvent(t, W, midi))
        t += 2 * W

    for triad in ((60, 64, 67), (65, 69, 72), (67, 71, 74)):    # C, F, G major
        for midi in triad:
            (treble if midi >= 67 else bass).add(NoteEvent(t, W, midi))
        t += 2 * W

    song.add_track(treble)
    song.add_track(bass)
    return song


def main() -> int:
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TESTSLOW.MCS")
    song = build()
    data = encode_song(song, tempo_byte0=0x83)
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"wrote {out} ({len(data)} bytes)")
    print("validate:", summary(data))
    exp = [midi_to_name(n.midi_note) for n in song.tracks[0].notes if not n.is_rest]
    print(f"treble sweep: {exp[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
