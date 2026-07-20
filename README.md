# MCS-Convert

A **universal chiptune tracker and converter**, built around a byte-exact
reverse-engineering of **Will Harvey's Music Construction Set** (IBM-PC, 1984).
Import NES (`.nsf`), Vortex Tracker (`.pt3`), or MCS songs into one neutral
tracker that preserves *every* nuance — waveforms, duty cycles, per-note
volumes, effects, a real noise voice — then **export** to any of a dozen
targets (the original `.MCS` format, standalone DOS `.COM` players for Tandy /
PC-speaker / SoundBlaster, or WAV), each reduced to what that hardware can
actually play.

## New in v1.0 — the universal tracker

- **One model, every nuance.** Imports no longer flatten to four square voices.
  A song can carry any number of tracks (including dedicated **noise** voices),
  each with its own waveform — NES pulse duties (12.5/25/50/75%), the 4-bit
  stepped NES triangle, LFSR noise — plus per-note velocities and a universal
  **effects vocabulary** (PT3 ornaments / envelopes / slides / vibrato, NES
  duty / sweep).
- **Export lives after play.** Load a song, audition it, then open the **Export
  dialog**: pick a target, *preview it through that target's constraints*,
  **Retrack** it back into the tracker to see exactly what you'll get, and write
  the file.
- **Retrack.** Every exporter reduces the universal song to its own limits at
  export time (`retrack.py`) — MCS gets 4 square voices + register-extreme drum
  clicks; Tandy/PC-speaker get 3 tones + a native noise voice; SoundBlaster
  *keeps* the real waveforms.
- **Waveforms reach DOS.** SoundBlaster `.COM`s play sine / triangle / NES
  waveforms through a wavetable on the 8-bit DAC; the PC speaker models
  non-square waveforms via high-rate multi-level PWM.
- **Live visualization windows.** Oscilloscope, **VU meters**, **spectrum
  analyzer**, and a **DOS-replica preview** that mirrors the `.COM` scope you're
  about to build — all open alongside playback.

The MCS format itself remains fully documented and byte-exact (see below); it's
now one target among many rather than the whole program.

MCS was one of the first music notation programs for home computers. Its
`.MCS`/`.MCD` song format was never documented — until now. This project
recovered the complete byte-level format by disassembling the original
playback engine, and ships a modern player that reproduces what the 1984
program plays, note for note — tested against the 12 songs from the
original retail disk plus dozens of user-made songs collected over the
years (86 files in all, every one round-tripping byte-identically).

## The Player + Importer (v0.4)

Open a song, watch it scroll by in a tracker-style grid, and listen:

- **Tracker view** — one row per 32nd note, four voice columns ranked high→low,
  `PITCH:DUR` notation with dotted (`.`), tied (`~`), and irregular (`!N`)
  markers, plus an event column for 8va spans and mid-staff clef changes.
- **Real transport** — play, pause/resume, stop; click any row to start there
  (or to seek live during playback); a playhead follows the music.
- **"Play as" an output target** — playback reduces through the chosen target's
  constraints and renders in its own voice: Native (as imported), MCS's 4-voice
  1-bit PC speaker (a faithful delta-sigma model, gritty texture included),
  Tandy/PCjr squares, the 1- and 4-voice speaker engines, or SoundBlaster
  (where the real waveforms survive). Plus a live volume slider.
- **Oscilloscope window** — the four voices on scopes plus a master mix,
  phosphor green on black, resizable.
