# Music Construction Set (IBM-PC) song format — decoded

**Status: decoded.** No public spec exists. The format was recovered in three stages:
corpus analysis (~80 songs), controlled edit-and-diff experiments in MartyPC
(MIN2/SCALE2/SCALE3/SCALE4), and finally a **disassembly of MCSDISK.EXE's playback
engine** (capstone, 16-bit real mode), which replaced all remaining inference with the
program's own tables and arithmetic. The reader in
[`mcs_convert/mcs/reader.py`](../mcs_convert/mcs/reader.py) reproduces the engine and
decodes MINUETG (complete Minuet in G incl. the key-signature F♯), DIXIE, and the
SCALE test file note-perfectly.

## Corpus & tools

Internet Archive "Will Harvey's Music Construction Set (1984)" DOSBox rip
(`samples/ia_1984/`, gitignored): `MCSDISK.EXE` (the disassembly target),
54 `.MCS` + 26 `.MCD` songs (same format; `.MCD` = bundled demos).
Ground-truth edits are made on the self-booting test disk
([martypc-test-disk.md](martypc-test-disk.md)); extract saved files with
`tools/make_mcs_dsk.py`'s `extract_file_from_image`.

## File layout

### Header (15 bytes)
| Offset | Size | Field |
|--------|------|-------|
| 0x00–0x04 | 5 | vertical scroll/view bytes (ladder of 3: 0x77, 0x7A … 0x8C). **Display state only — pitch does not depend on them.** |
| 0x05   | 2 | **tempo word**, stored as `0x3AF9 + 3·level`. `level` (0–3 across the corpus: 13/44/8/15 songs) is fed into the note-timing multiply at image 0x1535. Each level is one equal-tempered semitone (~5.9%) faster. |
| 0x07   | 2 | ? (often 0; a few songs hold 16/18/20 — likely the clef+key-sig pixel width, not meter) |
| 0x09   | 2 | byte size of the staff-1 section (confirmed by SCALE4: +2/−2 when a note moved between staves) |
| 0x0B   | 2 | byte size of the staff-2 section |
| 0x0D   | 2 | total file length |

### Derived metadata (tempo / time signature / key / volume)
The reader surfaces three display values; a fourth (volume) provably isn't in the file.
- **Tempo** — the 0x05 level (above). It's a coarse 4-step speed index, not a BPM. We
  reproduce the *relative* steps faithfully and anchor level 1 (the default) to 120 BPM;
  the absolute anchor is a calibration (true rate depends on the PIT ISR).
- **Time signature** — **not stored**; the engine just plays measures back to back, so
  meter is emergent. We report the modal measure length in sixteenth-ticks
  (12→3/4, 16→4/4, 8→2/4…). Corpus: 4/4 ×39, 3/4 ×12, 2/4 ×11, 1/4 ×5, 6/8 ×3, plus a
  handful of pickup/irregular songs. MINUETG → 3/4 ✓.
- **Key signature** — from the accidental glyphs in the clef record (see below).
  MINUETG's single sharp → **G major** ✓.
- **Volume** — **there is none.** The note word is fully accounted for (x, v, symbol);
  the PC-speaker output is 1-bit. The engine's Tandy/SN76489 path (OUT 0xC0) has 4-bit
  attenuation but it's driven by clef/voice config, not per-note or per-song file data.
  In the player, "volume" can only ever be a synth-amplitude control, not song data.

### Body: measures and staves
`FF FF count prev_count` records, each followed by `count` 2-byte entries.
- **A record is one measure.** `prev_count` back-links the previous record's count.
- `(0, prev≠0)` = **empty measure**; `(0, 0)` = staff terminator. Staff 1, then staff 2.
- The **first record of each staff** is the clef record: the clef glyph (`0x06`
  treble / `0x0D` bass) plus optional **key-signature accidental glyphs** and an
  optional **8va glyph**.

## Note entry = one little-endian 16-bit word

```
 bit 15..11        10..5              4..0
[ x slot (8px) ][ v: vertical pos ][ symbol ]
   = byte1>>3    =(byte1&7)<<3      = byte0 & 0x1F
                  | byte0>>5
```

The engine literally does `AND AX,0x07E0` to extract v (image 0x1633/0x29b2) and
dispatches `(byte0+1) & 0x1F` through a 32-entry jump table (image 0x22ac).

### v — the 6-bit vertical position
1-based, top-down (smaller = higher). **Staff 1 occupies v 1..20, staff 2 v 21..41**
(the engine splits at `4·v ≥ 0x54`). The draw code renders at `y = 2·v + 12` within
the staff. Every earlier wrong model traced to missing that **byte1's low 3 bits are
the high half of v** — they masquerade as ±7px "jitter" in x.

### Pitch: fixed per-clef windows
The engine xlats `v−1` through a 41-byte grand-staff ladder selected by the two
staves' clefs (four concatenated tables at MCSDISK image `0x5c88`; value = 2×semitone,
0 = unusable):

- **treble window** (20 positions): `70 6c 68 66 62 5e 5a 58 54 50 4e 4a 46 42 40 3c 38 36 32 2e` = **E7 down to G4**
- **bass window** (20+1): `46 42 40 3c 38 36 32 2e 2a 28 24 20 1e 1a 16 12 10 0c 08 06 00` = **G5 down to B2**

