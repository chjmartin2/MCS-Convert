"""FlatSong -> DOS performance streams, PERF chunks, and full .COM builds.

The bridge between the RCT effects engine and the proven hand-assembled DOS
players. Every compiler here consumes the SAME flattened per-sub-tick arrays
the Windows preview renders, so slides/vibrato/arpeggios land on hardware at
full sub-tick fidelity -- an effect is just literal retunes in the stream.

Stream formats (docs/RCT-FORMAT.md, byte-compatible with the v1 engines):
  * 4voice:        [nchanges][voice|level<<4, inc_lo, inc_hi, viz]* per sub-tick
  * tandy/1voice:  [count][port, value]* per sub-tick (viz as pseudo-ports)

MODE_4TONE note: the DOS 4-voice and Tandy chips physically have 3 tone voices
+ 1 noise, so a 4th TONE channel is folded onto voice 2 by priority (it sounds
whenever channel 2 is silent). The MCS export and the Windows preview keep all
four true tones.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import dosplayer as D
from .audio import midi_to_freq
from .effects import FlatSong, flatten
from .mcs.reader import tick_seconds_for
from .rct import (PERF_1VOICE, PERF_4VOICE, PERF_TANDY, RctSong, make_perf)

_ARP_FLOOR = 110.0               # 1-voice arp: octave-up below this (audio.py)


def _fold_4tone(flat: FlatSong) -> List[Tuple[Optional[float], int, bool, str]]:
    """Channel-3 view for the 3-tone chips: the real noise channel in 3+noise
    mode, or None-silence in 4-tone mode (its notes fold onto voice 2)."""
    ch2, ch3 = flat.channels[2], flat.channels[3]
    fold = ch3.kind == "tone"
    out2, out3 = [], []
    for s in range(flat.total_subs):
        p2, p3 = ch2.pitch[s], ch3.pitch[s]
        if fold:
            if p2 is None and p3 is not None:        # ch3 borrows voice 2
                out2.append((p3, ch3.vol[s], ch3.onset[s], ch3.wave[s]))
            else:
                out2.append((p2, ch2.vol[s], ch2.onset[s], ch2.wave[s]))
            out3.append((None, 0, False, ""))
        else:
            out2.append((p2, ch2.vol[s], ch2.onset[s], ch2.wave[s]))
            out3.append((p3, ch3.vol[s], ch3.onset[s], ch3.wave[s]))
    return out2, out3


def _voice_rows(flat: FlatSong):
    """Per-voice per-sub-tick (pitch, vol, onset, wave) with 4-tone folding."""
    v2, v3 = _fold_4tone(flat)
    rows = []
    for s in range(flat.total_subs):
        row = []
        for c in (0, 1):
            ch = flat.channels[c]
            row.append((ch.pitch[s], ch.vol[s], ch.onset[s], ch.wave[s]))
        row.append(v2[s])
        row.append(v3[s])
        rows.append(row)
    return rows


# --- 4-voice PC speaker / SoundBlaster stream --------------------------------

def spk4_stream(flat: FlatSong, fs: float) -> Tuple[bytes, int]:
    """(stream, total_subs) in the 4-voice engine's record format. Emits a
    record only when a voice's (inc, level) changes or it re-attacks, so held
    notes cost nothing and slides are one small record per sub-tick."""
    rows = _voice_rows(flat)
    noise = flat.channels[3].kind == "noise"
    last: List[Tuple[int, int]] = [(0, 0)] * 4       # engines start silent
    out = bytearray()
    for s, row in enumerate(rows):
        recs = []
        for v in range(4):
            pitch, vol, onset, _wave = row[v]
            if pitch is None:
                inc = lvl = viz = 0
            elif v == 3 and noise:
                inc = D._spk4_noise_inc(pitch >= D._DRUM_BRIGHT_MIDI, fs)
                lvl = D._sb_level(round(vol * 127 / 15)) if vol else 0
                viz = D._NOISE_VIZ_P
            else:
                freq = midi_to_freq(pitch)
                inc = D._spk4_inc(freq, fs)
                lvl = D._sb_level(round(vol * 127 / 15)) if vol else 0
                viz = D._viz_period(freq)
            if (inc, lvl) != last[v] or onset:
                recs.append((v, inc, lvl, viz if inc else 0))
                last[v] = (inc, lvl)
        out.append(len(recs))
        for v, inc, lvl, viz in recs:
            out += bytes([(v & 0x0F) | ((lvl & 0x0F) << 4),
                          inc & 0xFF, (inc >> 8) & 0xFF, viz & 0xFF])
    out.append(4)                                    # trailing all-off sub-tick
    for v in range(4):
        out += bytes([v, 0, 0, 0])
    return bytes(out), flat.total_subs + 1


# --- 1-voice PC speaker stream -----------------------------------------------

def mono_stream(flat: FlatSong, arp: bool = True,
                viz: bool = True) -> Tuple[bytes, int]:
    """(stream, total_subs) of [count][port,val]* records for the beeper.
    With `arp` the one voice cycles every sounding tone (octave-up below
    ~110 Hz); otherwise it takes the highest. Volume is ignored (1 bit)."""
    from .audio import octave_up_to_floor
    rows = _voice_rows(flat)
    noise = flat.channels[3].kind == "noise"
    out = bytearray()
    idx = 0
    prev_freq: Optional[float] = None
    for row in rows:
        sounding = [p for v, (p, vol, _o, _w) in enumerate(row)
                    if p is not None and not (v == 3 and noise)]
        writes: List[Tuple[int, int]] = []
        if not sounding:
            if prev_freq is not None:
                writes += D._spk_note_off()
                if viz:
                    writes.append((D._VIZ_PORT, 0))
            prev_freq = None
        else:
            if arp:
                ups = sorted({octave_up_to_floor(round(p)) for p in sounding})
                midi = float(ups[idx % len(ups)])
                idx += 1
            else:
                midi = max(sounding)
            freq = midi_to_freq(midi)
            if prev_freq is None:
                writes += D._spk_note_on(freq)
                if viz:
                    writes.append((D._VIZ_PORT, D._viz_period(freq)))
            elif abs(freq - prev_freq) > 0.01:
                writes += D._spk_note_change(freq)
                if viz:
                    writes.append((D._VIZ_PORT, D._viz_period(freq)))
            prev_freq = freq
        out.append(len(writes))
        for port, val in writes:
            out += bytes([port & 0xFF, val & 0xFF])
    return bytes(out), flat.total_subs


# --- Tandy SN76489 stream ----------------------------------------------------

def tandy_stream(flat: FlatSong, viz: bool = True) -> Tuple[bytes, int]:
    """(stream, total_subs) of [count][port,val]* records for the SN76489:
    3 tone channels with 4-bit attenuation (real volume!) + the noise channel."""
    rows = _voice_rows(flat)
    noise = flat.channels[3].kind == "noise"
    last_div = [-1] * 3
    last_att = [-1] * 4
    noise_on = False
    out = bytearray()
    for row in rows:
        writes: List[Tuple[int, int]] = []
        for ch in range(3):
            pitch, vol, onset, _w = row[ch]
            if pitch is None:
                if last_att[ch] != 15:
                    writes.append((D._SN76489, 0x80 | (ch << 5) | 0x10 | 0x0F))
                    last_att[ch] = 15
                    if viz:
                        writes.append((D._VIZ_PORT | ch, 0))
                last_div[ch] = -1
                continue
            freq = midi_to_freq(pitch)
            n = D._sn_divider(freq)
            if n != last_div[ch]:
                writes.append((D._SN76489, 0x80 | (ch << 5) | (n & 0x0F)))
                writes.append((D._SN76489, (n >> 4) & 0x3F))
                last_div[ch] = n
                if viz:
                    writes.append((D._VIZ_PORT | ch, D._viz_period(freq)))
            att = 15 - vol                            # SN: 0 loud .. 15 silent
            if att != last_att[ch]:
                writes.append((D._SN76489, 0x80 | (ch << 5) | 0x10 | (att & 0x0F)))
                last_att[ch] = att
        pitch, vol, onset, _w = row[3]
        if noise:
            if pitch is not None and (onset or not noise_on):
                writes.extend(D._tandy_noise_on(pitch >= D._DRUM_BRIGHT_MIDI))
                noise_on = True
                if viz:
                    writes.append((D._VIZ_PORT | 3, D._NOISE_VIZ_P))
            elif pitch is None and noise_on:
                writes.extend(D._tandy_noise_off())
                noise_on = False
                if viz:
                    writes.append((D._VIZ_PORT | 3, 0))
        out.append(len(writes))
        for port, val in writes:
            out += bytes([port & 0xFF, val & 0xFF])
    return bytes(out), flat.total_subs


# --- PERF chunk compilation + .COM builds ------------------------------------

def _subtick_divider(tempo_byte0: int) -> int:
    return max(1, min(65535, round(D._PIT_HZ * tick_seconds_for(tempo_byte0) / 4)))


def perf_chunks(song: RctSong, mix_rate: Optional[int] = None) -> Dict[int, bytes]:
    """Compile all three PERF streams for an RctSong (written into the file on
    save, consumed by RCPLAY.COM). Raises ValueError past the DOS size cap."""
    flat = flatten(song)
    div4 = D._spk4_div_for(mix_rate)
    fs = D._PIT_HZ / div4
    subtick_s = tick_seconds_for(song.tempo_byte0) / 4.0
    samps = max(1, min(65535, round(fs * subtick_s)))
    sdiv = _subtick_divider(song.tempo_byte0)

    def _sil(rec):                                   # trailing silence sub-tick,
        return bytes([len(rec)]) + b"".join(          # so auto-repeat rewinds from
            bytes([p, v]) for p, v in rec)            # a quiet state (like build_com)

    s4, t4 = spk4_stream(flat, fs)                    # (has its own all-off tail)
    s1, t1 = mono_stream(flat, arp=True)
    st, tt = tandy_stream(flat)
    return {
        PERF_4VOICE: make_perf(PERF_4VOICE, div4, samps, t4, s4),
        PERF_1VOICE: make_perf(PERF_1VOICE, sdiv, 1, t1 + 1,
                               s1 + _sil(D._spk_note_off())),
        PERF_TANDY: make_perf(PERF_TANDY, sdiv, 1, tt + 1,
                              st + _sil(D._tandy_silence())),
    }


def build_com(song: RctSong, mode: str, mix_rate: Optional[int] = None,
              text_scope: int = 0, scope: bool = False,
              foreground: bool = False, sb: bool = False,
              sb_port: int = 0x220, fps=None) -> bytes:
    """RctSong -> standalone .COM via the proven engines, at full effect
    fidelity (streams compiled straight from the flattened arrays)."""
    flat = flatten(song)
    tempo = song.tempo_byte0
    subtick_s = tick_seconds_for(tempo) / 4.0
    vis = D._vis_for(scope, text_scope)
    skip = D._draw_skip_for(vis, fps)

    if mode == "4voice":
        if foreground:
            fs_fg = float(mix_rate) if mix_rate else D._FG_FS
            stream, total = spk4_stream(flat, fs_fg)
            return D._assemble_spk4_fg(_subtick_divider(tempo), total, stream,
                                       fs_fg)
        div = D._spk4_div_for(mix_rate)
        fs = D._PIT_HZ / div
        stream, total = spk4_stream(flat, fs)
        samps = max(1, min(65535, round(fs * subtick_s)))
        wave_table = b""
        scope_wave = "square"
        if sb:
            waves = [w for ch in flat.channels for w in ch.wave
                     if w and ch.kind == "tone"]
            scope_wave = max(set(waves), key=waves.count) if waves else "square"
            wave_table = D._sb_wave_bank(scope_wave)
        com = D._assemble_spk4(div, samps, total, stream, vis, skip, b"",
                               False, sb, sb_port, wave_table, scope_wave, False)
    elif mode in ("tandy", "1voice"):
        stream, total = (tandy_stream(flat) if mode == "tandy"
                         else mono_stream(flat, arp=True))
        sil = D._tandy_silence() if mode == "tandy" else D._spk_note_off()
        sil_bytes = bytes([len(sil)]) + b"".join(bytes([p, v]) for p, v in sil)
        stream += sil_bytes
        total += 1
        com = D._assemble(_subtick_divider(tempo), 1, total, sil_bytes, stream,
                          vis, skip)
    else:
        raise ValueError(f"unknown .COM mode {mode!r}")
    if len(com) > 0xFF00:
        raise ValueError(f".COM is {len(com)} bytes — too big for one segment")
    return com
