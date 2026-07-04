# Music Construction Set (IBM-PC) song format — reverse-engineering log

**Status: unknown. No public byte-level spec exists.** This is the project's critical
blocker. This file is the running investigation; update it as facts land.

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

## How we crack it (plan)

1. **Get sample song files.** Pull a disk image from archive.org; extract the song
   files. The 1984 disk is non-standard, so a plain loop-mount may fail — options:
   a DOS-format disk made *inside* MCS (per the manual it can write to standard DOS
   disks), or a disk-imaging tool that understands the layout. Drop samples in
   `samples/` (gitignored).
2. **Dissect.** Hex-dump several songs — ideally ones we can also see rendered in an
   emulator (DOSBox) so we know ground-truth notes. Look for:
   - a header/magic and note count (cross-check the ~700 cap),
   - the per-note record: pitch (staff position + accidental? absolute semitone?),
     duration (note value + dotting), voice/staff, accidentals, ties,
   - global metadata: tempo, key sig, time sig, title,
   - record size / stride (diff two songs that differ by one note),
   - any terminator or checksum.
3. **Validate by editing.** Change one note in MCS, re-save, diff the bytes — the delta
   localizes each field precisely.
4. **Write it up here**, then implement [`mcs_convert/mcs/writer.py`](../mcs_convert/mcs/writer.py).

## Open questions
- Which IBM version do we target — 1984 (speaker/4-voice) or 1987 (IMFC)? Their song
  formats may differ. Leaning 1984 as the canonical, simplest target.
- Pitch representation and the exact note-value set (drives duration quantization).
- Endianness, record alignment, and whether note timing is absolute or delta.

## Field table (fill in as discovered)

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| ?      | ?    | magic/header | unknown |
| ?      | ?    | note count   | unknown |
| ?      | ?    | per-note record | unknown |
