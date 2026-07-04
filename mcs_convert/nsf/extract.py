"""NSF -> Song extraction: emulate the player, log APU writes, segment into notes.

Pipeline (per selected subsong):

    1. Parse header, load program data at load_addr.
    2. Set up the 6502; JSR init_addr with A = song index, X = region.
    3. For each frame (at header.play_rate_hz): JSR play_addr, then sample APU state.
    4. Turn the per-frame channel pitch/on-state stream into NoteEvents
       (a note ends when the channel goes silent or its pitch changes).

Steps 2-3 depend on the 6502 core (cpu6502.py), which is still a skeleton, so
`extract_song` raises until that lands. Step 4's segmentation logic lives in
`segment_frames`, which is pure and independently testable.
"""

from __future__ import annotations

from typing import List, Optional

from ..model import NoteEvent, Song, Track
from .apu import APUState, PULSE1, PULSE2, TRIANGLE
from .cpu6502 import CPU6502, MemoryBus
from .header import NSFHeader, HEADER_SIZE

_CHANNEL_NAMES = {PULSE1: "Pulse 1", PULSE2: "Pulse 2", TRIANGLE: "Triangle"}


def segment_frames(frames: List[Optional[int]]) -> List[NoteEvent]:
    """Collapse a per-frame [midi_note or None] stream into NoteEvents.

    A run of identical, non-None notes on consecutive frames becomes one NoteEvent whose
    duration is the run length. This is deliberately simple; retriggers of the *same*
    pitch (a rapid off/on) are merged unless a None frame separates them.
    """
    events: List[NoteEvent] = []
    run_note: Optional[int] = None
    run_start = 0
    for i, note in enumerate(frames):
        if note != run_note:
            if run_note is not None:
                events.append(NoteEvent(run_start, i - run_start, run_note))
            run_note = note
            run_start = i
    if run_note is not None:
        events.append(NoteEvent(run_start, len(frames) - run_start, run_note))
    return events


def extract_song(path: str, subsong: Optional[int] = None) -> Song:
    """Emulate an NSF subsong and return a populated Song. Requires the 6502 core."""
    with open(path, "rb") as fh:
        data = fh.read()
    header = NSFHeader.parse(data)
    program = data[HEADER_SIZE:]
    song_index = (subsong if subsong is not None else header.starting_song) - 1

    apu = APUState()
    bus = MemoryBus(on_apu_write=apu.write)
    if not header.uses_bankswitching:
        bus.load(header.load_addr, program)
    cpu = CPU6502(bus)

    # --- the part that needs the CPU core ---
    cpu.a = song_index & 0xFF
    cpu.x = 1 if header.is_pal else 0
    cpu.reset(header.init_addr)
    cpu.run_until_rts()  # raises NotImplementedError today

    # Once the core works, this loop fills per-channel frame streams:
    #   for _ in range(num_frames):
    #       cpu.reset(header.play_addr); cpu.run_until_rts()
    #       for ch in (PULSE1, PULSE2, TRIANGLE):
    #           streams[ch].append(apu.channels[ch].midi_note(ch)
    #                              if apu.channels[ch].is_sounding(ch) else None)
    raise NotImplementedError("blocked on cpu6502 core")


def build_song(streams: dict, title: str, tick_hz: float, source: str) -> Song:
    """Assemble a Song from finished per-channel frame streams (used once emulation runs)."""
    song = Song(title=title, tick_hz=tick_hz, source=source)
    for index in (PULSE1, PULSE2, TRIANGLE):
        track = Track(name=_CHANNEL_NAMES[index])
        track.notes = segment_frames(streams.get(index, []))
        song.add_track(track)
    return song
