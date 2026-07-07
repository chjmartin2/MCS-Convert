"""MCS/MCD viewer + player — a tracker-style read-only window.

Open a Music Construction Set song and see its notes laid out per staff (tracker columns),
then play it back through the built-in synth. This is a validation harness for the format
reverse-engineering: when a song plays back recognizably, we've decoded it correctly.

Run:  python -m mcs_convert.gui.player [SONG.MCS]      (or:  mcs-convert play SONG.MCS)

The decoder matches MCSDISK.EXE's own playback engine (recovered by disassembly):
6-bit vertical positions into fixed per-clef pitch windows, key signatures and
accidentals, dots, chords, rests, and measure-aligned staves. See docs/mcs-format.md.
"""

from __future__ import annotations

# Allow "Run" in an IDE (which executes this file as a plain script, so relative imports
# would fail). Re-launch ourselves as a proper package module, then stop.
if __package__ in (None, ""):
    import os as _os
    import sys as _sys
    import runpy as _runpy

    _root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    _runpy.run_module("mcs_convert.gui.player", run_name="__main__", alter_sys=True)
    raise SystemExit(0)

import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np

from ..audio import WaveOutPlayer, pcm16, render_song, tempo_bpm, wav_bytes
from ..mcs.reader import parse, tick_seconds_for
from ..model import NoteEvent, Song, Track
from ..pitch import midi_to_name
from ..tracker import tracker_rows, tracker_text

_BG = "#12141a"
_FG = "#d7dae0"
_ACCENT = "#7fd1b9"
_SCOPE_LINE = "#00ff41"      # phosphor green
_SCOPE_DIM = "#0c3d1e"       # midline / frame green


def channel_stats(track: Track) -> dict:
    """Percussion-likelihood stats for one imported channel. Three signals:
    noise commands per note (AY drums live on the noise generator), pitch
    repetitiveness (drums hammer 1-3 'pitches' forever), and shortness (drums
    are wall-to-wall short hits). The score is advisory — ears decide."""
    notes = [n for n in track.notes if not n.is_rest]
    if not notes:
        return {"count": 0, "range": "—", "noise": 0.0, "repet": 0.0,
                "score": 1.0, "verdict": "empty"}
    midis = [n.midi_note for n in notes]
    drum_ratio = track.meta.get("drum_notes", 0) / len(notes)
    noise = min(1.0, max(track.meta.get("noise_cmds", 0) / len(notes), drum_ratio))
    # consecutive same-pitch hits: a kick/snare hammers one "pitch", a melody
    # moves (global pitch reuse saturates on any long tonal piece, so it's local)
    repet = (sum(1 for a, b in zip(midis, midis[1:]) if a == b) /
             max(1, len(midis) - 1))
    short = sum(1 for n in notes if n.duration_ticks <= 2) / len(notes)
    score = 0.5 * noise + 0.3 * repet + 0.2 * short
    if drum_ratio > 0.5 or score > 0.55:
        verdict = "percussion"
    elif score > 0.40:
        verdict = "rhythm?"
    else:
        verdict = "bass" if sum(midis) / len(midis) < 55 else "melody"
    return {"count": len(notes),
            "range": f"{midi_to_name(min(midis))}..{midi_to_name(max(midis))}",
            "noise": noise, "repet": repet, "score": score, "verdict": verdict}


def _dos_name(name: str) -> str:
    """A DOS 8.3-compliant basename: uppercase, alphanumerics and underscore
    only (spaces dropped, other punctuation folded to _), max 8 chars — so the
    file is loadable if it ends up on a real MCS disk."""
    cleaned = "".join(c if c.isalnum() else ("" if c in " ." else "_")
                      for c in name.upper())
    return (cleaned.strip("_") or "IMPORTED")[:8]


