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

from ..audio import pcm16, render_song, wav_bytes
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


def nearest_tempo_byte0(tick_seconds: float) -> int:
    """The MCS tempo byte whose tick length is closest to `tick_seconds`."""
    def err(b):
        step = (b - 0x77) // 3
        return abs((0.067 + 0.016 * step) - tick_seconds)
    return min(_MCS_TEMPOS, key=err)


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

        tk.Label(self, text="Scope", bg=_BG, fg=_ACCENT).grid(
            row=row, column=0, sticky="w", **pad)
        self.scope = tk.StringVar(value="text 5")
        ttk.Combobox(self, textvariable=self.scope, width=14, state="readonly",
                     values=_SCOPES).grid(row=row, column=1, sticky="w", **pad)
        self.dosviz = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="DOS preview window during playback",
                       variable=self.dosviz, bg=_BG, fg=_FG, selectcolor="#22252e",
                       activebackground=_BG, activeforeground=_FG).grid(
            row=row, column=2, sticky="w", **pad)
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

    # -- selections ---------------------------------------------------------
    def _spec(self):
        label = self.target.get()
        for t in TARGETS:
            if t[0] == label:
                return t
        return TARGETS[0]

    def _on_target(self) -> None:
        _, rt, _, _ = self._spec()
        allowed = TARGET_WAVEFORMS.get(rt, ("square",)) if rt else \
            ("square", "triangle", "sine")
        vals = ["native"] + [w for w in ("square", "triangle", "sine")
                             if w in allowed or rt is None]
        self.wave_box.configure(values=vals)
        if self.wave.get() not in vals:
            self.wave.set("native")

    def _byte0(self) -> int:
        return nearest_tempo_byte0(self.song.tempo_tick_seconds)

    def _reduced(self) -> Song:
        """The song as the selected target will hear it."""
        _, rt, _, _ = self._spec()
        if rt is None:                        # WAV: the universal song itself
            return self.song
        out = retrack(self.song, rt)
        wave = self.wave.get()
        if wave != "native":                  # user forced a waveform
            for t in out.tracks:
                if t.kind == "tone":
                    t.waveform = wave
        return out
    # -- preview ------------------------------------------------------------
    def preview(self) -> None:
        self.stop_preview()
        song = self._reduced()
        master, voices, sr = render_song(
            song, step_seconds=self.song.tempo_tick_seconds, waveform="auto")
        if not np.any(master):
            self.status.configure(text="Nothing to preview (no notes survive "
                                       "this target).")
            return
        self._preview_data = (master, voices, sr)
        self.app.player.play(pcm16(master), sr,
                             self.app.volume.get() / 100.0)
        if self.dosviz.get():
            names = [t.name[:4] for t in song.tracks][:4] or ["P1", "P2", "Tr", "Nz"]
            style = self._dos_style()
            if self._dos_win is None or not self._dos_win.alive():
                self._dos_win = viz.DosVizWindow(self, style, names)
            else:
                self._dos_win.style = style
            self._periods = viz.voice_periods(song)
        self._tick()
        self.status.configure(text=f"Previewing through "
                                   f"{self._spec()[0]}…")

    def _dos_style(self) -> str:
        s = self.scope.get()
        return {"text 5": viz.DOS_STYLES[0], "text 4": viz.DOS_STYLES[1],
                "VU meters": viz.DOS_STYLES[2], "text 3": viz.DOS_STYLES[3],
                "text 2": viz.DOS_STYLES[3], "text 1": viz.DOS_STYLES[4],
                }.get(s, viz.DOS_STYLES[0])

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
            self._dos_win.draw(levels, spec, getattr(self, "_periods", None))
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

    def _build(self, label: str, rt, com_mode) -> bytes:
        byte0 = self._byte0()
        if label.startswith(".MCS"):
            from ..mcs.encode import encode_song
            return encode_song(retrack(self.song, "mcs"), tempo_byte0=byte0,
                               cap=True)
        if label.startswith("WAV"):
            master, _, sr = render_song(self.song,
                                        step_seconds=self.song.tempo_tick_seconds,
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
                kwargs["sb_port"] = int(self.sb_port.get(), 0)
                kwargs["text_scope"] = 0          # SB build: audio only for now
                wave = self.wave.get()
                if wave != "native":
                    kwargs["sb_wave"] = wave
            else:
                wave = self.wave.get()
                if wave not in ("native", "square"):
                    kwargs["spk_wave"] = wave
        return build_com(self.song, com_mode, byte0, **kwargs)

    def _close(self) -> None:
        self.stop_preview()
        if self._dos_win is not None and self._dos_win.alive():
            self._dos_win.win.destroy()
        self.destroy()
