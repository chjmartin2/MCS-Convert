# Architecture

```
 .nsf ──▶ header.py ──▶ cpu6502 + apu ──▶ frame streams ──▶ segment ──▶ Song ──▶ writer ──▶ .mcs
          (parse)       (emulate+log)     [note|None]/ch    (notes)   (model)   (encode)
          WORKING       SKELETON          ---- extract.py ----        NEUTRAL    STUB
```

## Why this shape

A neutral intermediate model (`model.Song`) sits between input and output so neither end
knows about the other. Adding VGM or MIDI input later, or a different notation output,
touches only one side.

## Input side: NSF → note events

NSF isn't a note list — it's 6502 code plus music data. The player writes APU registers
over time; those writes *are* the music. So extraction means emulation:

1. **`header.py`** — parse the 128-byte NESM header (load/init/play addresses, region,
   play rate, bankswitching, expansion chips). **Done.**
2. **`cpu6502.py`** — a 2A03 (6502, no decimal mode) with a memory bus that intercepts
   `$4000-$4017` writes. **Skeleton** — the instruction core is the main remaining input
   work. Options: hand-roll (~150 opcodes) or vendor `py65`.
3. **`apu.py`** — fold register writes into per-channel pitch/on-state. Pulse 1/2 +
   triangle only. **Partial** (decode logic done; needs the emulator feeding it).
4. **`extract.py`** — drive init once, then play once per frame, sampling channel state
   into per-frame streams; `segment_frames` collapses each stream into `NoteEvent`s.
   Segmentation is **done and tested**; the drive loop waits on the CPU core.

### Note-detection caveats (input)
- **Note start/end**: a note ends when the channel goes silent or changes pitch. Legato
  same-pitch retriggers are ambiguous — the frame stream may not reveal them. Length
  counters / envelopes could refine this later.
- **How long to record**: NSFs loop forever. We'll need a stop heuristic (fixed seconds,
  or loop detection) — a `convert` flag.
- **Vibrato/pitch slides** become staircases of distinct notes; may need smoothing before
  quantization.

## Output side: note events → MCS

**Blocked on an unknown format.** Once reverse-engineered (see `mcs-format.md`), the
writer must:
- Estimate tempo/beat so frame-tick durations quantize onto MCS note-values.
- Map 3 NES tracks onto MCS staves (grand staff: treble + bass) and its voice limit.
- Emit pitch as whatever MCS uses (staff position + accidental vs absolute semitone),
  plus header/metadata and any checksum/terminator.

## Testing
`pytest` — pure logic (pitch math, header parse on synthetic headers, frame segmentation)
is covered now. Emulation and the writer get tests as they land.
