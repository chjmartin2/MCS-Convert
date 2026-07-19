"""Tiny software synth: render a Song to PCM and play it (Windows, winmm waveOut).

Deliberately simple — a chiptune-flavoured square/triangle synth good enough to *hear*
whether the decoded notes are right. Not a faithful PC-speaker/Tandy emulation.

Playback goes through winmm's waveOut API (WaveOutPlayer): live volume, pause/resume,
and sample-accurate position, which drives the GUI playhead. Non-Windows hosts can
still synthesize and get WAV bytes; only live playback is Windows-only for now.
"""

from __future__ import annotations

import io
import wave
from typing import List, Tuple

import numpy as np

from .model import NoteEvent, Song


def midi_to_freq(midi: int) -> float:
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


def _note_events(notes: List[NoteEvent]) -> List[Tuple[int, int, int]]:
    """(start_tick, duration_ticks, midi) for sounding notes, with tied same-pitch chains
    merged into ONE event. A tie means "don't re-attack": rendered as two notes, the
    second restarts phase and re-fades, which pops audibly (AXEL.MCD bar 3.28's D5~ into
    a half-note D5). A tie to a *different* pitch is a slur and keeps its own onset."""
    sounding = [n for n in notes if not n.is_rest]
    starts = {}
    for i, n in enumerate(sounding):
        starts.setdefault((n.start_tick, n.midi_note), i)
    consumed: set = set()
    events: List[Tuple[int, int, int]] = []
    for i, n in enumerate(sounding):
        if i in consumed:
            continue
        dur, cur = n.duration_ticks, n
        while cur.tied:
            j = starts.get((n.start_tick + dur, n.midi_note))
            if j is None or j in consumed:
                break
            consumed.add(j)
            cur = sounding[j]
            dur += cur.duration_ticks
        events.append((n.start_tick, dur, n.midi_note))
    return events


def _allocate_voices(per_track: List[List[Tuple[int, int, int]]], n: int = 4
                     ) -> List[List[Tuple[int, int, int]]]:
    """Deal events onto n voice channels: at each onset the highest pitch takes the
    first free channel, so channel 0 is the tracker's v1. Overflow (shouldn't happen —
    MCS itself has four voices) doubles up on the channel that frees soonest."""
    evs = sorted((e for track in per_track for e in track), key=lambda e: (e[0], -e[2]))
    ends = [0] * n
    chans: List[List[Tuple[int, int, int]]] = [[] for _ in range(n)]
    for e in evs:
        free = [i for i in range(n) if ends[i] <= e[0]]
        i = free[0] if free else min(range(n), key=ends.__getitem__)
        chans[i].append(e)
        ends[i] = max(ends[i], e[0] + e[1])
    return chans


def tempo_bpm(tick_seconds: float) -> float:
    """Quarter-note BPM for a per-tick duration. A tick is a 32nd, so 8 ticks per quarter.

    The real tempo is set by the file's header byte 0 (see reader.tick_seconds_for),
    measured from DOSBox-X captures — NOT the 0x05 "level" word."""
    return 60.0 / (8.0 * tick_seconds)


#: NES pulse duty fractions by universal waveform name (square == pulse50).
_DUTIES = {"pulse12": 0.125, "pulse25": 0.25, "pulse50": 0.5, "pulse75": 0.75}


def _wave(phase: np.ndarray, waveform: str) -> np.ndarray:
    """One cycle-normalized oscillator for every universal waveform name."""
    if waveform in _DUTIES:                          # NES pulse duty cycles
        return np.where(np.mod(phase, 1.0) < _DUTIES[waveform], 1.0, -1.0
                        ).astype(np.float32)
    if waveform == "square":
        return np.sign(np.sin(2 * np.pi * phase)).astype(np.float32)
    if waveform == "triangle":
        return (2.0 * np.abs(2.0 * (phase - np.floor(phase + 0.5))) - 1.0).astype(np.float32)
    if waveform == "nestri":                         # NES triangle: 4-bit staircase
        tri = 2.0 * np.abs(2.0 * (phase - np.floor(phase + 0.5))) - 1.0
        return (np.round((tri + 1.0) * 7.5) / 7.5 - 1.0).astype(np.float32)
    return np.sin(2 * np.pi * phase).astype(np.float32)  # sine


