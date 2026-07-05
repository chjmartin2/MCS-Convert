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

- **byte1 = a vertical PIXEL position**, higher value = higher pitch, normalized against the
  clef's own byte1 anchor. Decode:

  ```
  steps = round((byte1 - clef_byte1) / STEP) + anchor_offset
  midi  = walk C-major white keys from G4 by `steps`      # accidentals dropped
  ```

  - **Clef anchor offsets** (diatonic steps from G4, zoom-independent):
    - **treble = +5** (anchor = E5) — from `MINUETG.MCS`, whose opening byte1 `34,50,66,82,98`
      → **G4 A4 B4 C5 D5** (the tune's rising scale).
    - **bass = −8** (anchor = F3, exactly what the F-clef points at) — from `SCALE.MCS`, an
      edited-in full white-key scale whose notes cross seamlessly from the bass staff
      (…B2 C3 D3 … **C4**) into the treble staff (**D4** E4 F4 …). Bass was previously ~2
      octaves too high (it reused the treble math).
  - **STEP = pixels per diatonic step is a per-song ZOOM.** 16 for most songs (MINUETG,
    DIXIE), but MCS zooms out for wide-range pieces — `SCALE.MCS` spans ~6 octaves and uses
    **8**. `parse(diatonic_step=…)` takes it; default 16. Not yet auto-detected: the obvious
    odd/even-grid test misfires because normal songs carry ~8px byte1 jitter (some notes sit a
    half-step off the 16-grid). Finding the zoom (likely a header field) is an open item.

  Implemented + tested in [`mcs_convert/mcs/reader.py`](../mcs_convert/mcs/reader.py).
- **byte0 = duration + rest flag + render bits** (CONFIRMED via emulator ground truth):
  - **bits[3:0] = note/rest SYMBOL.** Bit 3 (`0x08`) is the **rest flag**; the low value is
    the note value on a doubling ladder:

    | nibble | 1 | 2 | 3 | 4 | 5 | | 8 | 9 | 10 | 11 | 12 |
    |--------|---|---|---|---|---|-|---|---|----|----|----|
    | value  | 16th | 8th | quarter | half | whole | | 16th **rest** | 8th rest | quarter rest | half rest | whole rest |

    Duration in sixteenth-ticks = `2**(nibble-1)` for a note, `2**(nibble-8)` for a rest.
    Ground truth: MINUETG opens **quarter + 4 eighths** (nibbles `3,2,2,2,2` — matches the
    on-screen notation in MartyPC), and editing that first eighth into an eighth rest changed
    `byte0 0x82 → 0x89` (nibble `2 → 9`) with `byte1 0x32 → 0x39` (pitch → a fixed rest-glyph
    staff position). Corpus: notes 1–5 = 17/29/22/5/4 %, rests 8–12 present at ~11 % overall —
    a textbook duration distribution. Implemented in `decode_duration()`.
    Nibbles **0, 6, 7** (notes) and **13, 14, 15** (rests) are uncommon (~13 %) and not yet
    pinned — likely dotted/ornamented; mapped provisionally to their measure-completion mode.
  - **bits[7:5] = stem/beam render length, NOT musical.** It moves *linearly with pitch*
    within a beam group (MINUETG's rising G-A-B-C-D run → 4,3,2,1 as noteheads climb toward
    the beam). A drawing artifact; ignored for timing.

### Records are MEASURES (CONFIRMED)
Each `FF FF (count,prev)` record is one **bar**. With the duration ladder above, MINUETG's
treble records each sum to **12 sixteenths = 3/4** (quarter + 4 eighths, or 3 quarters, …).
This is what validated the ladder, and it means a record's note+rest durations should total
the time signature — a strong consistency check for future decoding.

### Still to tweak (first-pass gaps)
- **Per-song zoom (STEP) auto-detection** — 16 vs 8 pixels/step; currently a `parse()` arg
  defaulting to 16. Likely a header field (0x00–0x0C); find it so wide songs decode unaided.
- **Dotted/ornament nibbles 0,6,7** (and rest mirrors 13,14,15) — provisional; pin them with
  more emulator edits (e.g. save a known dotted note and diff).
- **Accidentals** — dropped (e.g. MINUETG's F♯ reads as F); the middle byte0 bits are suspect.
  Also explains some ~8px byte1 jitter that blocks naive zoom detection.
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
- [x] **Decode byte0 duration + rests** — low nibble = note/rest value on a doubling ladder,
      bit3 = rest flag (confirmed by a MartyPC edit-and-diff). bits[7:5] = stem render (ignored).
      Records confirmed to be measures. Dotted nibbles 0,6,7 still provisional.
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