class PlayerApp:
    def __init__(self, root: tk.Tk, initial: str | None = None) -> None:
        self.root = root
        self.player = WaveOutPlayer()
        self.song = None
        self._rows = []             # cached tracker_rows for the loaded song
        self._children = ()         # cached tree row ids, one per 32nd-tick row
        self._follow_id = None      # pending root.after id for the playhead loop
        self._start_row = 0         # row playback began from (playhead = start + position)
        self._last_row = None       # row under the playhead — Play starts from here
        self._step = 0.1            # seconds per row (the song's tick length)
        self._scope_win = None      # oscilloscope Toplevel (None until opened)
        self._scope_panels = []     # (canvas, trace_item) — v1..v4 then master
        self._scope_data = None     # (master, voices, sr) buffers from the last render
        root.title("MCS-Convert — Player")
        root.configure(bg=_BG)
        root.geometry("560x680")

        self._build_toolbar()
        self._build_meta()
        self._build_tracker()
        self._build_statusbar()

        if initial:
            self.load(initial)

    # ---- layout ----------------------------------------------------------
    def _build_toolbar(self) -> None:
        # Row 1: file + transport. Row 2: voice + a volume slider big enough to see.
        row1 = tk.Frame(self.root, bg=_BG)
        row1.pack(fill="x", padx=8, pady=(6, 2))
        tk.Button(row1, text="Open…", command=self.open_dialog).pack(side="left")
        tk.Button(row1, text="⬆ Import…", command=self.import_dialog).pack(
            side="left", padx=(4, 0))
        self.play_btn = tk.Button(row1, text="▶ Play", command=self.play, state="disabled")
        self.play_btn.pack(side="left", padx=(8, 0))
        self.pause_btn = tk.Button(row1, text="⏸ Pause", width=9,
                                   command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=(4, 0))
        self.stop_btn = tk.Button(row1, text="■ Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(4, 0))
        self.export_btn = tk.Button(row1, text="⬇ WAV…", command=self.export_wav,
                                    state="disabled")
        self.export_btn.pack(side="left", padx=(12, 0))
        self.track_btn = tk.Button(row1, text="⬇ Tracker…", command=self.export_tracker,
                                   state="disabled")
        self.track_btn.pack(side="left", padx=(4, 0))
        tk.Button(row1, text="〰 Scope", command=self.open_scope).pack(side="left",
                                                                      padx=(12, 0))

        row2 = tk.Frame(self.root, bg=_BG)
        row2.pack(fill="x", padx=8, pady=(0, 4))
        # Voice: the clean synth waveforms, plus "PC Speaker" — MCS's own 4-voice 1-bit
        # rendering (see audio._render_pcspeaker), for comparing against real hardware.
        tk.Label(row2, text="Voice", bg=_BG, fg=_ACCENT).pack(side="left")
        self._voices = {"PC Speaker": "pcspeaker", "Square": "square",
                        "Triangle": "triangle", "Sine": "sine"}
        self.voice = tk.StringVar(value="PC Speaker")
        ttk.Combobox(row2, textvariable=self.voice, width=11, state="readonly",
                     values=list(self._voices)).pack(side="left", padx=(6, 0))

        # Volume is LIVE (waveOutSetVolume) — dragging it changes the playing audio.
        tk.Label(row2, text="Volume", bg=_BG, fg=_ACCENT).pack(side="left", padx=(24, 6))
        self.volume = tk.DoubleVar(value=80.0)
        tk.Scale(row2, from_=0, to=100, orient="horizontal", variable=self.volume,
                 command=self._on_volume, showvalue=False, length=200, bg=_BG,
                 troughcolor="#2a2e3a", bd=0, highlightthickness=0,
                 activebackground=_ACCENT, sliderrelief="flat").pack(side="left")
        self.vol_label = tk.Label(row2, text="80%", bg=_BG, fg=_FG, width=4, anchor="w",
                                  font=("TkDefaultFont", 10, "bold"))
        self.vol_label.pack(side="left", padx=(6, 0))

    def _on_volume(self, value: str) -> None:
        """Slider callback: update the readout and the playing stream's volume live."""
        v = float(value)
        self.vol_label.configure(text=f"{round(v):d}%")
        self.player.set_volume(v / 100.0)          # no-op when nothing is playing

    def _build_meta(self) -> None:
        # Read-only song metadata extracted from the file. Playback follows these; there
        # are no manual overrides — the point is to reproduce what the .MCS actually stores.
        meta = tk.Frame(self.root, bg=_BG)
        meta.pack(fill="x", padx=8, pady=(0, 2))
        self.meta_vars = {k: tk.StringVar(value="—") for k in ("Time", "Key", "Tempo")}
        for label in ("Time", "Key", "Tempo"):
            cell = tk.Frame(meta, bg=_BG)
            cell.pack(side="left", padx=(0, 20))
            tk.Label(cell, text=label, bg=_BG, fg=_ACCENT,
                     font=("TkDefaultFont", 8)).pack(anchor="w")
            tk.Label(cell, textvariable=self.meta_vars[label], bg=_BG, fg=_FG,
                     font=("TkDefaultFont", 11, "bold")).pack(anchor="w")

    def _build_tracker(self) -> None:
        frame = tk.Frame(self.root, bg=_BG)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("bar", "evt", "v1", "v2", "v3", "v4")   # events + 4 voices, highest -> lowest
        style = ttk.Style()
        # Explicit light grid: set foreground too, so a shaded row never hides its text
        # (the old bar tag set only a dark background -> black-on-black first note).
        style.configure("Tracker.Treeview", background="#ffffff", fieldbackground="#ffffff",
                        foreground="#141414", rowheight=20)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=24,
                                 style="Tracker.Treeview")
        self.tree.tag_configure("stripe", background="#e8f6e8", foreground="#141414")  # zebra
        self.tree.tag_configure("bar", background="#bfe3bf", foreground="#0b3d0b",      # bar start
                                font=("TkDefaultFont", 9, "bold"))
        self.tree.tag_configure("playhead", background="#ff8f1f", foreground="#ffffff",  # now-playing
                                font=("TkDefaultFont", 9, "bold"))
        heads = {"bar": "Bar", "evt": "Evt"}
        for c, w in (("bar", 44), ("evt", 40), ("v1", 96), ("v2", 96), ("v3", 96), ("v4", 96)):
            self.tree.heading(c, text=heads.get(c, c.upper()))
            self.tree.column(c, width=w, anchor="center")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        # Click a row to set the playback position (seeks live if already playing).
        self.tree.bind("<ButtonRelease-1>", self._on_row_click)

    def _on_row_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid or not self.song:
            return
        try:
            row = self._children.index(iid)
        except ValueError:
            return
        if self._playing:                                  # seek the running stream
            was_paused = self.player.paused
            if self._play_from(row) and was_paused:
                self.player.pause()
                self.pause_btn.configure(text="▶ Resume")
        else:                                              # just park the position here
            self._clear_playhead()
            self.tree.item(self._children[row], tags=("playhead",))
            self._last_row = row
            bar = sum(1 for r in self._rows[:row + 1] if r[1])
            self.status.configure(text=f"Position set to bar {bar} — Play starts here.")

    def _build_statusbar(self) -> None:
        self.status = tk.Label(self.root, text="Open an .MCS / .MCD song to begin.",
                               bg="#0c0d11", fg=_ACCENT, anchor="w")
        self.status.pack(fill="x", side="bottom")

    # ---- actions ---------------------------------------------------------
    def open_dialog(self) -> None:
        path = filedialog.askopenfilename(
            title="Open Music Construction Set song",
            filetypes=[("MCS/MCD songs", "*.mcs *.mcd *.MCS *.MCD"), ("All files", "*.*")])
        if path:
            self.load(path)

    def import_dialog(self) -> None:
        """Import a chiptune module: parse it, open the channel-preview dialog
        (solo audition, filtering, octave/tempo), then convert and load.
        Currently: Vortex Tracker .pt3; importers dispatch on the extension."""
        src = filedialog.askopenfilename(
            title="Import a chiptune module",
            filetypes=[("Vortex Tracker modules", "*.pt3 *.PT3"),
                       ("All files", "*.*")])
        if not src:
            return
        try:
            song, byte0 = self._load_module(src)
        except Exception as exc:  # noqa: BLE001 - show any import error to the user
            messagebox.showerror("Cannot import", f"{os.path.basename(src)}:\n{exc}")
            return
        ImportPreview(self, src, song, byte0)

    @staticmethod
    def _load_module(src: str, percussion: str = "clicks",
                     drum_sound: str = "cluster"):
        """Module file -> (Song, mcs_tempo_byte0), dispatched on the extension."""
        ext = src.lower().rsplit(".", 1)[-1]
        if ext == "pt3":
            from ..pt3 import parse_pt3
            with open(src, "rb") as fh:
                return parse_pt3(fh.read(), percussion=percussion,
                                 drum_sound=drum_sound)
        raise ValueError(f"no importer for .{ext} files (supported: .pt3)")

    def save_and_load(self, data: bytes, src: str) -> None:
        """Save converted .MCS bytes (8.3-named by default) and open them."""
        default = _dos_name(os.path.splitext(os.path.basename(src))[0]) + ".MCS"
        out = filedialog.asksaveasfilename(
            title="Save converted song as", defaultextension=".mcs",
            initialdir=os.path.dirname(src), initialfile=default,
            filetypes=[("MCS songs", "*.mcs *.MCS")])
        if not out:
            return
        with open(out, "wb") as fh:
            fh.write(data)
        self.load(out)
        self.status.configure(text=f"Imported {os.path.basename(src)} → "
                                   f"{os.path.basename(out)}.")

    def load(self, path: str) -> None:
        try:
            self.song = parse(path)
        except Exception as exc:  # noqa: BLE001 - surface any parse error to the user
            messagebox.showerror("Cannot open file", f"{os.path.basename(path)}:\n{exc}")
            return
        self.path = path
        self._populate(path)
        self.play_btn.configure(state="normal")
        self.export_btn.configure(state="normal")
        self.track_btn.configure(state="normal")

    def _populate(self, path: str) -> None:
        self.player.stop()                   # a new song silences the old one
        self._stop_follow()
        self._last_row = None
        self._scope_data = None              # ...and drops its scope buffers
        self._draw_scope(None)
        self.pause_btn.configure(state="disabled", text="⏸ Pause")
        self.stop_btn.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        # 4-voice tracker: one row per 32nd-tick, sounding notes ranked highest -> lowest.
        rows = tracker_rows(self.song)
        self._rows = rows
        for idx, (lbl, is_bar, evt, cols) in enumerate(rows):
            tag = "bar" if is_bar else ("stripe" if idx % 2 else "")
            self.tree.insert("", "end", values=(lbl, evt, *cols),
                             tags=(tag,) if tag else ())
        self._children = self.tree.get_children()   # row ids for playhead + click-seek
        self.tree.yview_moveto(0.0)          # always show the first row after a load
        self.tree.update_idletasks()         # force the grid to repaint now

        # Stamp the decoded first note into the title so THIS window self-identifies —
        # if the title and the grid ever disagree, this window is running stale code.
        first = next((c.split(":")[0] for _, _, _, cols in rows
                      for c in cols if c and not c.startswith("R")), "?")
        self.root.title(f"MCS-Convert — {os.path.basename(path)}  (first note {first})")

        self.meta_vars["Time"].set(self.song.time_signature or "—")
        self.meta_vars["Key"].set(self.song.key_signature or "—")
        self.meta_vars["Tempo"].set(f"≈{round(tempo_bpm(self.song.tempo_tick_seconds))} BPM")

        total = sum(len(t.notes) for t in self.song.tracks)
        rests = sum(1 for tr in self.song.tracks for n in tr.notes if n.is_rest)
        self.status.configure(
            text=f"{os.path.basename(path)} — {len(self.song.tracks)} staff/staves, "
                 f"{total} notes ({rests} rests). Click a row to set the play position.")

    def _render(self) -> bool:
        """Synthesize the loaded song at its own tempo, stashing (master, voices, sr)
        for playback, WAV export, and the oscilloscope. True if anything is audible."""
        # Timing comes from the file's own tempo (header byte 0); voice from the dropdown.
        master, voices, sr = render_song(self.song,
                                         step_seconds=self.song.tempo_tick_seconds,
                                         waveform=self._voices[self.voice.get()])
        self._scope_data = (master, voices, sr)
        return bool(np.any(master))

    def play(self) -> None:
        """Play from the position highlight (or the top if there isn't one)."""
        if not self.song:
            return
        if not self._render():
            self.status.configure(text="Nothing to play (no decoded notes).")
            return
        start_row = self._last_row or 0
        if start_row >= len(self._rows) - 1:               # highlight parked at the end
            start_row = 0
        if not self._play_from(start_row):
            self.status.configure(text="Nothing to play (no decoded notes).")

    def _play_from(self, row: int) -> bool:
        """Start the waveOut stream at tracker row `row`. Returns False if silent."""
        master, _, sr = self._scope_data
        self._step = self.song.tempo_tick_seconds or 0.1   # seconds per row (one tick)
        pcm = pcm16(master[int(row * self._step * sr):])
        if not pcm:
            return False
        try:
            self.player.play(pcm, sr, volume=self.volume.get() / 100.0)
        except RuntimeError as exc:
            messagebox.showerror("Cannot play", str(exc))
            return False
        self.pause_btn.configure(state="normal", text="⏸ Pause")
        self.stop_btn.configure(state="normal")
        self._start_follow(row)
        return True

    def toggle_pause(self) -> None:
        if self._follow_id is None:
            return
        if self.player.paused:
            self.player.resume()
            self.pause_btn.configure(text="⏸ Pause")
        else:
            self.player.pause()
            self.pause_btn.configure(text="▶ Resume")

    # ---- playhead: scroll the grid in time with playback --------------------
    @property
    def _playing(self) -> bool:
        return self._follow_id is not None

    def _start_follow(self, start_row: int = 0) -> None:
        self._stop_follow()
        self._clear_playhead()
        self._children = self.tree.get_children()
        self._start_row = start_row
        self._follow_playhead()

    def _follow_playhead(self) -> None:
        if not self._children:
            return
        pos = self.player.position_seconds()               # frozen while paused
        row = self._start_row + int(pos / self._step)
        if self.player.is_done() or row >= len(self._children):
            self._finish_playback()
            return
        if row != self._last_row:
            self._move_playhead(row)
        self._draw_scope(self._start_row * self._step + pos)
        self._follow_id = self.root.after(30, self._follow_playhead)

    def _finish_playback(self) -> None:
        """Natural end of the song: transport off, position back to the top."""
        self.player.stop()
        self._stop_follow()
        self._clear_playhead()
        self._last_row = None                              # next Play starts at the top
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled", text="⏸ Pause")
        self._draw_scope(None)

    def _move_playhead(self, row: int) -> None:
        self._clear_playhead()                             # restore the row we're leaving
        iid = self._children[row]
        self.tree.item(iid, tags=("playhead",))
        self._last_row = row
        n = len(self._children)
        visible = max(1, self.tree.winfo_height() // 20)   # rowheight is 20px
        top = min(max(0, row - visible // 2), max(0, n - visible))   # keep it centred
        self.tree.yview_moveto(top / n if n else 0.0)

    def _clear_playhead(self) -> None:
        """Give the row under the playhead its normal zebra/bar styling back."""
        if self._last_row is None or not (0 <= self._last_row < len(self._children)):
            return
        idx = self._last_row
        is_bar = self._rows[idx][1] if idx < len(self._rows) else False
        tag = "bar" if is_bar else ("stripe" if idx % 2 else "")
        self.tree.item(self._children[idx], tags=(tag,) if tag else ())

    def _stop_follow(self) -> None:
        if self._follow_id is not None:
            self.root.after_cancel(self._follow_id)
            self._follow_id = None

    # ---- oscilloscope: four voice scopes + a master, fed by render_song ------
    def open_scope(self) -> None:
        """Open (or raise) the oscilloscope window: v1..v4 in a 2×2 grid, master below.
        The window is resizable — every canvas tracks its cell and redraws to size."""
        if self._scope_win is not None and self._scope_win.winfo_exists():
            self._scope_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("MCS-Convert — Oscilloscope")
        win.configure(bg=_BG)
        win.minsize(380, 300)
        win.protocol("WM_DELETE_WINDOW", self._close_scope)
        win.rowconfigure(0, weight=2)                       # voice grid gets 2/3
        win.rowconfigure(1, weight=1)                       # master gets 1/3
        win.columnconfigure(0, weight=1)
        self._scope_win = win
        self._scope_panels = []
        grid = tk.Frame(win, bg=_BG)
        grid.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 2))
        for rc in (0, 1):
            grid.rowconfigure(rc, weight=1)
            grid.columnconfigure(rc, weight=1)
        for k in range(4):                                  # v1 v2 / v3 v4
            c = tk.Canvas(grid, width=224, height=92, bg="#000000", highlightthickness=1,
                          highlightbackground=_SCOPE_DIM)
            c.grid(row=k // 2, column=k % 2, padx=3, pady=3, sticky="nsew")
            self._scope_panels.append(self._scope_panel(c, f"v{k + 1}"))
        m = tk.Canvas(win, width=458, height=116, bg="#000000", highlightthickness=1,
                      highlightbackground=_SCOPE_DIM)
        m.grid(row=1, column=0, sticky="nsew", padx=11, pady=(2, 8))
        self._scope_panels.append(self._scope_panel(m, "master"))
        self._draw_scope(None)               # live frames take over within 30 ms

    def _scope_panel(self, c: tk.Canvas, label: str):
        midline = c.create_line(0, 0, 0, 0, fill=_SCOPE_DIM)
        c.create_text(6, 4, text=label, anchor="nw", fill=_SCOPE_DIM,
                      font=("TkDefaultFont", 8))
        trace = c.create_line(0, 0, 0, 0, fill=_SCOPE_LINE)
        c.bind("<Configure>", lambda e, c=c, t=trace, m=midline:
               self._scope_resized(c, t, m))
        return (c, trace)

    @staticmethod
    def _scope_size(c: tk.Canvas):
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:                               # not mapped yet: creation size
            w, h = int(c["width"]), int(c["height"])
        return w, h

    def _scope_resized(self, c: tk.Canvas, trace: int, midline: int) -> None:
        """Refit a scope canvas after a window resize; the play loop redraws the trace,
        so only idle canvases need their flat line restretched here."""
        w, h = self._scope_size(c)
        c.coords(midline, 2, h / 2, w - 2, h / 2)
        if not self._playing:
            c.coords(trace, 2, h / 2, w - 2, h / 2)

    def _close_scope(self) -> None:
        if self._scope_win is not None:
            self._scope_win.destroy()
        self._scope_win = None
        self._scope_panels = []

    def _draw_scope(self, elapsed: float | None) -> None:
        """Draw a ~30 ms window of each voice (and the master) at the playback position;
        None flatlines all five traces."""
        if self._scope_win is None or not self._scope_win.winfo_exists():
            return
        bufs = [None] * 5
        idx, span = 0, 0
        if elapsed is not None and self._scope_data is not None:
            master, voices, sr = self._scope_data
            bufs = list(voices[:4]) + [None] * (4 - len(voices)) + [master]
            idx = max(0, int(elapsed * sr))
            span = int(0.030 * sr)
        for buf, (c, trace) in zip(bufs, self._scope_panels):
            w, h = self._scope_size(c)
            mid = h / 2
            seg = buf[idx:idx + span] if buf is not None else ()
            n = min(w // 2, len(seg))
            if n < 2:
                c.coords(trace, 2, mid, w - 2, mid)
                continue
            ys = seg[np.linspace(0, len(seg) - 1, n).astype(int)]
            pts = np.empty(2 * n)
            pts[0::2] = np.linspace(2, w - 2, n)
            pts[1::2] = mid - ys * (mid - 8)
            c.coords(trace, *pts.tolist())

    def export_wav(self) -> None:
        if not self.song:
            return
        if not self._render():
            self.status.configure(text="Nothing to export (no decoded notes).")
            return
        master, _, sr = self._scope_data
        wav = wav_bytes(pcm16(master), sr)                 # always full volume
        default = os.path.splitext(os.path.basename(getattr(self, "path", "song")))[0] + ".wav"
        out = filedialog.asksaveasfilename(
            title="Export decoded playback as WAV", defaultextension=".wav",
            initialfile=default, filetypes=[("WAV audio", "*.wav")])
        if not out:
            return
        try:
            with open(out, "wb") as fh:
                fh.write(wav)
        except OSError as exc:
            messagebox.showerror("Cannot write WAV", str(exc))
            return
        self.status.configure(text=f"Exported {len(wav)} bytes → {os.path.basename(out)}")

    def export_tracker(self) -> None:
        if not self.song:
            return
        default = os.path.splitext(os.path.basename(getattr(self, "path", "song")))[0] + ".txt"
        out = filedialog.asksaveasfilename(
            title="Export tracker grid as text", defaultextension=".txt",
            initialfile=default, filetypes=[("Text", "*.txt")])
        if not out:
            return
        try:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(tracker_text(self.song))
        except OSError as exc:
            messagebox.showerror("Cannot write tracker", str(exc))
            return
        self.status.configure(text=f"Exported tracker → {os.path.basename(out)}")

    def stop(self) -> None:
        """Stop playback. The playhead stays put — Play resumes from that row."""
        self.player.stop()
        self._stop_follow()
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled", text="⏸ Pause")
        self._draw_scope(None)       # flatline the scopes


class ImportPreview(tk.Toplevel):
    """Channel preview for a module import: per-channel stats and verdicts,
    solo/selection audition through the synth, per-channel octave shift, and
    an MCS tempo picker — so the drums stay on the ZX Spectrum where they
    belong. Statistics suggest; ears decide."""

    _PREVIEW_SECONDS = 15

    def __init__(self, app: PlayerApp, src: str, song: Song, byte0: int) -> None:
        super().__init__(app.root)
        self.app, self.src, self.song = app, src, song
        self.title(f"Import Preview — {os.path.basename(src)}")
        self.configure(bg=_BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)

        total = max((n.end_tick for t in song.tracks for n in t.notes), default=0)
        secs = total * tick_seconds_for(byte0)
        tk.Label(self, text=f"{song.title or os.path.basename(src)} — "
                            f"{total // 32} bars, ~{int(secs) // 60}:{int(secs) % 60:02d}",
                 bg=_BG, fg=_FG).grid(row=0, column=0, columnspan=8,
                                      sticky="w", padx=10, pady=(10, 6))

        hdr = ("", "channel", "notes", "range", "noise", "repet.", "verdict", "")
        for c, text in enumerate(hdr):
            tk.Label(self, text=text, bg=_BG, fg=_ACCENT,
                     font=("TkDefaultFont", 8)).grid(row=1, column=c, padx=6)

        self.include = []                       # BooleanVar per channel
        self.octave = []                        # StringVar per channel: +2..-2
        self.perc = tk.StringVar(value="clicks")   # percussion mode
        self._stat_labels = []                  # updatable per-channel labels
        for i, tr in enumerate(song.tracks):
            st = channel_stats(tr)
            # percussion stays checked: the importer renders AY drums as short
            # clicks at the register extremes, so they're worth keeping — solo
            # the channel and uncheck it if the clicks don't earn their voice
            keep = tk.BooleanVar(value=st["verdict"] != "empty")
            self.include.append(keep)
            row = 2 + i
            tk.Checkbutton(self, variable=keep, bg=_BG, activebackground=_BG,
                           selectcolor="#2a2e3a",
                           command=self._update_size).grid(row=row, column=0)
            tk.Label(self, text=tr.name, bg=_BG, fg=_FG).grid(row=row, column=1)
            labels = {}
            for col, key in ((2, "count"), (3, "range"), (4, "noise"),
                             (5, "repet"), (6, "verdict")):
                labels[key] = tk.Label(self, bg=_BG, fg=_FG)
                labels[key].grid(row=row, column=col)
            self._stat_labels.append(labels)
            tk.Button(self, text="▶ solo", command=lambda i=i: self._audition([i])
                      ).grid(row=row, column=7, padx=(4, 2))
            var = tk.StringVar(value="0")
            self.octave.append(var)
            oct_box = ttk.Combobox(self, textvariable=var, width=3, state="readonly",
                                   values=("+2", "+1", "0", "-1", "-2"))
            oct_box.grid(row=row, column=8, padx=(2, 10))
            oct_box.bind("<<ComboboxSelected>>", lambda _e: self._update_size())
        tk.Label(self, text="8va", bg=_BG, fg=_ACCENT,
                 font=("TkDefaultFont", 8)).grid(row=1, column=8)
        self._update_stats()

        # Percussion handling — one of three states, live for audition.
        perc = tk.Frame(self, bg=_BG)
        perc.grid(row=6, column=0, columnspan=9, sticky="w", padx=10, pady=(8, 0))
        tk.Label(perc, text="Percussion", bg=_BG, fg=_ACCENT).pack(side="left")
        for value, label in (("clicks", "as clicks"),
                             ("pitched", "as written notes"),
                             ("drop", "dropped")):
            tk.Radiobutton(perc, text=label, value=value, variable=self.perc,
                           command=self._on_percussion, bg=_BG, fg=_FG,
                           activebackground=_BG, activeforeground=_FG,
                           selectcolor="#2a2e3a").pack(side="left", padx=(10, 0))
        tk.Label(perc, text="sound", bg=_BG, fg=_ACCENT).pack(side="left",
                                                              padx=(18, 4))
        self.drum = tk.StringVar(value="cluster")
        snd = ttk.Combobox(perc, textvariable=self.drum, width=10,
                           state="readonly", values=("cluster", "wood block"))
        snd.pack(side="left")
        snd.bind("<<ComboboxSelected>>", lambda _e: self._on_percussion())

        # MCS tempo: ten discrete speeds; default = the row-rate suggestion.
        bar = tk.Frame(self, bg=_BG)
        bar.grid(row=7, column=0, columnspan=9, sticky="w", padx=10, pady=(8, 2))
        tk.Label(bar, text="Tempo", bg=_BG, fg=_ACCENT).pack(side="left")
        self._tempos = [0x77 + 3 * s for s in range(10)]
        labels = [f"≈{round(tempo_bpm(tick_seconds_for(b)))} BPM" for b in self._tempos]
        self.tempo = tk.StringVar(value=labels[self._tempos.index(byte0)])
        ttk.Combobox(bar, textvariable=self.tempo, width=11, state="readonly",
                     values=labels).pack(side="left", padx=(6, 16))
        tk.Button(bar, text="▶ Preview selection", command=lambda: self._audition(
            [i for i, v in enumerate(self.include) if v.get()])).pack(side="left")
        tk.Button(bar, text="■ Stop", command=self.app.player.stop).pack(
            side="left", padx=(4, 0))

        btns = tk.Frame(self, bg=_BG)
        btns.grid(row=8, column=0, columnspan=9, sticky="e", padx=10, pady=(6, 10))
        self.size_label = tk.Label(btns, bg=_BG, fg=_FG)
        self.size_label.pack(side="left", padx=(0, 14))
        tk.Button(btns, text="Import…", command=self._do_import).pack(side="left")
        tk.Button(btns, text="Cancel", command=self._close).pack(side="left", padx=(6, 0))
        self._update_size()

    def _update_stats(self) -> None:
        """Refresh the per-channel stat labels from the current parse."""
        for tr, labels in zip(self.song.tracks, self._stat_labels):
            st = channel_stats(tr)
            verdict = st["verdict"]
            if st["verdict"] == "percussion" and self.perc.get() == "clicks":
                verdict = "percussion→clicks"
            labels["count"].configure(text=st["count"])
            labels["range"].configure(text=st["range"])
            labels["noise"].configure(text=f"{st['noise']:.0%}")
            labels["repet"].configure(text=f"{st['repet']:.0%}")
            labels["verdict"].configure(
                text=verdict,
                fg="#e0b060" if st["verdict"] == "percussion" else _ACCENT)

    def _on_percussion(self) -> None:
        """Reparse the module under the chosen percussion mode (fast — the
        file is small) so auditions and the import reflect it immediately."""
        self.app.player.stop()
        sound = "block" if self.drum.get() == "wood block" else "cluster"
        try:
            self.song, _ = self.app._load_module(self.src, self.perc.get(), sound)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Cannot re-import", str(exc), parent=self)
            return
        self._update_stats()
        self._update_size()

    def _update_size(self) -> None:
        """Live size readout for the current selection. Purely informational —
        the 1984 editor saved at most ~4.2KB, but its player handles more."""
        try:
            size = len(self.encode_selection())
        except Exception:  # noqa: BLE001 - e.g. nothing selected
            size = 0
        self.size_label.configure(text=f"{size:,} bytes", fg=_ACCENT)

    # -- selection -> Song ------------------------------------------------------
    def _tempo_byte0(self) -> int:
        labels = [f"≈{round(tempo_bpm(tick_seconds_for(b)))} BPM" for b in self._tempos]
        return self._tempos[labels.index(self.tempo.get())]

    def selected_song(self, indices=None) -> Song:
        """The checked (or given) channels, octave shifts applied."""
        if indices is None:
            indices = [i for i, v in enumerate(self.include) if v.get()]
        out = Song(title=self.song.title, source=self.song.source)
        for i in indices:
            tr = self.song.tracks[i]
            shift = 12 * int(self.octave[i].get())
            nt = Track(name=tr.name, meta=dict(tr.meta))
            for n in tr.notes:
                # percussion clicks stay pinned to the floor: the octave knob
                # exists to move a channel's MUSIC away from the drums
                s = 0 if (n.is_rest or n.percussive) else shift
                nt.add(NoteEvent(start_tick=n.start_tick,
                                 duration_ticks=n.duration_ticks,
                                 midi_note=n.midi_note + s,
                                 is_rest=n.is_rest, tied=n.tied,
                                 percussive=n.percussive))
            out.add_track(nt)
        return out

    def encode_selection(self) -> bytes:
        from ..mcs.encode import encode_song
        return encode_song(self.selected_song(), tempo_byte0=self._tempo_byte0())

    # -- actions ----------------------------------------------------------------
    def _audition(self, indices) -> None:
        """Play the first seconds of the given channels through the synth."""
        if not indices:
            return
        sel = self.selected_song(indices)
        step = tick_seconds_for(self._tempo_byte0())
        master, _, sr = render_song(sel, step_seconds=step, waveform="pcspeaker")
        pcm = pcm16(master[:self._PREVIEW_SECONDS * sr])
        if pcm:
            self.app.player.play(pcm, sr,
                                 volume=self.app.volume.get() / 100.0)

    def _do_import(self) -> None:
        self.app.player.stop()
        if not any(v.get() for v in self.include):
            messagebox.showwarning("Nothing selected",
                                   "Every channel is unchecked — nothing to import.",
                                   parent=self)
            return
        data = self.encode_selection()
        self.destroy()
        self.app.save_and_load(data, self.src)

    def _close(self) -> None:
        self.app.player.stop()
        self.destroy()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    initial = argv[0] if argv else None
    root = tk.Tk()
    PlayerApp(root, initial)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
