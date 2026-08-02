# .RCT — RetroComputerist Tracker format (version 1)

A chunk-based little-endian binary format holding **both** the editable song
(patterns, order list, instruments, ornaments, effects) **and** precompiled
per-target performance streams, so simple players (RCPLAY.COM on an 8088) never
have to interpret patterns — they load a stream and play.

All multi-byte integers are little-endian. Strings are fixed-length ASCII
(cp437-safe), NUL-padded.

## File header (16 bytes)

| off | size | field |
|----:|-----:|-------|
| 0 | 4 | magic `"RCT!"` |
| 4 | 1 | format version (1) |
| 5 | 1 | channel mode: 0 = four tone channels, 1 = three tone + noise |
| 6 | 1 | target hint: 0 any, 1 mcs, 2 tandy, 3 1voice, 4 4voice, 5 sb, 6 sbfm |
| 7 | 1 | tempo (the MCS tempo byte, 0x77 fastest … 0x92 slowest) |
| 8 | 1 | initial speed (sub-ticks per row, 1–32; 4 = one row per 32nd note) |
| 9 | 7 | reserved (zero) |

**Timing model.** Time is counted in *sub-ticks* — quarters of the MCS
32nd-note tick (8–26 ms depending on tempo). A pattern row lasts `speed`
sub-ticks. Effects update once per sub-tick. At speed 4 a row is exactly one
32nd note, so the .MCS export aligns to its grid losslessly.

## Chunks

Each chunk: `type` (4 ASCII bytes) + `size` (u32, payload bytes) + payload.
Unknown chunk types must be skipped (forward compatibility). Chunks appear in
any order; readers walk to EOF.

### `SONG` — metadata (128 bytes)
| size | field |
|-----:|-------|
| 32 | title |
| 32 | author |
| 64 | comment |

### `ORDR` — order list
| size | field |
|-----:|-------|
| 2 | count (1–256) |
| count | pattern indices (u8) |

### `PATT` — one pattern
| size | field |
|-----:|-------|
| 1 | pattern index (0–255) |
| 1 | rows (1–64) |
| rows × 4 × 5 | cells, row-major, channels left→right |

**Cell (5 bytes): `note, inst, vol, fx, param`**

- `note`: 0 = empty; 1–96 = C-0…B-7 (MIDI = note + 11); 97 = note-off (`===`).
  On the noise channel (mode 1, channel 4) the pitch selects the noise
  brightness — ≥ C-5 is a bright hiss (hi-hat), below is a dark rumble (kick).
- `inst`: 0 = keep current; 1–15 = instrument index.
- `vol`: 0 = keep; 1–16 = volume 0–15.
- `fx` / `param`:

| fx | letter | effect | param |
|---:|:-:|---|---|
| 0x00 | — | none | — |
| 0x01 | `A` | arpeggio | `xy`: cycle +0, +x, +y semitones per sub-tick |
| 0x02 | `1` | pitch slide up | `xx` × 4 cents per sub-tick |
| 0x03 | `2` | pitch slide down | `xx` × 4 cents per sub-tick |
| 0x04 | `3` | tone portamento | glide toward this row's note at `xx` × 4 cents per sub-tick |
| 0x05 | `4` | vibrato | `xy`: speed x (cycles per 32 sub-ticks × x), depth y × 12.5 cents |
| 0x06 | `V` | volume slide | `xy`: up x per sub-tick, down y (x wins if both) |
| 0x07 | `C` | note cut | silence after `xx` sub-ticks |
| 0x08 | `D` | note delay | onset delayed `xx` sub-ticks |
| 0x09 | `O` | ornament | select ornament `xx` (0 = off) for this channel |
| 0x0A | `F` | set speed | `xx` = new sub-ticks per row (1–32) |
| 0x0B | `B` | pattern break | jump to row `xx` of the next order position |

Effects persist for the row only (classic tracker semantics), except ornament
select (`O`) which persists on the channel until changed, and speed (`F`).

### `INST` — one instrument (20 bytes)
| size | field |
|-----:|-------|
| 1 | index (1–15) |
| 1 | waveform: 0 square, 1 pulse12, 2 pulse25, 3 pulse50, 4 pulse75, 5 triangle, 6 nestri, 7 sine |
| 1 | default volume (0–15) |
| 1 | default ornament (0 = none) |
| 16 | name |

### `ORNM` — one ornament
| size | field |
|-----:|-------|
| 1 | index (1–15) |
| 1 | length (1–32) |
| 1 | loop point (index restarted at after the end; 0xFF = one-shot, hold last) |
| 1 | reserved |
| length | signed semitone offsets (i8), applied per sub-tick |

### `PERF` — one precompiled performance stream
| size | field |
|-----:|-------|
| 1 | target: 1 tandy, 2 1voice, 3 4voice |
| 1 | stream format version (1) |
| 2 | PIT divider (ISR rate; for 4voice also the reference mixing rate) |
| 2 | samples per sub-tick |
| 2 | total sub-ticks |
| 1 | flags (bit0: stream carries viz bytes) |
| 3 | reserved |
| 4 | stream length |
| — | stream bytes |

Stream formats are exactly what the proven `.COM` engines consume:

- **tandy / 1voice**: per sub-tick `[count] [port, value]×count` — a dumb
  (port, value) player. Viz records ride along as pseudo-ports 0xF0–0xF3.
- **4voice**: per sub-tick `[nchanges] [voice|level<<4, inc_lo, inc_hi, viz]×n`
  — phase-accumulator increments at the reference rate. The same stream drives
  the ISR engine (fixed rate) and the calibrated foreground engine (increments
  rescaled to measured loop speed at startup).

All effects are already baked into these streams at sub-tick resolution when
the file is saved — an arpeggio is literal per-sub-tick pitch changes, vibrato
is literal retunes. Players stay dumb; the tracker is the only thing that ever
interprets patterns.

**Size cap:** a PERF stream that must fit RCPLAY.COM's single 64 KB segment is
rejected at save time above ~50 KB with a clear message.

## Design rationale

- **Patterns + streams in one file** keeps the format honest for editing while
  keeping the DOS player trivial (and fast on a 4.77 MHz XT). Media-player hosts
  (RCPlay, RCPLAY.COM) never need the pattern chunks; editors never need PERF
  (they recompile it on save).
- **Sub-tick timing** ties the tracker grid, the effects engine, the .MCS 32nd
  grid, and the DOS engines to one clock, so every output agrees.
- **Chunk skipping** lets v2 add chunks (samples? SB PERF targets?) without
  breaking v1 players.