The windows are **fixed** — the header scroll bytes only choose which slice is on
screen. The two staves overlap by exactly one octave (G4..G5), which is why a
visually continuous scale across the staff hop (SCALE.MCS) actually dips an octave.

Absolute anchor: pitch value → the engine's 68-entry chromatic PIT-divisor table
(image `0x5db9`); G4's divisor is 3044 → 1193182/3044 = **392.00 Hz exactly**, so
`MIDI = value/2 + 44` (ladder zero = G♯2). A parallel ascending table (image
`0x5e43`) holds the 4-voice mode's phase increments.

### Symbols (byte0 & 0x1F)
| sym | meaning |
|-----|---------|
| 0x01–0x05 | note: 16th, 8th, quarter, half, whole → `2^(n−1)` sixteenth-ticks |
| 0x15–0x19 | the same five notes, **beamed** (value = sym − 0x14). The engine dispatches these to the identical duration handlers (jump table at image 0x22b3, entries aliasing 0x01–0x05). Fast beamed runs store notes entirely this way — **BUMBLE.MCD** (Flight of the Bumblebee) is almost all `0x15` beamed-16ths; dropping them silently gutted the melody. |
| 0x06 / 0x0D | treble / bass clef glyph |
| 0x08–0x0C | rest, same ladder (= note sym + 7; MIN2 ground truth `0x82→0x89`) |
| 0x0E / 0x0F / 0x10 | natural / sharp / flat glyph (engine values 0x0C, +2, −2) |
| 0x11 | augmentation dot — the engine adds **half the note's own duration** to the sounding note (handler at image 0x245c) |
| 0x12 | in the clef record: 8va for the staff (+0x18 = +12 semitones) |
| 0x1F | the `FF FF` record marker seen as an entry |

**Accidentals & key signature.** Glyphs in the *clef record* build the key signature:
the glyph's staff degree `(v−1) mod 7` gets ±1 semitone in **every octave** (the scan
at image 0x1600 replicates it across three 7-slot tables per staff). MINUETG carries a
sharp on each staff's F line — the G-major key signature — which is how its F♯ is
stored with no per-note mark. Inside a measure, a glyph sets a **measure-scoped
accidental at that exact position**, overriding the key signature (natural = forced
override 0x0C); the per-position override table is cleared at each measure.

**Chords**: entries in the same 8-px x slot share a stem and sound together
(MINUETG's bass opens with a {G3,B3} half chord + A3 quarter = exactly 3/4).

## How it was cracked (experiment + disassembly log)
| Evidence | Lesson |
|----------|--------|
| MIN2 edit (note→rest): `0x82→0x89` | bit 3 of the symbol = rest; rest = note+7 |
| SCALE2 (move right): byte1 +8 | x = byte1's top 5 bits |
| SCALE3 (move up 1): `0x21→0x01` | v's low half = byte0[7:5], inverted |
| SCALE4 (move up 8): entry migrated staff sections; byte1 `0x93→0x92` | v's high half = byte1's low 3 bits; header 0x09/0x0B = section sizes |
| MCSDISK.EXE `AND AX,0x07E0` (0x1633, 0x29b2) | v is one 6-bit field spanning both bytes |
| xlat ladders at image 0x5c88, jump table at 0x22ac, dot handler 0x245c, key-sig scan 0x1600, PIT table 0x5db9 | fixed pitch windows, symbol map, dots, key signatures, absolute tuning |

Two interim pitch models (byte1-as-pitch; 3-bit class + contextual octave) each fit
the data available at the time and were wrong — the disassembly ended the guessing.

## Remaining minor unknowns
- Symbols 0x00 (252×, 37 distinct verticals — note-like but no duration fits), 0x13
  (425×, dispatches to a skip stub), and 0x1A–0x1E (rare, timing-control handlers at
  image 0x224b). 0x14 never occurs. None are common enough to matter for playback, but
  0x00 is the one worth another controlled edit if a decoded song still sounds thin.
- Header bytes 0x07–0x08; the exact view semantics of the five scroll bytes.
- Playback nuances we approximate: MCS's own inter-staff sync within a measure
  (we front-pad underfilled measures by x slot, which matches SCALE).
- **Writer** ([`mcs_convert/mcs/writer.py`](../mcs_convert/mcs/writer.py)): now fully
  unblocked — every field needed to emit byte-identical files is known.

## Background
*Will Harvey's Music Construction Set*, Electronic Arts (Apple II 1983; IBM-PC 1984
booter). The 1984 IBM release boots from its own 360K disk; Demonlord's DOS rip wraps
the original booter code with an INT 13h shim (`MCSHAND.ASM`), which is why
MCSDISK.EXE contains the original engine verbatim. Sound: PC speaker, 4-voice
CPU-bound mode or 1-note-with-scroll (PIT channel 2). ~700 notes max per song.

- Wikipedia: https://en.wikipedia.org/wiki/Music_Construction_Set
- Internet Archive (1984): https://archive.org/details/msdos_Will_Harveys_Music_Construction_Set_1984
