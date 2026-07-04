# NSF (NES Sound Format) — input notes

Well-documented, unlike the output side. Primary references:
- NESdev Wiki — NSF: https://www.nesdev.org/wiki/NSF
- NESdev Wiki — APU: https://www.nesdev.org/wiki/APU
- kevtris spec: http://kevtris.org/nes/nsfspec.txt (note: self-signed TLS cert)
- OverClocked ReMix mirror: https://ocremix.org/info/NSF_Format_Specification

## Header (128 bytes, "NESM")

| Off  | Size | Field |
|------|------|-------|
| 0x00 | 5    | magic `NESM` + 0x1A |
| 0x05 | 1    | version |
| 0x06 | 1    | total songs |
| 0x07 | 1    | starting song (1-based) |
| 0x08 | 2    | load address (LE) |
| 0x0A | 2    | init address |
| 0x0C | 2    | play address |
| 0x0E | 32   | song name |
| 0x2E | 32   | artist |
| 0x4E | 32   | copyright |
| 0x6E | 2    | NTSC play speed (µs/call) |
| 0x70 | 8    | bankswitch init (all 0 = none) |
| 0x78 | 2    | PAL play speed (µs/call) |
| 0x7A | 1    | region flags: bit0 PAL, bit1 dual |
| 0x7B | 1    | expansion chip flags |
| 0x7C | 4    | NSF2 / reserved |
| 0x80 | ...  | program data (loaded at load address) |

Implemented in [`mcs_convert/nsf/header.py`](../mcs_convert/nsf/header.py).

## Playback model
- JSR **init** once with A = (song# − 1), X = region (0 NTSC / 1 PAL).
- JSR **play** once per tick; tick rate = 1e6 / speed µs (≈60 Hz NTSC, ≈50 Hz PAL).
- Player writes APU registers each tick — that's what we capture.

## APU pitched channels (what we use)

| Channel  | Regs        | period bits            | freq |
|----------|-------------|------------------------|------|
| Pulse 1  | $4000-$4003 | lo=$4002, hi=$4003[2:0]| CPU / (16·(t+1)) |
| Pulse 2  | $4004-$4007 | lo=$4006, hi=$4007[2:0]| CPU / (16·(t+1)) |
| Triangle | $4008-$400B | lo=$400A, hi=$400B[2:0]| CPU / (32·(t+1)) |

$4015 = channel enable / length status. Pulse volume/env in $4000/$4004 low nibble.
CPU clock: NTSC 1,789,773 Hz; PAL 1,662,607 Hz. Ignored for now: noise ($400C-F),
DMC ($4010-13) — unpitched/sampled.

Decode + pitch math: [`apu.py`](../mcs_convert/nsf/apu.py), [`pitch.py`](../mcs_convert/pitch.py).

## Out of scope (for now)
Expansion audio (VRC6/VRC7/FDS/MMC5/N163/5B), NSFe/NSF2 metadata, bankswitching
(header flags it; the load path doesn't handle it yet).
