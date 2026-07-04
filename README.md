# MCS-Convert

Convert chiptune music into **Will Harvey's Music Construction Set** (IBM-PC, 1984/1987)
song format.

## Status: early scaffolding

The project is being built in phases. The first supported input is the **NES Sound
Format (`.nsf`)**, chosen because the NES APU exposes a small, discrete set of pitched
channels (2 pulse + 1 triangle) that map cleanly onto staff notation — sidestepping the
pitch-detection problems that raw audio (WAV/MP3) would bring. Raw audio is explicitly
**out of scope**.

```
  .nsf  ──▶  [ 6502 + APU emulation ]  ──▶  note events  ──▶  [ MCS writer ]  ──▶  MCS song
 (input)        extract register writes      (intermediate)      encode bytes       (output)
```

### What works today
- NSF header parsing and inspection: `python -m mcs_convert inspect song.nsf`
- Frequency/period ↔ MIDI-note conversion helpers.
- The intermediate note-event model that both ends talk through.

### What's stubbed / in progress
- **6502 + APU emulation** — needed to turn an NSF's player code into a timed stream of
  register writes, then into note-on/note-off events. Interface is defined; core is a TODO.
- **MCS format writer** — *blocked on reverse-engineering the format* (see below).

## The big open problem: the MCS file format is undocumented

There is **no public byte-level specification** for the Music Construction Set IBM-PC song
format. The 1984 IBM release boots from a non-standard single-sided disk; songs are stored
as files but nobody has published the layout. To build the writer we must **reverse-engineer
it from real sample song files**, which live inside the disk images on archive.org.

See [docs/mcs-format.md](docs/mcs-format.md) for the running reverse-engineering log and
[docs/architecture.md](docs/architecture.md) for the overall design.

## Layout

```
mcs_convert/
  cli.py            command-line entry point
  model.py          Song / Track / NoteEvent  (the intermediate representation)
  pitch.py          NES period <-> frequency <-> MIDI note
  nsf/
    header.py       NSF header parsing            (working)
    apu.py          APU register decode -> pitch  (partial)
    cpu6502.py      6502 CPU core                 (skeleton)
    extract.py      emulate player -> note events (skeleton)
  mcs/
    writer.py       encode note events -> MCS     (stub, pending RE)
docs/               format notes + architecture
tests/              unit tests
samples/            drop .nsf / .mcs sample files here (gitignored)
```

## Development

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -e .
pytest
```