- **Export** — decoded playback as WAV, or the full tracker grid as text.
- **Import** — convert chiptunes to MCS through a preview dialog with
  per-channel stats, solo audition, percussion handling, per-channel octave
  shift, and an MCS tempo picker:
  - **NES chiptunes (`.nsf`) — new in 0.3.** A cycle-checked 6502 core (all
    151 official opcodes, validated against `nestest`) runs the game's own
    sound driver; the APU model logs the pulse/triangle/noise stream, detects
    the loop, and quantizes onto MCS's beat-aligned grid. Output targets:
    **Tandy** (3 square voices), **PC Speaker 1-Note**, and **PC Speaker
    4-Note** (adds a percussion voice). Noise percussion is read two-tone —
    kick vs hi-hat by NES period — and mapped to drum clicks, with an
    **Exhaustive Optimize** that searches tempo × subdivision for the tightest
    beat alignment.
  - **Vortex Tracker `.pt3` modules** (the ZX Spectrum AY-3-8910 scene format)
    — percussion detection with three handling modes (synthesized clicks — a
    register-extreme kick/hi-hat or wood-block timbre — played as written
    pitches, or dropped), and optional decay shaping that recovers plucks and
    staccato from the modules' volume tables.

### Just want to listen? (Windows, no Python)

Download **`MCS-Player.exe`** from the
[latest release](https://github.com/chjmartin2/MCS-Convert/releases/latest)
and double-click it. (SmartScreen will warn about an unrecognized app — the
exe isn't code-signed; choose *More info → Run anyway*.)

### Quick start from source (Windows)

```powershell
git clone https://github.com/chjmartin2/MCS-Convert.git
cd MCS-Convert
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python -m mcs_convert play path\to\SONG.MCS
```

Live playback uses the Windows audio API; on other platforms the parsing,
tracker export, and WAV export still work.

### Where do I get songs?

A demo is included: [`demos/MAPLERAG.MCS`](demos/MAPLERAG.MCS) — Scott
Joplin's **Maple Leaf Rag** (1899, public domain), A strain with repeat,
arranged for MCS's four voices by [`demos/make_maple_leaf.py`](demos/make_maple_leaf.py)
from the Mutopia Project's edition. It exercises the whole decoded format:
a 4-flat key signature, mid-measure accidentals and cancellations, naturals
against the key, ties across barlines, four-note chords, both clefs — and a
finale where the echo phrase returns an octave up under an 8va.

Beyond that, song files are copyrighted, so none are included. The original
1984 disk (with classics like *The Entertainer*, *Flight of the Bumblebee*,
and Pachelbel's *Canon*) is preserved on archive.org — extract the `*.MCS` /
`*.MCD` files from the disk image and drop them in `samples/` (gitignored).

## The format, documented

[docs/mcs-format.md](docs/mcs-format.md) is, as far as we know, the only
byte-level specification of the MCS song format in existence: the 16-bit note
word (position / vertical / symbol), the four clef-selected pitch ladders, key
signatures and accidentals, beamed notes, dots, ties, 8va spans, mid-staff
clef changes, tempo, and the engine's voice-fed timing model — all recovered
from a capstone disassembly of `MCSDISK.EXE` and validated by ear against the
original program running in an emulator.

The reader round-trips every sample byte-identically, and the decode is
engine-exact: what this player shows and plays is what MCS 1984 plays.

## The bigger project

The goal (in progress): a fully functional **tracker editor and converter**
for various music formats, built around the neutral note-event model at the
project's core.

**Working today — convert Vortex Tracker (`.pt3`) modules to MCS:**

```powershell
python -m mcs_convert convert SONG.pt3 SONG.MCS
```

PT3 is the ZX Spectrum / Atari ST scene's AY-3-8910 tracker format — three
pure tone channels, note-based, no samples — which makes it the ideal MCS
source. The importer extracts the notes and maps the row rate onto MCS's
tempo; the general `Song → MCS` encoder handles staff split, voice capping,
rests, tied sustains across barlines, and accidental spelling. (The encoder
is verified by re-encoding our Maple Leaf Rag demo losslessly.)

**NES chiptunes (`.nsf`, new in 0.3)** import the same way — from source:

```powershell
python -m mcs_convert convert SONG.nsf SONG.MCS
```

A full 6502 core executes the game's sound driver frame by frame while the
APU model captures the pulse/triangle/noise registers; the emulation detects
the song loop, quantizes onto MCS's grid, and converts to Tandy or PC-speaker
voicing. See [docs/architecture.md](docs/architecture.md).

### Standalone player (`.COM`) — greatly expanded in 0.4

Convert to a self-contained DOS executable that plays the song with no runtime —
just a `.COM` you run in DOSBox (or on a real PC), hand-assembled with no
toolchain dependency:

```powershell
python -m mcs_convert convert SONG.nsf SONG.COM --tandy            # 3 square voices + noise drums
python -m mcs_convert convert SONG.pt3 SONG.COM --1voice           # PC-speaker melody
python -m mcs_convert convert SONG.nsf SONG.COM --4voice --mix-rate 24000   # software-mixed speaker
python -m mcs_convert convert SONG.nsf SONG.COM --4voice --sb      # SoundBlaster 8-bit DAC
```

**Four audio targets:**

- `--tandy` — the Tandy 1000 / PCjr SN76489: three real square channels plus
  two-tone noise-channel percussion (run DOSBox with `machine=tandy`).
- `--1voice` — the PC speaker, monophonic top line.
- `--4voice` — a software 1-bit mixer on the PC speaker: four phase-accumulator
  voices (3 squares + LFSR noise) delta-sigma'd onto the cone at an **arbitrary
  mixing rate** (`--mix-rate`, ~1–48 kHz: 4 kHz suits a real XT, 24 kHz+ puts the
  carrier ultrasonic for the cleanest sound on a fast CPU). Add `--mcs` for the
  speaker drive reverse-engineered from the original MCS.EXE (timer-2 one-shot
  pulse-density DAC).
- `--4voice --sb` — **SoundBlaster** output: the same four voices mixed at full
  amplitude through the DSP's 8-bit DAC (`--sb-port` overridable) — much closer
  to the original NES/PT3 sound than any 1-bit speaker.

**On-screen visualizations** (pick one per build):

- `--scope` — Tandy 320×200×16 graphics oscilloscopes (2×2 grid + master).
- `--scope-vga` — the same scope grid on **universal VGA mode 13h** (320×200×256).
- `--scope-text` / `--scope-text2` / `--scope-text3` — 80×25 text-mode scopes:
  block bars, box-drawing line traces, and the 2×2 box-line grid + master.
- `--scope-text4` — a faux spectrum analyzer (green/yellow/red bars, peak caps,
  square-wave odd-harmonic model).
- `--scope-text5` — the combined monitor: 2×2 scopes + spectrum + VU meters on
  one screen.
- `--scope-vu` — lightweight VU meters only.
- `--scope-static` — a **static CGA piano-roll poster of the whole song**, drawn
  once with zero runtime cost (the real-XT option; playback gets the entire CPU).
- `--draw-skip N` throttles any live scope for slow machines.

Every `.COM` auto-repeats until a keypress, then restores the timer, interrupt
vector, video mode, and hardware before returning to DOS.

## Layout

```
mcs_convert/
  gui/player.py     the player/viewer GUI (tkinter)
  tracker.py        tracker-grid rendering
  audio.py          synth (square/triangle/sine/PC-speaker) + waveOut transport
  mcs/reader.py     .MCS/.MCD -> Song   (the decoded format lives here)
  mcs/writer.py     Song -> .MCS        (round-trips all samples byte-identically)
  dosplayer.py      Song -> standalone DOS .COM (Tandy / PC speaker / SoundBlaster + scopes)
  retrack.py        reduce a universal Song to a target's capabilities (export-time)
  model.py          Song / Track / NoteEvent — the UNIVERSAL tracker (waveforms, effects, N tracks)
  gui/export.py     the Export dialog (target choice, constrained preview, retrack)
  gui/viz.py        VU / spectrum / DOS-replica visualization windows
  nsf/              NSF header/APU/6502 emulation (NES converter input)
docs/               the format spec + architecture notes
tools/              disk-image and test-song utilities
tests/              157 tests, including engine ground-truth checks
```

## Development

```powershell
pip install -e ".[dev]"
pytest
```

The standalone player exe attached to releases is built with:

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --name MCS-Player --distpath dist --workpath build --specpath build play.py
```

## License

MIT — see [LICENSE](LICENSE). The Music Construction Set itself, its disk
images, and its song files remain the property of their rights holders; this
project contains only original code and documentation.
