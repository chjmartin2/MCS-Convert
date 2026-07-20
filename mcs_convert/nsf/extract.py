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
       pipeline (register-extreme or wood-block clicks, or dropped).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .. import drums
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
        self.pitched: List[List[Optional[int]]] = [[], [], []]   # p1, p2, tri (midi)
        self.freqs: List[List[float]] = [[], [], []]             # continuous Hz/frame
        self.timbres: List[Tuple] = []       # per frame (p1duty, p1vol, p2duty, p2vol, trivol)
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
        freqs = apu.pitched_freqs()
        sig = tuple(apu.writes)
        apu.end_frame()
        for k in range(3):
            log.pitched[k].append(midis[k])
            log.freqs[k].append(freqs[k])
        log.timbres.append(apu.pitched_timbres())
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
        log.freqs[k] = log.freqs[k][:log.frames]
    log.timbres = log.timbres[:log.frames]
    return header, log


# Drum-click pitches live in the shared palette (see mcs_convert/drums.py).
_CLICKS = drums.CLICKS
_CHANNEL_NAMES = ("Pulse 1", "Pulse 2", "Triangle")

# "auto" splits the noise line by its NES period index: bright/high tones
# (period <= 7, snare/hi-hat "tss") become the hi-hat click, dark/low tones
# (kick "boom") become the low bass. That two-tone mapping is what gives SMB's
# converted drums the same kick/snare shuffle the hardware plays.
_DRUM_BRIGHT_MAX = 7

# Noise-track pitch mapping: midi = base - 3*period, so period 0 (brightest) is
# midi 93 and period 15 (darkest) is 48; the bright/dark threshold (period 7)
# lands exactly on the exporters' _DRUM_BRIGHT_MIDI = 72.
_NOISE_MIDI_BASE = 93


def _drum_pitches(drum_sound: str, period: int) -> Tuple[int, ...]:
    """Pitches for one noise hit of the given NES period index. For "auto",
    bright periods -> hi-hat, dark periods -> low bass; otherwise the fixed
    click the user chose."""
    if drum_sound == "auto":
        return drums.two_tone(period <= _DRUM_BRIGHT_MAX)
    return _CLICKS.get(drum_sound, _CLICKS["block"])

# MCS's tick lands between ~33.5 ms (byte0 0x77) and ~105 ms (0x92); at 60 Hz
# that's a window of ~2.0 to ~6.3 NES frames per tick.
_MIN_TICK_FRAMES, _MAX_TICK_FRAMES = 2.0, 6.3


def _as_channels(onsets) -> List[List[int]]:
    """Normalize the onset argument to a list of per-channel onset lists. A flat
    list of ints (the historical single-stream form, still used by tests) becomes
    one channel; a list of lists passes through."""
    if onsets and isinstance(onsets[0], (list, tuple)):
        return [list(ch) for ch in onsets]
    return [list(onsets)]


def detect_base_unit(onsets) -> int:
    """The song's fundamental note spacing, in NES frames — the most common gap
    between note onsets, ignoring the sub-grid jitter of arpeggios and grace
    notes (gaps of 1-2 frames). Dr. Wily's theme is 227 gaps of exactly 6.

    `onsets` is either a flat onset list or a list of per-channel onset lists.
    Gaps are ALWAYS measured WITHIN a channel, never across channels: two voices
    that share the beat but start on different frames would, if interleaved,
    manufacture phantom sub-grid gaps (Zelda's overworld: three voices each on a
    clean 10-frame grid, but phase-shifted, merged into a spurious mode of 9 — a
    4.5-frame tick that put a quarter of the onsets off the beat)."""
    from collections import Counter
    gaps: "Counter[int]" = Counter()
    for ch in _as_channels(onsets):
        ordered = sorted(ch)
        gaps.update(b - a for a, b in zip(ordered, ordered[1:]) if b - a >= 3)
    if not gaps:
        return 6
    return gaps.most_common(1)[0][0]


