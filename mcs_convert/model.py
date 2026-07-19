"""The intermediate representation both ends talk through — the UNIVERSAL TRACKER.

Importers (*NSF, PT3, MCS, future formats*) *produce* a `Song` capturing every
nuance the source expresses — waveforms, duty cycles, volumes, effects, noise
voices, any number of tracks. Exporters *consume* one, enforcing their own
constraints at export time (MCS: 4 square voices on a notation grid; Tandy: 3
squares + noise; SoundBlaster: any waveform...). The model itself never limits.

Timing is expressed in absolute *ticks*. One tick = one player call (one video frame
for a standard NSF: ~1/60 s NTSC). `Song.tick_hz` records the real-world rate so the
MCS writer can quantize durations to notation values.

Waveform names (Track.waveform, NoteEvent.waveform override):
    "square"    50% pulse (== pulse50)          "sine"      pure sine
    "triangle"  ideal triangle                  "nestri"    NES 4-bit stepped triangle
    "pulse12"   NES 12.5% duty pulse            "pulse25"   NES 25% duty pulse
    "pulse50"   NES 50% duty pulse              "pulse75"   NES 75% duty (25% inverted)
    "noise"     LFSR white noise (pitch sets the shift-clock rate)
    "pcspeaker" the 1-bit delta-sigma ensemble render (render-level, not per-voice)

Effects nomenclature (NoteEvent.effects keys — the universal vocabulary):
    PT3 / AY-3-8910:
        "orn": N       ornament number (arpeggio table cycling relative semitones)
        "env": shape   hardware envelope shape 0-15 driving the note's timbre
        "envper": P    hardware envelope period
        "slide": +/-N  tone slide, semitones per row (PT3 effect 1/2)
        "porta": N     portamento toward the note over N rows (PT3 effect 3)
        "sampfx": N    sample number when it shapes timbre beyond volume
    NES / 2A03:
        "duty": 0-3    pulse duty index (12.5/25/50/75%) — also mirrored in
                       NoteEvent.waveform as pulse12/25/50/75
        "sweep": reg   $4001/$4005 sweep-unit register when active
        "decay": 1     envelope (non-constant volume) was driving this note
    Generic:
        "vib": (speed, depth)   vibrato
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

#: Track.kind values
KIND_TONE = "tone"          # a pitched melodic voice
KIND_NOISE = "noise"        # an LFSR noise voice (NES noise channel, AY noise)
KIND_DRUM = "drum"          # synthesized percussion clicks (legacy percussive path)

#: All waveform names the synth understands (see module docstring).
WAVEFORMS = ("square", "triangle", "sine", "nestri",
             "pulse12", "pulse25", "pulse50", "pulse75", "noise")


@dataclass
class NoteEvent:
    """A single sounded note (or a rest) on one track."""

    start_tick: int
    duration_ticks: int
    midi_note: int          # 0-127 (ignored when is_rest)
    velocity: int = 100     # 0-127, derived from channel volume where available
    is_rest: bool = False   # a timed silence; occupies duration but sounds nothing
    tied: bool = False       # a tie/slur mark carries this note into the next (MCS 0x13/0x19)
    octave: int = 0          # 1 if under an 8va (MCS 0x12; the engine only shifts up)
    percussive: bool = False  # a synthesized drum click: pinned to the floor, exempt
    #                           from octave shifts (importers set this, e.g. AY noise)
    waveform: str = ""       # per-note waveform override ("" = the track's default)
    effects: dict = field(default_factory=dict)   # universal effects (module docstring)

    @property
    def end_tick(self) -> int:
        return self.start_tick + self.duration_ticks


@dataclass
class Track:
    """One monophonic voice (e.g. NES pulse 1). Notes are time-ordered, non-overlapping.

    A Song may carry ANY number of tracks; exporters that support fewer voices
    reduce at export time (that's the "retrack" step), never at import."""

    name: str
    notes: List[NoteEvent] = field(default_factory=list)
    meta: dict = field(default_factory=dict)   # importer hints (e.g. noise usage)
    kind: str = KIND_TONE                      # tone / noise / drum
    waveform: str = "square"                   # default synth waveform for this track
    chip: str = ""                             # provenance: "nes-pulse", "nes-triangle",
    #                                            "nes-noise", "ay-tone", "mcs", ...

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
    # Extracted display metadata (populated by the MCS reader; see docs/mcs-format.md).
    time_signature: str = ""                    # e.g. "3/4", derived from measure length
    key_signature: str = ""                     # e.g. "G major", from the clef-record accidentals
    timesig_code: Optional[int] = None          # 0x05 meter code: 0=2/4,1=4/4,2=6/8,3=3/4
    tempo_tick_seconds: float = 0.042           # real seconds per 32nd-tick (from header byte0)
    # Positional annotations for the tracker's event column: (start_tick, staff_name, label),
    # e.g. a mid-staff clef change. Not sounded — a diagnostic marker.
    events: List[tuple] = field(default_factory=list)

    def add_track(self, track: Track) -> Track:
        self.tracks.append(track)
        return track

    @property
    def length_ticks(self) -> int:
        return max((t.last_tick for t in self.tracks), default=0)
