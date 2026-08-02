"""The .RCT (RetroComputerist Tracker) native format — read, write, validate.

Chunk-based little-endian binary holding BOTH the editable song (patterns,
order, instruments, ornaments) AND precompiled per-target performance streams,
so dumb players (RCPLAY.COM on an 8088) never interpret patterns. The full
byte-level spec lives in docs/RCT-FORMAT.md; this module is its one
implementation and must round-trip byte-exactly.

Layers above:
  * effects.py flattens an RctSong to per-sub-tick freq/vol arrays (the one
    semantics authority for effects);
  * exporters compile those into the PERF streams and the .MCS/.COM/WAV
    outputs;
  * the RCTracker UI edits RctSong directly.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MAGIC = b"RCT!"
VERSION = 1

#: channel modes
MODE_4TONE = 0                   # four tone channels
MODE_3TONE_NOISE = 1             # three tone + one noise channel

#: header target hints (what the song was authored for)
TARGET_HINTS = ("any", "mcs", "tandy", "1voice", "4voice", "sb", "sbfm")

#: PERF chunk targets (DOS playback engines). The SoundBlaster DAC has no id
#: of its own: it consumes the 4voice stream (same records, the level nibble
#: is its per-note volume). SBFM carries packed OPL2 note words instead of
#: phase increments, at one record set per sub-tick.
PERF_TANDY, PERF_1VOICE, PERF_4VOICE, PERF_SBFM = 1, 2, 3, 4
PERF_TARGETS = {PERF_TANDY: "tandy", PERF_1VOICE: "1voice",
                PERF_4VOICE: "4voice", PERF_SBFM: "sbfm"}

#: instrument waveform ids
WAVEFORM_IDS = ("square", "pulse12", "pulse25", "pulse50", "pulse75",
                "triangle", "nestri", "sine")

#: effect numbers (cell fx byte) and their tracker display letters
FX_NONE, FX_ARP, FX_SLIDE_UP, FX_SLIDE_DOWN, FX_PORTA, FX_VIBRATO, \
    FX_VOLSLIDE, FX_CUT, FX_DELAY, FX_ORNAMENT, FX_SPEED, FX_BREAK = range(12)
FX_LETTERS = "-A1234VCDOFB"      # index by fx number; '-' = none

NOTE_OFF = 97                    # cell note value for === (key off)
NOTE_MAX = 96                    # 1..96 = C-0..B-7 (midi = note + 11)

_MAX_STREAM = 50 * 1024          # PERF cap: must fit RCPLAY.COM's one segment


def note_to_midi(note: int) -> int:
    """Cell note value (1-96) -> MIDI (12-107)."""
    return note + 11


def midi_to_note(midi: int) -> int:
    """MIDI -> cell note value, clamped into the tracker's 8 octaves."""
    return max(1, min(NOTE_MAX, midi - 11))


def note_name(note: int) -> str:
    """Display name for a cell note value: 'C-4', 'D#3', '===' or '...'."""
    if note == 0:
        return "..."
    if note == NOTE_OFF:
        return "==="
    names = ("C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-")
    n = note - 1
    return f"{names[n % 12]}{n // 12}"


@dataclass
class RctCell:
    """One pattern cell: note / instrument / volume / effect+param."""
    note: int = 0                # 0 empty, 1-96 pitch, 97 note-off
    inst: int = 0                # 0 keep, 1-15
    vol: int = 0                 # 0 keep, 1-16 => volume 0-15
    fx: int = FX_NONE
    param: int = 0

    def pack(self) -> bytes:
        return bytes([self.note, self.inst, self.vol, self.fx, self.param])

    @classmethod
    def unpack(cls, b: bytes) -> "RctCell":
        return cls(b[0], b[1], b[2], b[3], b[4])

    @property
    def empty(self) -> bool:
        return not (self.note or self.inst or self.vol or self.fx or self.param)


