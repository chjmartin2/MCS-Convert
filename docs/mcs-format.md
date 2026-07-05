# Music Construction Set (IBM-PC) song format — reverse-engineering log

**Status: largely decoded.** No public spec exists, but with the original 1984 IBM-PC
release, ~80 sample songs, and controlled edit-and-diff experiments in MartyPC
(MIN2/SCALE2/SCALE3/SCALE4), the note encoding is now understood end to end except for
one open rule (octave resolution of large leaps) and a few flags. This file is the
running investigation; update it as facts land.

## Corpus

From the Internet Archive "Will Harvey's Music Construction Set (1984)" DOSBox rip
(`samples/ia_1984/`, gitignored). Contains `MCS.EXE`/`MCSDISK.EXE`, `READ.TXT`, and ~80
song files: 54 `.MCS` + 26 `.MCD` (same format; `.MCD` are the bundled demos). Analyze
with [`tools/mcs_dump.py`](../tools/mcs_dump.py). Ground-truth edits are made on the
self-booting test disk ([martypc-test-disk.md](martypc-test-disk.md)) and extracted with
`tools/make_mcs_dsk.py`'s `extract_file_from_image`.

## File layout (confirmed)

### Header (first 15 bytes)
| Offset | Size | Field | Status |
|--------|------|-------|--------|
| 0x00   | 1    | byte in the 0x77–0x8C range | global vertical scroll? (view state) |
| 0x01   | 4    | four bytes on a ladder of 3 (0x77, 0x7A, … 0x8C) | per-staff vertical scroll positions (view state; possibly top/bottom per staff) |
| 0x05   | 2    | uint16 ~0x3AF9-0x3B02 | hypothesis: tempo |
| 0x07   | 2    | uint16 | ? (often 0; 0x12/0x14 seen) |
| **0x09** | **2** | **uint16 byte size of the staff-1 section** | **CONFIRMED** |
| **0x0B** | **2** | **uint16 byte size of the staff-2 section** | **CONFIRMED** |
| **0x0D** | **2** | **uint16 total file length** | **CONFIRMED (80/80)** |

0x09/0x0B were confirmed by SCALE4: moving one note from the bass staff to the treble
staff changed them by exactly +2/−2 (one 2-byte entry migrating). Each size spans from
the staff's first record up to but not including its `(0,0)` terminator.

Note data begins at **0x0F**.

### Body = two staff sections of measure records (confirmed)
A sequence of records, each `FF FF count prev_count` followed by `count` 2-byte entries.
`prev_count` back-links to the previous record's count (editor convenience).

