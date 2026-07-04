# Music Construction Set (IBM-PC) song format — reverse-engineering log

**Status: actively decoded.** No public spec exists, but we now have the original 1984
IBM-PC release and ~80 sample songs, and the structure is largely worked out (below).
This file is the running investigation; update it as facts land.

## Corpus

From the Internet Archive "Will Harvey's Music Construction Set (1984)" DOSBox rip
(`samples/ia_1984/`, gitignored). Contains `MCS.EXE`/`MCSDISK.EXE`, `MCSHAND.ASM`
(Demonlord's INT-13h rip shim — *not* the note format), `READ.TXT`, and ~80 song files:
54 `.MCS` + 26 `.MCD`. Analyze with [`tools/mcs_dump.py`](../tools/mcs_dump.py).

`.MCS` vs `.MCD`: both are songs and share the same structure; `.MCD` are the bundled
demo tunes (demo mode plays the first `.MCD`), `.MCS` is the default save extension.

## Confirmed structure

### File header (first 15 bytes)
| Offset | Size | Field | Status |
|--------|------|-------|--------|
| 0x00   | 1    | ? (0x77-0x86 range) | hypothesis: view/scroll or top staff-position |
| 0x01   | 4    | four bytes in pitch range (e.g. 77 77 89 89) | hypothesis: staff view bounds |
| 0x05   | 2    | uint16 ~0x3AFC-0x3B06 | hypothesis: tempo |
| 0x09   | 2    | uint16 | hypothesis: offset/size of staff 1 region |
| 0x0B   | 2    | uint16 | hypothesis: offset/size of staff 2 region |
| **0x0D** | **2** | **uint16 total file length** | **CONFIRMED (80/80 songs)** |

Note data begins at **0x0F**.

### Note data = doubly-linked records (CONFIRMED)
The body is a sequence of records, each introduced by the marker **`FF FF`** followed by a
2-byte tag **`(count, prev_count)`**:
- `count` = number of 2-byte note entries in *this* record.
- `prev_count` = the `count` of the *previous* record (a back-link for MCS's editor).

Verified across SCALES/YANKEE/BARG/BUGGY: e.g. chain `(1,0) (7,1) (6,7) (8,6) (6,8) ...`.

### Grand staff = two sections (CONFIRMED)
A staff's record chain ends with `(0, prev)` then `(0, 0)`. After the terminator, a **new
chain starts fresh** for the second staff. So each song has two staves — MCS's **treble +
bass grand staff**. (The header words at 0x09/0x0B probably locate these two regions.)

### The clef is the first record (CONFIRMED)
Every treble staff opens with a single-entry record whose note is **pitch 106, byte0 0x06**;
every bass staff opens with **pitch 108, byte0 0x0d**. Constant across all songs ⇒ these are
the **clef glyphs**, not sounding notes. (Time signature may share this leading record —
BARG opens `(2,0)` with a second entry; to confirm.)

### Note entry = `(byte0, byte1)` (pitch CONFIRMED; byte0 partial)
Notes appear in **time order** as stored (early doubt about pitch-sorting was wrong —
those songs simply had scalar/arpeggiated passages).

- **byte1 = pitch.** Exactly **16 units per diatonic staff step**, higher value = higher
  pitch. Pinned to ground truth: `MINUETG.MCS` (Bach Minuet in G) opens byte1
  `34,50,66,82,98` → **G4 A4 B4 C5 D5**, the tune's rising-scale opening. Decode:

  ```
  steps_above_G4 = round((byte1 - clef_byte1 + 80) / 16)   # 80 = G4 offset below the
  midi = walk C-major white keys from G4 by steps_above_G4  #      treble clef's anchor
  ```

  The clef's own byte1 is the per-song vertical anchor (114 in MINUETG, 106 elsewhere),
  so pitch is normalized relative to it. Implemented + tested in
  [`mcs_convert/mcs/reader.py`](../mcs_convert/mcs/reader.py).
- **byte0 = duration + attributes** — NOT yet decoded. Low 2 bits ∈ {0,1,2,3}, higher
  bits vary; likely note value + dotting + accidental (sharp/flat/natural). Reader uses a
  placeholder duration of 1 tick/note until this is cracked.

### Still to tweak (first-pass gaps)
- **byte0 → duration** (whole/half/quarter/…) and **accidentals** — the melody pitches are
  right, but rhythm is flat and sharps/flats are dropped (e.g. MINUETG's F♯ reads as F).
- **Bass-clef anchor** — bass staff currently reuses the treble offset, so bass octaves are
  wrong; needs its own calibration constant.
- **Key signature** — probably in the header (0x00–0x0C); would supply default accidentals.

## What we know (from research, 2026-07)

- *Will Harvey's Music Construction Set*, designed by Will Harvey, published by Electronic
  Arts. Original 1983 Apple II; **IBM-PC ports in 1984 and 1987**.
- The 1984 IBM release **boots from a non-standard single-sided double-density 5.25" disk**
  — "readable in DOS, but only the song files will be seen." Save/load happens through a
  DOS/BASIC-like prompt. So songs are individual files on a semi-DOS disk.
- Sound backends (1984 booter): PC speaker or cassette, in **4-note** (CPU-bound, no
  scroll) or **1-note-with-scroll** modes. The 1987 version added IBM Music Feature Card
  (240 voice patches). Implies the on-disk song stores up to ~4 simultaneous voices.
- Capacity anecdote: ~**700 notes** max per song.
- File extension for the DOS version: **not confirmed** (.SNG / .MUS both guessed, neither
  verified).

Sources:
- Wikipedia: https://en.wikipedia.org/wiki/Music_Construction_Set
- Nerdly Pleasures (PC sound support): http://nerdlypleasures.blogspot.com/2015/02/electronic-arts-music-construction-set.html
- Internet Archive disk images (1984): https://archive.org/details/msdos_Will_Harveys_Music_Construction_Set_1984
- Internet Archive (1987): https://archive.org/details/EAMusicConstructionSet1987

## Progress

- [x] **Get sample song files** — 80 songs from the 1984 IA rip (`samples/ia_1984/`).
- [x] **Dissect gross structure** — file-length field, `FF FF (count,prev)` record chain,
      two-staff grand-staff layout, clef-as-first-record, `(byte0=dur, byte1=pitch)` entry.
- [x] **Decode byte1 → pitch** — 16 units/diatonic step, clef-relative, validated against
      MINUETG's known opening. Reader + tests landed. Accidentals still dropped.
- [ ] **Decode byte0** — note value (whole/half/quarter/…), dotting, beaming, accidental.
- [ ] **Bass-clef anchor + key signature** — fix bass octaves; find the key sig for defaults.
- [ ] **Decode the header** (0x00–0x0C): confirm tempo, time/key signature, the two staff
      region offsets at 0x09/0x0B.
- [ ] **Validate by editing** — open a song in DOSBox MCS, change ONE known note/duration,
      re-save, diff the bytes to nail each field exactly. (Highest-signal next step.)
- [ ] **Implement** [`mcs_convert/mcs/writer.py`](../mcs_convert/mcs/writer.py) + a round-trip
      test (parse a sample → re-emit → byte-identical).

## Open questions
- byte1 → semitone: what pixel/units-per-step, and where's the accidental bit? Rendering a
  known song (DOSBox) or the edit-one-note diff will settle it fastest.
- byte0: exact duration table and whether horizontal position is stored or derived.
- Header 0x00–0x0C: which word is tempo, and do 0x09/0x0B point at the two staves?
- Is there ever more than 2 staves, or >1 voice per staff? (700-note cap noted in READ.TXT.)
- 1987 IMFC version format may differ; we target **1984** as canonical.
