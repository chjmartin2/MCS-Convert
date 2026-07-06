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

### Quick start (Windows)

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

Song files are copyrighted, so none are included. The original 1984 disk
(with classics like *The Entertainer*, *Flight of the Bumblebee*, and
Pachelbel's *Canon*) is preserved on archive.org — extract the `*.MCS` /
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
project's core. The MCS reader/writer and the player are the first pieces;
editing and more formats come next. Work has started on importing **NES
chiptunes (`.nsf`)** — the 6502 + APU emulation that turns a player rip into
note events is partially built. See
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

## License

MIT — see [LICENSE](LICENSE). The Music Construction Set itself, its disk
images, and its song files remain the property of their rights holders; this
project contains only original code and documentation.
