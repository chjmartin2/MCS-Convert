# MCS-Convert

A player, viewer, and fully reverse-engineered format spec for **Will Harvey's
Music Construction Set** (IBM-PC, 1984) — the first pieces of a fully
functional tracker editor and converter for various music formats.

MCS was one of the first music notation programs for home computers. Its
`.MCS`/`.MCD` song format was never documented — until now. This project
recovered the complete byte-level format by disassembling the original
playback engine, and ships a modern player that reproduces what the 1984
program plays, note for note, on all 86 songs we could find.

## The Player (v0.1)

Open a song, watch it scroll by in a tracker-style grid, and listen:

- **Tracker view** — one row per 32nd note, four voice columns ranked high→low,
  `PITCH:DUR` notation with dotted (`.`), tied (`~`), and irregular (`!N`)
  markers, plus an event column for 8va spans and mid-staff clef changes.
- **Real transport** — play, pause/resume, stop; click any row to start there
  (or to seek live during playback); a playhead follows the music.
- **Live volume slider** and four synth voices, including **"PC Speaker"** — a
  faithful model of the original 4-voice 1-bit delta-sigma output, gritty
  texture included.
- **Oscilloscope window** — the four voices on scopes plus a master mix,
  phosphor green on black, resizable.
- **Export** — decoded playback as WAV, or the full tracker grid as text.

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

Work has also started on importing **NES chiptunes (`.nsf`)** — the
6502 + APU emulation is partially built. See
[docs/architecture.md](docs/architecture.md).

## Layout

```
mcs_convert/
  gui/player.py     the player/viewer GUI (tkinter)
  tracker.py        tracker-grid rendering
  audio.py          synth (square/triangle/sine/PC-speaker) + waveOut transport
  mcs/reader.py     .MCS/.MCD -> Song   (the decoded format lives here)
  mcs/writer.py     Song -> .MCS        (round-trips all samples byte-identically)
  model.py          Song / Track / NoteEvent  (the neutral middle)
  nsf/              NSF header/APU/6502 (converter input, in progress)
docs/               the format spec + architecture notes
tools/              disk-image and test-song utilities
tests/              58 tests, including engine ground-truth checks
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