- **A record is one MEASURE.** Durations in a full record sum to the time signature
  (12 sixteenths for MINUETG's 3/4, 16 for 4/4 songs).
- **`(0, prev)` with prev ≠ 0 is an EMPTY MEASURE**, not a terminator. (SCALE's treble
  measure 1 is empty while the bass plays; in SCALE4 that record gained the moved note.)
- **`(0, 0)` terminates the staff**; the next chain is the second staff of the grand
  staff. Both staves advance measure-by-measure in parallel.
- The **first record of each staff** holds the clef glyph — byte0 `0x06` treble,
  `0x0D` bass — and optionally a **time-signature glyph** (low nibble `0xF`, e.g.
  MINUETG's `(0xEF, 128)` for 3/4). These are display glyphs, not sounding notes; a
  glyph entry's byte1 is its x position (~106–128).

## Note entry = `(byte0, byte1)` — ground-truthed field by field

```
byte0:  [7:5] vertical class   [4] flag?   [3:0] symbol (bit3 = rest)
byte1:  horizontal pixel position within the measure
```

### byte1 = HORIZONTAL position (confirmed)
SCALE2 (move one note one slot right): byte1 +8, nothing else moves. Horizontal slot
spacing is ~8 px for dense 16th passages, ~16 px otherwise, with placement jitter —
MCS reloads exact placement, which is why it's stored. Uses:
- notes at (nearly) the same x share a stem = a **chord** (MINUETG's bass opens with
  two half notes at x=36 — with the quarter that follows, exactly 3/4);
- a staff entering a shared measure late sits to the right (SCALE's treble enters
  measure 2 at slot 4, after the bass's 4 notes).

**Earlier byte1-as-pitch decoding was wrong.** It "worked" on ascending scales because
pitch and x rise together there; MINUETG's opening was decoded as a rising G-A-B-C-D
when the real tune is D5 then G4-A4-B4-C5.

### byte0 bits[3:0] = note/rest symbol (confirmed)
Bit3 (`0x08`) is the **rest flag**; the value is the note value on a doubling ladder:

| nibble | 1 | 2 | 3 | 4 | 5 | | 8 | 9 | 10 | 11 | 12 |
|--------|---|---|---|---|---|-|---|---|----|----|----|
| value  | 16th | 8th | quarter | half | whole | | 16th rest | 8th rest | quarter rest | half rest | whole rest |

Duration in sixteenth-ticks = `2**(v-1)` (notes) / `2**(v-8)` (rests). Ground truth:
MIN2's note→rest edit changed `0x82 → 0x89`. Nibbles 0, 6, 7 (and 13, 14, 15) are
uncommon and still provisional — likely dotted values. Nibbles 6/13 also appear as the
clef glyphs, and 15 as the time signature, in the clef record only.

### byte0 bits[7:5] = vertical position, low 3 bits, INVERTED (confirmed)
One staff step **up** decrements the class, mod 8 (SCALE3: note moved up one position,
`0x21 → 0x01`, byte1 untouched). The frame is **global**: `class == (-pos) % 8` where
`pos` counts diatonic steps from **C4 = class 0** (so B3=1, A3=2, …, D4=7, G4=4,
D5=0…). This one frame fits every ground truth simultaneously:
- MINUETG treble bars 1–4: classes `0,4,3,2,1 | 0,4,4 | 7,1,0,7,6 | 5,4,4` =
  D5 G4 A4 B4 C5 | D5 G4 G4 | E5 C5 D5 E5 F(♯)5 | G5 G4 G4 — the actual Minuet.
- DIXIE's melody: `4,6 | 0,0,0,7,6,5,4,4,4,6` = G E | C C C D E F G G G E —
  "Oh I wish I was in the land of cotton", rhythm and all.
- SCALE's 40-note scale counts down `0,7,6,…,1` continuously across measures **and
  across the bass→treble staff hop** (consistent with a C2..E7 program range).

### The octave is NOT stored (proven) — resolution rule still open
SCALE stores D4, E5 and F6 as **byte-identical `0x81` entries on the same staff** — with
3 class bits there is nothing left to distinguish octaves, so MCS must reconstruct the
coarse vertical from context on load. SCALE4 agrees: moving a note up by 8 positions
(one full class wrap) left byte0 unchanged and only relocated the entry into the other
staff's section.

Our reader resolves each note to the class candidate nearest its predecessor (first note:
nearest a per-clef anchor), **ties broken downward** — from D5, class 4 must give G4
(4 down), not A5 (4 up). That decodes stepwise music and leaps up to a fifth correctly,
which covers the corpus's overwhelming majority. It provably canNOT be MCS's actual rule,
though: MINUETG bar 4 opens with a real G5→G4 drop (7 steps) that nearest-candidate
mis-resolves, yet MCS reloads it fine — and no memoryless previous-note rule can decode
both SCALE's +1 ladders and that drop, since both store a class delta of 1. Whatever MCS
uses (per-staff scroll state? two-pass layout?) lives in MCSDISK.EXE's loader —
disassembling it is the definitive next step. Practical effect meanwhile: passages after
a >fifth leap can ride 8 steps high/low until stepwise motion re-centers; a per-clef
range clamp keeps the drift bounded.

### byte0 bit[4] = unknown flag (~22% of notes)
Set on scattered notes (e.g. DIXIE `0x30|…` entries), never in SCALE, not on MINUETG's
F♯s (so probably not a plain accidental unless key signatures supply those). Candidate
meanings: accidental, tie, staccato/articulation. Pin with an edit-and-diff (add one
sharp / one tie to a saved song).

## Experiment log (MartyPC edit-and-diff)
| File | Edit vs baseline | Diff | Lesson |
|------|------------------|------|--------|
| MIN2 | 8th note → 8th rest | `0x82→0x89`, byte1 `0x32→0x39` | bit3 = rest flag; rest glyph x shifts |
| SCALE2 | last note of m.1 one slot RIGHT | byte1 +8; byte0 `0x21→0x01` | byte1 = horizontal. (byte0 change = the note also slipped one position UP — same signature as SCALE3; reload SCALE2 and check: predicted G3 where F3 was.) |
| SCALE3 | same note one position UP | byte0 `0x21→0x01` only | class bits = vertical, −1 per step up, byte1 untouched |
| SCALE4 | same note up 8 positions (placed as "an octave") | entry moved bass→treble section; counts, prev-links, header sizes 0x09/0x0B all updated; byte0 unchanged | octave not stored per note; staff sections + header sizes confirmed. (A true 7-step octave would have set byte0 `0x41`.) |

## Still open
- **Octave-resolution rule for leaps > a fifth** — disassemble MCSDISK.EXE's song
  loader (it's a plain DOS EXE; find the class-bit extraction and the y computation).
- **bit4** (~22% of notes) — accidental / tie / articulation?
- **Accidentals & key signature** — MINUETG's F♯ carries no per-note mark we've
  identified; key sig probably in the header or the clef record.
- **Dotted nibbles 0, 6, 7** (and 13, 14, 15) — pin with a dotted-note edit.
- **Header 0x00–0x08** — scroll bytes (ladder of 3), tempo word.
- **Writer + round-trip test** ([`mcs_convert/mcs/writer.py`](../mcs_convert/mcs/writer.py)):
  now unblocked for everything except leap octaves.

## Background (research, 2026-07)
*Will Harvey's Music Construction Set*, designed by Will Harvey, published by Electronic
Arts (Apple II 1983; IBM-PC 1984 booter, 1987 re-release). The 1984 IBM release boots
from its own 360K disk; Demonlord's rip supplies `MCSDISK.EXE` (saves to 360K floppies —
what our test disk uses) and `MCS.EXE` (hard-disk version). Sound: PC speaker, 4-note
CPU-bound mode or 1-note with scrolling. ~700 notes max per song.

- Wikipedia: https://en.wikipedia.org/wiki/Music_Construction_Set
- Nerdly Pleasures: http://nerdlypleasures.blogspot.com/2015/02/electronic-arts-music-construction-set.html
- Internet Archive (1984): https://archive.org/details/msdos_Will_Harveys_Music_Construction_Set_1984

## Progress
- [x] Sample corpus (80 songs) + structural dissection (records, staves, clef records).
- [x] Self-booting MartyPC test disk for controlled edit-and-diff experiments.
- [x] byte0 low nibble: durations + rest flag (records = measures).
- [x] byte1 = horizontal position; chords (shared x); late-entry measures.
- [x] byte0[7:5] = vertical class (global frame, C4 ≡ 0, inverted); header staff sizes.
- [x] Octave reconstruction good to a fifth; MINUETG/DIXIE decode to their real tunes.
- [ ] Exact octave rule (disassembly), bit4, accidentals/key sig, dotted values, tempo.
- [ ] Writer + byte-identical round-trip.
