"""NSF -> Song extraction: emulate the player, log APU state, segment into notes.

Pipeline (per selected subsong):

    1. Parse the header, load program data (with 4 KB bankswitching if used).
    2. JSR init_addr with A = song index, X = region; then JSR play_addr once per
       frame at the header's play rate, clocking the APU's envelope/length gates.
    3. Stop at a detected LOOP (the per-frame register-write stream repeats — cut
       after one clean pass), at sustained silence (jingles end), or at a cap.
    4. Fit the 60 Hz frame stream onto MCS's 32nd-tick grid (frames-per-tick and
       tempo byte chosen to minimize onset quantization error), segment pitched
       channels into notes, and render noise/DPCM key-ons through the percussion
       pipeline (cluster or wood-block clicks, or dropped).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..model import NoteEvent, Song, Track
from .apu import APUState
from .cpu6502 import CPU6502, MemoryBus
from .header import HEADER_SIZE, NSFHeader

_LOOP_WINDOW = 120          # frames that must match to PROPOSE a loop (~2 s)
_MIN_LOOP = 900             # song loops are long: periods under ~15 s are just
#                             repeated sections (SMB plays its A strain twice)
_CONFIRM_CAP = 1800         # verify up to ~30 s of verbatim replay before cutting
_SILENCE_FRAMES = 150       # this much dead air ends the song (~2.5 s)


def segment_frames(frames: List[Optional[int]]) -> List[NoteEvent]:
    """Collapse a per-frame [midi_note or None] stream into NoteEvents.

    A run of identical, non-None notes on consecutive frames becomes one NoteEvent
    whose duration is the run length (in frames). Retriggers of the *same* pitch
    are merged unless a None frame separates them."""
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


class FrameLog:
    """Everything the emulation run produced, still in 60 Hz frame time."""

    def __init__(self) -> None:
        self.pitched: List[List[Optional[int]]] = [[], [], []]   # p1, p2, tri
        self.noise_hits: List[Tuple[int, int]] = []
        self.dpcm_hits: List[int] = []
        self.frames = 0
        self.ended_by = "cap"


def run_nsf(data: bytes, subsong: Optional[int] = None,
            max_seconds: float = 180.0,
            detect_end: bool = True) -> Tuple[NSFHeader, FrameLog]:
    """Emulate one subsong; returns (header, frame log). subsong is 1-based.
    With detect_end=False the loop/silence detectors are off — you get exactly
    max_seconds (the dialog's explicit length override)."""
    header = NSFHeader.parse(data)
    if header.expansion_chips:
        raise ValueError("expansion-chip NSFs (" + ", ".join(header.expansion_chips)
                         + ") aren't supported yet")
    program = data[HEADER_SIZE:]
    song_index = (subsong if subsong is not None else header.starting_song) - 1

    apu = APUState()
    bus = MemoryBus(on_apu_write=apu.write)
    if header.uses_bankswitching:
        pad = header.load_addr & 0x0FFF
        rom = b"\x00" * pad + program
        banks = [rom[i * 4096:(i + 1) * 4096].ljust(4096, b"\x00")
                 for i in range((len(rom) + 4095) // 4096)]

        def set_bank(addr: int, value: int) -> None:
            slot = addr - 0x5FF8
            if value < len(banks):
                bus.ram[0x8000 + slot * 4096: 0x9000 + slot * 4096] = banks[value]

        bus.on_bank_write = set_bank
        for slot, value in enumerate(header.bankswitch):
            set_bank(0x5FF8 + slot, value)
    else:
        bus.load(header.load_addr, program)

    cpu = CPU6502(bus)
    cpu.call(header.init_addr, a=song_index, x=1 if header.is_pal else 0)

    log = FrameLog()
    max_frames = int(max_seconds * header.play_rate_hz)
    sigs: List[tuple] = []                       # per-frame APU-write signatures
    window_seen: Dict[int, int] = {}
    silent_run = 0
    hits_before = 0
    # Loop hypothesis: (period, frames_confirmed). Songs repeat short PHRASES
    # constantly, so a matching window only proposes a period — we cut only
    # after a FULL extra period replays verbatim.
    hypothesis: Optional[Tuple[int, int]] = None

    for frame in range(max_frames):
        cpu.call(header.play_addr)
        midis = apu.pitched_midis()
        sig = tuple(apu.writes)
        apu.end_frame()
        for k in range(3):
            log.pitched[k].append(midis[k])
        log.frames = frame + 1

        if not detect_end:
            continue
        # silence: nothing pitched and no new percussion for a stretch
        new_hits = len(apu.noise_hits) + len(apu.dpcm_hits)
        if all(m is None for m in midis) and new_hits == hits_before:
            silent_run += 1
            if silent_run >= _SILENCE_FRAMES and frame > _SILENCE_FRAMES:
                log.frames = frame + 1 - silent_run + 30    # keep a small tail
                log.ended_by = "silence"
                break
        else:
            silent_run = 0
        hits_before = new_hits

        sigs.append(sig)
        if hypothesis is not None:
            period, confirmed = hypothesis
            if sig == sigs[frame - period]:
                confirmed += 1
                if confirmed >= min(period, _CONFIRM_CAP):
                    log.frames = frame + 1 - confirmed      # one clean pass
                    log.ended_by = "loop"
                    break
                hypothesis = (period, confirmed)
            else:
                hypothesis = None                # phrase repeat, not a song loop
        if hypothesis is None and len(sigs) >= _LOOP_WINDOW:
            window = tuple(sigs[-_LOOP_WINDOW:])
            h = hash(window)
            prev = window_seen.get(h)
            if prev is not None and frame - prev >= _MIN_LOOP and \
                    tuple(sigs[prev - _LOOP_WINDOW + 1: prev + 1]) == window:
                hypothesis = (frame - prev, _LOOP_WINDOW)
            elif prev is None:
                window_seen[h] = frame

    log.noise_hits = [(f, p) for f, p in apu.noise_hits if f < log.frames]
    log.dpcm_hits = [f for f in apu.dpcm_hits if f < log.frames]
    for k in range(3):
        log.pitched[k] = log.pitched[k][:log.frames]
    return header, log


def fit_grid(onsets: List[int], play_hz: float) -> Tuple[int, int]:
    """Choose (frames_per_tick, mcs_tempo_byte0) for the 32nd-tick grid: the tick
    must land in MCS's tempo range and note onsets should sit near tick edges.
    Among near-equal fits prefer the COARSEST grid — durations then land on
    musical note values instead of every note becoming a 32nd."""
    cands = []
    for fpt in (2, 3, 4, 5, 6):
        tick = fpt / play_hz
        step = round((2 * tick - 0.067) / 0.016)
        if not 0 <= step <= 9:
            continue
        got = (0.067 + 0.016 * step) / 2.0
        tempo_err = abs(got - tick) / tick
        if onsets:
            q = sum(min(o % fpt, fpt - o % fpt) / fpt for o in onsets) / len(onsets)
        else:
            q = 0.0
        cands.append((q + 2.0 * tempo_err, fpt, 0x77 + 3 * step))
    if not cands:
        return 3, 0x77
    best_score = min(c[0] for c in cands)
    good = [c for c in cands if c[0] <= best_score + 0.02]
    _, fpt, byte0 = max(good, key=lambda c: c[1])
    return fpt, byte0


_CLICKS = {"cluster": (55, 56), "block": (62,)}
_CHANNEL_NAMES = ("Pulse 1", "Pulse 2", "Triangle")


def frames_to_song(header: NSFHeader, log: FrameLog, subsong: int,
                   percussion: str = "clicks",
                   drum_sound: str = "cluster") -> Tuple[Song, int]:
    """Frame-domain log -> (Song in 32nd ticks, mcs_tempo_byte0)."""
    runs = [segment_frames(ch) for ch in log.pitched]
    onsets = [n.start_tick for ch in runs for n in ch]
    onsets += [f for f, _ in log.noise_hits] + list(log.dpcm_hits)
    fpt, byte0 = fit_grid(sorted(onsets), header.play_rate_hz)

    title = header.song_name or "NSF"
    song = Song(title=f"{title} #{subsong}", source=f"nsf:{header.artist}")
    for name, events in zip(_CHANNEL_NAMES, runs):
        track = Track(name=name)
        for n in events:
            start = round(n.start_tick / fpt)
            end = round((n.start_tick + n.duration_ticks) / fpt)
            if end <= start:
                if n.duration_ticks * 2 < fpt:      # sub-half-tick blip: drop
                    continue
                end = start + 1
            track.add(NoteEvent(start_tick=start, duration_ticks=end - start,
                                midi_note=n.midi_note))
        song.add_track(track)                       # kept even when empty: the
        #                                             import dialog shows 5 rows

    for name, hits in (("Noise", [f for f, _ in log.noise_hits]),
                       ("DPCM", log.dpcm_hits)):
        track = Track(name=name)
        if percussion != "drop":
            last = -1
            for f in hits:
                tick = round(f / fpt)
                if tick == last:                    # one hit per tick is plenty
                    continue
                last = tick
                for midi in _CLICKS[drum_sound]:
                    track.add(NoteEvent(start_tick=tick, duration_ticks=1,
                                        midi_note=midi, percussive=True))
        track.meta["drum_notes"] = len(track.notes)
        song.add_track(track)
    return song, byte0


def extract_song(path: str, subsong: Optional[int] = None,
                 max_seconds: float = 180.0, percussion: str = "clicks",
                 drum_sound: str = "cluster",
                 detect_end: bool = True) -> Tuple[Song, int]:
    """Emulate an NSF subsong and return (Song, mcs_tempo_byte0)."""
    with open(path, "rb") as fh:
        data = fh.read()
    header, log = run_nsf(data, subsong, max_seconds, detect_end)
    n = subsong if subsong is not None else header.starting_song
    return frames_to_song(header, log, n, percussion, drum_sound)
