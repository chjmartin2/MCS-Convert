#!/usr/bin/env python
"""Author demos/RCDEMO.RCT — a little tune that exercises the RCT effects.

Sixteen bars in A minor: an arpeggiated chord channel (the classic 8-bit Axy
shimmer), a vibrato'd lead with a portamento phrase-end, an octave-ornament
bass, and a noise backbeat. Run from the repo root:

    python demos/make_rcdemo.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcs_convert import rct as R                     # noqa: E402
from mcs_convert.streams import perf_chunks          # noqa: E402

N = R.midi_to_note                                   # midi -> cell note


def pattern(rows=32):
    return R.RctPattern(rows=rows)


def put(pat, row, ch, note=None, inst=0, vol=0, fx=0, param=0):
    c = pat.cell(row, ch)
    if note is not None:
        c.note = note
    c.inst, c.vol, c.fx, c.param = inst or c.inst, vol, fx, param


song = R.RctSong(title="RC DEMO", author="RetroComputerist",
                 comment="arp / vibrato / portamento / ornament demo",
                 channel_mode=R.MODE_3TONE_NOISE, tempo_byte0=0x7A, speed=4)
song.patterns.clear()
song.instruments = {
    1: R.RctInstrument(name="lead", waveform="pulse25", volume=14),
    2: R.RctInstrument(name="chord", waveform="square", volume=9),
    3: R.RctInstrument(name="bass", waveform="triangle", volume=15, ornament=1),
}
song.ornaments = {
    1: R.RctOrnament(name="octbass", steps=[0, 0, 12, 0], loop=0),
}

Am, F, C, G = 57, 53, 48, 55                          # chord roots (A2 area +12)

# --- pattern 0: intro — chords + bass + hats --------------------------------
p0 = pattern()
for bar, root in enumerate((Am, F, C, G)):
    r = bar * 8
    # channel 1: chord root with an Axy arpeggio (minor for Am, major else)
    arp = 0x37 if root == Am else 0x47                # +3+7 minor / +4+7 major
    put(p0, r, 0, note=N(root + 12), inst=2, fx=R.FX_ARP, param=arp)
    for rr in range(r + 1, r + 8):
        put(p0, rr, 0, fx=R.FX_ARP, param=arp)        # keep it shimmering
    # channel 3: ornament bass hits (the instrument's octave-blip table)
    put(p0, r, 2, note=N(root - 12), inst=3)
    put(p0, r + 4, 2, note=N(root - 12), inst=3)
    # channel 4: noise — kick on the 1, hat on the off-beats
    put(p0, r, 3, note=N(45))                         # dark = kick
    put(p0, r + 2, 3, note=N(88))                     # bright = hat
    put(p0, r + 4, 3, note=N(45))
    put(p0, r + 6, 3, note=N(88))

# --- pattern 1: the lead enters — vibrato phrases ----------------------------
p1 = pattern()
for row in range(32):                                 # carry the backing
    for ch in range(1, 4):
        p1.cells[row][ch] = p0.cells[row][ch]
    p1.cells[row][0] = p0.cells[row][0]
melody = [(0, 69), (2, 72), (4, 76), (8, 74), (12, 72), (16, 71),
          (20, 69), (24, 67), (28, 69)]
for row, midi in melody:
    put(p1, row, 1, note=N(midi), inst=1)
    if row % 8 == 4:                                  # long notes get vibrato
        for rr in range(row + 1, min(32, row + 4)):
            put(p1, rr, 1, fx=R.FX_VIBRATO, param=0x6C)

# --- pattern 2: portamento answer — the lead slides between notes ------------
p2 = pattern()
for row in range(32):
    for ch in (0, 2, 3):
        p2.cells[row][ch] = p0.cells[row][ch]
slides = [(0, 76, 0), (4, 81, 0x30), (12, 79, 0x18), (20, 76, 0x18),
          (28, 74, 0x10)]
for row, midi, rate in slides:
    if rate:
        put(p2, row, 1, note=N(midi), inst=1, fx=R.FX_PORTA, param=rate)
        for rr in range(row + 1, min(32, row + 3)):
            put(p2, rr, 1, fx=R.FX_PORTA, param=rate)
    else:
        put(p2, row, 1, note=N(midi), inst=1)

# --- pattern 3: outro — volume-slide fade ------------------------------------
p3 = pattern()
for row in range(32):
    for ch in range(4):
        p3.cells[row][ch] = p1.cells[row][ch]
for row in range(16, 32):
    put(p3, row, 0, fx=R.FX_VOLSLIDE, param=0x01)
    if row == 31:
        for ch in range(4):
            p3.cell(row, ch).note = R.NOTE_OFF

song.patterns = {0: p0, 1: p1, 2: p2, 3: p3}
song.order = [0, 1, 2, 1, 3]
song.perf = perf_chunks(song)

out = Path(__file__).parent / "RCDEMO.RCT"
R.save(str(out), song)
print(f"wrote {out} ({out.stat().st_size} bytes) — "
      f"{len(song.order)} positions, {len(song.patterns)} patterns")
