"""Generate TESTTONE.MCS — a deliberately simple, unambiguous validation song.

Play it in MCS (Tandy 3-voice), capture the audio, and every note's pitch and
timing is trivially predictable, so we can confirm the encoder + real-MCS
playback are correct in isolation from any chiptune conversion.

Layout (all quarter notes = 8 thirty-second ticks, ~one every 0.4s):
  1. SYNC: three short A4 taps then a rest, so the capture's start is obvious.
  2. TREBLE CHROMATIC RUN: every semitone from G4 (the window bottom) up to E7
     (the top) and back down — 34 notes up, 34 down. Confirms every treble
     pitch maps right and nothing drops.
  3. BASS CHROMATIC RUN: B2 up to G5 and back, on the bass staff.
  4. THREE-VOICE CHORD TEST: four sustained triads (C, F, G, C) to confirm all
     three Tandy voices sound together.

Run:  python demos/make_test_tone.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcs_convert.mcs.encode import encode_song                 # noqa: E402
from mcs_convert.mcs.reader import parse as mcs_parse           # noqa: E402
from mcs_convert.mcs.validate import summary                    # noqa: E402
from mcs_convert.model import NoteEvent, Song, Track            # noqa: E402
from mcs_convert.pitch import midi_to_name                      # noqa: E402

Q = 8                          # a quarter note in 32nd-ticks


def build() -> Song:
    song = Song(title="TEST TONE")
    treble = Track(name="Treble")
    bass = Track(name="Bass")
    # Everything is a quarter note (8 ticks) on the quarter grid, and each section
    # starts on a 32-tick barline, so no note ever splits across a measure — the
    # captured sequence is exactly the notes listed here, in order.
    t = 0

    # 1. sync: three A4 quarter taps + a quarter rest = one full measure
    for _ in range(3):
        treble.add(NoteEvent(t, Q, 69))
        t += Q
    t += Q                                                       # rest to barline

    # 2. treble chromatic run G4..E7 and back, quarter notes (68 notes = 17 bars)
    up = list(range(67, 101))
    for midi in up + up[::-1]:
        treble.add(NoteEvent(t, Q, midi))
        t += Q
    t += 4 * Q                                                   # one empty bar

    # 3. bass chromatic run B2..F4 and back — kept BELOW the treble/bass split
    #    (G4) so every note lands on the bass staff, not spilling up to treble
    tb = t
    down = list(range(47, 66))
    for midi in down + down[::-1]:
        bass.add(NoteEvent(tb, Q, midi))
        tb += Q
    t = tb + 4 * Q

    # 4. three-voice triads: C, F, G, C — each a half note (16 ticks)
    for root, third, fifth in ((60, 64, 67), (65, 69, 72), (67, 71, 74), (72, 76, 79)):
        treble.add(NoteEvent(t, 16, fifth))
        treble.add(NoteEvent(t, 16, third))
        bass.add(NoteEvent(t, 16, root))
        t += 16

    song.add_track(treble)
    song.add_track(bass)
    return song


def main() -> int:
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TESTTONE.MCS")
    song = build()
    # a calm, clearly-spaced tempo: byte0 0x83 ~ 131 BPM 16th, so a quarter ~0.4s
    data = encode_song(song, tempo_byte0=0x83)
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"wrote {out} ({len(data)} bytes)")
    print("validate:", summary(data))
    back = mcs_parse(out)
    tre = [n.midi_note for n in back.tracks[0].notes if not n.is_rest]
    print(f"treble notes ({len(tre)}): first {[midi_to_name(m) for m in tre[:8]]} ...")
    print(f"expected sync = A4 A4 A4 then G4 G#4 A4 ... up to E7 and back")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