def fit_grid(onsets: List[int], play_hz: float) -> Tuple[float, int]:
    """(frames_per_tick, mcs_tempo_byte0) that quantizes to the SONG's own grid.

    The base unit (a 16th note ≈ 6 frames for Wily) must map to a whole number of
    MCS 32nd-ticks or every rhythm distorts. We map it to a 16th = 2 TICKS first:
    that's the natural reading (the shortest common note is a 16th, leaving 1 tick
    for the odd 32nd) and it keeps durations on clean note-values. Mapping it to
    an 8th = 4 ticks instead (finer) over-refines — a song with off-grid 5-7 frame
    notes (Zelda's dungeon theme) smears into muddy 3-tick, tie-ridden rhythms
    (dropped from 50% to 77% clean note-values by preferring 2). We fall to 4
    ticks only for a slow song too coarse for 2, and to 1 tick for a fast one too
    quick — whichever lands in MCS's tempo range.

    `onsets` may be a flat list or per-channel lists; the base unit is measured
    within channels either way (see detect_base_unit)."""
    unit = detect_base_unit(onsets)
    for ticks_per_unit in (2, 4, 1):                 # 16th=2 ticks first (see above)
        fpt = unit / ticks_per_unit
        if _MIN_TICK_FRAMES <= fpt <= _MAX_TICK_FRAMES:
            break
    else:
        fpt = min(max(unit, _MIN_TICK_FRAMES), _MAX_TICK_FRAMES)
    step = max(0, min(9, round((2 * fpt / play_hz - 0.067) / 0.016)))
    byte0 = 0x77 + 3 * step
    # Quantize at the BEAT-ALIGNED fpt (unit maps to a whole number of ticks so
    # every beat lands on an exact tick — no drift). byte0 is just the nearest
    # PLAYABLE MCS tempo; the tiny gap between them is a <~5% playback-speed
    # offset (inaudible), not a per-note timing error. Using the rounded tempo's
    # fpt here instead put SMB's overworld 43 of 85 onsets a tick off the beat.
    return fpt, byte0


def _quantize_channel(events, fpt: float):
    """Quantize one channel's frame-domain notes to ticks with ONSETS ANCHORED
    to their true position — never pushed later (pushing accumulates into a
    drift that desyncs the whole song). Overlaps from rounding truncate the
    earlier note; two notes landing on the same tick (a sub-tick arpeggio or
    grace note that MCS can't resolve) keep the longer one. Returns
    [(start, end, midi)] with 0 < start-spacing."""
    out = []                                        # [start, end, midi]
    for n in events:
        start = round(n.start_tick / fpt)
        end = round((n.start_tick + n.duration_ticks) / fpt)
        if end <= start:
            end = start + 1                         # a real note is worth >=1 tick
        if out and start <= out[-1][0]:             # same onset tick: keep longer
            if end - start > out[-1][1] - out[-1][0]:
                out[-1] = [start, end, n.midi_note]
            continue
        if out and start < out[-1][1]:              # rounding overlap: truncate prev
            out[-1][1] = start
        out.append([start, end, n.midi_note])
    return [(s, e, m) for s, e, m in out if e > s]


