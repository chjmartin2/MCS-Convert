"""The Export dialog — every output the project can produce, in one place.

Export lives AFTER the play/tracker stage now: import brings a UNIVERSAL Song
into the tracker (everything the source expressed), and this dialog reduces it
to a chosen target at export time. It offers:

  * a play preview rendered through the target's constraints (what you'll hear),
  * a DOS-visualization replica window matching the .COM scope selection
    (what you'll see),
  * "Retrack" — load the reduced song back into the tracker (what you'll get),
  * and the file export itself (.MCS, .COM variants, .WAV).
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

from ..audio import pcm16, render_song, tempo_bpm, wav_bytes
from ..mcs.reader import tick_seconds_for
from ..model import Song
from ..retrack import TARGET_WAVEFORMS, retrack
from . import viz

_BG = "#14161c"
_FG = "#e8e8e8"
_ACCENT = "#7fd17f"

#: (label, retrack target, build_com mode or None, extension)
TARGETS = (
    (".MCS song (Music Construction Set)", "mcs", None, ".mcs"),
    ("Tandy 1000 / PCjr .COM", "tandy", "tandy", ".com"),
    ("PC Speaker 1-voice .COM", "1voice", "1voice", ".com"),
    ("PC Speaker 4-voice .COM", "4voice", "4voice", ".com"),
    ("PC Speaker 4-voice (MCS drive) .COM", "4voice", "4voice", ".com"),
    ("SoundBlaster .COM", "sb", "4voice", ".com"),
    ("WAV (universal render)", None, None, ".wav"),
)

_SCOPES = ("none", "graphics (Tandy)", "VGA 256", "text 1", "text 2", "text 3",
           "text 4", "text 5", "VU meters", "static screen")
_SCOPE_TS = {"none": 0, "text 1": 1, "text 2": 2, "text 3": 3, "text 4": 4,
             "text 5": 5, "VU meters": 6, "static screen": 7, "VGA 256": 8}

_MCS_TEMPOS = tuple(0x77 + 3 * s for s in range(10))

#: Drums dropdown -> (retrack percussion mode, click palette)
_DRUM_MODES = {
    "auto two-tone": ("clicks", "auto"),
    "wood block": ("clicks", "block"),
    "low bass": ("clicks", "low bass"),
    "hi-hat": ("clicks", "hi-hat"),
    "pitched (as written)": ("pitched", "auto"),
    "drop": ("drop", "auto"),
}


def nearest_tempo_byte0(tick_seconds: float) -> int:
    """The MCS tempo byte whose tick length is closest to `tick_seconds`."""
    return min(_MCS_TEMPOS,
               key=lambda b: abs(tick_seconds_for(b) - tick_seconds))


class ExportDialog(tk.Toplevel):
    """Pick a target, preview it (audio + DOS viz replica), retrack the tracker
    to it, or export the file."""

    def __init__(self, app) -> None:
        super().__init__(app.root)
        self.app = app
        self.song: Song = app.song
        self.title("MCS-Convert — Export")
        self.configure(bg=_BG)
        self.resizable(False, False)
        self._dos_win = None
        self._preview_data = None            # (master, voices, sr)
        self._follow_id = None

        pad = dict(padx=8, pady=3)
        row = 0
        tk.Label(self, text="Target", bg=_BG, fg=_ACCENT).grid(
            row=row, column=0, sticky="w", **pad)
        self.target = tk.StringVar(value=TARGETS[0][0])
        tgt = ttk.Combobox(self, textvariable=self.target, state="readonly",
                           width=38, values=[t[0] for t in TARGETS])
        tgt.grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        tgt.bind("<<ComboboxSelected>>", lambda e: self._on_target())
        row += 1

        tk.Label(self, text="Waveform", bg=_BG, fg=_ACCENT).grid(
            row=row, column=0, sticky="w", **pad)
        self.wave = tk.StringVar(value="native")
        self.wave_box = ttk.Combobox(self, textvariable=self.wave, width=12,
                                     state="readonly",
                                     values=("native", "square", "triangle", "sine"))
        self.wave_box.grid(row=row, column=1, sticky="w", **pad)
        tk.Label(self, text="(SB keeps NES duties as 'native'; speaker targets "
                            "need high mix rates for non-square)",
                 bg=_BG, fg="#888888", font=("TkDefaultFont", 8)).grid(
            row=row, column=2, sticky="w", **pad)
        row += 1

        tk.Label(self, text="Mix rate Hz", bg=_BG, fg=_ACCENT).grid(
            row=row, column=0, sticky="w", **pad)
        self.mix = tk.StringVar(value="16000")
        ttk.Combobox(self, textvariable=self.mix, width=12, state="normal",
                     values=("4000", "6000", "12000", "16000", "24000", "48000")
                     ).grid(row=row, column=1, sticky="w", **pad)
        tk.Label(self, text="SB port", bg=_BG, fg=_ACCENT).grid(
            row=row, column=2, sticky="w", **pad)
        self.sb_port = tk.StringVar(value="0x220")
        tk.Entry(self, textvariable=self.sb_port, width=8, bg="#22252e", fg=_FG,
                 insertbackground=_FG).grid(row=row, column=2, sticky="e", **pad)
        row += 1

        tk.Label(self, text="Visualization", bg=_BG, fg=_ACCENT).grid(
            row=row, column=0, sticky="w", **pad)
        self.scope = tk.StringVar(value="text 5")
        self.scope_box = ttk.Combobox(self, textvariable=self.scope, width=14,
                                      state="readonly", values=_SCOPES)
        self.scope_box.grid(row=row, column=1, sticky="w", **pad)
        self.dosviz = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="DOS preview window during playback",
                       variable=self.dosviz, bg=_BG, fg=_FG, selectcolor="#22252e",
                       activebackground=_BG, activeforeground=_FG).grid(
            row=row, column=2, sticky="w", **pad)
        row += 1

        # -- Drums: an OUTPUT decision (the universal import only captures and
        # marks them). Clicks voice them on the target's percussion path with
        # the chosen palette; "pitched" plays their written pitches as tones;
        # "drop" silences them.
        tk.Label(self, text="Drums", bg=_BG, fg=_ACCENT).grid(
            row=row, column=0, sticky="w", **pad)
        self.drum = tk.StringVar(value="auto two-tone")
        drum_box = ttk.Combobox(self, textvariable=self.drum, width=18,
                                state="readonly", values=list(_DRUM_MODES))
        drum_box.grid(row=row, column=1, sticky="w", **pad)
        drum_box.bind("<<ComboboxSelected>>", lambda e: self._update_size())
        self.drop_noise = tk.BooleanVar(value=False)
        noise_chk = tk.Checkbutton(self, text="remove noise channel",
                                   variable=self.drop_noise,
                                   command=self._update_size, bg=_BG, fg=_FG,
                                   selectcolor="#22252e", activebackground=_BG,
                                   activeforeground=_FG)
        noise_chk.grid(row=row, column=2, sticky="w", **pad)
        row += 1

        # .MCS destination player: how many voices the file is arranged for.
        tk.Label(self, text="MCS voices", bg=_BG, fg=_ACCENT).grid(
            row=row, column=0, sticky="w", **pad)
        self._mcs_voices = {"4 voices (PC Speaker)": 4,
                            "3 voices (Tandy / PCjr)": 3,
                            "single voice": 1}
        self.mcs_voices = tk.StringVar(value="4 voices (PC Speaker)")
        self.mcs_voices_box = ttk.Combobox(self, textvariable=self.mcs_voices,
                                           width=22, state="readonly",
                                           values=list(self._mcs_voices))
        self.mcs_voices_box.grid(row=row, column=1, sticky="w", **pad)
        self.mcs_voices_box.bind("<<ComboboxSelected>>",
                                 lambda e: self._update_size())
        tk.Label(self, text="(.MCS target only: the player the file is "
                            "arranged for)",
                 bg=_BG, fg="#888888", font=("TkDefaultFont", 8)).grid(
            row=row, column=2, sticky="w", **pad)
        row += 1

        # -- BPM quantization: a TARGET concern, so it lives here (moved out of
        # the import dialog). The tempo picker re-stamps playback speed; the
        # Optimize buttons RE-QUANTIZE from the kept source emulation/module.
        # The meter + size readout apply to the .MCS target's notation grid.
        tk.Label(self, text="Tempo", bg=_BG, fg=_ACCENT).grid(
            row=row, column=0, sticky="w", **pad)
        auto = nearest_tempo_byte0(self.song.tempo_tick_seconds)
        self._tempos = list(_MCS_TEMPOS)
        self._tempo_labels = [
            f"≈{round(tempo_bpm(tick_seconds_for(b)))} BPM"
            + (" (imported)" if b == auto else "") for b in self._tempos]
        self.tempo = tk.StringVar(
            value=self._tempo_labels[self._tempos.index(auto)])
        tempo_box = ttk.Combobox(self, textvariable=self.tempo, width=18,
                                 state="readonly", values=self._tempo_labels)
        tempo_box.grid(row=row, column=1, sticky="w", **pad)
        tempo_box.bind("<<ComboboxSelected>>", lambda e: self._update_size())
        tk.Label(self, text="Meter", bg=_BG, fg=_ACCENT).grid(
            row=row, column=2, sticky="w", **pad)
        self._meters = {"Auto": None, "2/4": 16, "3/4": 24, "4/4": 32, "6/8": 48}
        self.meter = tk.StringVar(value="Auto")
        meter_box = ttk.Combobox(self, textvariable=self.meter, width=5,
                                 state="readonly", values=list(self._meters))
        meter_box.grid(row=row, column=2, sticky="e", **pad)
        meter_box.bind("<<ComboboxSelected>>", lambda e: self._update_size())
        row += 1

        can_requant = (hasattr(self.song, "nsf_frames")
                       or hasattr(self.song, "pt3_source"))
        optbar = tk.Frame(self, bg=_BG)
        optbar.grid(row=row, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        state = "normal" if can_requant else "disabled"
        tk.Button(optbar, text="⌖ Exhaustive Optimize", state=state,
                  command=self._optimize).pack(side="left")
        tk.Button(optbar, text="⌖ Optimize at this Tempo", state=state,
                  command=self._optimize_current).pack(side="left", padx=(4, 0))
        self.size_label = tk.Label(optbar, bg=_BG, fg=_FG,
                                   font=("TkDefaultFont", 8))
        self.size_label.pack(side="left", padx=(12, 0))
        row += 1

        btns = tk.Frame(self, bg=_BG)
        btns.grid(row=row, column=0, columnspan=3, sticky="we", padx=8, pady=8)
        tk.Button(btns, text="▶ Preview", command=self.preview).pack(side="left")
        tk.Button(btns, text="■ Stop", command=self.stop_preview).pack(
            side="left", padx=(4, 12))
        tk.Button(btns, text="⟳ Retrack into tracker",
                  command=self.retrack_into_tracker).pack(side="left")
        tk.Button(btns, text="⬇ Export…", command=self.export).pack(
            side="left", padx=(12, 0))
        tk.Button(btns, text="Close", command=self._close).pack(side="right")

        self.status = tk.Label(self, text="Preview plays the song through the "
                                          "target's constraints.",
                               bg="#0c0d11", fg=_ACCENT, anchor="w")
        self.status.grid(row=row + 1, column=0, columnspan=3, sticky="we")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._on_target()
        self._update_size()

    # -- selections ---------------------------------------------------------
    def _spec(self):
        label = self.target.get()
        for t in TARGETS:
            if t[0] == label:
                return t
        return TARGETS[0]

    def _on_target(self) -> None:
        label, rt, _, _ = self._spec()
        allowed = TARGET_WAVEFORMS.get(rt, ("square",)) if rt else \
            ("square", "triangle", "sine")
        vals = ["native"] + [w for w in ("square", "triangle", "sine")
                             if w in allowed or rt is None]
        self.wave_box.configure(values=vals)
        if self.wave.get() not in vals:
            self.wave.set("native")
        self.mcs_voices_box.configure(
            state="readonly" if rt == "mcs" else "disabled")
        # the Visualization picker builds a .COM's on-screen display: a WAV has
        # no screen, and the .MCS target always previews as music notation
        com_display = not (label.startswith("WAV") or rt == "mcs")
        self.scope_box.configure(state="readonly" if com_display else "disabled")

    def _byte0(self) -> int:
        """The MCS tempo byte from the Tempo picker (pure playback speed —
        re-quantizing the grid is the Optimize buttons' job)."""
        try:
            return self._tempos[self._tempo_labels.index(self.tempo.get())]
        except (AttributeError, ValueError):
            return nearest_tempo_byte0(self.song.tempo_tick_seconds)

    def _step_seconds(self) -> float:
        return tick_seconds_for(self._byte0())

    # -- BPM quantization (moved here from the import dialog) ----------------
    def _update_size(self) -> None:
        """The .MCS estimate at the chosen tempo/meter: byte size, dropped
        onsets, densest staff-measure vs the 32-event engine ceiling."""
        from ..mcs.encode import encode_song
        from ..mcs.reader import parse_records, split_staves, symbol, _note_value
        try:
            data = self._build_mcs()
            dropped = encode_song.last_dropped
            staves = split_staves(parse_records(data))
            busiest = max((sum(1 for b0, b1 in r.entries
                               if _note_value(symbol(b0))[0])
                           for st in staves for r in st[1:]), default=0)
            drops = f" · ⚠ {dropped} dropped" if dropped else " · all notes fit"
            self.size_label.configure(
                text=f".MCS {len(data):,} B · busiest {busiest}/32{drops}",
                fg=("#e0a030" if busiest >= 32 or dropped else _ACCENT))
        except Exception:  # noqa: BLE001 — e.g. empty song
            self.size_label.configure(text="", fg=_FG)

    def _requantized(self, exhaustive: bool):
        """Re-quantize the kept source (NSF frame log / PT3 module) onto a new
        grid; returns (song, byte0, off, speed) or None if there's no source."""
        if hasattr(self.song, "nsf_frames"):
            from ..nsf.extract import optimize_song, optimize_song_at
            return (optimize_song(self.song) if exhaustive
                    else optimize_song_at(self.song, self._byte0()))
        if hasattr(self.song, "pt3_source"):
            from ..pt3 import optimize_pt3, optimize_pt3_at
            return (optimize_pt3(self.song) if exhaustive
                    else optimize_pt3_at(self.song, self._byte0()))
        return None

    def _apply_requantized(self, new: Song, byte0: int, off: float,
                           speed: float) -> None:
        """Adopt a re-quantized song: re-apply the import-dialog selection and
        octave shifts (the rebuild resurrects every channel), stamp the tempo,
        reload the tracker, and sync the dialog."""
        adjust = getattr(self.song, "import_adjust", None)
        if adjust and max(adjust[0], default=-1) < len(new.tracks):
            indices, shifts = adjust
            kept = []
            for i in indices:
                tr = new.tracks[i]
                shift = shifts[i] if i < len(shifts) else 0
                if shift and tr.kind == "tone":
                    for n in tr.notes:
                        if not (n.is_rest or n.percussive):
                            n.midi_note += shift
                kept.append(tr)
            new.tracks = kept
            new.import_adjust = (list(range(len(kept))), [0] * len(kept))
        new.tempo_tick_seconds = tick_seconds_for(byte0)
        self.song = new
        self.app.load_song(new, label=self.app.root.title() or "requantized")
        if byte0 in self._tempos:
            auto_label = self._tempo_labels[self._tempos.index(byte0)]
            self.tempo.set(auto_label)
        ref = ("from source speed" if hasattr(new, "nsf_frames")
               else "grid error")
        self.status.configure(text=f"Re-quantized: {off:.2f} avg off-beat · "
                                   f"{speed * 100:.0f}% {ref}.")
        self._update_size()

    def _optimize(self) -> None:
        """Exhaustive Optimize: search every MCS tempo x subdivision for the
        tightest beat alignment, then adopt that grid."""
        self.stop_preview()
        result = self._requantized(exhaustive=True)
        if result:
            self._apply_requantized(*result)

    def _optimize_current(self) -> None:
        """Re-quantize AT the picked tempo: the base note takes as many ticks
        as fit it (faster = finer grid), with a minimal speed nudge."""
        self.stop_preview()
        result = self._requantized(exhaustive=False)
        if result:
            self._apply_requantized(*result)

    def _voice_count(self) -> int:
        return self._mcs_voices.get(self.mcs_voices.get(), 4)

    def _drum_opts(self):
        """(percussion mode, drum_sound) from the Drums dropdown."""
        return _DRUM_MODES.get(self.drum.get(), ("clicks", "auto"))

    def _reduced(self) -> Song:
        """The song as the selected target will hear it (drums included)."""
        _, rt, _, _ = self._spec()
        if rt is None:                        # WAV: the universal song itself
            return self.song
        percussion, drum_sound = self._drum_opts()
        out = retrack(self.song, rt, drum_sound=drum_sound,
                      percussion=percussion,
                      voices=(self._voice_count() if rt == "mcs" else None),
                      drop_noise=self.drop_noise.get())
        wave = self.wave.get()
        if wave != "native":                  # user forced a waveform
            for t in out.tracks:
                if t.kind == "tone":
                    t.waveform = wave
        return out
    # -- preview ------------------------------------------------------------
    def preview(self) -> None:
        self.stop_preview()
        label, rt, _, _ = self._spec()
        is_mcs = label.startswith(".MCS")
        staves = None
        if is_mcs:
            # the TRUEST preview: encode the actual .MCS bytes and play the
            # round-trip — the notation view draws from the same records.
            from ..mcs import reader as R
            try:
                data = self._build_mcs()
                song = R.parse_bytes(data)
                staves = R.split_staves(R.parse_records(data))
            except Exception:                         # fall back to the reduction
                song = self._reduced()
        else:
            song = self._reduced()
        master, voices, sr = render_song(
            song, step_seconds=self._step_seconds(), waveform="auto")
        if not np.any(master):
            self.status.configure(text="Nothing to preview (no notes survive "
                                       "this target).")
            return
        self._preview_data = (master, voices, sr)
        self.app.player.play(pcm16(master), sr,
                             self.app.volume.get() / 100.0)
        style = self._dos_style()
        if self.dosviz.get() and style is not None:
            names = [t.name[:4] for t in song.tracks][:4] or ["P1", "P2", "Tr", "Nz"]
            if self._dos_win is None or not self._dos_win.alive():
                # closing the preview window stops the audio with it
                self._dos_win = viz.DosVizWindow(self, style, names,
                                                 on_close=self.stop_preview)
            else:
                self._dos_win.set_style(style)        # live restyle + retitle
            self._dos_win.set_wave(self._preview_wave())
            if style == "static poster":
                self._dos_win.set_song(song, self._step_seconds())
            elif style == "MCS notation" and staves is not None:
                from ..tracker import _measure_ticks
                self._dos_win.set_notation(
                    staves, _measure_ticks(song.time_signature),
                    self._step_seconds())
            self._periods = viz.voice_periods(song)
        elif self._dos_win is not None and self._dos_win.alive():
            self._dos_win.win.destroy()               # selection says: no window
            self._dos_win = None
        self._tick()
        self.status.configure(text=f"Previewing through {label}…")

    def _preview_wave(self) -> str:
        """The waveform the selected target will actually sound — what the
        replica's traces should draw. Only a real DAC (or the speaker's
        high-rate PWM modelling) sounds anything but squares."""
        label, rt, com_mode, _ = self._spec()
        wave = self.wave.get()
        if rt == "sb":
            if wave != "native":
                return wave
            from ..dosplayer import _native_waveform
            return _native_waveform(self.song)
        if rt == "4voice" and wave not in ("native", "square"):
            return wave                              # PWM-modelled on the speaker
        return "square"                              # MCS / Tandy / PIT hardware

    def _dos_style(self):
        """The visual for the current selections, or None for no window at all:
        WAV has no display of any kind; the .MCS target always shows its music
        notation; 'none' shows nothing; every .COM visualization maps to its own
        distinct replica."""
        label, rt, _com, _ext = self._spec()
        if label.startswith("WAV"):
            return None                      # an audio file has nothing to draw
        if rt == "mcs":
            return "MCS notation"
        return {"none": None,
                "graphics (Tandy)": "Tandy graphics",
                "VGA 256": "VGA 256",
                "text 1": "block scopes (text 1)",
                "text 2": "line trace (text 2)",
                "text 3": "line scopes (text 3)",
                "text 4": "spectrum analyzer (text 4)",
                "text 5": "combined monitor (text 5)",
                "VU meters": "VU meters",
                "static screen": "static poster"}.get(self.scope.get())

    def _tick(self) -> None:
        if self._preview_data is None:
            return
        master, voices, sr = self._preview_data
        pos = self.app.player.position_seconds()
        if self.app.player.is_done():
            self.stop_preview()
            return
        if self._dos_win is not None and self._dos_win.alive():
            idx = max(0, int(pos * sr))
            span = int(0.03 * sr)
            levels = viz._rms_levels(voices, idx, span)
            spec = viz._spectrum(master, idx, sr, 18)
            self._dos_win.draw(levels, spec, getattr(self, "_periods", None),
                               elapsed=pos)
        self._follow_id = self.after(33, self._tick)

    def stop_preview(self) -> None:
        if self._follow_id is not None:
            self.after_cancel(self._follow_id)
            self._follow_id = None
        self.app.player.stop()
        self._preview_data = None

    # -- retrack ------------------------------------------------------------
    def retrack_into_tracker(self) -> None:
        """Reduce to the target and RELOAD the tracker with the result — the
        tracker then shows exactly what the export will contain."""
        _, rt, _, _ = self._spec()
        if rt is None:
            self.status.configure(text="WAV has no constraints — nothing to retrack.")
            return
        reduced = self._reduced()
        reduced.tempo_tick_seconds = self._step_seconds()
        self.app.load_song(reduced, label=f"retrack:{rt}")
        self.status.configure(text=f"Tracker reloaded with the {rt} reduction "
                                   f"({sum(len(t.notes) for t in reduced.tracks)} notes).")

    # -- export -------------------------------------------------------------
    def export(self) -> None:
        label, rt, com_mode, ext = self._spec()
        base = os.path.splitext(os.path.basename(
            getattr(self.app, "path", None) or self.song.title or "song"))[0]
        out = filedialog.asksaveasfilename(
            title=f"Export {label}", defaultextension=ext,
            initialfile=(base[:8] + ext.upper()) if ext != ".wav" else base + ext,
            filetypes=[(label, "*" + ext)], parent=self)
        if not out:
            return
        try:
            data = self._build(label, rt, com_mode)
        except Exception as exc:  # noqa: BLE001 — surface build errors
            messagebox.showerror("Export failed", str(exc), parent=self)
            return
        with open(out, "wb") as fh:
            fh.write(data)
        self.status.configure(text=f"Exported {len(data)} bytes → "
                                   f"{os.path.basename(out)}")

    def _build_mcs(self) -> bytes:
        from ..mcs.encode import encode_song
        bar_ticks = self._meters[self.meter.get()]
        percussion, drum_sound = self._drum_opts()
        nv = self._voice_count()
        return encode_song(retrack(self.song, "mcs", drum_sound=drum_sound,
                                   percussion=percussion, voices=nv,
                                   drop_noise=self.drop_noise.get()),
                           tempo_byte0=self._byte0(), cap=True,
                           fit_meter=bar_ticks is None,
                           bar_ticks=bar_ticks or 32, balance=True, voices=nv)

    def _build(self, label: str, rt, com_mode) -> bytes:
        byte0 = self._byte0()
        if label.startswith(".MCS"):
            return self._build_mcs()
        if label.startswith("WAV"):
            master, _, sr = render_song(self.song,
                                        step_seconds=self._step_seconds(),
                                        waveform="auto")
            return wav_bytes(pcm16(master), sr)
        from ..dosplayer import build_com
        scope = self.scope.get()
        kwargs = dict(
            scope=(scope == "graphics (Tandy)" and com_mode == "tandy"),
            text_scope=_SCOPE_TS.get(scope, 0),
            draw_skip=1)
        if com_mode == "4voice":
            kwargs["mix_rate"] = int("".join(c for c in self.mix.get()
                                             if c.isdigit()) or "16000")
            kwargs["mcs"] = "MCS drive" in label
            kwargs["sb"] = label.startswith("SoundBlaster")
            if kwargs["sb"]:
                # the scope engine is audio-agnostic (it reads viz[]/strike[]),
                # so SoundBlaster builds get the full visualization family too
                kwargs["sb_port"] = int(self.sb_port.get(), 0)
                wave = self.wave.get()
                if wave != "native":
                    kwargs["sb_wave"] = wave
            else:
                wave = self.wave.get()
                if wave not in ("native", "square"):
                    kwargs["spk_wave"] = wave
        # the .COM plays the RETRACKED song, so the Drums choice (clicks with
        # its palette / pitched / drop) is honoured in the standalone player too
        return build_com(self._reduced(), com_mode, byte0, **kwargs)

    def _close(self) -> None:
        self.stop_preview()
        if self._dos_win is not None and self._dos_win.alive():
            self._dos_win.win.destroy()
        self.destroy()