# A maximal 15-bit LFSR bit sequence (the NES noise generator), precomputed once:
# 32767 pseudo-random bits we resample at the note's clock rate.
_LFSR_BITS = None


def _lfsr_bits() -> np.ndarray:
    global _LFSR_BITS
    if _LFSR_BITS is None:
        reg, out = 1, np.empty(32767, dtype=np.float32)
        for i in range(32767):
            bit = (reg ^ (reg >> 1)) & 1
            reg = (reg >> 1) | (bit << 14)
            out[i] = 1.0 if (reg & 1) else -1.0
        _LFSR_BITS = out
    return _LFSR_BITS


def _render_noise_track(events, sr: int, step: float, amp: float) -> np.ndarray:
    """A kind="noise" track: LFSR noise whose shift clock follows the note's pitch
    (midi -> freq x 64), so high notes hiss and low notes rumble — the universal
    noise voice (NES noise channel, AY noise)."""
    if not events:
        return np.zeros(1, dtype=np.float32)
    total = int(max(s + d for s, d, *_ in events) * step * sr)
    out = np.zeros(max(total, 1), dtype=np.float32)
    bits = _lfsr_bits()
    fade = max(1, int(0.004 * sr))
    for ev in events:
        start, dur, midi = ev[0], ev[1], ev[2]
        vel = ev[3] if len(ev) > 3 else 100
        pos = int(start * step * sr)
        ns = int(dur * step * sr)
        if ns <= 0:
            continue
        clock = midi_to_freq(midi) * 64.0            # LFSR shifts per second
        idx = (np.arange(ns, dtype=np.float64) * clock / sr).astype(np.int64) % len(bits)
        seg = bits[idx] * (amp * vel / 100.0)
        f = min(fade, ns // 2)
        if f > 0:
            seg[:f] *= np.linspace(0.0, 1.0, f, dtype=np.float32)
            seg[-f:] *= np.linspace(1.0, 0.0, f, dtype=np.float32)
        out[pos:pos + ns] += seg
    return out


def _render_track(events, sr: int, step: float, amp: float, waveform: str) -> np.ndarray:
    """Render note events at their absolute positions. Events are (start, dur, midi)
    or the richer (start, dur, midi, velocity, waveform) — the per-note waveform
    (when non-empty) overrides the track default, velocity scales the amplitude.

    Placement is by start_tick (not cumulative), so chords overlap and a staff that
    sits out a measure stays silent for it. Rests need no events — silence is the
    default between placed notes.
    """
    total_samples = int(max(e[0] + e[1] for e in events) * step * sr)
    out = np.zeros(max(total_samples, 1), dtype=np.float32)
    fade = max(1, int(0.006 * sr))
    for ev in events:
        start, dur, midi = ev[0], ev[1], ev[2]
        vel = ev[3] if len(ev) > 3 else 100
        wf = (ev[4] if len(ev) > 4 and ev[4] else waveform)
        pos = int(start * step * sr)
        ns = int(dur * step * sr)
        if ns <= 0:
            continue
        t = np.arange(ns, dtype=np.float32) / sr
        seg = (amp * vel / 100.0) * _wave(midi_to_freq(midi) * t, wf)
        # short linear fades to kill clicks between notes
        f = min(fade, ns // 2)
        if f > 0:
            seg[:f] *= np.linspace(0.0, 1.0, f, dtype=np.float32)
            seg[-f:] *= np.linspace(1.0, 0.0, f, dtype=np.float32)
        out[pos:pos + ns] += seg
    return out


def _full_events(notes: List[NoteEvent]):
    """Like _note_events but keeping velocity + per-note waveform:
    (start, dur, midi, velocity, waveform). Tied same-pitch chains merge."""
    merged = _note_events(notes)
    detail = {(n.start_tick, n.midi_note): (n.velocity, n.waveform)
              for n in notes if not n.is_rest}
    return [(s, d, m, *detail.get((s, m), (100, ""))) for s, d, m in merged]


def _render_pcspeaker(events: List[Tuple[int, int, int]], sr: int, step: float) -> np.ndarray:
    """Reproduce MCS's 4-voice PC-speaker rendering.

    The real engine (MCSDISK.EXE loop at image 0x1929) runs four phase accumulators, one
    per voice, adds a per-voice increment each pass, and combines their overflows into the
    single 1-bit speaker — all four voices sound at once, quantized to 1 bit. This models
    that faithfully (not cycle-exact): sum the voices' 1-bit square waves, then render the
    sum back to 1 bit with first-order delta-sigma (PDM), which is what gives the speaker
    its gritty texture. Comparable directly against real captures. See docs/mcs-format.md.
    """
    if not events:
        return np.zeros(1, dtype=np.float32)
    total = int(max(s + d for s, d, _ in events) * step * sr) + 1
    level = np.zeros(total, dtype=np.float32)               # 0..N voices high per sample
    for start, dur, midi in events:
        pos = int(start * step * sr)
        ns = int(dur * step * sr)
        if ns <= 0:
            continue
        phase = midi_to_freq(midi) * np.arange(pos, pos + ns, dtype=np.float64) / sr
        level[pos:pos + ns] += (np.mod(phase, 1.0) < 0.5).astype(np.float32)  # 1-bit square
    peak = float(level.max()) or 1.0
    duty = level / peak                                     # target speaker density 0..1
    # First-order delta-sigma: the running count of output 1s tracks the integral of duty,
    # so each output bit is floor(cumsum) stepping up. Vectorized (no per-sample loop).
    cum = np.floor(np.cumsum(duty, dtype=np.float64))
    bits = np.diff(np.concatenate(([0.0], cum)))           # 0/1 speaker state per sample
    return (bits * 2.0 - 1.0).astype(np.float32) * 0.6


def _pulse_wave(phase: np.ndarray, duty: float = 0.5) -> np.ndarray:
    """NES pulse channel: a square with a duty cycle (12.5/25/50%). Duty 0.5 is
    a plain square; the narrower duties give the reedy NES lead timbre."""
    return np.where(np.mod(phase, 1.0) < duty, 1.0, -1.0).astype(np.float32)


# NES noise timer periods (NTSC), index 0..15. The LFSR is clocked at
# 1.789773 MHz / period, so a SMALL index (short period) is a fast clock = bright
# high-frequency hiss (snare/hi-hat), and a LARGE index is a slow clock = dark
# low-frequency rumble (kick). SMB's drum line alternates period ~3-4 (tss) with
# period ~12 (boom) — that two-tone contrast is the shuffle's groove.
_NOISE_PERIODS = (4, 8, 16, 32, 64, 96, 128, 160,
                  202, 254, 380, 508, 762, 1016, 2034, 4068)
_NES_CLOCK = 1789773.0


def _lowpass(sig: np.ndarray, cutoff: float, sr: int) -> np.ndarray:
    """One-pole IIR low-pass. cutoff >= nyquist -> passthrough (bright noise)."""
    if cutoff >= sr / 2:
        return sig
    import math
    dt = 1.0 / sr
    rc = 1.0 / (2 * math.pi * cutoff)
    a = dt / (rc + dt)
    out = np.empty_like(sig)
    y = 0.0
    for i in range(len(sig)):
        y += a * (sig[i] - y)
        out[i] = y
    return out


def _render_noise(mix, noise_frames, frame_len, total, sample_rate) -> None:
    """Mix in the noise/drum hits. `noise_frames` may be bare frame indices OR
    (frame, period_index) pairs; with the period we color each burst by tone
    (bright hat vs dark kick) so SMB's two-tone shuffle survives instead of
    collapsing to one undifferentiated hiss."""
    rng = np.random.default_rng(0)
    for item in noise_frames:
        if isinstance(item, (tuple, list)):
            fr, period = item[0], int(item[1])
        else:
            fr, period = item, 3                     # legacy: bright default
        pos = int(fr * frame_len)
        # dark tones (high period) ring a touch longer, like a kick body
        secs = 0.03 if period <= 7 else 0.06
        burst = int(secs * sample_rate)
        ns = min(burst, total - pos)
        if ns <= 0:
            continue
        env = np.linspace(1.0, 0.0, burst, dtype=np.float32)[:ns] ** 2
        noise = rng.uniform(-1.0, 1.0, ns).astype(np.float32)
        cutoff = _NES_CLOCK / _NOISE_PERIODS[max(0, min(15, period))]
        shaped = _lowpass(noise, cutoff, sample_rate).astype(np.float32)
        # a low sine "boom" gives the dark (kick) hits a pitched body
        if period > 7:
            t = np.arange(ns, dtype=np.float32) / sample_rate
            shaped = shaped * 0.6 + 0.9 * np.sin(2 * np.pi * 70.0 * t).astype(np.float32)
        peak = float(np.max(np.abs(shaped))) or 1.0
        mix[pos:pos + ns] += 0.20 * (shaped / peak) * env


def render_nes(freqs, noise_frames, play_hz: float, sample_rate: int = 22050,
               max_seconds: float = None) -> Tuple[np.ndarray, int]:
    """Render the raw NES per-frame CONTINUOUS-frequency streams to PCM with
    NES-like timbres — the true-hardware reference, before any MCS quantization.

    `freqs` is three lists of per-frame Hz (0 = silent). Because we drive a
    phase-accumulating oscillator from the actual per-frame frequency (not a
    rounded MIDI note), vibrato and pitch slides survive — that continuous
    pitch wobble is much of what makes NES music sound alive. pulse 1 & 2 are
    duty-cycle squares, triangle a triangle wave, noise white-noise bursts."""
    frame_len = sample_rate / play_hz
    n_frames = max((len(s) for s in freqs), default=0)
    total = int(n_frames * frame_len) + sample_rate // 2
    if max_seconds:
        total = min(total, int(max_seconds * sample_rate))
    mix = np.zeros(max(total, 1), dtype=np.float32)

    specs = [("square", 0.125, 0.22), ("square", 0.25, 0.22), ("triangle", 0.0, 0.26)]
    for stream, (kind, duty, amp) in zip(freqs, specs):
        if not stream:
            continue
        # per-SAMPLE frequency + gate: hold each frame's value across its samples
        idx = np.minimum((np.arange(total) / frame_len).astype(np.int64),
                         len(stream) - 1)
        fps = np.asarray(stream, dtype=np.float64)[idx]          # freq per sample
        gate = (fps > 0).astype(np.float32)
        # soften note edges so gate changes don't click
        g = np.copy(gate)
        edge = max(1, int(0.003 * sample_rate))
        # phase integrates the instantaneous frequency -> vibrato/slides preserved
        phase = np.cumsum(fps / sample_rate)
        wave = (_wave(phase, "triangle") if kind == "triangle"
                else _pulse_wave(phase, duty))
        # a cheap 1-pole smoothing of the gate to taper attacks/releases
        for _ in range(edge // 4 + 1):
            g[1:] = 0.5 * g[1:] + 0.5 * g[:-1]
        mix += amp * (wave.astype(np.float32) * g)

    _render_noise(mix, noise_frames, frame_len, total, sample_rate)

    peak = float(np.max(np.abs(mix))) or 1.0
    if peak > 0.95:
        mix *= 0.95 / peak
    return mix, sample_rate


def _render_voice_bits(events: List[Tuple[int, int, int]], sr: int, step: float,
                       total: int) -> np.ndarray:
    """One voice's own 1-bit square (±0.6 while sounding, 0 when silent) — the scope's
    view of what that voice contributes to the PC-speaker mix."""
    out = np.zeros(total, dtype=np.float32)
    for start, dur, midi in events:
        pos = int(start * step * sr)
        ns = min(int(dur * step * sr), total - pos)
        if ns <= 0:
            continue
        phase = midi_to_freq(midi) * np.arange(pos, pos + ns, dtype=np.float64) / sr
        out[pos:pos + ns] = ((np.mod(phase, 1.0) < 0.5).astype(np.float32) * 2.0 - 1.0) * 0.6
    return out


def render_song(song: Song, sample_rate: int = 22050, step_seconds: float = 0.0625,
                amplitude: float = 0.35, waveform: str = "square"
                ) -> Tuple[np.ndarray, List[np.ndarray], int]:
    """Render (master, four voice channels, sample_rate) as float32 arrays in [-1, 1].

    The master is the playback mix (synth_song wraps it in PCM); the four channels are
    the same notes dealt onto MCS's voices (channel 0 = highest, like the tracker's v1)
    for the oscilloscope view. Tied same-pitch notes are merged before rendering."""
    per_track = [_note_events(tr.notes) for tr in song.tracks]
    chans = _allocate_voices(per_track)
    silent = np.zeros(1, dtype=np.float32)
    if not any(per_track):
        return silent, [silent] * 4, sample_rate
    if waveform == "pcspeaker":
        flat = [e for track in per_track for e in track]
        master = _render_pcspeaker(flat, sample_rate, step_seconds)
        voices = [_render_voice_bits(c, sample_rate, step_seconds, len(master))
                  for c in chans]
        return master, voices, sample_rate
    if waveform == "auto":
        # UNIVERSAL render: each track speaks its own waveform (NES duties, the
        # stepped triangle, LFSR noise...) at its notes' velocities — the voices
        # returned are PER TRACK, however many there are.
        bufs = []
        for tr in song.tracks:
            evs = _full_events(tr.notes)
            if not evs:
                bufs.append(silent)
            elif tr.kind == "noise":
                bufs.append(_render_noise_track(evs, sample_rate, step_seconds,
                                                amplitude * 0.8))
            else:
                bufs.append(_render_track(evs, sample_rate, step_seconds,
                                          amplitude, tr.waveform or waveform))
        length = max(len(b) for b in bufs)
        mix = np.zeros(length, dtype=np.float32)
        for b in bufs:
            mix[:len(b)] += b
        peak = float(np.max(np.abs(mix))) or 1.0
        gain = 0.9 / peak
        voices = [np.pad(b, (0, length - len(b))) * gain for b in bufs]
        return mix * gain, voices, sample_rate
    bufs = [_render_track(c, sample_rate, step_seconds, amplitude, waveform) if c else silent
            for c in chans]
    length = max(len(b) for b in bufs)
    mix = np.zeros(length, dtype=np.float32)
    for b in bufs:
        mix[:len(b)] += b
    peak = float(np.max(np.abs(mix))) or 1.0
    gain = 0.9 / peak
    voices = [np.pad(b, (0, length - len(b))) * gain for b in bufs]
    return mix * gain, voices, sample_rate


def pcm16(buf: np.ndarray) -> bytes:
    """Float [-1, 1] buffer -> mono 16-bit PCM bytes; b'' when silent (nothing to play)."""
    if not np.any(buf):
        return b""
    return (buf * 32767.0).astype("<i2").tobytes()


def synth_song(song: Song, sample_rate: int = 22050, step_seconds: float = 0.0625,
               amplitude: float = 0.35, waveform: str = "square") -> Tuple[bytes, int]:
    """Render every track (mixed) to mono 16-bit PCM. Returns (pcm_bytes, sample_rate)."""
    master, _, sr = render_song(song, sample_rate, step_seconds, amplitude, waveform)
    return pcm16(master), sr


def wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap mono 16-bit PCM in a WAV container (for playback or file save)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


_WINMM_STRUCTS = None


def _winmm_structs():
    """WAVEFORMATEX / WAVEHDR / MMTIME ctypes structures, built once on first use
    (kept out of module import so non-Windows hosts can still import this module)."""
    global _WINMM_STRUCTS
    if _WINMM_STRUCTS is None:
        import ctypes

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                        ("nSamplesPerSec", ctypes.c_uint), ("nAvgBytesPerSec", ctypes.c_uint),
                        ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                        ("cbSize", ctypes.c_ushort)]

        class WAVEHDR(ctypes.Structure):
            _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", ctypes.c_uint),
                        ("dwBytesRecorded", ctypes.c_uint), ("dwUser", ctypes.c_void_p),
                        ("dwFlags", ctypes.c_uint), ("dwLoops", ctypes.c_uint),
                        ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_void_p)]

        class MMTIME(ctypes.Structure):
            _fields_ = [("wType", ctypes.c_uint), ("u", ctypes.c_uint),
                        ("pad", ctypes.c_uint)]

        _WINMM_STRUCTS = (WAVEFORMATEX, WAVEHDR, MMTIME)
    return _WINMM_STRUCTS