@dataclass
class RctPattern:
    """rows x 4 channels of cells."""
    rows: int = 32
    cells: List[List[RctCell]] = field(default_factory=list)   # [row][channel]

    def __post_init__(self):
        while len(self.cells) < self.rows:
            self.cells.append([RctCell() for _ in range(4)])

    def cell(self, row: int, ch: int) -> RctCell:
        return self.cells[row][ch]


@dataclass
class RctInstrument:
    name: str = "square"
    waveform: str = "square"     # one of WAVEFORM_IDS
    volume: int = 15             # default volume 0-15
    ornament: int = 0            # default ornament index (0 = none)


@dataclass
class RctOrnament:
    """A stored arpeggio table: per-sub-tick semitone offsets (PT3-style)."""
    name: str = ""
    steps: List[int] = field(default_factory=lambda: [0])   # signed semitones
    loop: int = 0                # restart index after the end; -1 = hold last


@dataclass
class RctSong:
    title: str = "untitled"
    author: str = ""
    comment: str = ""
    channel_mode: int = MODE_3TONE_NOISE
    target_hint: str = "4voice"
    tempo_byte0: int = 0x80      # the MCS tempo byte (the SNAP, kept in sync)
    subtick_us: int = 0          # exact sub-tick period in µs; 0 = derive from
    #                              tempo_byte0 (legacy files). This is what makes
    #                              BPM arbitrary: engines/exports use the exact
    #                              period, the MCS byte is just its nearest snap.
    speed: int = 4               # sub-ticks per row (4 = one row per 32nd)
    patterns: Dict[int, RctPattern] = field(default_factory=dict)
    order: List[int] = field(default_factory=lambda: [0])
    instruments: Dict[int, RctInstrument] = field(default_factory=dict)
    ornaments: Dict[int, RctOrnament] = field(default_factory=dict)
    perf: Dict[int, bytes] = field(default_factory=dict)   # PERF target -> chunk payload

    def __post_init__(self):
        if not self.patterns:
            self.patterns[0] = RctPattern()
        if not self.instruments:
            self.instruments[1] = RctInstrument()

    @property
    def channels(self) -> int:
        return 4

    @property
    def subtick_seconds(self) -> float:
        """The exact sub-tick period: the free-BPM field when set, else the
        MCS tempo byte's grid (legacy files and MCS-locked songs)."""
        if self.subtick_us:
            return self.subtick_us / 1_000_000.0
        from .mcs.reader import tick_seconds_for
        return tick_seconds_for(self.tempo_byte0) / 4.0

    @property
    def bpm(self) -> float:
        """Quarter-note BPM (a quarter is 8 ticks = 32 sub-ticks)."""
        return 60.0 / (32.0 * self.subtick_seconds)

    def set_bpm(self, bpm: float) -> None:
        """Set an ARBITRARY tempo. Stores the exact sub-tick period and keeps
        tempo_byte0 tracking the nearest MCS speed (the MCS-mode/export snap)."""
        bpm = max(20.0, min(900.0, float(bpm)))
        self.subtick_us = max(1, min(65535, round(60_000_000.0 / (32.0 * bpm))))
        self.tempo_byte0 = self.mcs_tempo_byte()

    def mcs_tempo_byte(self) -> int:
        """The MCS tempo byte whose grid is closest to the exact tempo."""
        from .mcs.reader import tick_seconds_for
        tick = self.subtick_seconds * 4.0
        return min((0x77 + 3 * s for s in range(10)),
                   key=lambda b: abs(tick_seconds_for(b) - tick))

    def channel_kind(self, ch: int) -> str:
        """'tone' or 'noise' for a channel index (0-3)."""
        return ("noise" if ch == 3 and self.channel_mode == MODE_3TONE_NOISE
                else "tone")

    def total_rows(self) -> int:
        return sum(self.patterns[p].rows for p in self.order if p in self.patterns)


# --- writing -----------------------------------------------------------------