def fpt_for_byte0(byte0: int, play_hz: float) -> float:
    """Frames-per-tick that an MCS tempo byte implies: an MCS 32nd-tick lasts
    (0.067 + 0.016*step)/2 s (step = (byte0-0x77)//3), so at `play_hz` frames a
    second that's this many NES frames."""
    step = max(0, min(9, (byte0 - 0x77) // 3))
    return (0.067 + 0.016 * step) / 2.0 * play_hz


_MCS_TEMPOS = tuple(0x77 + 3 * s for s in range(10))


def _off_beat(pts, unit: int, n: int) -> float:
    """Average distance (in ticks, 0..0.5) of the onsets from an exact tick when
    the base unit maps to n ticks. Anchored to the first onset so a whole-song
    PHASE offset (a tune that starts a frame or two in) isn't counted as error —
    only genuine off-grid notes are."""
    o0 = pts[0]
    return sum(abs((o - o0) * n / unit - round((o - o0) * n / unit))
               for o in pts) / len(pts)


def optimize_grid(onsets: List[int], play_hz: float):
    """Exhaustively search (MCS tempo × ticks-per-base-unit) for the grid that
    lands the MOST onsets exactly on ticks, then take the tempo that needs the
    SMALLEST playback-speed nudge to make those beats land dead-on.

    Quantizing at the beat-aligned grid fpt = unit/N puts every unit-multiple on
    an exact tick — but MCS only has ten discrete tempos, so the grid's natural
    speed sits between two. We pick the closest and accept the tiny speed offset —
    a slight, inaudible whole-tune rate change in exchange for exact beats.
    Returns (fpt, byte0, off_beat, speed_pct)."""
    channels = _as_channels(onsets)
    pts = sorted({o for ch in channels for o in ch})
    if len(pts) < 2:
        fpt, byte0 = fit_grid(pts, play_hz)
        return fpt, byte0, 0.0, 0.0
    unit = detect_base_unit(channels)
    best = None
    for n in (1, 2, 3, 4, 6, 8):
        fpt = unit / n
        if not (_MIN_TICK_FRAMES <= fpt <= _MAX_TICK_FRAMES):
            continue
        err = _off_beat(pts, unit, n)
        clean = 0.0 if n in (1, 2, 4) else 0.10   # odd N muddies note-values
        for b in _MCS_TEMPOS:
            speed = abs(fpt_for_byte0(b, play_hz) / fpt - 1.0)   # rate nudge
            score = err + clean + 0.05 * speed
            if best is None or score < best[0]:
                best = (score, fpt, b, err, speed)
    _, fpt, byte0, err, speed = best
    return fpt, byte0, err, speed


def align_to_tempo(onsets: List[int], play_hz: float, target_byte0: int):
    """Grid that plays at a CHOSEN MCS tempo while staying near the real NES
    speed — the base note is given as many ticks as fit that tempo (a faster
    tempo => more ticks per note => a finer grid that pins syncopations tighter),
    then quantized beat-aligned. `speed_pct` is how far the result drifts from the
    true NES rate. Returns (fpt, target_byte0, off_beat, speed_pct)."""
    channels = _as_channels(onsets)
    pts = sorted({o for ch in channels for o in ch})
    if len(pts) < 2:
        return fpt_for_byte0(target_byte0, play_hz), target_byte0, 0.0, 0.0
    unit = detect_base_unit(channels)
    fpt_target = fpt_for_byte0(target_byte0, play_hz)
    n = max(1, round(unit / fpt_target))          # ticks/note to hold NES speed
    fpt = unit / n                                # beat-aligned grid for this N
    err = _off_beat(pts, unit, n)
    speed = abs(n * fpt_target / unit - 1.0)      # drift from true NES speed
    return fpt, target_byte0, err, speed


def frames_to_song(header: NSFHeader, log: FrameLog, subsong: int,
                   percussion: str = "clicks", drum_sound: str = "auto",
                   grid: Optional[Tuple[float, int]] = None) -> Tuple[Song, int]:
    """Frame-domain log -> (Song in 32nd ticks, mcs_tempo_byte0).

    Quantized to `grid` = (frames_per_tick, tempo_byte0), or auto-fit (fit_grid,
    base unit → 2 ticks) when None. The grid's fpt is BEAT-ALIGNED (a whole tick
    count per base unit) so onsets land on ticks; byte0 is only the playback
    tempo. The BPM dial is pure speed — it re-stamps byte0 without touching the
    ticks — so only Exhaustive Optimize (optimize_grid) ever changes the grid."""
    runs = [segment_frames(ch) for ch in log.pitched]
    if grid is not None:
        fpt, byte0 = grid
    else:
        # Fit the grid to the PITCHED onsets only — the melody/harmony is what
        # defines the beat and what the ear tracks. Folding in percussion (SMB's
        # 127 noise hits) drags the base unit to a subdivision that makes the
        # melody's 9-frame notes a muddy 3 ticks instead of a clean 16th (2).
        # Per channel (not merged) so phase-shifted voices don't fake a sub-grid.
        onsets = [[n.start_tick for n in ch] for ch in runs]
        fpt, byte0 = fit_grid(onsets, header.play_rate_hz)

    title = header.song_name or "NSF"
    song = Song(title=f"{title} #{subsong}", source=f"nsf:{header.artist}")
    timbres = log.timbres
    chip_info = (("nes-pulse", None), ("nes-pulse", None), ("nes-triangle", "nestri"))
    for k, (name, events) in enumerate(zip(_CHANNEL_NAMES, runs)):
        chip, fixed_wf = chip_info[k]
        track = Track(name=name, chip=chip, waveform=fixed_wf or "pulse50")
        duty_counts = [0, 0, 0, 0]
        for start, end, midi in _quantize_channel(events, fpt):
            # Universal-tracker nuances: the duty + volume the APU held at the
            # note's onset frame -> per-note waveform (pulse12/25/50/75) and
            # velocity. The triangle has neither; it's always the 4-bit staircase.
            frame = min(len(timbres) - 1, int(start * fpt)) if timbres else -1
            wf, vel, effects = "", 100, {}
            if frame >= 0 and k < 2:
                duty, vol = timbres[frame][2 * k], timbres[frame][2 * k + 1]
                wf = ("pulse12", "pulse25", "pulse50", "pulse75")[duty & 3]
                vel = max(10, round((vol or 15) * 100 / 15))
                effects["duty"] = duty & 3
                duty_counts[duty & 3] += 1
            track.add(NoteEvent(start_tick=start, duration_ticks=end - start,
                                midi_note=midi, velocity=vel, waveform=wf,
                                effects=effects))
        if k < 2 and any(duty_counts):              # the track default = majority duty
            track.waveform = ("pulse12", "pulse25", "pulse50", "pulse75")[
                max(range(4), key=duty_counts.__getitem__)]
        song.add_track(track)                       # kept even when empty: the
        #                                             import dialog shows 5 rows
    song.dropped_short = 0
    # raw per-frame streams so the dialog can play the true NES render for A/B
    song.nsf_preview = {"freqs": log.freqs,
                        "noise": list(log.noise_hits),    # (frame, period) pairs
                        "play_hz": header.play_rate_hz}
    # keep the frame log so changing the tempo can REQUANTIZE (frames_to_song
    # again) instead of re-running the whole emulation
    song.nsf_frames = (header, log, subsong, percussion, drum_sound)
    # retrack() reads these when reducing for MCS/Tandy targets
    song.percussion_pref = (percussion, drum_sound)

    # The NOISE channel is now a first-class kind="noise" track (the universal
    # tracker keeps the real thing); midi encodes the period's brightness
    # (93 - 3*period: p=0 brightest hiss, p=15 darkest rumble) so the synth's
    # LFSR voice and the exporters' bright/dark split both read it directly.
    # Converting to MCS drum CLICKS happens at retrack/export time, not here.
    noise = Track(name="Noise", kind="noise", chip="nes-noise", waveform="noise")
    if percussion != "drop":
        last = -1
        for f, period in log.noise_hits:
            tick = round(f / fpt)
            if tick == last:                        # one hit per tick is plenty
                continue
            last = tick
            noise.add(NoteEvent(start_tick=tick, duration_ticks=1,
                                midi_note=_NOISE_MIDI_BASE - 3 * (period & 15),
                                effects={"nesperiod": period & 15}))
    noise.meta["drum_notes"] = len(noise.notes)
    song.add_track(noise)
    # DPCM sample hits have no pitch/period: keep them as dark drum hits.
    dpcm = Track(name="DPCM", kind="drum", chip="nes-dpcm", waveform="noise")
    if percussion != "drop":
        last = -1
        for f in log.dpcm_hits:
            tick = round(f / fpt)
            if tick == last:
                continue
            last = tick
            dpcm.add(NoteEvent(start_tick=tick, duration_ticks=1,
                               midi_note=_NOISE_MIDI_BASE - 3 * 15,
                               percussive=True))
    dpcm.meta["drum_notes"] = len(dpcm.notes)
    song.add_track(dpcm)
    return song, byte0


def optimize_song(song: Song):
    """Re-quantize an NSF-imported Song onto its best-aligning grid (see
    optimize_grid), reusing the stashed frame log (no re-emulation). Returns
    (new_song, tempo_byte0, off_beat_error, speed_pct); the song unchanged with
    (0x80, 0, 0) if it wasn't NSF-imported."""
    return _requantize(song, optimize_grid)


def optimize_song_at(song: Song, target_byte0: int):
    """Re-quantize an NSF-imported Song to a CHOSEN MCS tempo (align_to_tempo),
    keeping close to real NES speed. Returns (new_song, tempo_byte0,
    off_beat_error, speed_pct)."""
    return _requantize(song, lambda o, hz: align_to_tempo(o, hz, target_byte0))


def _requantize(song: Song, grid_fn):
    """Shared body for optimize_song / optimize_song_at: run `grid_fn(pitched
    onsets, play_hz) -> (fpt, byte0, err, speed)` and re-quantize from the stashed
    frame log (no re-emulation)."""
    frames = getattr(song, "nsf_frames", None)
    if not frames:
        return song, 0x80, 0.0, 0.0
    header, log, subsong, percussion, drum_sound = frames
    runs = [segment_frames(ch) for ch in log.pitched]
    onsets = [[n.start_tick for n in ch] for ch in runs]   # per channel (see fit)
    fpt, byte0, err, speed = grid_fn(onsets, header.play_rate_hz)
    new, byte0 = frames_to_song(header, log, subsong, percussion, drum_sound,
                                grid=(fpt, byte0))
    return new, byte0, err, speed


def extract_song(path: str, subsong: Optional[int] = None,
                 max_seconds: float = 180.0, percussion: str = "clicks",
                 drum_sound: str = "auto", detect_end: bool = True,
                 grid: Optional[Tuple[float, int]] = None) -> Tuple[Song, int]:
    """Emulate an NSF subsong and return (Song, mcs_tempo_byte0)."""
    with open(path, "rb") as fh:
        data = fh.read()
    header, log = run_nsf(data, subsong, max_seconds, detect_end)
    n = subsong if subsong is not None else header.starting_song
    return frames_to_song(header, log, n, percussion, drum_sound, grid=grid)
