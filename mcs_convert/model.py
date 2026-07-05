"""The intermediate representation both ends talk through.

The NSF side *produces* a `Song`; the MCS side *consumes* one. Keeping a neutral model
in the middle means adding a new input (e.g. VGM, MIDI) or a new output later doesn't
require touching the other end.

Timing is expressed in absolute *ticks*. One tick = one player call (one video frame for
a standard NSF: ~1/60 s NTSC). `Song.tick_hz` records the real-world rate so the MCS
writer can quantize durations to notation values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class NoteEvent:
    """A single sounded note (or a rest) on one track."""

    start_tick: int
    duration_ticks: int
    midi_note: int          # 0-127 (ignored when is_rest)
    velocity: int = 100     # 0-127, derived from channel volume where available
    is_rest: bool = False   # a timed silence; occupies duration but sounds nothing

    @property
    def end_tick(self) -> int:
        return self.start_tick + self.duration_ticks


@dataclass
class Track:
    """One monophonic voice (e.g. NES pulse 1). Notes are time-ordered, non-overlapping."""

    name: str
    notes: List[NoteEvent] = field(default_factory=list)

    def add(self, note: NoteEvent) -> None:
        self.notes.append(note)

    @property
    def last_tick(self) -> int:
        return max((n.end_tick for n in self.notes), default=0)


@dataclass
class Song:
    """A whole piece: several parallel tracks plus timing metadata."""

    title: str = ""
    tick_hz: float = 60.0                       # player calls per second
    tracks: List[Track] = field(default_factory=list)
    source: str = ""                            # provenance, e.g. "nsf:mygame.nsf#3"

    def add_track(self, track: Track) -> Track:
        self.tracks.append(track)
        return track

    @property
    def length_ticks(self) -> int:
        return max((t.last_tick for t in self.tracks), default=0)
