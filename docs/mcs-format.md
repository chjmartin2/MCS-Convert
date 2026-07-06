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
| **0x00** | **1** | **TEMPO** — byte 0 (0x77..0x92, steps of 3) sets the real playback speed. **CALIBRATED AGAINST AUDIO:** seconds per sixteenth-tick = `0.067 + 0.016·step`, `step = (byte0−0x77)//3`. Fits ENTERTAN 0x7a→83ms/181BPM, AXEL/YANKEE 0x80→115ms/130BPM, MINUETG 0x83→131ms, DIXIE 0x89→163ms/92BPM. (Both ENTERTAN and AXEL share 0x05 word 0x3AFC yet play at different speeds — byte 0 is what differs.) |
| 0x01–0x04 | 4 | vertical scroll/view bytes; display state only, pitch-independent. |
| 0x05   | 2 | word `0x3AF9 + 3·n`. Read by the engine but does **not** determine playback tempo (byte 0 does); role unclear — a CPU-speed calibration or fine adjust. |
| 0x07   | 2 | ? (often 0; a few songs hold 16/18/20 — likely the clef+key-sig pixel width, not meter) |
| 0x09   | 2 | byte size of the staff-1 section (confirmed by SCALE4: +2/−2 when a note moved between staves) |
| 0x0B   | 2 | byte size of the staff-2 section |
| 0x0D   | 2 | total file length |

### Derived metadata (tempo / time signature / key / volume)
The reader surfaces three display values; a fourth (volume) provably isn't in the file.
- **Tempo** — from **header byte 0** (see the header table), measured against real audio.
  Earlier attempts keyed tempo to the 0x05 word; that was wrong (two 0x3AFC songs,
  ENTERTAN and AXEL, play at 83 vs 115 ms/tick — byte 0 is what differs). The absolute
  value is approximate (repeat structure blurs the total-duration estimate).
- **Time signature** — **not stored**; the engine just plays measures back to back, so
  meter is emergent. We report the modal measure length in thirty-second-ticks
  (24→3/4, 32→4/4, 16→2/4…). Corpus: 4/4 ×39, 3/4 ×12, 2/4 ×11, 1/4 ×5, 6/8 ×3, plus a
  handful of pickup/irregular songs. MINUETG → 3/4 ✓. Two timing conventions matter
  when laying measures on a grid: a measure holding a **lone whole rest means "rest
  the whole measure"** whatever the meter (BUMBLE's 2/4 bass opens with four), and the
  grid must be the **modal** measure length, not the maximum — one long finale bar
  otherwise inserts silence into every measure of the song.
- **Key signature** — from the accidental glyphs in the clef record (see below).
  MINUETG's single sharp → **G major** ✓.
- **Volume** — **there is none.** The note word is fully accounted for (x, v, symbol);
  the PC-speaker output is 1-bit. The engine's Tandy/SN76489 path (OUT 0xC0) has 4-bit
  attenuation but it's driven by clef/voice config, not per-note or per-song file data.
  In the player, "volume" can only ever be a synth-amplitude control, not song data.

### PC-speaker rendering (the actual 4-voice engine)
MCS's "4-note" mode is **additive 1-bit synthesis**, not voice-multiplexing (the speaker
plays all four voices at once). The tight loop at MCSDISK image **0x1929** keeps four
phase accumulators (`bp/di/bx/si`), adds a per-voice increment (patched per note, ∝
frequency) to each pass, and folds their overflows (`rcl ah,1` ×4) into the single 1-bit
speaker via a PWM step, timed against the CGA retrace (`in al,0x3da`). So the audible
signal is the **sum of four square waves quantised to 1 bit** — hence the grit.
The player's **"PC Speaker" voice** ([`audio._render_pcspeaker`](../mcs_convert/audio.py))
reproduces this (faithful, not cycle-exact): sum the voices' 1-bit squares, then
delta-sigma the sum back to 1 bit. It exists to be compared against real captures.

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

- **treble window** (20 positions): `70 6c 68 66 62 5e 5a 58 54 50 4e 4a 46 42 40 3c 38 36 32 2e`
- **bass window** (20+1): `46 42 40 3c 38 36 32 2e 2a 28 24 20 1e 1a 16 12 10 0c 08 06 00`

The windows are **fixed** (the header scroll bytes only choose which slice is on screen)
and overlap by an octave, which is why a visually continuous scale across the staff hop
(SCALE.MCS) dips an octave.

**Absolute anchor.** `MIDI = value/2 + 44`, from the engine's PIT-divisor table (G4's
divisor 3044 = 1193182/3044 = **392.00 Hz exactly**). This matches the **notation MCS
draws on screen** — the ground truth: e.g. ENTERTAN reads `D..E..C..` in C major, note
for note, exactly as the program displays it.