def _pad(s: str, n: int) -> bytes:
    b = s.encode("ascii", "replace")[:n]
    return b + b"\x00" * (n - len(b))


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("<I", len(payload)) + payload


def write_rct(song: RctSong) -> bytes:
    """Serialize an RctSong (spec: docs/RCT-FORMAT.md). Deterministic output:
    chunks in a fixed order, patterns/instruments/ornaments sorted by index."""
    if not 1 <= song.speed <= 32:
        raise ValueError(f"speed must be 1-32 sub-ticks/row, not {song.speed}")
    if song.target_hint not in TARGET_HINTS:
        raise ValueError(f"unknown target hint {song.target_hint!r}")
    if not song.order:
        raise ValueError("the order list is empty")
    for i, p in enumerate(song.order):
        if p not in song.patterns:
            raise ValueError(f"order position {i} references missing pattern {p}")

    out = bytearray()
    out += MAGIC
    out += bytes([VERSION, song.channel_mode,
                  TARGET_HINTS.index(song.target_hint),
                  song.tempo_byte0, song.speed])
    out += struct.pack("<H", song.subtick_us)        # exact tempo (0 = legacy)
    out += bytes(5)                                  # reserved

    out += _chunk(b"SONG", _pad(song.title, 32) + _pad(song.author, 32) +
                  _pad(song.comment, 64))

    if len(song.order) > 256:
        raise ValueError("order list longer than 256 positions")
    out += _chunk(b"ORDR", struct.pack("<H", len(song.order)) +
                  bytes(song.order))

    for idx in sorted(song.patterns):
        pat = song.patterns[idx]
        if not 1 <= pat.rows <= 64:
            raise ValueError(f"pattern {idx}: rows must be 1-64, not {pat.rows}")
        body = bytearray([idx, pat.rows])
        for row in range(pat.rows):
            for ch in range(4):
                body += pat.cells[row][ch].pack()
        out += _chunk(b"PATT", bytes(body))

    for idx in sorted(song.instruments):
        ins = song.instruments[idx]
        if ins.waveform not in WAVEFORM_IDS:
            raise ValueError(f"instrument {idx}: unknown waveform {ins.waveform!r}")
        out += _chunk(b"INST", bytes([idx, WAVEFORM_IDS.index(ins.waveform),
                                      ins.volume & 0x0F, ins.ornament]) +
                      _pad(ins.name, 16))

    for idx in sorted(song.ornaments):
        orn = song.ornaments[idx]
        if not 1 <= len(orn.steps) <= 32:
            raise ValueError(f"ornament {idx}: 1-32 steps, not {len(orn.steps)}")
        loop = 0xFF if orn.loop < 0 else orn.loop
        out += _chunk(b"ORNM", bytes([idx, len(orn.steps), loop, 0]) +
                      bytes((s & 0xFF) for s in orn.steps))

    for target in sorted(song.perf):
        payload = song.perf[target]
        if len(payload) > _MAX_STREAM + 16:
            raise ValueError(
                f"PERF stream for {PERF_TARGETS.get(target, target)} is "
                f"{len(payload)} bytes — past the ~50 KB DOS player cap. "
                f"Shorten the song or reduce the mixing rate.")
        out += _chunk(b"PERF", payload)

    return bytes(out)


def make_perf(target: int, divider: int, samps_per_sub: int, total_subs: int,
              stream: bytes, viz: bool = True) -> bytes:
    """Build one PERF chunk payload (header + stream) for `write_rct`."""
    if target not in PERF_TARGETS:
        raise ValueError(f"unknown PERF target {target}")
    if len(stream) > _MAX_STREAM:
        raise ValueError(
            f"PERF stream is {len(stream)} bytes — past the ~50 KB DOS player "
            f"cap. Shorten the song or reduce the mixing rate.")
    return (bytes([target, 1]) +
            struct.pack("<HHH", divider, samps_per_sub, total_subs) +
            bytes([1 if viz else 0, 0, 0, 0]) +
            struct.pack("<I", len(stream)) + stream)