class WaveOutPlayer:
    """PCM playback through winmm's waveOut API (ctypes, Windows).

    The old winsound player could only fire-and-forget a WAV file. waveOut gives the
    GUI a real transport: live volume (waveOutSetVolume), pause/resume, stop, and
    sample-accurate position (waveOutGetPosition) — the position is what drives the
    playhead and lets playback pick up exactly where it paused.
    """

    _WHDR_DONE = 0x01
    _TIME_MS, _TIME_SAMPLES, _TIME_BYTES = 0x1, 0x2, 0x4

    def __init__(self) -> None:
        self._h = None          # HWAVEOUT handle
        self._hdr = None        # WAVEHDR — must outlive the driver's use of it
        self._buf = None        # PCM buffer — ditto
        self._sr = 22050
        self.paused = False

    def play(self, pcm: bytes, sample_rate: int, volume: float | None = None) -> None:
        import ctypes

        self.stop()
        WAVEFORMATEX, WAVEHDR, _ = _winmm_structs()
        winmm = ctypes.windll.winmm
        fmt = WAVEFORMATEX(1, 1, sample_rate, sample_rate * 2, 2, 16, 0)  # PCM mono 16-bit
        h = ctypes.c_void_p()
        if winmm.waveOutOpen(ctypes.byref(h), -1, ctypes.byref(fmt), 0, 0, 0):  # WAVE_MAPPER
            raise RuntimeError("waveOutOpen failed — no audio output device?")
        self._h, self._sr = h, sample_rate
        if volume is not None:
            self.set_volume(volume)
        self._buf = ctypes.create_string_buffer(pcm, len(pcm))
        hdr = WAVEHDR()
        hdr.lpData = ctypes.cast(self._buf, ctypes.c_void_p)
        hdr.dwBufferLength = len(pcm)
        self._hdr = hdr
        winmm.waveOutPrepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        winmm.waveOutWrite(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        self.paused = False

    def pause(self) -> None:
        if self._h is not None and not self.paused:
            import ctypes
            ctypes.windll.winmm.waveOutPause(self._h)
            self.paused = True

    def resume(self) -> None:
        if self._h is not None and self.paused:
            import ctypes
            ctypes.windll.winmm.waveOutRestart(self._h)
            self.paused = False

    def set_volume(self, volume: float) -> None:
        """0.0..1.0, applied immediately (per-app wave volume, both channels)."""
        if self._h is None:
            return
        import ctypes
        word = max(0, min(0xFFFF, int(volume * 0xFFFF)))
        ctypes.windll.winmm.waveOutSetVolume(self._h, (word << 16) | word)

    def position_seconds(self) -> float:
        """Seconds of audio actually played (frozen while paused)."""
        if self._h is None:
            return 0.0
        import ctypes
        _, _, MMTIME = _winmm_structs()
        t = MMTIME()
        t.wType = self._TIME_SAMPLES
        ctypes.windll.winmm.waveOutGetPosition(self._h, ctypes.byref(t), ctypes.sizeof(t))
        if t.wType == self._TIME_SAMPLES:
            return t.u / self._sr
        if t.wType == self._TIME_BYTES:                 # driver fallback
            return t.u / (self._sr * 2)
        if t.wType == self._TIME_MS:
            return t.u / 1000.0
        return 0.0

    def is_done(self) -> bool:
        """True once the driver has finished the submitted buffer."""
        return self._hdr is not None and bool(self._hdr.dwFlags & self._WHDR_DONE)

    def stop(self) -> None:
        if self._h is None:
            return
        import ctypes
        winmm = ctypes.windll.winmm
        winmm.waveOutReset(self._h)                     # returns the buffer immediately
        if self._hdr is not None:
            winmm.waveOutUnprepareHeader(self._h, ctypes.byref(self._hdr),
                                         ctypes.sizeof(self._hdr))
        winmm.waveOutClose(self._h)
        self._h = self._hdr = self._buf = None
        self.paused = False
