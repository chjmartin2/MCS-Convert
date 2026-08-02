# RetroComputerist Tracker — the rebuild plan

*(This document is the working roadmap for the overnight v2 rebuild. It is the
source of truth if work is interrupted; each phase commits separately.)*

## The goal

Unify the MCS converter, the tkinter tracker/player, and the DOS `.COM`
generator into one coherent product family around a **new native format**:

**`.RCT` — RetroComputerist Tracker format.**

| Deliverable | Name | What it is |
|---|---|---|
| Native format | `.RCT` | Chunk-based binary; 4 channels (4 tone or 3 tone + noise); patterns/order/instruments/ornaments/effects; **precompiled per-target performance streams** so dumb players stay dumb |
| Windows editor | **RCTracker** | Classic vertical tracker (FastTracker-style rows, keyboard-first, dark theme); imports NSF/PT3/MCS/RCT; exports everything |
| Windows player | **RCPlay** | Lightweight standalone .RCT player with visualizations |
| DOS player | **RCPLAY.COM** | Generic 8088+ real-mode player: loads a .RCT from disk, picks the right engine for the hardware/CPU, plays its PERF stream |
| Legacy exports | (kept) | .MCS files, all `.COM` targets (tandy/1voice/4voice/SB/FM), WAV |

## Decisions made with the user (final)

1. **DOS player consumes precompiled streams** — the .RCT carries editable
   pattern data AND compact per-target performance streams written on save.
   RCPLAY.COM loads a stream and plays it with the proven engines; effects are
   baked at compile time at sub-tick resolution.
2. **Windows apps are tkinter + PyInstaller one-file .exe builds.**
3. **Effects v1 = core PT3 set + ornaments**: arpeggio, pitch slide up/down,
   tone portamento, vibrato, volume slide, note cut, note delay, ornaments
   (stored arpeggio tables). Plus tracker plumbing: set speed, pattern break.
4. **Editor UI = classic vertical tracker grid** (patterns as rows scrolling
   down, 4 channel columns, note/inst/vol/fx per cell, order list, dark retro
   theme).
5. Bass policy for 1-voice arps stays octave-up below 110 Hz. Tempo stays the
   MCS tempo byte (0x77..0x92); a row is `speed` sub-ticks (default 4 = one row
   per 32nd note), so MCS export aligns exactly at speed 4.

## Architecture

```
                    ┌──────────────┐
   NSF / PT3 / MCS ─┤  importers   ├─► universal Song ─► patternize ─► RctSong
                    └──────────────┘                                     │
                                                                         ▼
                 ┌────────────────────────── rct.py ────────────────────────┐
                 │  RctSong (patterns, order, instruments, ornaments, meta) │
                 │  read_rct / write_rct  (+ PERF chunk compile on save)    │
                 └────────────┬────────────────────────────┬───────────────┘
                              ▼                            ▼
                    effects.py flattener          to_universal(Song)
                 (per-channel per-sub-tick               │
                  freq/vol/noise arrays)                 ▼
                              │                   .MCS encode (32nd grid)
        ┌─────────────┬───────┴──────┬──────────────┐
        ▼             ▼              ▼              ▼
   preview PCM   4voice stream   tandy stream   1voice stream
   (RCTracker/   (PERF + .COM)   (PERF + .COM)  (PERF + .COM)
    RCPlay)
```

- `mcs_convert/` stays as the engine library. New modules: `rct.py`,
  `effects.py`, `tracker/` (RCTracker UI), `player.py` (RCPlay UI),
  `rcplay_dos.py` (RCPLAY.COM builder).
- The flattener is the single semantics authority for effects: every output
  (preview, PERF, .COM) renders the same per-sub-tick data, so what you hear
  in RCTracker is what DOS plays.

## Phases (commit each)

- [x] **0. v1.1.0 GitHub release** — capture the pre-rebuild state.
- [ ] **1. Format**: docs/RCT-FORMAT.md spec; rct.py dataclasses + read/write +
      byte-exact roundtrip tests.
- [ ] **2. Effects**: effects.py flattener (all v1 effects + ornaments) with
      unit tests per effect; preview rendering from flattened arrays.
- [ ] **3. Import/Export**: universal Song ⇄ RctSong (patternize with pattern
      dedup); PERF compilation for all targets; export paths to .MCS/.COM/WAV
      driven from an RctSong.
- [ ] **4. RCTracker**: vertical tracker editor with keyboard entry, order
      list, instruments/ornaments, live preview, import/export, .RCT save/load.
- [ ] **5. RCPlay**: standalone Windows player (open/play .RCT, viz, metadata).
- [ ] **6. RCPLAY.COM**: DOS player — loads .RCT via int 21h, walks chunks,
      picks PERF by target/CPU (foreground engine calibration reused), startup-
      selectable visualization; capstone-verified.
- [ ] **7. Ship**: PyInstaller builds (build script, not committed binaries),
      README overhaul, final test pass.

## Risks / notes

- RCPLAY.COM code+loaded stream must fit one 64 KB segment (same constraint as
  today's exports). PERF streams above ~50 KB are rejected at save with a clear
  message (use a shorter song or coarser speed).
- The tkinter grid must stay fast: render the pattern view on a Canvas with
  cell-level damage tracking, not per-cell widgets.
- PyInstaller onefile + tkinter is proven; winmm audio uses ctypes (no extra
  deps).
- Existing tests (172) must keep passing untouched; new subsystems get their
  own test modules.