def parse_perf(payload: bytes) -> dict:
    """PERF chunk payload -> dict(target, divider, samps_per_sub, total_subs,
    viz, stream)."""
    if len(payload) < 16:
        raise ValueError("PERF chunk too short")
    target, ver = payload[0], payload[1]
    if ver != 1:
        raise ValueError(f"PERF stream version {ver} not supported")
    divider, samps, total = struct.unpack_from("<HHH", payload, 2)
    viz = bool(payload[8] & 1)
    (slen,) = struct.unpack_from("<I", payload, 12)
    stream = payload[16:16 + slen]
    if len(stream) != slen:
        raise ValueError("PERF stream truncated")
    return dict(target=target, divider=divider, samps_per_sub=samps,
                total_subs=total, viz=viz, stream=stream)


# --- reading -----------------------------------------------------------------

def _unpad(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("ascii", "replace")


def read_rct(data: bytes) -> RctSong:
    """Parse .RCT bytes; unknown chunk types are skipped (forward compat)."""
    if len(data) < 16 or data[:4] != MAGIC:
        raise ValueError("not an RCT file (bad magic)")
    if data[4] != VERSION:
        raise ValueError(f"RCT version {data[4]} not supported (this reads v{VERSION})")
    song = RctSong(channel_mode=data[5],
                   target_hint=TARGET_HINTS[data[6]] if data[6] < len(TARGET_HINTS)
                   else "any",
                   tempo_byte0=data[7],
                   subtick_us=struct.unpack_from("<H", data, 9)[0],
                   speed=max(1, data[8]))
    song.patterns.clear()
    song.instruments.clear()

    pos = 16
    while pos + 8 <= len(data):
        kind = data[pos:pos + 4]
        (size,) = struct.unpack_from("<I", data, pos + 4)
        payload = data[pos + 8:pos + 8 + size]
        if len(payload) != size:
            raise ValueError(f"chunk {kind!r} truncated")
        pos += 8 + size

        if kind == b"SONG":
            song.title = _unpad(payload[0:32])
            song.author = _unpad(payload[32:64])
            song.comment = _unpad(payload[64:128])
        elif kind == b"ORDR":
            (count,) = struct.unpack_from("<H", payload, 0)
            song.order = list(payload[2:2 + count])
        elif kind == b"PATT":
            idx, rows = payload[0], payload[1]
            cells, off = [], 2
            for _ in range(rows):
                row = []
                for _ch in range(4):
                    row.append(RctCell.unpack(payload[off:off + 5]))
                    off += 5
                cells.append(row)
            song.patterns[idx] = RctPattern(rows=rows, cells=cells)
        elif kind == b"INST":
            song.instruments[payload[0]] = RctInstrument(
                name=_unpad(payload[4:20]),
                waveform=WAVEFORM_IDS[payload[1]]
                if payload[1] < len(WAVEFORM_IDS) else "square",
                volume=payload[2] & 0x0F, ornament=payload[3])
        elif kind == b"ORNM":
            length, loop = payload[1], payload[2]
            steps = [s - 256 if s > 127 else s for s in payload[4:4 + length]]
            song.ornaments[payload[0]] = RctOrnament(
                steps=steps, loop=-1 if loop == 0xFF else loop)
        elif kind == b"PERF":
            song.perf[payload[0]] = payload
        # unknown chunks: skipped

    if not song.patterns:
        song.patterns[0] = RctPattern()
    if not song.instruments:
        song.instruments[1] = RctInstrument()
    if not song.order:
        song.order = [min(song.patterns)]
    return song


def load(path: str) -> RctSong:
    with open(path, "rb") as fh:
        return read_rct(fh.read())


def save(path: str, song: RctSong) -> None:
    with open(path, "wb") as fh:
        fh.write(write_rct(song))