> **Cautionary note.** A previous revision changed this to `value/2 + 28` ("16 semitones
> low"), chasing pitches read off DOSBox-X audio. That was **wrong**: the 4-voice output
> is 1-bit and polyphonic, and pitch detection on it octave-errors and confuses voices
> badly (a decoded A#5 was detected as C3). The on-screen notation, not the audio, is the
> reliable reference for pitch — reverted to +44.

### Symbols (byte0 & 0x1F)
| sym | meaning |
|-----|---------|
| 0x00–0x05 | note: **32nd**, 16th, 8th, quarter, half, whole → `2^n` thirty-second-ticks. `0x00` is the 32nd (the first note in the program's palette); it was originally dropped, which shortened every measure using it (ALLEGRO, DIE, …). One tick = one 32nd. |
| 0x14–0x19 | the same six notes, **beamed** (value = sym − 0x14). The engine dispatches these to the identical duration handlers (jump table at image 0x22b3, entries aliasing 0x00–0x05). Fast beamed runs store notes entirely this way — **BUMBLE.MCD** (Flight of the Bumblebee) is almost all `0x15` beamed-16ths; dropping them silently gutted the melody. |
| 0x06 / 0x0D | treble / bass clef glyph |
| 0x07–0x0C | rest, same ladder (= note sym + 7; `0x07` = 32nd rest; MIN2 ground truth `0x82→0x89`) |
| 0x0E / 0x0F / 0x10 | natural / sharp / flat glyph (engine values 0x0C, +2, −2) |
| 0x11 | augmentation dot — the engine adds **half the note's own duration** to the sounding note (handler at image 0x245c) |
| 0x12 | 8va/8vb. In the clef record it octave-shifts the whole staff; **mid-measure (612×)** it shifts that measure ±1 octave — up when the glyph sits above the measure's notes, down when below (whole-measure scope; a second glyph switches from its x on). Dropping these low-clef-position notes made ENTERTAN's main theme an octave flat. |
| 0x13 | tie/slur mark (848×) — flags the preceding note as carried into the next (same-pitch = tie, different-pitch = slur). Marked, not merged: the notes already occupy the right total time. |
| 0x1F | the `FF FF` record marker seen as an entry |

**Accidentals & key signature.** `0x0e` / `0x0f` / `0x10` = natural / sharp / flat.
Glyphs in the *clef record* build the key signature: the glyph's staff degree
`(v−1) mod 7` gets ±1 semitone in **every octave** (the scan at image 0x1600 replicates
it across three 7-slot tables per staff). MINUETG carries a `0x0f` (sharp) on the F line
— the G-major key signature — which is how its F♯ is stored with no per-note mark. Inside
a measure the same glyphs set a **measure-scoped accidental at that exact position** and
mean the same thing (sharp raises, flat lowers): ENTERTAN's main theme is the chromatic
D-D#-E, the D# from a body `0x0f`. The per-position override table is cleared each measure.

> A previous revision "inverted" the body accidentals (0x0f lowers) to chase a
> mis-detected audio pitch; that was part of the same bad-audio mistake as the anchor and
> has been reverted — sharp raises, flat lowers, in both the clef record and the body.

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

## Writing & round-trip (validation)
[`mcs_convert/mcs/writer.py`](../mcs_convert/mcs/writer.py) is the inverse of the reader
and the format's strongest self-check:
- **`serialize_records` reproduces all 80 corpus songs byte-for-byte** (`rewrite(path)`
  == the original file). Every field of the record model is therefore confirmed exact.
- `build_file` assembles a valid header (tempo, the two section sizes = each staff's
  serialized length **minus its `(0,0)` separator**, total length, the `0x0F` pad byte)
  and staff bodies from note entries.
- [`tools/make_test_mcs.py`](../tools/make_test_mcs.py) emits **MCSTEST.MCS**, one song
  exercising every element the reader claims — all five note durations, their beamed
  forms, all five rests, the whole-rest-fills-measure convention, mid-measure sharp/
  flat/natural, a key signature, a dotted note, chords, an empty measure, both clefs,
  and an 8va staff. It is injected onto the MartyPC boot disk (`add_file_to_image`) so
  the real program can render it and confirm our encoding matches MCS's own.

## Multi-staff
Two corpus songs (GOOD.MCS, PRETTY6.MCS) have 3–4 staves; the reader decodes them all
(no longer capped at 2). Each staff's pitch window is chosen by where its notes actually
sit (v 1–20 → treble, v 21–41 → bass) rather than by staff order.

## Remaining unknowns (playback-immaterial)
- **Solved:** 0x00 is the **32nd note** and 0x07 the **32nd rest** (see the symbol table) —
  previously listed here as "note-like but no duration fits"; they are now decoded, one
  tick = one 32nd. 0x1B occurs only inside SCALES.MCS's clef record (a staff-header glyph,
  not a note) and is ignored. 0x14 (beamed 32nd) never occurs.
- **Mid-staff clef change** (`0x06`/`0x0D` inside a measure, 16 songs): shown as a tracker
  event marker (`G`/`F`) but pitch **re-windowing is not yet applied** — notes after a
  mid-staff clef change may read in the wrong octave in those songs.
- Symbols 0x1A/0x1C–0x1E (rare, timing-control handlers) still unmapped. None appear in
  enough songs to affect playback; MCSTEST.MCS + MartyPC is the way to pin them with a
  controlled edit if a song ever sounds thin.
- **Note-symbol dispatch could not be isolated by emulation.** The image exceeds 64 KB,
  so the note engine's true segment base is not the image base; a Unicorn harness over
  the isolated `0x22ac` dispatch resolves the on-screen *drawing* handlers, not the
  playback duration path, and the latter needs the full runtime (segments, PIT timer,
  video) reconstructed. The behaviour is instead validated end-to-end by the round-trip
  above and by the decoded music being correct.
- **Absolute tempo** remains a calibration (relative steps faithful, level 1 = 120 BPM);
  the true rate is in the PIT timing loop, not yet reduced.
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
